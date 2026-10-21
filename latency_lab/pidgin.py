"""Nigerian Pidgin's batch speech stack; the LLM and call transport stay unchanged."""
from __future__ import annotations

import asyncio
import io
import wave
from dataclasses import replace

import aiohttp
from livekit import rtc

EJIRO_MODEL = "mms-pcm-ejiro-3h"
EJIRO_URL = "http://102.88.137.124:8080/tts-ejiro/v1/audio/speech"
SBPN_MODEL = "ogunlao/SBPN_multilingual_base"
SBPN_URL = "http://102.88.137.124:8080/asr-sbpn/v1/audio/transcriptions"
LANGUAGE_RULES = """

CALL LANGUAGE: Nigerian Pidgin (pcm). Speak natural Nigerian Pidgin throughout
this call, not standard English. This applies to greetings, tool acknowledgements,
progress updates, answers and goodbyes, even when the instructions, retrieved
information or tool results are in English. Preserve names, amounts, account
numbers and factual meaning. Do not translate tool names or JSON argument keys.
Do not imitate an accent with misspellings or invent facts. Use short, complete
spoken sentences and ask one question at a time. Keep all existing safety and
business rules. For example: "Abeg give me small time make I check am for you."
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
    return replace(config, tts_language="Nigerian Pidgin", tts_model=EJIRO_MODEL,
                   tts_voice="ejiro", tts_url=EJIRO_URL, tts_ref_audio="",
                   stt_model=SBPN_MODEL, stt_ws_url="", stt_batch_url=SBPN_URL,
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
        async def chunks():
            for part in split_text(text):
                if not part.strip():
                    continue
                async with self.session.post(self.config.tts_url, json={
                    "model": EJIRO_MODEL, "voice": "ejiro", "input": part,
                    "response_format": "wav",
                }, timeout=aiohttp.ClientTimeout(total=60)) as response:
                    response.raise_for_status()
                    # Batch WAV is decoded before playback, never treated as raw PCM.
                    body = bytearray()
                    async for block in response.content.iter_chunked(65536):
                        body.extend(block)
                        if len(body) > 2 * 1024 * 1024:
                            raise ValueError("Ejiro audio exceeds the 30-second response budget")
                    pcm = wav_to_call_pcm(bytes(body))
                if self.usage is not None:
                    self.usage.tts_accepted(part)
                for offset in range(0, len(pcm), 4800):
                    yield pcm[offset:offset + 4800]
        return 24000, chunks()


class SBPNClient:
    """VAD supplies complete turns; no realtime socket or English-only cleanup."""
    def __init__(self, config, on_partial, on_final, on_provider_event):
        self.config, self.on_final = config, on_final
        self.event = on_provider_event
        self.session = None
        self.usage = None
        self.tasks = set()

    @property
    def ready(self):
        return self.session is not None and not self.session.closed

    async def connect(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self.event("stt_batch_ready", {"model": SBPN_MODEL, "transport": "http_batch"})

    async def warmup(self, duration_ms=0):
        return self.ready

    async def append(self, pcm):
        pass  # The pipeline already owns the VAD turn buffer.

    async def commit(self, pcm):
        await self.recover("new_turn")
        task = asyncio.create_task(self._transcribe(bytes(pcm)), name="sbpn-final")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

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
            await self.on_final(text)
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

    async def close(self):
        await self.recover("closed")
        if self.session:
            await self.session.close()
