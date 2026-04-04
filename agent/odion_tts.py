from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, replace

import aiohttp
from livekit.agents import APIConnectOptions, tts
from livekit.agents._exceptions import APIConnectionError, APIStatusError, APITimeoutError


@dataclass
class _TTSOptions:
    base_url: str
    owner_id: str
    voice_id: str | None
    language: str
    seed: int | None


class OdionTTS(tts.TTS):
    def __init__(
        self,
        *,
        owner_id: str,
        voice_id: str,
        language: str = "Auto",
        seed: int | None = None,
        base_url: str | None = None,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=24000,
            num_channels=1,
        )
        self._opts = _TTSOptions(
            base_url=(base_url or os.getenv("ODION_TTS_BASE_URL", "https://tts.odion.ai")).rstrip("/"),
            owner_id=str(owner_id or "").strip(),
            voice_id=(str(voice_id or "").strip() or None),
            language=str(language or "Auto").strip() or "Auto",
            seed=seed if isinstance(seed, int) and seed >= 0 else None,
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
        payload = {
            "text": self._input_text,
            "language": self._opts.language,
            "owner_id": self._opts.owner_id,
        }
        if self._opts.voice_id:
            payload["voice_id"] = self._opts.voice_id
        if self._opts.seed is not None:
            payload["seed"] = self._opts.seed
        try:
            async with self._tts._ensure_session().post(
                f"{self._opts.base_url}/api/v1/tts/stream",
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
