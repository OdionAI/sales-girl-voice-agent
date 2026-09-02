from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import unicodedata
import wave
from typing import Any
from urllib.parse import urlparse, urlunparse

import aiohttp
from livekit import rtc
from livekit.agents import stt, vad
from livekit.agents._exceptions import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
)
from livekit.agents.types import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
    NotGivenOr,
)

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
ODION_STT_HTTP_TRANSPORT = "http"
ODION_STT_WEBSOCKET_TRANSPORT = "ws"
ODION_STT_REALTIME_SAMPLE_RATE = 16000
ODION_STT_REALTIME_CHUNK_MS = 100
ODION_STT_REALTIME_ENDPOINTING_SILENCE_SECONDS = 0.7
ODION_STT_REALTIME_MIN_SPEECH_SECONDS = 0.2
ODION_STT_REALTIME_VAD_ACTIVATION_THRESHOLD = 0.5

logger = logging.getLogger("salesgirl.odion_stt")

_REALTIME_HYPOTHESIS_PREFIX = re.compile(r"^\s*language\b", re.IGNORECASE)
_CJK_PUNCTUATION_TRANSLATION = str.maketrans(
    {
        "，": ",",
        "。": ".",
        "！": "!",
        "？": "?",
        "：": ":",
        "；": ";",
        "、": ",",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "（": "(",
        "）": ")",
    }
)


def _is_full_endpoint_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    return (
        parsed.scheme in {"http", "https", "ws", "wss"}
        and bool(parsed.netloc)
        and parsed.path not in {"", "/"}
    )


def _resolve_stt_endpoint_url(base_url: str, api_path: str) -> str:
    resolved_base_url = str(base_url or DEFAULT_ODION_STT_BASE_URL).strip().rstrip("/")
    if _is_full_endpoint_url(resolved_base_url):
        return resolved_base_url
    return f"{resolved_base_url}{api_path}"


def _normalize_transport(value: str, *, endpoint: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"ws", "wss", "websocket", "realtime"}:
        return ODION_STT_WEBSOCKET_TRANSPORT
    if normalized in {"http", "https"}:
        return ODION_STT_HTTP_TRANSPORT
    return (
        ODION_STT_WEBSOCKET_TRANSPORT
        if urlparse(endpoint).scheme in {"ws", "wss"}
        else ODION_STT_HTTP_TRANSPORT
    )


