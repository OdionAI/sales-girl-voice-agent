from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, replace
from urllib.parse import urlparse

import aiohttp
from livekit.agents import APIConnectOptions, tts
from livekit.agents._exceptions import APIConnectionError, APIStatusError, APITimeoutError

logger = logging.getLogger("salesgirl.odion_tts")

DEFAULT_ODION_TTS_BASE_URL = "https://eu-tts.odion.ai"
DEFAULT_ODION_TTS_STREAM_PATH = "/api/v1/tts/stream"
_PCM16_BYTES_PER_SAMPLE = 2
_DEFAULT_FRAME_SIZE_MS = 200
_DEFAULT_NPU_INITIAL_BUFFER_MS = 0


@dataclass
class _TTSOptions:
    endpoint_url: str
    owner_id: str
    voice_id: str | None
    model: str
    language: str
    seed: int | None
    mode: str
    frame_size_ms: int
    http_chunk_bytes: int
    initial_buffer_ms: int


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using %s", name, raw, default)
        return default
    return max(minimum, min(maximum, value))


def _default_initial_buffer_ms(endpoint_url: str) -> int:
    try:
        host = (urlparse(endpoint_url).hostname or "").lower()
    except Exception:
        host = ""
    if host == "eu-tts.odion.ai":
        return 0
    return _DEFAULT_NPU_INITIAL_BUFFER_MS


def _initial_buffer_bytes(*, sample_rate: int, channels: int, initial_buffer_ms: int) -> int:
    if initial_buffer_ms <= 0:
        return 0
    raw = int(sample_rate * channels * _PCM16_BYTES_PER_SAMPLE * initial_buffer_ms / 1000)
    return raw - (raw % _PCM16_BYTES_PER_SAMPLE)


def _is_full_endpoint_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.path not in {"", "/"}
    )


def _resolve_explicit_tts_endpoint_url(value: str | None) -> str:
    endpoint_or_base = str(value or "").strip().rstrip("/")
    if not endpoint_or_base:
        return ""
    if _is_full_endpoint_url(endpoint_or_base):
        return endpoint_or_base
    return f"{endpoint_or_base}{DEFAULT_ODION_TTS_STREAM_PATH}"


def _resolve_tts_endpoint_url(value: str | None) -> str:
    return _resolve_explicit_tts_endpoint_url(
        value or os.getenv("ODION_TTS_BASE_URL", DEFAULT_ODION_TTS_BASE_URL)
    )


def _endpoint_rewrite_hosts() -> set[str]:
    raw = str(os.getenv("ODION_TTS_ENDPOINT_REWRITE_HOSTS") or "").strip()
    if not raw:
        return set()
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _rewrite_tts_endpoint_url(endpoint_url: str) -> str:
    rewrite_url = _resolve_explicit_tts_endpoint_url(
        os.getenv("ODION_TTS_ENDPOINT_REWRITE_URL")
    )
    if not rewrite_url:
        return endpoint_url
    try:
        host = (urlparse(endpoint_url).hostname or "").lower()
    except Exception:
        host = ""
    if host and host in _endpoint_rewrite_hosts():
        logger.info(
            "Rewriting Odion TTS endpoint from %s to %s for host=%s",
            endpoint_url,
            rewrite_url,
            host,
        )
        return rewrite_url
    return endpoint_url


