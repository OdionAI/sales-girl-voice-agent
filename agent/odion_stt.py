from __future__ import annotations

import base64
import io
import json
import logging
import wave
from typing import Any
from urllib.parse import urlparse

import aiohttp
from livekit import rtc
from livekit.agents import stt
from livekit.agents._exceptions import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions, NotGivenOr

try:
    from livekit.agents._exceptions import create_api_error_from_http
except ImportError:
    def create_api_error_from_http(
        *,
        message: str,
        status: int,
        request_id: str,
        body: Any,
    ) -> APIStatusError:
        return APIStatusError(
            message=message,
            status_code=status,
            request_id=request_id,
            body=body,
        )

DEFAULT_ODION_STT_BASE_URL = "https://eu-stt.odion.ai"
DEFAULT_ODION_STT_STREAM_PATH = "/stt/v1/stt/stream"
DEFAULT_ODION_STT_PATH = DEFAULT_ODION_STT_STREAM_PATH

logger = logging.getLogger("salesgirl.odion_stt")


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


def _extract_transcript_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        for item in reversed(payload):
            event_type = str(item.get("type") or "").lower() if isinstance(item, dict) else ""
            if event_type in {"final", "final_transcript"}:
                return item
        for item in reversed(payload):
            if isinstance(item, dict):
                return item
    return {}


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


def _audio_frame_bytes(frame: Any) -> bytes:
    data = getattr(frame, "data", b"")
    if hasattr(data, "tobytes"):
        return data.tobytes()
    return bytes(data)