def _websocket_endpoint_url(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme in {"ws", "wss"}:
        return endpoint
    if parsed.scheme not in {"http", "https"}:
        return endpoint
    return urlunparse(
        parsed._replace(scheme="wss" if parsed.scheme == "https" else "ws")
    )


def _clean_realtime_transcript(value: Any) -> str:
    text = str(value or "")
    marker = "<asr_text>"
    marker_index = text.lower().rfind(marker)
    if marker_index >= 0:
        text = text[marker_index + len(marker) :]
    elif _REALTIME_HYPOTHESIS_PREFIX.match(text):
        return ""
    return text.replace("</asr_text>", "").strip()


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


def _normalize_realtime_transcript(
    value: Any,
    *,
    language: str,
    model: str = "",
) -> str:
    transcript = _clean_realtime_transcript(value)
    if not transcript:
        return ""
    if _display_language(language, model=model) not in {
        "English",
        "French",
        "Pidgin",
    }:
        return transcript

    normalized: list[str] = []
    for character in transcript.translate(_CJK_PUNCTUATION_TRANSLATION):
        if character.isalpha() and "LATIN" not in unicodedata.name(character, ""):
            normalized.append(" ")
        else:
            normalized.append(character)

    transcript = "".join(normalized)
    if not any(
        character.isalpha() and "LATIN" in unicodedata.name(character, "")
        for character in transcript
    ):
        return ""

    transcript = re.sub(r"\s+", " ", transcript)
    transcript = re.sub(r"\s+([,.!?;:])", r"\1", transcript)
    transcript = re.sub(r"([,!?;:])(?=\S)", r"\1 ", transcript)
    return transcript.strip().lstrip(",.!?;:").lstrip()


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
        transport: str = "",
        http_session: aiohttp.ClientSession | None = None,
        endpointing_vad: vad.VAD | None = None,
    ) -> None:
        resolved_base_url = str(base_url or DEFAULT_ODION_STT_BASE_URL).strip().rstrip("/")
        resolved_api_path = (
            str(api_path or DEFAULT_ODION_STT_PATH).strip()
            or DEFAULT_ODION_STT_PATH
        )
        resolved_endpoint = _resolve_stt_endpoint_url(resolved_base_url, resolved_api_path)
        resolved_transport = _normalize_transport(transport, endpoint=resolved_endpoint)
        if resolved_transport == ODION_STT_WEBSOCKET_TRANSPORT:
            resolved_endpoint = _websocket_endpoint_url(resolved_endpoint)
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=resolved_transport == ODION_STT_WEBSOCKET_TRANSPORT,
                interim_results=resolved_transport == ODION_STT_WEBSOCKET_TRANSPORT,
                diarization=False,
            )
        )
        self._language = str(language or "en").strip().lower() or "en"
        self._model = str(model or "").strip() or "Qwen/Qwen3-ASR-1.7B"
        self._base_url = resolved_base_url
        self._api_path = resolved_api_path
        self._endpoint = resolved_endpoint
        self._transport = resolved_transport
        self._session = http_session
        self._owns_session = http_session is None
        self._endpointing_vad = endpointing_vad

    @property
    def model(self) -> str:
        return self._model

    @property
    def endpointing_vad(self) -> vad.VAD | None:
        return self._endpointing_vad

    @property
    def provider(self) -> str:
        return "Odion"

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def transport(self) -> str:
        return self._transport

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
        if self._transport == ODION_STT_WEBSOCKET_TRANSPORT:
            return _OdionRealtimeSTTStream(
                stt=self,
                language=resolved_language,
                conn_options=conn_options,
            )
        return _OdionSTTStream(
            stt=self,
            language=resolved_language,
            conn_options=conn_options,
        )

    async def _recognize_realtime(
        self,
        buffer: Any,
        *,
        language: str,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        combined = rtc.combine_audio_frames(buffer)
        final_event: stt.SpeechEvent | None = None
        async with self.stream(
            language=language,
            conn_options=conn_options,
        ) as stream:
            stream.push_frame(combined)
            stream.end_input()
            async for event in stream:
                if event.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                    final_event = event

        if final_event is not None:
            return final_event
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[],
            recognition_usage=stt.RecognitionUsage(
                audio_duration=_audio_duration(combined)
            ),
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
        if self._transport == ODION_STT_WEBSOCKET_TRANSPORT:
            return await self._recognize_realtime(
                buffer,
                language=resolved_language,
                conn_options=conn_options,
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


class _OdionRealtimeSTTStream(stt.RecognizeStream):
    def __init__(
        self,
        *,
        stt: OdionSTT,
        language: str,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(
            stt=stt,
            conn_options=conn_options,
            sample_rate=ODION_STT_REALTIME_SAMPLE_RATE,
        )
        self._odion_stt = stt
        self._language = language
        self._audio_duration = 0.0
        self._input_ended = False
        self._speaking = False
        self._request_id = ""
        self._raw_transcript = ""
        self._last_interim = ""
        self._generation_has_audio = False
        self._generation_final_requested = False
        self._generation_ready = asyncio.Event()
        self._generation_lock = asyncio.Lock()
        self._vad_speech_active = False
        self._vad_events_drained = False
        self._endpointing_vad_stream = (
            stt._endpointing_vad.stream() if stt._endpointing_vad is not None else None
        )

    async def _run(self) -> None:
        ws: aiohttp.ClientWebSocketResponse | None = None
        tasks: set[asyncio.Task[None]] = set()
        try:
            ws = await self._connect_ws()
            tasks = {
                asyncio.create_task(self._send_audio(ws)),
                asyncio.create_task(self._receive_events(ws)),
            }
            if self._endpointing_vad_stream is not None:
                tasks.add(asyncio.create_task(self._endpoint_on_silence(ws)))
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_EXCEPTION,
            )
            for task in done:
                task.result()
            if pending:
                await asyncio.gather(*pending)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if ws is not None:
                await ws.close()
            if self._endpointing_vad_stream is not None:
                await self._endpointing_vad_stream.aclose()
            self._emit_usage()

    async def _connect_ws(self) -> aiohttp.ClientWebSocketResponse:
        try:
            ws = await asyncio.wait_for(
                self._odion_stt._ensure_session().ws_connect(
                    self._odion_stt.endpoint,
                    headers={"User-Agent": "SalesGirlVoiceAgent/1.0"},
                    heartbeat=20,
                ),
                timeout=self._conn_options.timeout,
            )
            message = await asyncio.wait_for(
                ws.receive(),
                timeout=self._conn_options.timeout,
            )
        except asyncio.TimeoutError as exc:
            raise APITimeoutError() from exc
        except aiohttp.ClientError as exc:
            raise APIConnectionError(
                "failed to connect to Odion realtime STT service"
            ) from exc

        if message.type != aiohttp.WSMsgType.TEXT:
            await ws.close()
            raise APIStatusError(
                "Odion realtime STT did not create a session",
                status_code=ws.close_code or -1,
                body=str(message.data),
            )
        try:
            payload = json.loads(message.data)
        except json.JSONDecodeError as exc:
            await ws.close()
            raise APIStatusError(
                "Odion realtime STT returned an invalid session event",
                body=message.data,
            ) from exc
        if payload.get("type") != "session.created":
            await ws.close()
            raise APIStatusError(
                "Odion realtime STT did not create a session",
                body=payload,
            )

        self._request_id = str(payload.get("id") or "").strip()
        await ws.send_json(
            {
                "type": "session.update",
                "model": self._odion_stt.model,
                "language": _display_language(
                    self._language,
                    model=self._odion_stt.model,
                ),
            }
        )
        await self._arm_generation(ws)
        logger.info(
            "Connected to Odion realtime STT: endpoint_url=%s model=%s session_id=%s",
            self._odion_stt.endpoint,
            self._odion_stt.model,
            self._request_id,
        )
        return ws

    async def _arm_generation(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._generation_has_audio = False
        self._generation_final_requested = False
        await ws.send_json(
            {
                "type": "input_audio_buffer.commit",
                "final": False,
            }
        )
        self._generation_ready.set()

    async def _finalize_generation(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        *,
        force: bool = False,
    ) -> bool:
        async with self._generation_lock:
            if self._generation_final_requested:
                return False
            if not force and not self._generation_has_audio:
                return False
            await self._generation_ready.wait()
            self._generation_ready.clear()
            self._generation_final_requested = True
            await ws.send_json(
                {
                    "type": "input_audio_buffer.commit",
                    "final": True,
                }
            )
            return True

    async def _endpoint_on_silence(
        self,
        ws: aiohttp.ClientWebSocketResponse,
    ) -> None:
        assert self._endpointing_vad_stream is not None
        chunk_size = (
            ODION_STT_REALTIME_SAMPLE_RATE
            * 2
            * ODION_STT_REALTIME_CHUNK_MS
            // 1000
        )
        pending = bytearray()

        async for event in self._endpointing_vad_stream:
            if event.type == vad.VADEventType.START_OF_SPEECH:
                self._vad_speech_active = True
                pending.clear()
                for frame in event.frames:
                    pending.extend(_audio_frame_bytes(frame))
                await self._flush_audio(ws, pending, chunk_size=chunk_size)
                continue

            if event.type == vad.VADEventType.INFERENCE_DONE:
                if not self._vad_speech_active:
                    continue
                for frame in event.frames:
                    pending.extend(_audio_frame_bytes(frame))
                await self._flush_audio(ws, pending, chunk_size=chunk_size)
                continue

            if (
                event.type != vad.VADEventType.END_OF_SPEECH
                or not self._vad_speech_active
            ):
                continue

            if pending:
                await self._send_audio_chunk(ws, bytes(pending))
                pending.clear()
            self._vad_speech_active = False
            if await self._finalize_generation(ws):
                logger.info(
                    "Odion realtime STT finalized utterance after %.1fms silence",
                    ODION_STT_REALTIME_ENDPOINTING_SILENCE_SECONDS * 1000,
                )

        self._vad_events_drained = True
        if self._vad_speech_active:
            if pending:
                await self._send_audio_chunk(ws, bytes(pending))
                pending.clear()
            self._vad_speech_active = False
            await self._finalize_generation(ws)
        elif self._input_ended and not self._generation_has_audio:
            await ws.close()

    async def _send_audio(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        chunk_size = (
            ODION_STT_REALTIME_SAMPLE_RATE
            * 2
            * ODION_STT_REALTIME_CHUNK_MS
            // 1000
        )
        pending = bytearray()

        async for item in self._input_ch:
            if isinstance(item, self._FlushSentinel):
                if self._endpointing_vad_stream is not None:
                    self._endpointing_vad_stream.flush()
                else:
                    await self._flush_audio(ws, pending, chunk_size=chunk_size)
                continue
            if int(getattr(item, "num_channels", 1) or 1) != 1:
                raise APIStatusError(
                    "Odion realtime STT requires mono audio",
                    body={"num_channels": getattr(item, "num_channels", None)},
                )
            if self._endpointing_vad_stream is not None:
                self._endpointing_vad_stream.push_frame(item)
            self._audio_duration += _audio_duration(item)
            if self._endpointing_vad_stream is not None:
                continue
            pending.extend(_audio_frame_bytes(item))
            await self._flush_audio(ws, pending, chunk_size=chunk_size)

        self._input_ended = True
        if self._endpointing_vad_stream is not None:
            self._endpointing_vad_stream.end_input()
            return
        if pending:
            await self._send_audio_chunk(ws, bytes(pending))
            pending.clear()
        await self._finalize_generation(ws, force=True)

    async def _flush_audio(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        pending: bytearray,
        *,
        chunk_size: int,
    ) -> None:
        while len(pending) >= chunk_size:
            chunk = bytes(pending[:chunk_size])
            del pending[:chunk_size]
            await self._send_audio_chunk(ws, chunk)

    async def _send_audio_chunk(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        pcm_bytes: bytes,
    ) -> None:
        if not pcm_bytes:
            return
        await self._generation_ready.wait()
        await ws.send_json(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm_bytes).decode("ascii"),
            }
        )
        self._generation_has_audio = True

    async def _receive_events(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            message = await ws.receive()
            if message.type in {
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
            }:
                if self._input_ended:
                    return
                raise APIStatusError(
                    "Odion realtime STT connection closed unexpectedly",
                    status_code=ws.close_code or -1,
                    body=str(message.data),
                )
            if message.type == aiohttp.WSMsgType.ERROR:
                raise APIConnectionError(
                    "Odion realtime STT WebSocket failed"
                ) from ws.exception()
            if message.type != aiohttp.WSMsgType.TEXT:
                logger.debug(
                    "Ignoring Odion realtime STT message type: %s",
                    message.type,
                )
                continue

            try:
                payload = json.loads(message.data)
            except json.JSONDecodeError:
                logger.debug(
                    "Ignoring invalid Odion realtime STT event: %s",
                    message.data,
                )
                continue
            event_type = str(payload.get("type") or "").strip().lower()
            if event_type == "transcription.delta":
                self._process_delta(str(payload.get("delta") or ""))
                continue
            if event_type == "transcription.done":
                self._process_done(str(payload.get("text") or ""))
                if self._input_ended and (
                    self._endpointing_vad_stream is None
                    or self._vad_events_drained
                ):
                    return
                await self._arm_generation(ws)
                continue
            if event_type == "error":
                raise APIStatusError(
                    str(payload.get("error") or "Odion realtime STT request failed"),
                    request_id=self._request_id or None,
                    body=payload,
                )

    def _process_delta(self, delta: str) -> None:
        if self._raw_transcript and _REALTIME_HYPOTHESIS_PREFIX.match(delta):
            self._raw_transcript = delta
        else:
            self._raw_transcript += delta
        transcript = _normalize_realtime_transcript(
            self._raw_transcript,
            language=self._language,
            model=self._odion_stt.model,
        )
        if not transcript or transcript == self._last_interim:
            return
        self._last_interim = transcript
        self._emit_transcript(
            transcript,
            event_type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
        )

    def _process_done(self, text: str) -> None:
        raw_transcript = _clean_realtime_transcript(text) or _clean_realtime_transcript(
            self._raw_transcript
        )
        transcript = _normalize_realtime_transcript(
            raw_transcript,
            language=self._language,
            model=self._odion_stt.model,
        )
        if transcript:
            self._emit_transcript(
                transcript,
                event_type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            )
        elif raw_transcript:
            logger.info(
                "Dropped Odion realtime STT final without Latin speech for language=%s",
                self._language,
            )
        if self._speaking:
            self._event_ch.send_nowait(
                stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH)
            )
            self._speaking = False
        self._raw_transcript = ""
        self._last_interim = ""

    def _emit_transcript(
        self,
        transcript: str,
        *,
        event_type: stt.SpeechEventType,
    ) -> None:
        if not self._speaking:
            self._event_ch.send_nowait(
                stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH)
            )
            self._speaking = True
        self._event_ch.send_nowait(
            stt.SpeechEvent(
                type=event_type,
                request_id=self._request_id,
                alternatives=[
                    stt.SpeechData(
                        language=self._language,
                        text=transcript,
                    )
                ],
            )
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
