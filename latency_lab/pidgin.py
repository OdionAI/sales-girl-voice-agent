"""Nigerian Pidgin speech adapters; the LLM and call transport stay unchanged."""
from __future__ import annotations

import asyncio
import io
import json
import re
import unicodedata
import wave
from dataclasses import replace

import aiohttp
from livekit import rtc

EJIRO_MODEL = "qwen-pidgin-f1-cleaned-epoch5"
PIDGIN_VOICE = "favour"
EJIRO_URL = "http://102.88.137.124:8080/tts-ejiro/v1/audio/speech"
DYNAMIC_PIDGIN_MODEL = "dynamic-accent-pidgin-flash"
DYNAMIC_PIDGIN_URL = "http://102.88.137.124:8080/tts-pidgin-dynamic/v1/audio/speech"
DYNAMIC_PIDGIN_VOICES = {"maro", "rukky"}
SBPN_MODEL = "ogunlao/SBPN_multilingual_base"
SBPN_URL = "http://102.88.137.124:8080/asr-sbpn/v1/audio/transcriptions"
SBPN_WS_URL = "ws://102.88.137.124:8080/asr-sbpn/v1/realtime"
LANGUAGE_RULES = """

CALL LANGUAGE: Nigerian Pidgin (pcm). Speak natural Nigerian Pidgin throughout
this call, not standard English. This applies to greetings, tool acknowledgements,
progress updates, answers and goodbyes, even when the instructions, retrieved
information, customer context or tool results are in English. Do not switch to
formal English unless the caller explicitly asks or an exact quotation is necessary.
Use abeg, wetin, wey, dey, don, fit, na, una, am and make I where natural; do not
force slang. For the spoken verb confirm, write konfam, with K. This is the only
tested pronunciation-spelling exception; do not invent other phonetic spellings.
Preserve facts, names, brands, addresses, amounts, URLs, account/phone numbers and
codes. Never respell tool names, JSON keys, enums, numeric arguments or stored
customer records. These rules apply only to spoken natural-language output.
Use short complete sentences, one question at a time, without Markdown or numbered
lists. Keep all existing safety and business rules. Speak amounts as the correct
amount in words, and read identifiers digit by digit without losing any leading
zero. Keep numeric tool arguments as numbers. For example:
"Abeg, konfam your account number again."
"Make I check am for you. Give me small time, abeg."
"""


def is_pidgin(language: str) -> bool:
    return str(language or "").strip().lower() in {"pcm", "pd", "pidgin", "nigerian pidgin"}


def apply_pidgin_stack(config, context=None, identity=None):
    overrides = (identity or {}).get("runtime_overrides") or {}
    # Saved agent settings are authoritative for SDK/web/SIP calls. Only an
    # explicit Voice Lab language override supersedes the saved language.
    language = overrides.get("tts_language_hint") or config.speech_language_override or getattr(context, "tts_language_hint", "") or config.tts_language
    if not is_pidgin(language):
        return config
    saved = (getattr(context, "tts_voice_id", "")
             if is_pidgin(getattr(context, "tts_language_hint", "")) else "")
    configured = str(config.tts_voice or "").strip().lower()
    if configured not in DYNAMIC_PIDGIN_VOICES:
        configured = ""
    requested = str(overrides.get("tts_voice_id") or overrides.get("tts_voice") or saved or configured).strip().lower()
    dynamic = requested in DYNAMIC_PIDGIN_VOICES
    # Only the two new IDs opt in; existing saved identifiers keep their Favour route.
    return replace(config, tts_language="Nigerian Pidgin",
                   tts_model=DYNAMIC_PIDGIN_MODEL if dynamic else EJIRO_MODEL,
                   tts_voice=requested if dynamic else PIDGIN_VOICE,
                   tts_url=DYNAMIC_PIDGIN_URL if dynamic else EJIRO_URL,
                   tts_ref_audio="", tts_seed="0" if dynamic else "17",
                   stt_model=SBPN_MODEL, stt_ws_url=SBPN_WS_URL, stt_batch_url=SBPN_URL,
                   stt_batch_timeout_seconds=120.0)