class OdionTTS(tts.TTS):
    def __init__(
        self,
        *,
        owner_id: str,
        voice_id: str,
        language: str = "Auto",
        seed: int | None = None,
        mode: str = "default_voice",
        model: str = "",
        base_url: str | None = None,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=24000,
            num_channels=1,
        )
        endpoint_url = _rewrite_tts_endpoint_url(_resolve_tts_endpoint_url(base_url))
        self._opts = _TTSOptions(
            endpoint_url=endpoint_url,
            owner_id=str(owner_id or "").strip(),
            voice_id=(str(voice_id or "").strip() or None),
            model=str(model or "").strip(),
            language=str(language or "Auto").strip() or "Auto",
            seed=seed if isinstance(seed, int) and seed >= 0 else None,
            mode=str(mode or "default_voice").strip() or "default_voice",
            frame_size_ms=_env_int(
                "ODION_TTS_FRAME_SIZE_MS",
                _DEFAULT_FRAME_SIZE_MS,
                minimum=10,
                maximum=200,
            ),
            http_chunk_bytes=_env_int(
                "ODION_TTS_HTTP_CHUNK_BYTES",
                4096,
                minimum=2,
                maximum=65536,
            ),
            initial_buffer_ms=_env_int(
                "ODION_TTS_INITIAL_BUFFER_MS",
                _default_initial_buffer_ms(endpoint_url),
                minimum=0,
                maximum=2000,
            ),
        )
        if not self._opts.owner_id:
            raise ValueError("owner_id is required for OdionTTS")
        self._session = http_session

    @property
    def model(self) -> str:
        return self._opts.model or "odion-tts"

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
                # Keep the remainder of the session on the same fallback voice once a
                # configured clone is unavailable, instead of retrying the missing
                # clone on every utterance and producing a shifting voice.
                self._tts._opts = replace(self._tts._opts, voice_id=None, mode="default_voice")
                logger.warning(
                    "Odion cloned voice lookup failed for owner_id=%s voice_id=%s; switching session to default voice and retrying",
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
        if opts.model:
            payload["model"] = opts.model
        if opts.voice_id:
            payload["voice_id"] = opts.voice_id
        if opts.seed is not None:
            payload["seed"] = opts.seed
        logger.info(
            "TTS request -> endpoint_url=%s owner_id=%s voice_id=%s model=%s seed=%s language=%s mode=%s",
            opts.endpoint_url,
            opts.owner_id,
            opts.voice_id,
            opts.model,
            opts.seed,
            opts.language,
            opts.mode,
        )
        try:
            async with self._tts._ensure_session().post(
                opts.endpoint_url,
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
                    frame_size_ms=opts.frame_size_ms,
                )
                initial_target_bytes = _initial_buffer_bytes(
                    sample_rate=sample_rate,
                    channels=channels,
                    initial_buffer_ms=opts.initial_buffer_ms,
                )
                buffered = bytearray()
                carried = b""
                started = initial_target_bytes == 0
                total_bytes = 0
                push_count = 0
                async for data in resp.content.iter_chunked(opts.http_chunk_bytes):
                    if not data:
                        continue
                    chunk = bytes(data)
                    if carried:
                        chunk = carried + chunk
                        carried = b""
                    if len(chunk) % _PCM16_BYTES_PER_SAMPLE:
                        carried = chunk[-1:]
                        chunk = chunk[:-1]
                    if not chunk:
                        continue
                    total_bytes += len(chunk)
                    if not started:
                        buffered.extend(chunk)
                        if len(buffered) < initial_target_bytes:
                            continue
                        output_emitter.push(bytes(buffered))
                        push_count += 1
                        buffered.clear()
                        started = True
                    else:
                        output_emitter.push(chunk)
                        push_count += 1
                if carried:
                    logger.warning("Dropping trailing odd PCM byte for request_id=%s", request_id)
                if buffered:
                    output_emitter.push(bytes(buffered))
                    push_count += 1
                output_emitter.flush()
                logger.info(
                    "TTS stream complete request_id=%s bytes=%d pushes=%d sample_rate=%d frame_size_ms=%d initial_buffer_ms=%d",
                    request_id,
                    total_bytes,
                    push_count,
                    sample_rate,
                    opts.frame_size_ms,
                    opts.initial_buffer_ms,
                )
        except asyncio.TimeoutError:
            raise APITimeoutError() from None
        except APIStatusError:
            raise
        except aiohttp.ClientError as exc:
            raise APIConnectionError() from exc
        except Exception as exc:
            raise APIConnectionError() from exc
