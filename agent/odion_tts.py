from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, replace
from urllib.parse import urlparse

import aiohttp
from livekit.agents import APIConnectOptions, tts
from livekit.agents._exceptions import APIConnectionError, APIStatusError, APITimeoutError

logger = logging.getLogger("salesgirl.ng_tts")

DEFAULT_NG_TTS_BASE_URL = "https://ng-tts.odion.ai"
DEFAULT_ODION_TTS_BASE_URL = DEFAULT_NG_TTS_BASE_URL
DEFAULT_ODION_TTS_STREAM_PATH = "/api/v1/tts/stream"
_PCM16_BYTES_PER_SAMPLE = 2
_DEFAULT_FRAME_SIZE_MS = 200
_DEFAULT_HTTP_CHUNK_BYTES = 4096
_DEFAULT_NPU_INITIAL_BUFFER_MS = 0
_DEFAULT_OUTPUT_SAMPLE_RATE = 24000
_SUPPORTED_OUTPUT_SAMPLE_RATES = {24000, 48000}
_NPU_ENDPOINT_HOSTS = {
    "ng-tts.odion.ai",
    "102.140.102.211",
    "10.130.151.11",
}


def _normalize_language(value: str | None) -> str:
    language = str(value or "Auto").strip() or "Auto"
    normalized = language.lower().replace("_", "-")
    if normalized in {"en", "eng", "english"} or normalized.startswith("en-"):
        return "English"
    if normalized in {"fr", "fra", "fre", "french"} or normalized.startswith("fr-"):
        return "French"
    return language


@dataclass
class _TTSOptions:
    endpoint_url: str
    owner_id: str
    voice_id: str | None
    model_profile: str
    language: str
    seed: int | None
    mode: str
    api_key: str | None
    is_runpod: bool
    request_timeout_seconds: int
    retry_attempts: int
    retry_backoff_seconds: float
    frame_size_ms: int
    http_chunk_bytes: int
    initial_buffer_ms: int
    output_sample_rate: int


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


def _endpoint_host(endpoint_url: str) -> str:
    try:
        return (urlparse(endpoint_url).hostname or "").lower()
    except Exception:
        return ""


def _is_npu_endpoint(endpoint_url: str) -> bool:
    return _endpoint_host(endpoint_url) in _NPU_ENDPOINT_HOSTS


def _default_output_sample_rate(endpoint_url: str) -> int:
    return _DEFAULT_OUTPUT_SAMPLE_RATE


def _default_frame_size_ms(endpoint_url: str) -> int:
    return _DEFAULT_FRAME_SIZE_MS


def _default_http_chunk_bytes(endpoint_url: str) -> int:
    return _DEFAULT_HTTP_CHUNK_BYTES


def _env_output_sample_rate(endpoint_url: str) -> int:
    default = _default_output_sample_rate(endpoint_url)
    value = _env_int(
        "ODION_TTS_OUTPUT_SAMPLE_RATE",
        default,
        minimum=min(_SUPPORTED_OUTPUT_SAMPLE_RATES),
        maximum=max(_SUPPORTED_OUTPUT_SAMPLE_RATES),
    )
    if value not in _SUPPORTED_OUTPUT_SAMPLE_RATES:
        logger.warning(
            "Ignoring unsupported ODION_TTS_OUTPUT_SAMPLE_RATE=%s; using %s",
            value,
            default,
        )
        return default
    return value


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
        value
        or os.getenv("NG_TTS_BASE_URL")
        or os.getenv("ODION_TTS_BASE_URL")
        or DEFAULT_NG_TTS_BASE_URL
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


def _is_runpod_endpoint_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    host = str(parsed.netloc or "").strip().lower()
    return host.endswith(".api.runpod.ai")


def _resolve_tts_api_key(value: str | None) -> str | None:
    candidate = str(
        value
        or os.getenv("NG_TTS_API_KEY")
        or os.getenv("NG_TTS_AUTH_TOKEN")
        or os.getenv("ODION_TTS_API_KEY")
        or os.getenv("ODION_TTS_AUTH_TOKEN")
        or ""
    ).strip()
    return candidate or None