def split_text(text: str, limit: int = 240) -> list[str]:
    """Lossless splitting, preferring sentence/word boundaries over mid-word cuts."""
    if limit < 1:
        raise ValueError("Text limit must be positive")
    parts = []
    while len(text) > limit:
        window = text[:limit]
        sentence = max(window.rfind(mark) + len(mark) for mark in (". ", "? ", "! ", "; "))
        boundary = sentence if sentence > limit // 2 else window.rfind(" ") + 1
        boundary = boundary or limit
        parts.append(text[:boundary])
        text = text[boundary:]
    if text:
        parts.append(text)
    return parts


def ejiro_spoken_text(text: str) -> str:
    """Spoken-only numeric compatibility; never rewrite structured/customer data."""
    words = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
    text = text.replace("\u20a6", " naira ").replace("%", " percent ")
    # Keep numeric punctuation audible without guessing the intended amount.
    text = re.sub(r"(?<=\d)\.(?=\d)", " point ", text)
    return re.sub(r"\d+", lambda match: " " + " ".join(words[unicodedata.decimal(c)] for c in match[0]) + " ", text)


def wav_to_call_pcm(body: bytes) -> bytes:
    with wave.open(io.BytesIO(body), "rb") as reader:
        if (reader.getnchannels(), reader.getsampwidth(), reader.getframerate(), reader.getcomptype()) != (1, 2, 16000, "NONE"):
            raise ValueError("Ejiro must return 16 kHz mono PCM16 WAV")
        pcm = reader.readframes(reader.getnframes())
        if len(pcm) != reader.getnframes() * 2:
            raise ValueError("Ejiro returned a truncated WAV")
    if not pcm:
        raise ValueError("Ejiro returned empty audio")
    resampler = rtc.AudioResampler(input_rate=16000, output_rate=24000, num_channels=1)
    frames = resampler.push(rtc.AudioFrame(pcm, 16000, 1, len(pcm) // 2))
    frames.extend(resampler.flush())
    return b"".join(bytes(frame.data) for frame in frames)


class EjiroTTSClient:
    def __init__(self, config, session):
        self.config, self.session, self.usage = config, session, None

    async def stream(self, text):
        if self.config.tts_voice in DYNAMIC_PIDGIN_VOICES:
            return 24000, self._dynamic_chunks(text)
        async def chunks():
            for part in split_text(ejiro_spoken_text(text)):
                if not part.strip():
                    continue
                async with self.session.post(self.config.tts_url, json={
                    "model": EJIRO_MODEL, "voice": PIDGIN_VOICE, "input": part,
                    "response_format": "pcm", "stream": True, "seed": 17, "speed": 1,
                }, timeout=aiohttp.ClientTimeout(total=60)) as response:
                    response.raise_for_status()
                    if (response.content_type != "audio/pcm" or
                        response.headers.get("x-sample-rate") != "16000" or
                        response.headers.get("x-channels") != "1" or
                        response.headers.get("x-audio-format") != "pcm_s16le"):
                        raise ValueError("Ejiro must stream 16 kHz mono PCM16")
                    resampler = rtc.AudioResampler(input_rate=16000, output_rate=24000, num_channels=1)
                    pending = b""
                    received = 0
                    async for block in response.content.iter_chunked(10240):
                        received += len(block)
                        if received > 30 * 16000 * 2:
                            raise ValueError("Ejiro audio exceeds the 30-second response budget")
                        pending += block
                        length = len(pending) // 2 * 2
                        if length:
                            frame = rtc.AudioFrame(pending[:length], 16000, 1, length // 2)
                            pending = pending[length:]
                            for output in resampler.push(frame):
                                yield bytes(output.data)
                    if not received or pending:
                        raise ValueError("Ejiro returned empty or truncated PCM16")
                    for output in resampler.flush():
                        yield bytes(output.data)
                    if self.usage is not None:
                        self.usage.tts_accepted(part)
        return 24000, chunks()

    async def _dynamic_chunks(self, text):
        # This model already produces call-rate PCM; never apply Favour's 16 kHz resampler.
        voice = self.config.tts_voice
        for part in split_text(text, limit=140):
            if not part.strip():
                continue
            payload = {"model": DYNAMIC_PIDGIN_MODEL, "voice": voice, "language": "pcm",
                       "input": part, "response_format": "pcm", "stream": True,
                       "seed": 0, "speed": 1}
            async with self.session.post(self.config.tts_url, json=payload,
                timeout=aiohttp.ClientTimeout(total=180, sock_read=90)) as response:
                response.raise_for_status()
                if (response.content_type != "audio/pcm" or any(
                    response.headers.get(key) != value for key, value in {
                        "X-Audio-Sample-Rate": "24000", "X-Audio-Channels": "1",
                        "X-Model": DYNAMIC_PIDGIN_MODEL, "X-Voice": voice,
                    }.items())):
                    raise ValueError("Dynamic Pidgin TTS returned an unexpected voice, model or PCM format")
                received = 0
                pending = b""
                async for block in response.content.iter_chunked(9600):
                    received += len(block)
                    if received > 30 * 24000 * 2:
                        raise ValueError("Dynamic Pidgin audio exceeds the 30-second segment budget")
                    pending += block
                    length = len(pending) // 2 * 2
                    if length:
                        yield pending[:length]
                        pending = pending[length:]
                if not received or pending:
                    raise ValueError("Dynamic Pidgin returned empty or truncated PCM16")
                if self.usage is not None:
                    self.usage.tts_accepted(part)


class SBPNClient:
    """Revisable live prefixes, with bounded batch recovery for a complete turn."""
    def __init__(self, config, on_partial, on_final, on_provider_event, *, on_final_unavailable=None):
        self.config, self.on_final = config, on_final
        self.on_final_unavailable = on_final_unavailable
        self.on_partial = on_partial
        self.event = on_provider_event
        self.session = None
        self.usage = None
        self.tasks = set()
        self.ws = None
        self.receiver = None
        self.final_result = None
        self.streamed_bytes = 0
        self.degraded = False
        self.pending_final = False

    @property
    def ready(self):
        return self.session is not None and not self.session.closed

    async def connect(self):
        if not self.ready:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        try:
            await self._open_socket()
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            self.degraded = True
            self.event("stt_realtime_connect_degraded", {"error": str(exc), "batch_final_available": True})

    async def _open_socket(self):
        if self.ws and not self.ws.closed:
            return
        ws = await self.session.ws_connect(self.config.stt_ws_url, heartbeat=20)
        try:
            hello = await asyncio.wait_for(ws.receive_json(), 8)
            if hello.get("type") != "session.created":
                raise ValueError("SBPN did not create a realtime session")
            await ws.send_json({"type": "session.update", "model": SBPN_MODEL})
        except BaseException:
            await ws.close()
            raise
        self.ws = ws
        self.receiver = asyncio.create_task(self._receive(ws), name="sbpn-receive")
        self.event("stt_session_created", {"model": SBPN_MODEL, "transport": "websocket"})

    async def _receive(self, ws):
        try:
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                payload = json.loads(message.data)
                if payload.get("type") == "error":
                    raise ValueError(str(payload.get("error") or "SBPN stream error"))
                text = str(payload.get("text") or "").strip()
                if payload.get("type") == "transcription.delta" and not self.pending_final:
                    # text is the entire revised prefix, not an append-only delta.
                    await self.on_partial(text, text)
                elif payload.get("type") == "transcription.done":
                    if self.final_result and not self.final_result.done():
                        self.final_result.set_result(text)
        except (aiohttp.ClientError, ValueError) as exc:
            self.event("stt_realtime_error", {"error": str(exc)})
        finally:
            if self.ws is ws:
                self.ws = None
            if self.final_result and not self.final_result.done():
                self.final_result.set_exception(ConnectionError("SBPN socket closed before final"))
            await ws.close()

    async def warmup(self, duration_ms=0):
        return self.ready

    async def append(self, pcm):
        if self.pending_final or self.degraded or not pcm:
            return
        if self.streamed_bytes + len(pcm) > 29 * 16000 * 2:
            self.degraded = True
            await self._close_socket()
            self.event("stt_stream_limit_fallback", {"model": SBPN_MODEL, "batch_final_available": True})
            return
        try:
            if not self.ws or self.ws.closed:
                if self.streamed_bytes:
                    raise ConnectionError("SBPN disconnected during a turn")
                await self._open_socket()
            for offset in range(0, len(pcm), 6400):
                await self.ws.send_bytes(pcm[offset:offset + 6400])
            self.streamed_bytes += len(pcm)
            if self.usage is not None:
                self.usage.stt_submitted(len(pcm))
        except (aiohttp.ClientError, ConnectionError, TimeoutError, ValueError) as exc:
            self.degraded = True
            self.event("stt_realtime_audio_send_failed", {"error": str(exc), "batch_final_available": True})

    async def commit(self, pcm):
        if self.pending_final:
            await self.recover("new_turn")
        self.pending_final = True
        task = asyncio.create_task(self._finalize(bytes(pcm)), name="sbpn-final")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _finalize(self, pcm):
        text = None
        try:
            if self.ws and not self.ws.closed and self.streamed_bytes and not self.degraded:
                self.final_result = asyncio.get_running_loop().create_future()
                try:
                    await self.ws.send_json({"type": "input_audio_buffer.commit", "final": True})
                    text = await asyncio.wait_for(self.final_result, 30)
                    if not text:
                        raise ValueError("SBPN returned an empty transcript")
                except (aiohttp.ClientError, ConnectionError, TimeoutError, ValueError) as exc:
                    self.event("stt_realtime_final_fallback", {"error": str(exc)})
                else:
                    self.event("stt_realtime_final", {"text": text, "model": SBPN_MODEL, "authoritative": True})
            if not text:
                await self._close_socket()
                text = await self._transcribe(pcm)
        finally:
            self.final_result = None
            self.streamed_bytes = 0
            self.pending_final = False
            self.degraded = False
        if text:
            await self.on_final(text)
        elif self.on_final_unavailable is not None:
            self.event("stt_final_unavailable", {"model": SBPN_MODEL})
            await self.on_final_unavailable()

    async def _transcribe(self, pcm):
        from .providers import pcm16_wav
        try:
            if not pcm or len(pcm) % 2:
                raise ValueError("Expected nonempty PCM16 audio")
            texts = []
            # Chunk long utterances rather than discarding audio or exceeding
            # SBPN's 30 s / 10 MiB limits. One final keeps a single caller turn.
            size = 29 * 16000 * 2
            async with asyncio.timeout(self.config.stt_batch_timeout_seconds):
                for offset in range(0, len(pcm), size):
                    audio = pcm[offset:offset + size]
                    form = aiohttp.FormData()
                    form.add_field("model", SBPN_MODEL)
                    form.add_field("file", pcm16_wav(audio), filename="turn.wav", content_type="audio/wav")
                    async with self.session.post(self.config.stt_batch_url, data=form) as response:
                        response.raise_for_status()
                        payload = await response.json()
                    if self.usage is not None:
                        self.usage.stt_submitted(len(audio))
                    texts.append(str(payload.get("text") or "").strip())
            text = " ".join(part for part in texts if part)
            if not text:
                raise ValueError("SBPN returned an empty transcript")
            self.event("stt_batch_final", {"text": text, "model": SBPN_MODEL, "authoritative": True})
            return text
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.event("stt_batch_error", {"error": str(exc), "error_type": type(exc).__name__})

    async def recover(self, reason):
        tasks = [task for task in self.tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        await self._close_socket()
        self.streamed_bytes = 0
        self.pending_final = self.degraded = False

    async def _close_socket(self):
        ws, receiver = self.ws, self.receiver
        self.ws = self.receiver = None
        if receiver and receiver is not asyncio.current_task():
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
        if ws:
            await ws.close()

    async def close(self):
        await self.recover("closed")
        if self.session:
            await self.session.close()
