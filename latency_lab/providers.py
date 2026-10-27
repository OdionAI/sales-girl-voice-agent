from __future__ import annotations

import asyncio
import base64
import codecs
import hashlib
import io
import json
import re
import time
import wave
from contextvars import ContextVar
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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

_llm_trace_scope: ContextVar[dict | None] = ContextVar("llm_trace_scope", default=None)


async def scoped_llm_stream(stream, *, turn_id, attempt_id, phase):
    """Scope observations to this iterator, including concurrent/background turns."""
    context = dict(turn_id=turn_id, attempt_id=attempt_id, phase=phase)
    iterator = stream.__aiter__()
    try:
        while True:
            token = _llm_trace_scope.set(context)
            try:
                event = await anext(iterator)
            except StopAsyncIteration:
                break
            finally:
                _llm_trace_scope.reset(token)
            yield event
    finally:
        token = _llm_trace_scope.set(context)
        try:
            if hasattr(iterator, "aclose"):
                await iterator.aclose()
        finally:
            _llm_trace_scope.reset(token)


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
        self._force_realtime_final_once = False
        self._closing = False
        self.usage = None

    @property
    def ready(self) -> bool:
        return bool(self.ws and not self.ws.closed)

    @property
    def uses_realtime_final(self) -> bool:
        # When a hint-capable chat final is configured the realtime socket is
        # partials-only and the authoritative text comes from _transcribe_chat.
        if self.config.stt_chat_url:
            return False
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
                            "language": "fr" if self.config.tts_language.lower() in {"fr", "french", "français"} else "en",
                            "to_language": "fr" if self.config.tts_language.lower() in {"fr", "french", "français"} else "en",
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
            if self.usage is not None:
                self.usage.stt_submitted(len(pcm16))
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
        if self.config.stt_chat_url:
            self.on_provider_event(
                "stt_chat_final_requested",
                {
                    "pcm_bytes": len(audio),
                    "audio_ms": round(len(audio) / 2 / 16000 * 1000, 1),
                    "has_context": bool(self.config.stt_context),
                },
            )
            task = asyncio.create_task(
                self._run_authoritative_chat(audio), name="rvc-lab-stt-chat-final"
            )
            self._batch_tasks.add(task)
            task.add_done_callback(self._batch_tasks.discard)
            return
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
        form.add_field("language", "fr" if self.config.tts_language.lower() in {"fr", "french", "français"} else "en")
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
                if self.usage is not None:
                    self.usage.stt_submitted(len(pcm16))
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

    async def _run_authoritative_chat(self, pcm16: bytes) -> None:
        """Committed final via the hint-capable Qwen chat endpoint.

        The realtime socket is quiesced first (it shares one Qwen/vLLM engine),
        then reopened for the next turn. If the chat call yields nothing we fall
        back to the realtime answer for this turn so a turn is never lost.
        """
        async with self._inference_mode_lock:
            self.on_provider_event(
                "stt_inference_handoff_start",
                {"from": "realtime", "to": "chat", "reason": "authoritative_final"},
            )
            await self._quiesce_realtime_locked()
            produced = False
            try:
                produced = await self._transcribe_chat(pcm16)
            finally:
                if not self._closing:
                    try:
                        await self._open_socket_locked()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.on_provider_event(
                            "stt_realtime_resume_degraded",
                            {"error": str(exc), "error_type": type(exc).__name__},
                        )
            if not produced and not self._closing:
                self.on_provider_event(
                    "stt_chat_final_fallback_realtime", {"reason": "chat_final_empty_or_failed"},
                )
                ws = self.ws
                if ws and not ws.closed:
                    self._reset_decoder_state()
                    step = 16000 * 2 // 5
                    for offset in range(0, len(pcm16), step):
                        await ws.send_json({
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(pcm16[offset:offset + step]).decode("ascii"),
                        })
                    self._force_realtime_final_once = True
                    await ws.send_json({"type": "input_audio_buffer.commit", "final": True})

    async def _transcribe_chat(self, pcm16: bytes) -> bool:
        started = time.monotonic()
        if not pcm16:
            self.on_provider_event(
                "stt_chat_error",
                {"error": "local utterance buffer was empty", "error_type": "EmptyAudio"},
            )
            return False
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
        wav_audio = pcm16_wav(pcm16)
        messages: list[dict[str, Any]] = []
        if self.config.stt_context:
            messages.append({"role": "system", "content": self.config.stt_context})
        messages.append({
            "role": "user",
            "content": [{
                "type": "input_audio",
                "input_audio": {
                    "data": base64.b64encode(wav_audio).decode("ascii"),
                    "format": "wav",
                },
            }],
        })
        body = {
            "model": self.config.stt_chat_model,
            "messages": messages,
            "max_tokens": 256,
            "temperature": 0.0,
        }
        self.on_provider_event(
            "stt_chat_request_start",
            {
                "url": self.config.stt_chat_url,
                "wav_bytes": len(wav_audio),
                "context": bool(self.config.stt_context),
            },
        )
        try:
            timeout = aiohttp.ClientTimeout(total=self.config.stt_batch_timeout_seconds)
            async with self.session.post(
                self.config.stt_chat_url, json=body, timeout=timeout
            ) as response:
                response.raise_for_status()
                if self.usage is not None:
                    self.usage.stt_submitted(len(pcm16))
                payload = await response.json()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.on_provider_event(
                "stt_chat_error",
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                },
            )
            return False
        content = ""
        if isinstance(payload, dict):
            try:
                content = payload["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                content = str(payload.get("text") or "")
        final = clean_transcript(content or "")
        elapsed_ms = (time.monotonic() - started) * 1000
        self.on_provider_event(
            "stt_chat_final",
            {"elapsed_ms": round(elapsed_ms, 1), "text": final, "authoritative": True},
        )
        if final:
            await self.on_final(final)
            return True
        self.on_provider_event(
            "stt_chat_error",
            {"error": "chat endpoint returned an empty transcript", "error_type": "EmptyTranscript",
             "elapsed_ms": round(elapsed_ms, 1)},
        )
        return False

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
                    authoritative = self.uses_realtime_final or self._force_realtime_final_once
                    self._force_realtime_final_once = False
                    if authoritative:
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
        self.usage = None
        self.trace = None

    def _observe(self, event, **data):
        if self.trace is not None:
            self.trace.record(event, **(_llm_trace_scope.get() or {}), **data)

    @staticmethod
    def _normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep system content at index 0 only.

        Some call sites added guidance as a system message mid-conversation; the
        provider answers 400 ("System message must be at the beginning."), which
        left the agent silent right after a tool result. Fold any such message
        into the leading system message so the request stays valid.
        """
        if not any(m.get("role") == "system" for m in messages[1:]):
            return messages
        system_parts: list[str] = []
        rest: list[dict[str, Any]] = []
        for message in messages:
            if message.get("role") == "system":
                system_parts.append(str(message.get("content") or ""))
            else:
                rest.append(message)
        merged = "\n\n".join(part for part in system_parts if part)
        return ([{"role": "system", "content": merged}] if merged else []) + rest

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        max_tokens: int = 180,
        temperature: float = 0.35,
    ) -> AsyncIterator[str | ActionToolCall]:
        payload = {
            "model": self.config.llm_model,
            "messages": self._normalize_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": temperature,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {
                "enable_thinking": bool(self.config.llm_enable_thinking),
                "thinking": bool(self.config.llm_enable_thinking),
            },
        }
        # Groq rejects the self-hosted Qwen chat-template extension.
        if urlsplit(self.config.llm_base_url).hostname == "api.groq.com":
            payload.pop("chat_template_kwargs", None)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        headers = {"Authorization": f"Bearer {self.config.llm_api_key}"}
        max_empty_retries = max(0, int(self.config.llm_empty_retries))
        max_attempts = len(self._retry_delays_seconds) + 1 + max_empty_retries
        empty_retries_used = 0
        pending_calls: dict[int, dict[str, str]] = {}
        emitted_output = False
        for request_index in range(max_attempts):
            request_id = f"llm-{time.perf_counter_ns()}"
            request_started = time.perf_counter_ns()
            request_finished_ns = None
            first_text_ns = None
            provider_done = False
            observed_usage = {}
            self._observe("llm_provider_request_start", request_id=request_id,
                          request_index=request_index + 1)
            pending_calls = {}
            produced_this_request = False
            usage_request = self.usage.llm_started() if self.usage is not None else None
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
                        request_finished_ns = time.perf_counter_ns()
                        await asyncio.sleep(self._retry_delays_seconds[request_index])
                        continue
                    response.raise_for_status()
                    buffered = ""
                    decoder = codecs.getincrementaldecoder("utf-8")()
                    spoken = SpokenTextFilter()
                    done = False
                    # Guard against dead air: while no usable output has arrived, read
                    # with a deadline so a stalling provider is abandoned quickly and the
                    # caller hears the graceful fallback instead of ~20s of silence.
                    first_output_deadline = asyncio.get_running_loop().time() + max(
                        0.2, float(self.config.llm_first_output_timeout_seconds)
                    )
                    stream_iter = response.content.iter_any().__aiter__()
                    while True:
                        if produced_this_request or done:
                            read_timeout = None
                        else:
                            read_timeout = max(
                                0.05,
                                first_output_deadline - asyncio.get_running_loop().time(),
                            )
                        try:
                            if read_timeout is None:
                                raw_line = await stream_iter.__anext__()
                            else:
                                raw_line = await asyncio.wait_for(
                                    stream_iter.__anext__(), read_timeout
                                )
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            if self.trace is not None:
                                self.trace.record(
                                    "llm_first_output_timeout",
                                    reason="no_output_within_deadline",
                                    seconds=round(
                                        float(self.config.llm_first_output_timeout_seconds), 2
                                    ),
                                    attempt=request_index + 1,
                                    model=self.config.llm_model,
                                )
                            break
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
                                if isinstance(item.get("usage"), dict):
                                    usage = item["usage"]
                                    cached = usage.get("prompt_tokens_details") or {}
                                    observed_usage.update({key: value for key, value in {
                                        "input_tokens": usage.get("prompt_tokens"),
                                        "output_tokens": usage.get("completion_tokens"),
                                        "cached_tokens": cached.get("cached_tokens") if isinstance(cached, dict) else None,
                                    }.items() if isinstance(value, int) and not isinstance(value, bool) and value >= 0})
                                if self.usage is not None and item.get("usage") is not None:
                                    self.usage.llm_usage(usage_request, item["usage"])
                                if not item.get("choices"):
                                    continue
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
                                    if first_text_ns is None:
                                        first_text_ns = time.perf_counter_ns()
                                        self._observe("llm_provider_first_text", request_id=request_id,
                                            request_index=request_index + 1,
                                            provider_ttft_ms=round((first_text_ns - request_started) / 1e6, 3))
                                    emitted_output = True
                                    produced_this_request = True
                                    yield text
                        if done:
                            provider_done = True
                            break
                request_finished_ns = time.perf_counter_ns()
                # A 200 response that carried neither spoken text nor a tool call is
                # an empty completion. Re-issue the request a bounded number of times
                # before letting the caller-facing fallback take over.
                got_tool_calls = any(str(call.get("name") or "") for call in pending_calls.values())
                if not produced_this_request and not got_tool_calls:
                    if empty_retries_used < max_empty_retries:
                        empty_retries_used += 1
                        if self.trace is not None:
                            self.trace.record(
                                "llm_empty_completion_retry",
                                reason="provider_returned_no_text_or_tool_call",
                                attempt=empty_retries_used,
                                max_retries=max_empty_retries,
                                model=self.config.llm_model,
                            )
                        await asyncio.sleep(self.config.llm_empty_retry_delay_seconds)
                        continue
                    if self.trace is not None and max_empty_retries:
                        self.trace.record(
                            "llm_empty_completion_exhausted",
                            reason="provider_still_empty_after_retries",
                            attempts=empty_retries_used + 1,
                            max_retries=max_empty_retries,
                            model=self.config.llm_model,
                        )
                break
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
                request_finished_ns = time.perf_counter_ns()
                if emitted_output or request_index >= len(self._retry_delays_seconds):
                    raise
                await asyncio.sleep(self._retry_delays_seconds[request_index])
            finally:
                self._observe("llm_provider_request_complete", request_id=request_id,
                    request_index=request_index + 1, completed=provider_done,
                    first_text_observed=first_text_ns is not None,
                    total_ms=round(((request_finished_ns or time.perf_counter_ns()) - request_started) / 1e6, 3),
                    **observed_usage)
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


def resolve_tts_seed(config: LabConfig) -> str:
    """Return the seed to pin a TTS voice.

    An explicit configured seed wins; otherwise derive a stable per-voice seed
    from the voice id so the voice never drifts between generations.
    """
    seed = str(getattr(config, "tts_seed", "") or "").strip()
    voice = str(getattr(config, "tts_voice", "") or "").strip()
    if not seed and voice:
        seed = str(int(hashlib.sha256(voice.encode("utf-8")).hexdigest()[:8], 16))
    return seed


class RTTSClient:
    def __init__(self, config: LabConfig, session: aiohttp.ClientSession) -> None:
        self.config = config
        self.session = session
        self.usage = None
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
        # Pin the voice so it does not drift between generations.
        seed = resolve_tts_seed(self.config)
        if seed:
            payload["seed"] = seed
        response = await self.session.post(self.config.tts_url, json=payload)
        response.raise_for_status()
        if self.usage is not None:
            self.usage.tts_accepted(text)
        sample_rate = int(response.headers.get("x-sample-rate") or 24000)

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for aligned in frame_pcm16_chunks(response.content.iter_chunked(4096)):
                    yield aligned
            finally:
                response.release()

        return sample_rate, chunks()