def _env_positive_int(name: str, default: int) -> int:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_positive_float(name: str, default: float) -> float:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


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
        api_key: str | None = None,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        endpoint_url = _rewrite_tts_endpoint_url(_resolve_tts_endpoint_url(base_url))
        output_sample_rate = _env_output_sample_rate(endpoint_url)
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=output_sample_rate,
            num_channels=1,
        )
        is_runpod = _is_runpod_endpoint_url(endpoint_url)
        self._opts = _TTSOptions(
            endpoint_url=endpoint_url,
            owner_id=str(owner_id or "").strip(),
            voice_id=(str(voice_id or "").strip() or None),
            model_profile=str(model or os.getenv("ODION_TTS_MODEL_PROFILE") or "").strip(),
            language=_normalize_language(language),
            seed=seed if isinstance(seed, int) and seed >= 0 else None,
            mode=str(mode or "default_voice").strip() or "default_voice",
            api_key=_resolve_tts_api_key(api_key),
            is_runpod=is_runpod,
            request_timeout_seconds=_env_positive_int(
                "NG_TTS_REQUEST_TIMEOUT_SECONDS",
                _env_positive_int(
                    "ODION_TTS_REQUEST_TIMEOUT_SECONDS",
                    600 if is_runpod else 120,
                ),
            ),
            retry_attempts=_env_positive_int(
                "NG_TTS_RETRY_ATTEMPTS",
                _env_positive_int(
                    "ODION_TTS_RETRY_ATTEMPTS",
                    3 if is_runpod else 1,
                ),
            ),
            retry_backoff_seconds=_env_positive_float(
                "NG_TTS_RETRY_BACKOFF_SECONDS",
                _env_positive_float(
                    "ODION_TTS_RETRY_BACKOFF_SECONDS",
                    8.0 if is_runpod else 1.0,
                ),
            ),
            frame_size_ms=_env_int(
                "ODION_TTS_FRAME_SIZE_MS",
                _default_frame_size_ms(endpoint_url),
                minimum=10,
                maximum=200,
            ),
            http_chunk_bytes=_env_int(
                "ODION_TTS_HTTP_CHUNK_BYTES",
                _default_http_chunk_bytes(endpoint_url),
                minimum=2,
                maximum=65536,
            ),
            initial_buffer_ms=_env_int(
                "ODION_TTS_INITIAL_BUFFER_MS",
                _default_initial_buffer_ms(endpoint_url),
                minimum=0,
                maximum=2000,
            ),
            output_sample_rate=output_sample_rate,
        )
        if not self._opts.owner_id:
            raise ValueError("owner_id is required for OdionTTS")
        self._session = http_session

    @property
    def model(self) -> str:
        return self._opts.model_profile or "odion-tts"

    @property
    def provider(self) -> str:
        return "NgTTS"

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
                    "Odion cloned voice unavailable for owner_id=%s voice_id=%s status=%s; switching session to default voice and retrying",
                    self._opts.owner_id,
                    self._opts.voice_id,
                    exc.status_code,
                )
                await self._stream_once(output_emitter, fallback_opts)
                return
            raise

    def _should_fallback_to_default(self, exc: APIStatusError) -> bool:
        if not self._opts.voice_id:
            return False
        body = str(getattr(exc, "body", "") or getattr(exc, "message", "") or "").lower()
        if exc.status_code == 404 and "voice_id not found" in body:
            return True
        return 500 <= int(exc.status_code or 0) < 600

    async def _stream_once(self, output_emitter: tts.AudioEmitter, opts: _TTSOptions) -> None:
        for attempt in range(1, max(1, opts.retry_attempts) + 1):
            try:
                await self._stream_request(output_emitter, opts)
                return
            except APIStatusError as exc:
                if attempt < opts.retry_attempts and self._should_retry_request(exc, opts):
                    logger.warning(
                        "NG TTS transient status from %s (attempt %s/%s): status=%s body=%s",
                        opts.endpoint_url,
                        attempt,
                        opts.retry_attempts,
                        exc.status_code,
                        str(getattr(exc, "body", "") or getattr(exc, "message", "") or "")[:200],
                    )
                    await asyncio.sleep(opts.retry_backoff_seconds)
                    continue
                raise
            except (asyncio.TimeoutError, aiohttp.ServerTimeoutError) as exc:
                if attempt < opts.retry_attempts:
                    logger.warning(
                        "NG TTS timeout from %s (attempt %s/%s): %s",
                        opts.endpoint_url,
                        attempt,
                        opts.retry_attempts,
                        exc,
                    )
                    await asyncio.sleep(opts.retry_backoff_seconds)
                    continue
                raise APITimeoutError(
                    f"NG TTS request timed out after {attempt} attempt(s)."
                ) from exc
            except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError) as exc:
                if attempt < opts.retry_attempts:
                    logger.warning(
                        "NG TTS connection issue from %s (attempt %s/%s): %s",
                        opts.endpoint_url,
                        attempt,
                        opts.retry_attempts,
                        exc,
                    )
                    await asyncio.sleep(opts.retry_backoff_seconds)
                    continue
                raise APIConnectionError(
                    f"NG TTS request failed after {attempt} attempt(s)."
                ) from exc

    def _request_headers(self, opts: _TTSOptions) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "SalesGirlVoiceAgent/1.0",
        }
        if opts.api_key:
            token = opts.api_key
            headers["Authorization"] = (
                token if token.lower().startswith("bearer ") else f"Bearer {token}"
            )
        return headers

    def _request_timeout(self, opts: _TTSOptions) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(
            total=max(30, int(opts.request_timeout_seconds)),
            sock_connect=self._conn_options.timeout,
            sock_read=None if opts.is_runpod else None,
        )

    def _should_retry_request(self, exc: APIStatusError, opts: _TTSOptions) -> bool:
        body = str(getattr(exc, "body", "") or getattr(exc, "message", "") or "").lower()
        if "no workers available" in body or "service not ready" in body:
            return True
        if opts.is_runpod and int(exc.status_code or 0) in {408, 409, 425, 429, 500, 502, 503, 504}:
            return True
        return False

    async def _stream_request(self, output_emitter: tts.AudioEmitter, opts: _TTSOptions) -> None:
        payload = {
            "text": self._input_text,
            "language": opts.language,
            "owner_id": opts.owner_id,
        }
        if opts.model_profile:
            payload["model_profile"] = opts.model_profile
        if opts.voice_id:
            payload["voice_id"] = opts.voice_id
        if opts.seed is not None:
            payload["seed"] = opts.seed
        if opts.output_sample_rate != _DEFAULT_OUTPUT_SAMPLE_RATE:
            payload["output_sample_rate"] = opts.output_sample_rate
        logger.info(
            "TTS request -> endpoint_url=%s owner_id=%s voice_id=%s model_profile=%s seed=%s language=%s mode=%s output_sample_rate=%s chars=%s",
            opts.endpoint_url,
            opts.owner_id,
            opts.voice_id,
            opts.model_profile,
            opts.seed,
            opts.language,
            opts.mode,
            opts.output_sample_rate,
            len(self._input_text or ""),
        )
        started_at = time.perf_counter()
        first_audio_at: float | None = None
        pushed_bytes = 0
        try:
            async with self._tts._ensure_session().post(
                opts.endpoint_url,
                headers=self._request_headers(opts),
                json=payload,
                timeout=self._request_timeout(opts),
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise APIStatusError(
                        message=body or f"ng tts stream failed ({resp.status})",
                        status_code=resp.status,
                        request_id=resp.headers.get("x-request-id"),
                        body=body,
                    )
                sample_rate = int(resp.headers.get("x-sample-rate") or 24000)
                channels = int(resp.headers.get("x-channels") or 1)
                request_id = str(resp.headers.get("x-request-id") or uuid.uuid4())
                audio_format = str(resp.headers.get("x-audio-format") or "").strip().lower()
                bytes_per_sample_frame = max(1, channels * _PCM16_BYTES_PER_SAMPLE)
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
                push_count = 0
                stream_incomplete = False
                try:
                    async for data in resp.content.iter_chunked(opts.http_chunk_bytes):
                        if not data:
                            continue
                        if first_audio_at is None:
                            first_audio_at = time.perf_counter()
                        chunk = bytes(data)
                        if audio_format in {"pcm_s16le", "s16le", "pcm", ""}:
                            if carried:
                                chunk = carried + chunk
                                carried = b""
                            remainder = len(chunk) % bytes_per_sample_frame
                            if remainder:
                                carried = chunk[-remainder:]
                                chunk = chunk[:-remainder]
                            if not chunk:
                                continue
                        pushed_bytes += len(chunk)
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
                except aiohttp.ClientPayloadError:
                    if pushed_bytes <= 0:
                        raise
                    stream_incomplete = True
                    logger.warning(
                        "TTS response stream ended early after %s byte(s); using received audio",
                        pushed_bytes,
                    )
                if carried:
                    logger.warning(
                        "Dropping %s unaligned trailing TTS byte(s) for request_id=%s",
                        len(carried),
                        request_id,
                    )
                if buffered:
                    output_emitter.push(bytes(buffered))
                    push_count += 1
                if hasattr(output_emitter, "end_input"):
                    output_emitter.end_input()
                else:
                    output_emitter.flush()
                logger.info(
                    "TTS response <- request_id=%s status=%s ttfb_ms=%s total_ms=%.0f bytes=%s pushes=%s sample_rate=%s channels=%s frame_size_ms=%s initial_buffer_ms=%s incomplete=%s",
                    request_id,
                    resp.status,
                    (
                        f"{(first_audio_at - started_at) * 1000:.0f}"
                        if first_audio_at is not None
                        else "none"
                    ),
                    (time.perf_counter() - started_at) * 1000,
                    pushed_bytes,
                    push_count,
                    sample_rate,
                    channels,
                    opts.frame_size_ms,
                    opts.initial_buffer_ms,
                    stream_incomplete,
                )
        except asyncio.TimeoutError:
            raise APITimeoutError() from None
        except APIStatusError:
            raise
        except aiohttp.ClientError as exc:
            raise APIConnectionError() from exc
        except Exception as exc:
            raise APIConnectionError() from exc