def _audio_duration(frame: Any) -> float:
    try:
        return float(getattr(frame, "duration", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _wav_bytes_from_audio_frame(frame: Any) -> bytes:
    sample_rate = int(getattr(frame, "sample_rate", 16000) or 16000)
    num_channels = int(getattr(frame, "num_channels", 1) or 1)
    pcm_bytes = _audio_frame_bytes(frame)
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(num_channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm_bytes)
    return out.getvalue()


def _stream_payloads_from_text(content: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    current_event = ""
    for raw_line in str(content or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("event:"):
            current_event = line[6:].strip()
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            if current_event and not payload.get("type"):
                payload["type"] = current_event
            payloads.append(payload)
            current_event = ""
    return payloads


def _stream_event_type(payload: dict[str, Any]) -> stt.SpeechEventType | None:
    event_type = str(
        payload.get("type")
        or payload.get("event")
        or payload.get("message_type")
        or ""
    ).strip().lower()
    is_final = bool(
        payload.get("is_final")
        or payload.get("final")
        or payload.get("speech_final")
    )
    if event_type in {"partial", "interim", "interim_transcript"} and not is_final:
        return stt.SpeechEventType.INTERIM_TRANSCRIPT
    if event_type in {"final", "final_transcript"} or is_final:
        return stt.SpeechEventType.FINAL_TRANSCRIPT
    if event_type in {"speech_start", "start_of_speech"}:
        return stt.SpeechEventType.START_OF_SPEECH
    if event_type in {"speech_end", "end_of_speech"}:
        return stt.SpeechEventType.END_OF_SPEECH
    if str(payload.get("text") or "").strip():
        return stt.SpeechEventType.INTERIM_TRANSCRIPT
    return None


def _speech_event_from_payload(
    payload: dict[str, Any],
    *,
    language: str,
) -> stt.SpeechEvent | None:
    event_type = _stream_event_type(payload)
    if event_type is None:
        return None

    request_id = str(
        payload.get("request_id")
        or payload.get("id")
        or ""
    ).strip()

    if event_type in {
        stt.SpeechEventType.START_OF_SPEECH,
        stt.SpeechEventType.END_OF_SPEECH,
    }:
        return stt.SpeechEvent(type=event_type, request_id=request_id)

    transcript = str(payload.get("text") or payload.get("transcript") or "").strip()
    if not transcript:
        return None

    return stt.SpeechEvent(
        type=event_type,
        request_id=request_id,
        alternatives=[
            stt.SpeechData(
                language=language,
                text=transcript,
            )
        ],
    )


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

    def _headers(self, *, language: str, sample_rate: int, num_channels: int) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/x-ndjson, application/json",
            "User-Agent": "SalesGirlVoiceAgent/1.0",
        }
        return headers

    def _json_payload(self, *, wav_bytes: bytes, language: str) -> dict[str, Any]:
        return {
            "audio_base64": base64.b64encode(wav_bytes).decode("ascii"),
            "language": _display_language(language, model=self._model),
            "context": "",
        }

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.RecognizeStream:
        resolved_language = (
            str(language).strip().lower()
            if language is not NOT_GIVEN and str(language or "").strip()
            else self._language
        )
        return _OdionSTTStream(
            stt=self,
            language=resolved_language,
            conn_options=conn_options,
        )

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
        combined = rtc.combine_audio_frames(buffer)
        sample_rate = int(combined.sample_rate)
        num_channels = int(combined.num_channels)
        headers = self._headers(
            language=resolved_language,
            sample_rate=sample_rate,
            num_channels=num_channels,
        )
        payload = self._json_payload(
            wav_bytes=_wav_bytes_from_audio_frame(combined),
            language=resolved_language,
        )

        try:
            async with self._ensure_session().post(
                self.endpoint,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(
                    total=max(60, int(conn_options.timeout) + 30),
                    sock_connect=conn_options.timeout,
                ),
            ) as response:
                content = await response.text()
                payload: Any
                try:
                    payload = json.loads(content)
                except json.JSONDecodeError:
                    payload = _stream_payloads_from_text(content)
                transcript_payload = _extract_transcript_payload(payload)
                if response.status >= 400:
                    raise create_api_error_from_http(
                        message=str(
                            transcript_payload.get("detail")
                            or "Odion STT request failed."
                        ),
                        status=response.status,
                        request_id=str(response.headers.get("x-request-id") or ""),
                        body=payload,
                    )
                transcript = str(transcript_payload.get("text") or "").strip()
                request_id = str(
                    transcript_payload.get("request_id")
                    or response.headers.get("x-request-id")
                    or ""
                ).strip()
                audio_duration = (
                    transcript_payload.get("audio_seconds")
                    or transcript_payload.get("audio_s")
                    or (transcript_payload.get("timing") or {}).get("audio_s")
                    or 0.0
                )
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
                        audio_duration=float(audio_duration)
                    ),
                )
        except TimeoutError as exc:
            raise APITimeoutError() from exc
        except aiohttp.ClientError as exc:
            raise APIConnectionError("failed to reach Odion STT service") from exc

    async def aclose(self) -> None:
        if self._session and self._owns_session:
            await self._session.close()


class _OdionSTTStream(stt.RecognizeStream):
    def __init__(
        self,
        *,
        stt: OdionSTT,
        language: str,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(stt=stt, conn_options=conn_options)
        self._odion_stt = stt
        self._language = language
        self._audio_duration = 0.0
        self._speaking = False
        self._sse_event_type = ""

    async def _next_audio_frame(self) -> Any | None:
        async for item in self._input_ch:
            if isinstance(item, self._FlushSentinel):
                continue
            return item
        return None

    async def _run(self) -> None:
        try:
            frames: list[Any] = []
            async for item in self._input_ch:
                if isinstance(item, self._FlushSentinel):
                    await self._transcribe_segment(frames)
                    frames = []
                    continue
                self._audio_duration += _audio_duration(item)
                frames.append(item)
            await self._transcribe_segment(frames)
        except TimeoutError as exc:
            raise APITimeoutError() from exc
        except aiohttp.ClientError as exc:
            raise APIConnectionError("failed to reach Odion STT service") from exc
        finally:
            self._emit_usage()

    async def _transcribe_segment(self, frames: list[Any]) -> None:
        if not frames:
            logger.info("STT segment skipped: no audio frames")
            return
        combined = rtc.combine_audio_frames(frames)
        sample_rate = int(getattr(combined, "sample_rate", 16000) or 16000)
        num_channels = int(getattr(combined, "num_channels", 1) or 1)
        headers = self._odion_stt._headers(
            language=self._language,
            sample_rate=sample_rate,
            num_channels=num_channels,
        )
        payload = self._odion_stt._json_payload(
            wav_bytes=_wav_bytes_from_audio_frame(combined),
            language=self._language,
        )

        logger.info(
            "STT segment request -> endpoint_url=%s model=%s language=%s audio_seconds=%.3f",
            self._odion_stt.endpoint,
            self._odion_stt.model,
            _display_language(self._language, model=self._odion_stt.model),
            sum(_audio_duration(frame) for frame in frames),
        )
        async with self._odion_stt._ensure_session().post(
            self._odion_stt.endpoint,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(
                total=max(60, int(self._conn_options.timeout) + 30),
                sock_connect=self._conn_options.timeout,
                sock_read=None,
            ),
        ) as response:
            if response.status >= 400:
                await self._raise_stream_response_error(response)
            await self._read_stream_response(response)

    def _track_audio_frame(self, frame: Any) -> bytes:
        self._audio_duration += _audio_duration(frame)
        return _audio_frame_bytes(frame)

    async def _raise_stream_response_error(self, response: aiohttp.ClientResponse) -> None:
        body = await response.text()
        try:
            payload: Any = json.loads(body)
        except json.JSONDecodeError:
            payload = body
        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("detail") or payload.get("message") or "")
        raise create_api_error_from_http(
            message=detail or "Odion STT stream request failed.",
            status=response.status,
            request_id=str(response.headers.get("x-request-id") or ""),
            body=payload,
        )

    async def _read_stream_response(self, response: aiohttp.ClientResponse) -> None:
        pending = ""
        async for chunk in response.content.iter_any():
            if not chunk:
                continue
            pending += chunk.decode("utf-8", errors="ignore")
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                self._process_stream_line(line)
        if pending.strip():
            self._process_stream_line(pending)

    def _process_stream_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        if line.startswith("event:"):
            self._sse_event_type = line[6:].strip()
            return
        if line.startswith("data:"):
            line = line[5:].strip()
        if line == "[DONE]":
            return
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("Ignoring non-JSON Odion STT stream line: %s", line)
            return
        if not isinstance(payload, dict):
            return
        if self._sse_event_type and not payload.get("type"):
            payload["type"] = self._sse_event_type
        self._sse_event_type = ""
        event = _speech_event_from_payload(payload, language=self._language)
        if event is None:
            return
        if event.type == stt.SpeechEventType.START_OF_SPEECH:
            self._speaking = True
        if (
            event.type
            in {
                stt.SpeechEventType.INTERIM_TRANSCRIPT,
                stt.SpeechEventType.FINAL_TRANSCRIPT,
            }
            and event.alternatives
            and event.alternatives[0].text
            and not self._speaking
        ):
            self._speaking = True
            self._event_ch.send_nowait(
                stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH)
            )
        self._event_ch.send_nowait(event)
        if event.type == stt.SpeechEventType.END_OF_SPEECH:
            self._speaking = False
        elif event.type == stt.SpeechEventType.FINAL_TRANSCRIPT and self._speaking:
            self._speaking = False
            self._event_ch.send_nowait(
                stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH)
            )

    def _emit_usage(self) -> None:
        if self._audio_duration <= 0:
            return
        self._event_ch.send_nowait(
            stt.SpeechEvent(
                type=stt.SpeechEventType.RECOGNITION_USAGE,
                recognition_usage=stt.RecognitionUsage(
                    audio_duration=self._audio_duration
                ),
            )
        )
