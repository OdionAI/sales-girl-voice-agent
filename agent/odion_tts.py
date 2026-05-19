from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, replace

import aiohttp
from livekit.agents import APIConnectOptions, tts
from livekit.agents._exceptions import APIConnectionError, APIStatusError, APITimeoutError

logger = logging.getLogger("salesgirl.odion_tts")


@dataclass
class _TTSOptions:
    base_url: str
    owner_id: str
    voice_id: str | None
    language: str
    seed: int | None
    mode: str


class OdionTTS(tts.TTS):
    def __init__(
        self,
        *,
        owner_id: str,
        voice_id: str,
        language: str = "Auto",
        seed: int | None = None,
        mode: str = "default_voice",
        base_url: str | None = None,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=24000,
            num_channels=1,
        )
        self._opts = _TTSOptions(
            base_url=(base_url or os.getenv("ODION_TTS_BASE_URL", "https://eu-tts.odion.ai")).rstrip("/"),
            owner_id=str(owner_id or "").strip(),
            voice_id=(str(voice_id or "").strip() or None),
            language=str(language or "Auto").strip() or "Auto",
            seed=seed if isinstance(seed, int) and seed >= 0 else None,
            mode=str(mode or "default_voice").strip() or "default_voice",
        )
        if not self._opts.owner_id:
            raise ValueError("owner_id is required for OdionTTS")
        self._session = http_session

    @property
    def model(self) -> str:
        return "odion-tts"

    @property
    def provider(self) -> str:
        return "OdionTTS"

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = APIConnectOptions()
    ) -> "ChunkedStream":
        return ChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def _ensure_session(self) -> aiohttp.ClientSession:
        if not self._session:
            self._session = aiohttp.ClientSession()
        return self._session

    async def aclose(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None


class ChunkedStream(tts.ChunkedStream):
    def __init__(self, *, tts: OdionTTS, input_text: str, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts: OdionTTS = tts
        self._opts = replace(tts._opts)

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        await self._stream_with_fallback(output_emitter)

    async def _stream_with_fallback(self, output_emitter: tts.AudioEmitter) -> None:
        try:
            await self._stream_once(output_emitter, self._opts)
        except APIStatusError as exc:
            if self._should_fallback_to_default(exc):
                fallback_opts = replace(self._opts, voice_id=None, mode="default_voice")
                logger.warning(
                    "Odion cloned voice lookup failed for owner_id=%s voice_id=%s; retrying with default voice",
                    self._opts.owner_id,
                    self._opts.voice_id,
                )
                await self._stream_once(output_emitter, fallback_opts)
                return
            raise

    def _should_fallback_to_default(self, exc: APIStatusError) -> bool:
        if not self._opts.voice_id:
            return False
        body = str(getattr(exc, "body", "") or getattr(exc, "message", "") or "").lower()
        return exc.status_code == 404 and "voice_id not found" in body

    async def _stream_once(self, output_emitter: tts.AudioEmitter, opts: _TTSOptions) -> None:
        payload = {
            "text": self._input_text,
            "language": opts.language,
            "owner_id": opts.owner_id,
        }
        if opts.voice_id:
            payload["voice_id"] = opts.voice_id
        if opts.seed is not None:
            payload["seed"] = opts.seed
        logger.info(
            "TTS request -> base_url=%s endpoint=/api/v1/tts/stream owner_id=%s voice_id=%s seed=%s language=%s mode=%s",
            opts.base_url,
            opts.owner_id,
            opts.voice_id,
            opts.seed,
            opts.language,
            opts.mode,
        )
        try:
            async with self._tts._ensure_session().post(
                f"{opts.base_url}/api/v1/tts/stream",
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120, sock_connect=self._conn_options.timeout),
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise APIStatusError(
                        message=body or f"odion tts stream failed ({resp.status})",
                        status_code=resp.status,
                        request_id=resp.headers.get("x-request-id"),
                        body=body,
                    )
                sample_rate = int(resp.headers.get("x-sample-rate") or 24000)
                channels = int(resp.headers.get("x-channels") or 1)
                request_id = str(resp.headers.get("x-request-id") or uuid.uuid4())
                output_emitter.initialize(
                    request_id=request_id,
                    sample_rate=sample_rate,
                    num_channels=channels,
                    mime_type="audio/pcm",
                )
                async for data in resp.content.iter_chunked(4096):
                    if data:
                        output_emitter.push(data)
                output_emitter.flush()
        except asyncio.TimeoutError:
            raise APITimeoutError() from None
        except APIStatusError:
            raise
        except aiohttp.ClientError as exc:
            raise APIConnectionError() from exc
        except Exception as exc:
            raise APIConnectionError() from exc
