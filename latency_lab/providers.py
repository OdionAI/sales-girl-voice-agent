from __future__ import annotations

import asyncio
import base64
import codecs
import io
import json
import re
import time
import wave
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp

from .action_tools import ActionToolCall, parse_tool_arguments
from .config import LabConfig, uses_realtime_stt_final
from .llm_output import SpokenTextFilter

_ASR_MARKER = re.compile(r"(?i).*?<asr_text>\s*")
_LANGUAGE_NAMES = {
    "english", "french", "chinese", "german", "spanish", "arabic",
    "japanese", "korean", "russian", "portuguese", "italian", "hindi",
    "indonesian", "vietnamese", "turkish", "dutch", "polish", "thai",
    "swedish", "norwegian", "danish", "finnish", "hebrew", "ukrainian",
    "pidgin", "none",
}
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def pcm16_wav(pcm16: bytes, *, sample_rate: int = 16000) -> bytes:
    """Wrap mono little-endian PCM16 in a WAV container for batch ASR."""
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(pcm16)
    return output.getvalue()


async def frame_pcm16_chunks(source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Preserve little-endian PCM16 sample boundaries across HTTP chunks.

    Transport chunks are arbitrary byte slices. Dropping an odd trailing byte
    shifts every subsequent sample by eight bits and sounds like loud static
    over recognizable speech, so carry that byte into the next chunk instead.
    """
    carried = b""
    async for incoming in source:
        if not incoming:
            continue
        data = carried + bytes(incoming)
        even_length = len(data) - (len(data) % 2)
        if even_length:
            yield data[:even_length]
        carried = data[even_length:]


def clean_transcript(value: str) -> str:
    value = str(value or "").strip()
    detected = [item.lower() for item in re.findall(r"(?i)language\s+([a-z]+)", value)]
    concrete = [item for item in detected if item not in {"none", "null", "undefined"}]
    if concrete and "english" not in concrete:
        return ""
    if "<asr_text>" in value.lower():
        bodies = re.findall(
            r"(?is)<asr_text>(.*?)(?=(?:\n?\s*language\s+[a-z]+\s*<asr_text>)|$)",
            value,
        )
        value = " ".join(body.strip() for body in bodies if body.strip())
    value = re.sub(r"(?i)^language\s+[a-z]+\s*", "", value).strip()
    if _CJK.search(value):
        return ""
    return value


class RSTTClient:
    def __init__(
        self,
        config: LabConfig,
        on_partial: Callable[[str, str], Awaitable[None]],
        on_final: Callable[[str], Awaitable[None]],
        on_provider_event: Callable[[str, dict[str, Any]], None],
    ) -> None:
        self.config = config
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_provider_event = on_provider_event
        self.session: aiohttp.ClientSession | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.recv_task: asyncio.Task[None] | None = None
        self.accumulated = ""
        # The deployed realtime and batch routes share one Qwen/vLLM engine.
        # Keep every socket lifecycle transition and authoritative batch request
        # in one lane so recovery cannot reopen realtime while batch is running.
        self._inference_mode_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._recovery_lock = asyncio.Lock()
        self._in_language_preamble = False
        self._drop_hypothesis = False
        self._batch_tasks: set[asyncio.Task[None]] = set()
        self._realtime_skip_reported = False
        self._closing = False

    @property
    def ready(self) -> bool:
        return bool(self.ws and not self.ws.closed)

    @property
    def uses_realtime_final(self) -> bool:
        return uses_realtime_stt_final(self.config)

    async def warmup(self, duration_ms: float = 800.0) -> bool:
        """Prime the realtime ASR socket so the first caller utterance is not dropped."""
        if not self.ready:
            return False
        samples = max(1, int(16000 * max(0.0, duration_ms) / 1000.0))
        silence = b"\x00\x00" * samples
        chunk_bytes = 16000 * 2 // 5
        try:
            for offset in range(0, len(silence), chunk_bytes):
                await self.append(silence[offset : offset + chunk_bytes])
            if self.ws and not self.ws.closed:
                await self.ws.send_json(
                    {"type": "input_audio_buffer.commit", "final": False}
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.on_provider_event(
                "stt_warmup_failed",
                {"error": str(exc), "error_type": type(exc).__name__},
            )
            return False
        self.on_provider_event(
            "stt_warmup_complete",
            {"duration_ms": round(duration_ms, 1), "bytes": len(silence)},
        )
        return True

    async def connect(self) -> None:
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
        try:
            await self._open_socket()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.on_provider_event(
                "stt_realtime_connect_degraded",
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "batch_final_available": not self.uses_realtime_final,
                },
            )
            if self.uses_realtime_final:
                raise

    async def _open_socket(self) -> None:
        async with self._inference_mode_lock:
            await self._open_socket_locked()

    async def _open_socket_locked(self) -> None:
        """Open realtime while the caller owns ``_inference_mode_lock``."""
        async with self._connect_lock:
            if self.ws and not self.ws.closed:
                return
            assert self.session is not None
            last_error: BaseException | None = None
            for attempt in range(1, 4):
                candidate: aiohttp.ClientWebSocketResponse | None = None
                self.on_provider_event(
                    "stt_connect_start",
                    {"url": self.config.stt_ws_url, "attempt": attempt, "max_attempts": 3},
                )
                try:
                    candidate = await self.session.ws_connect(
                        self.config.stt_ws_url,
                        heartbeat=20,
                        timeout=aiohttp.ClientWSTimeout(ws_receive=None, ws_close=5),
                    )
                    hello = await asyncio.wait_for(candidate.receive_json(), timeout=8)
                    if self.uses_realtime_final and hello.get("type") != "session.created":
                        raise aiohttp.ClientConnectionError("Whisper did not create a realtime session")
                    self.on_provider_event(
                        "stt_session_created", {"provider_event": hello.get("type")}
                    )
                    await candidate.send_json(
                        {
                            "type": "session.update",
                            "model": self.config.stt_model,
                            "language": "en",
                            "to_language": "en",
                        }
                    )
                    await candidate.send_json(
                        {"type": "input_audio_buffer.commit", "final": False}
                    )
                    self.ws = candidate
                    self._realtime_skip_reported = False
                    self.recv_task = asyncio.create_task(
                        self._receive(candidate), name="rvc-lab-stt-receive"
                    )
                    return
                except (aiohttp.ClientError, TimeoutError) as exc:
                    last_error = exc
                    if candidate and not candidate.closed:
                        await candidate.close()
                    self.ws = None
                    self.on_provider_event(
                        "stt_connect_retry",
                        {"attempt": attempt, "error": str(exc), "error_type": type(exc).__name__},
                    )
                    if attempt < 3:
                        await asyncio.sleep(0.2 * (2 ** (attempt - 1)))
            else:
                assert last_error is not None
                raise last_error

    async def append(self, pcm16: bytes) -> None:
        ws = self.ws
        if not ws or ws.closed:
            if not self._realtime_skip_reported:
                self._realtime_skip_reported = True
                self.on_provider_event(
                    "stt_realtime_audio_skipped",
                    {"batch_final_available": not self.uses_realtime_final},
                )
            return
        try:
            await ws.send_json(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm16).decode("ascii"),
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.on_provider_event(
                "stt_realtime_audio_send_failed",
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "batch_final_available": not self.uses_realtime_final,
                },
            )
            if self.ws is ws:
                self.ws = None

    async def commit(self, pcm16: bytes) -> None:
        """Finish the current utterance on the provider that can actually decode it."""
        audio = bytes(pcm16)
        if self.uses_realtime_final:
            await self._commit_realtime_final(audio)
            return
        self.on_provider_event(
            "stt_batch_final_requested",
            {
                "pcm_bytes": len(audio),
                "audio_ms": round(len(audio) / 2 / 16000 * 1000, 1),
            },
        )
        task = asyncio.create_task(
            self._run_authoritative_batch(audio), name="rvc-lab-stt-batch-final"
        )
        self._batch_tasks.add(task)
        task.add_done_callback(self._batch_tasks.discard)

    async def _commit_realtime_final(self, pcm16: bytes) -> None:
        """Keep the Whisper socket open and ask it for the turn transcript."""
        audio = bytes(pcm16)
        self.on_provider_event(
            "stt_realtime_final_requested",
            {
                "pcm_bytes": len(audio),
                "audio_ms": round(len(audio) / 2 / 16000 * 1000, 1),
            },
        )
        ws = self.ws
        if not ws or ws.closed:
            try:
                await self._open_socket()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.on_provider_event(
                    "stt_realtime_final_error",
                    {"error": str(exc), "error_type": type(exc).__name__},
                )
                return
            ws = self.ws
            if ws and not ws.closed and audio:
                chunk_bytes = 16000 * 2 // 5
                for offset in range(0, len(audio), chunk_bytes):
                    await self.append(audio[offset : offset + chunk_bytes])
        if not ws or ws.closed:
            self.on_provider_event(
                "stt_realtime_final_error",
                {"error": "realtime socket unavailable", "error_type": "SocketClosed"},
            )
            return
        try:
            await ws.send_json({"type": "input_audio_buffer.commit", "final": True})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.on_provider_event(
                "stt_realtime_final_error",
                {"error": str(exc), "error_type": type(exc).__name__},
            )

    async def _run_authoritative_batch(self, pcm16: bytes) -> None:
        """Hand inference ownership from realtime to batch and back.

        Qwen realtime remains useful for speculative partials, but the deployed
        websocket and HTTP routes share one vLLM EngineCore which cannot safely
        decode both requests at once. Closing and awaiting the websocket before
        POSTing the final removes that overlap without adding a fixed sleep.
        """
        async with self._inference_mode_lock:
            self.on_provider_event(
                "stt_inference_handoff_start",
                {"from": "realtime", "to": "batch", "reason": "authoritative_final"},
            )
            await self._quiesce_realtime_locked()
            try:
                await self._transcribe_batch(pcm16)
            finally:
                if not self._closing:
                    self.on_provider_event(
                        "stt_realtime_resume_start",
                        {"reason": "authoritative_batch_complete"},
                    )
                    try:
                        await self._open_socket_locked()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.on_provider_event(
                            "stt_realtime_resume_degraded",
                            {
                                "error": str(exc),
                                "error_type": type(exc).__name__,
                                "batch_final_available": True,
                            },
                        )
                    else:
                        self.on_provider_event(
                            "stt_realtime_resume_complete",
                            {"reason": "authoritative_batch_complete"},
                        )

    async def _quiesce_realtime_locked(self) -> None:
        """Close realtime and await its receiver before batch inference starts."""
        ws = self.ws
        recv_task = self.recv_task
        socket_was_open = bool(ws and not ws.closed)
        self.ws = None
        self.recv_task = None
        self._reset_decoder_state()

        if ws and not ws.closed:
            await ws.close()
        if recv_task and recv_task is not asyncio.current_task():
            await asyncio.gather(recv_task, return_exceptions=True)
        self.on_provider_event(
            "stt_realtime_quiesced",
            {
                "reason": "prevent_shared_engine_overlap",
                "socket_was_open": socket_was_open,
                "receiver_was_active": bool(recv_task),
            },
        )

    async def _transcribe_batch(self, pcm16: bytes) -> None:
        started = time.monotonic()
        if not pcm16:
            self.on_provider_event(
                "stt_batch_error",
                {"error": "local utterance buffer was empty", "error_type": "EmptyAudio"},
            )
            return
        assert self.session is not None and not self.session.closed
        wav_audio = pcm16_wav(pcm16)
        form = aiohttp.FormData()
        form.add_field(
            "file",
            wav_audio,
            filename="turn.wav",
            content_type="audio/wav",
        )
        form.add_field("model", self.config.stt_model)
        form.add_field("language", "en")
        self.on_provider_event(
            "stt_batch_request_start",
            {
                "url": self.config.stt_batch_url,
                "wav_bytes": len(wav_audio),
            },
        )
        try:
            timeout = aiohttp.ClientTimeout(total=self.config.stt_batch_timeout_seconds)
            async with self.session.post(
                self.config.stt_batch_url, data=form, timeout=timeout
            ) as response:
                response.raise_for_status()
                payload = await response.json()
            final = clean_transcript(str(payload.get("text") or ""))
            elapsed_ms = (time.monotonic() - started) * 1000
            self.on_provider_event(
                "stt_batch_final",
                {
                    "elapsed_ms": round(elapsed_ms, 1),
                    "text": final,
                    "authoritative": True,
                },
            )
            if final:
                await self.on_final(final)
            else:
                self.on_provider_event(
                    "stt_batch_error",
                    {
                        "error": "batch endpoint returned an empty transcript",
                        "error_type": "EmptyTranscript",
                        "elapsed_ms": round(elapsed_ms, 1),
                    },
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.on_provider_event(
                "stt_batch_error",
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                },
            )

    async def _receive(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                payload = json.loads(msg.data)
                kind = str(payload.get("type") or "")
                if kind == "transcription.delta":
                    raw = str(payload.get("delta") or payload.get("text") or "")
                    raw_word = raw.strip().lower()
                    if raw_word == "language":
                        self.accumulated = ""
                        self._in_language_preamble = True
                        self._drop_hypothesis = False
                        continue
                    if self._in_language_preamble and raw_word in _LANGUAGE_NAMES:
                        self._drop_hypothesis = raw_word not in {"english", "none"}
                        self._in_language_preamble = False
                        continue
                    if self._drop_hypothesis:
                        continue
                    cleaned = clean_transcript(raw)
                    if not cleaned:
                        continue
                    if re.match(r"(?i)^language\b|^<asr_text>", raw):
                        self.accumulated = ""
                    if (
                        self.accumulated
                        and cleaned[0].isalnum()
                        and (
                            raw[:1].isspace()
                            or self.accumulated[-1].isalnum()
                            or self.accumulated[-1] in ",;:"
                        )
                    ):
                        self.accumulated += " " + cleaned
                    else:
                        self.accumulated += cleaned
                    await self.on_partial(self.accumulated.strip(), raw)
                elif kind == "transcription.done":
                    final = clean_transcript(str(payload.get("text") or self.accumulated))
                    self.accumulated = ""
                    self._in_language_preamble = False
                    self._drop_hypothesis = False
                    if self.uses_realtime_final:
                        self.on_provider_event(
                            "stt_realtime_final",
                            {"text": final, "authoritative": True},
                        )
                        if final:
                            await self.on_final(final)
                        else:
                            self.on_provider_event(
                                "stt_realtime_final_error",
                                {
                                    "error": "realtime endpoint returned an empty transcript",
                                    "error_type": "EmptyTranscript",
                                },
                            )
                    else:
                        self.on_provider_event(
                            "stt_realtime_final_ignored",
                            {"text": final, "authoritative": False},
                        )
                    if not ws.closed:
                        await ws.send_json({"type": "input_audio_buffer.commit", "final": False})
                elif kind == "error":
                    self.on_provider_event(
                        "stt_realtime_error",
                        {"payload": payload, "batch_final_available": not self.uses_realtime_final},
                    )
                    return
        finally:
            self.on_provider_event("stt_socket_closed", {})
            if self.ws is ws:
                self.ws = None

    def _reset_decoder_state(self) -> None:
        self.accumulated = ""
        self._in_language_preamble = False
        self._drop_hypothesis = False

    async def recover(self, reason: str) -> None:
        """Replace a wedged STT socket without changing the healthy-turn path."""
        async with self._recovery_lock:
            async with self._inference_mode_lock:
                self.on_provider_event("stt_recovery_start", {"reason": reason})
                old_task = self.recv_task
                old_ws = self.ws
                self.recv_task = None
                self.ws = None
                self._reset_decoder_state()

                if old_ws and not old_ws.closed:
                    await old_ws.close()
                if old_task and old_task is not asyncio.current_task():
                    if not old_task.done():
                        old_task.cancel()
                    await asyncio.gather(old_task, return_exceptions=True)

                try:
                    if not self.session or self.session.closed:
                        self.session = aiohttp.ClientSession(
                            timeout=aiohttp.ClientTimeout(total=None)
                        )
                    await self._open_socket_locked()
                except Exception as exc:
                    self.on_provider_event(
                        "stt_recovery_failed",
                        {"reason": reason, "error": str(exc), "error_type": type(exc).__name__},
                    )
                    raise
                self.on_provider_event("stt_recovery_complete", {"reason": reason})

    async def close(self) -> None:
        self._closing = True
        batch_tasks = list(self._batch_tasks)
        self._batch_tasks.clear()
        for batch_task in batch_tasks:
            batch_task.cancel()
        if batch_tasks:
            await asyncio.gather(*batch_tasks, return_exceptions=True)
        task = self.recv_task
        ws = self.ws
        self.recv_task = None
        self.ws = None
        if task and task is not asyncio.current_task():
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if ws and not ws.closed:
            await ws.close()
        if self.session and not self.session.closed:
            await self.session.close()


class RLLMClient:
    _retryable_statuses = {502, 503, 504}
    _retry_delays_seconds = (0.3, 1.0, 2.0)

    def __init__(self, config: LabConfig, session: aiohttp.ClientSession) -> None:
        self.config = config
        self.session = session

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 180,
        temperature: float = 0.35,
    ) -> AsyncIterator[str | ActionToolCall]:
        payload = {
            "model": self.config.llm_model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {
                "enable_thinking": bool(self.config.llm_enable_thinking),
                "thinking": bool(self.config.llm_enable_thinking),
            },
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self.config.llm_api_key}"}
        pending_calls: dict[int, dict[str, str]] = {}
        emitted_output = False
        for request_index in range(len(self._retry_delays_seconds) + 1):
            pending_calls = {}
            try:
                async with self.session.post(
                    f"{self.config.llm_base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                ) as response:
                    if (
                        response.status in self._retryable_statuses
                        and request_index < len(self._retry_delays_seconds)
                    ):
                        await response.read()
                        await asyncio.sleep(self._retry_delays_seconds[request_index])
                        continue
                    response.raise_for_status()
                    buffered = ""
                    decoder = codecs.getincrementaldecoder("utf-8")()
                    spoken = SpokenTextFilter()
                    done = False
                    async for raw_line in response.content.iter_any():
                        buffered += decoder.decode(raw_line)
                        lines = buffered.split("\n")
                        buffered = lines.pop()
                        for line in lines:
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                done = True
                                break
                            try:
                                item = json.loads(data)
                                choice_delta = item["choices"][0].get("delta", {})
                                delta = choice_delta.get("content") or ""
                                for fragment in choice_delta.get("tool_calls") or []:
                                    index = int(fragment.get("index") or 0)
                                    current = pending_calls.setdefault(
                                        index, {"id": "", "name": "", "arguments": ""}
                                    )
                                    current["id"] = str(
                                        fragment.get("id") or current["id"]
                                    )
                                    function = fragment.get("function") or {}
                                    current["name"] += str(function.get("name") or "")
                                    current["arguments"] += str(
                                        function.get("arguments") or ""
                                    )
                            except (KeyError, IndexError, json.JSONDecodeError):
                                continue
                            if delta:
                                text = spoken.push(str(delta))
                                if text:
                                    emitted_output = True
                                    yield text
                        if done:
                            break
                break
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
                if emitted_output or request_index >= len(self._retry_delays_seconds):
                    raise
                await asyncio.sleep(self._retry_delays_seconds[request_index])
        for index in sorted(pending_calls):
            call = pending_calls[index]
            if call["name"]:
                yield ActionToolCall(
                    id=call["id"] or f"tool-call-{index}",
                    name=call["name"],
                    arguments=parse_tool_arguments(call["arguments"]),
                )


def tts_token_budget(text: str) -> int:
    # Numeric strings can speak as many words: a ten-digit account is not one word.
    spoken_words = max(1, len(text.split())) + sum(
        len(digits) - 1 for digits in re.findall(r"[0-9]+", text)
    )
    return max(24, min(360, int((spoken_words / 2.5 + 0.5) * 12.5) + 8))


class RTTSClient:
    def __init__(self, config: LabConfig, session: aiohttp.ClientSession) -> None:
        self.config = config
        self.session = session
        self._ref_audio: str | None = None
        if config.tts_ref_audio:
            raw = Path(config.tts_ref_audio).expanduser().read_bytes()
            self._ref_audio = "data:audio/wav;base64," + base64.b64encode(raw).decode("ascii")

    async def stream(self, text: str) -> tuple[int, AsyncIterator[bytes]]:
        payload: dict[str, Any] = {
            "input": text,
            "model": self.config.tts_model,
            "language": self.config.tts_language,
            "response_format": "pcm",
            "stream": True,
            "stream_format": "audio",
            "initial_codec_chunk_frames": self.config.tts_initial_codec_chunk_frames,
            "max_new_tokens": tts_token_budget(text),
        }
        if self.config.tts_voice:
            payload.update(
                task_type="Base",
                voice=self.config.tts_voice,
                x_vector_only_mode=False,
            )
        elif self._ref_audio:
            payload.update(
                task_type="Base",
                ref_audio=self._ref_audio,
                ref_text=self.config.tts_ref_text,
                x_vector_only_mode=False,
            )
        else:
            payload.update(
                task_type="VoiceDesign",
                instructions="A warm, clear female conversational voice. Natural pacing and no exaggerated emotion.",
            )
        response = await self.session.post(self.config.tts_url, json=payload)
        response.raise_for_status()
        sample_rate = int(response.headers.get("x-sample-rate") or 24000)

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for aligned in frame_pcm16_chunks(response.content.iter_chunked(4096)):
                    yield aligned
            finally:
                response.release()

        return sample_rate, chunks()
