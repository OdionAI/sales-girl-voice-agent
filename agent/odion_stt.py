from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import aiohttp
from livekit import rtc
from livekit.agents import stt
from livekit.agents._exceptions import (
    APIConnectionError,
    APITimeoutError,
    create_api_error_from_http,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions, NotGivenOr

DEFAULT_ODION_STT_BASE_URL = "https://eu-stt.odion.ai"
DEFAULT_ODION_STT_PATH = "/api/v1/stt/transcribe-file"


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


def _resolve_stt_endpoint_url(base_url: str, api_path: str) -> str:
    resolved_base_url = str(base_url or DEFAULT_ODION_STT_BASE_URL).strip().rstrip("/")
    if _is_full_endpoint_url(resolved_base_url):
        return resolved_base_url
    return f"{resolved_base_url}{api_path}"


def _looks_like_pidgin_model(model: str) -> bool:
    normalized = str(model or "").strip().lower()
    return any(token in normalized for token in ("pidgin", "pijin", "naija", "pcm"))


def _display_language(language: str, *, model: str = "") -> str:
    normalized = str(language or "").strip().lower()
    if normalized in {"pidgin", "pijin", "naija", "nigerian pidgin", "pcm"}:
        return "Pidgin"
    if _looks_like_pidgin_model(model):
        return "Pidgin"
    if normalized in {"fr", "french", "francais", "français"}:
        return "French"
    return "English"


class OdionSTT(stt.STT):
    def __init__(
        self,
        *,
        language: str = "en",
        model: str = "",
        base_url: str = DEFAULT_ODION_STT_BASE_URL,
        api_path: str = DEFAULT_ODION_STT_PATH,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=False,
                interim_results=False,
                diarization=False,
            )
        )
        self._language = str(language or "en").strip().lower() or "en"
        self._model = str(model or "").strip() or "Qwen/Qwen3-ASR-1.7B"
        self._base_url = str(base_url or DEFAULT_ODION_STT_BASE_URL).strip().rstrip("/")
        self._api_path = str(api_path or DEFAULT_ODION_STT_PATH).strip() or DEFAULT_ODION_STT_PATH
        self._endpoint = _resolve_stt_endpoint_url(self._base_url, self._api_path)
        self._session = http_session
        self._owns_session = http_session is None

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "Odion"

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def _ensure_session(self) -> aiohttp.ClientSession:
        if not self._session:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def _recognize_impl(
        self,
        buffer: Any,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        resolved_language = (
            str(language).strip().lower()
            if language is not NOT_GIVEN and str(language or "").strip()
            else self._language
        )
        form = aiohttp.FormData()
        form.add_field(
            "audio_file",
            rtc.combine_audio_frames(buffer).to_wav_bytes(),
            filename="audio.wav",
            content_type="audio/wav",
        )
        form.add_field("language", _display_language(resolved_language, model=self._model))
        if self._model:
            form.add_field("model", self._model)

        try:
            async with self._ensure_session().post(
                self.endpoint,
                data=form,
                timeout=aiohttp.ClientTimeout(
                    total=max(60, int(conn_options.timeout) + 30),
                    sock_connect=conn_options.timeout,
                ),
            ) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    raise create_api_error_from_http(
                        message=str(payload.get("detail") or "Odion STT request failed."),
                        status=response.status,
                        request_id=str(response.headers.get("x-request-id") or ""),
                        body=payload,
                    )
                transcript = str(payload.get("text") or "").strip()
                request_id = str(
                    payload.get("request_id") or response.headers.get("x-request-id") or ""
                ).strip()
                return stt.SpeechEvent(
                    type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                    request_id=request_id,
                    alternatives=[
                        stt.SpeechData(
                            language=resolved_language,
                            text=transcript,
                        )
                    ]
                    if transcript
                    else [],
                    recognition_usage=stt.RecognitionUsage(
                        audio_duration=float(payload.get("audio_seconds") or 0.0)
                    ),
                )
        except TimeoutError as exc:
            raise APITimeoutError() from exc
        except aiohttp.ClientError as exc:
            raise APIConnectionError("failed to reach Odion STT service") from exc

    async def aclose(self) -> None:
        if self._session and self._owns_session:
            await self._session.close()
