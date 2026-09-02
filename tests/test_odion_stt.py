from __future__ import annotations

import asyncio
import base64
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import wave

import aiohttp
from livekit import rtc
from livekit.agents import stt, vad

import main
from agent.odion_stt import (
    DEFAULT_ODION_STT_BASE_URL,
    DEFAULT_ODION_STT_PATH,
    ODION_STT_REALTIME_ENDPOINTING_SILENCE_SECONDS,
    ODION_STT_REALTIME_MIN_SPEECH_SECONDS,
    ODION_STT_REALTIME_VAD_ACTIVATION_THRESHOLD,
    OdionSTT,
    _normalize_realtime_transcript,
    _realtime_endpointing_silence_seconds,
)


class _FakeAudioBuffer:
    sample_rate = 16000
    num_channels = 1
    duration = 1.25

    class _Data:
        def tobytes(self) -> bytes:
            return b"\0" * 4000

    data = _Data()


class _FakeFrame:
    duration = 0.125


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        body: str | None = None,
        stream_lines: list[str] | None = None,
        payload: dict | list | None = None,
        headers: dict | None = None,
    ) -> None:
        self.status = status
        self._payload = payload or {}
        self._body = body
        self._stream_lines = stream_lines or []
        self.headers = headers or {}
        self.request_data = None
        self.streamed_chunks: list[bytes] = []
        self.content = _FakeStreamContent(self)

    async def json(self, content_type=None):  # noqa: ANN001
        return self._payload

    async def text(self) -> str:
        if self._body is not None:
            return self._body
        import json

        return json.dumps(self._payload)

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None


class _FakeStreamContent:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def iter_any(self):  # noqa: ANN201
        if self._response.request_data is not None:
            async for chunk in self._response.request_data:
                self._response.streamed_chunks.append(chunk)
        for line in self._response._stream_lines:
            yield line.encode("utf-8")


class _FakeWebSocket:
    def __init__(self, stream_payloads: list[dict]) -> None:
        self._messages: asyncio.Queue[SimpleNamespace] = asyncio.Queue()
        self._messages.put_nowait(
            SimpleNamespace(
                type=aiohttp.WSMsgType.TEXT,
                data=json.dumps({"type": "session.created", "id": "sess-123"}),
            )
        )
        self._stream_payloads = stream_payloads
        self.sent_json: list[dict] = []
        self.closed = False
        self.close_code = 1000

    async def send_json(self, payload: dict) -> None:
        self.sent_json.append(payload)
        if payload == {"type": "input_audio_buffer.commit", "final": True}:
            for item in self._stream_payloads:
                self._messages.put_nowait(
                    SimpleNamespace(
                        type=aiohttp.WSMsgType.TEXT,
                        data=json.dumps(item),
                    )
                )

    async def receive(self) -> SimpleNamespace:
        return await self._messages.get()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._messages.put_nowait(
            SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=None)
        )

    def exception(self):  # noqa: ANN201
        return None


class _FakeEndpointingVADStream:
    def __init__(self) -> None:
        self._events: asyncio.Queue[SimpleNamespace | None] = asyncio.Queue()
        self._scheduled = False

    def push_frame(self, frame) -> None:  # noqa: ANN001
        if self._scheduled:
            return
        self._scheduled = True
        loop = asyncio.get_running_loop()
        loop.call_soon(
            self._events.put_nowait,
            SimpleNamespace(
                type=vad.VADEventType.START_OF_SPEECH,
                frames=[frame],
            ),
        )
        loop.call_soon(
            self._events.put_nowait,
            SimpleNamespace(
                type=vad.VADEventType.END_OF_SPEECH,
                frames=[frame],
            ),
        )

    def flush(self) -> None:
        return None

    def end_input(self) -> None:
        self._events.put_nowait(None)

    async def aclose(self) -> None:
        self._events.put_nowait(None)

    def __aiter__(self):  # noqa: ANN204
        return self

    async def __anext__(self):  # noqa: ANN204
        event = await self._events.get()
        if event is None:
            raise StopAsyncIteration
        return event


class _FakeEndpointingVAD:
    def __init__(self) -> None:
        self.vad_stream = _FakeEndpointingVADStream()

    def stream(self) -> _FakeEndpointingVADStream:
        return self.vad_stream


class _FakeIdleEndpointingVADStream(_FakeEndpointingVADStream):
    def push_frame(self, frame) -> None:  # noqa: ANN001
        self._events.put_nowait(
            SimpleNamespace(
                type=vad.VADEventType.INFERENCE_DONE,
                frames=[frame],
                speaking=False,
            )
        )


class _FakeIdleEndpointingVAD:
    def __init__(self) -> None:
        self.vad_stream = _FakeIdleEndpointingVADStream()

    def stream(self) -> _FakeIdleEndpointingVADStream:
        return self.vad_stream


class _FakeSession:
    def __init__(
        self,
        response: _FakeResponse | None = None,
        *,
        websocket: _FakeWebSocket | None = None,
    ) -> None:
        self.response = response
        self.websocket = websocket
        self.calls: list[dict] = []
        self.ws_calls: list[dict] = []

    def post(self, url, **kwargs):  # noqa: ANN001
        if self.response is None:
            raise AssertionError("No fake HTTP response configured")
        self.calls.append({"url": url, **kwargs})
        self.response.request_data = kwargs.get("data")
        return self.response

    async def ws_connect(self, url, **kwargs):  # noqa: ANN001
        if self.websocket is None:
            raise AssertionError("No fake WebSocket configured")
        self.ws_calls.append({"url": url, **kwargs})
        return self.websocket

    async def close(self) -> None:
        return None


class OdionSTTTests(unittest.IsolatedAsyncioTestCase):
    def test_realtime_endpointing_silence_uses_stable_default(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_realtime_endpointing_silence_seconds(), 0.5)

    def test_realtime_endpointing_silence_can_be_overridden(self) -> None:
        with patch.dict(
            "os.environ",
            {"ODION_STT_REALTIME_ENDPOINTING_SILENCE_SECONDS": "0.45"},
        ):
            self.assertEqual(_realtime_endpointing_silence_seconds(), 0.45)

    def test_realtime_transcript_removes_non_latin_hallucinations(self) -> None:
        self.assertEqual(
            _normalize_realtime_transcript(
                "language English<asr_text>Jane啊。",
                language="en",
                model="Qwen3-ASR",
            ),
            "Jane.",
        )
        self.assertEqual(
            _normalize_realtime_transcript(
                "嗯，no，I meant Michael。",
                language="en",
                model="Qwen3-ASR",
            ),
            "no, I meant Michael.",
        )
        self.assertEqual(
            _normalize_realtime_transcript(
                "Bonjour, ça va？",
                language="fr",
                model="Qwen3-ASR",
            ),
            "Bonjour, ça va?",
        )

    def test_realtime_transcript_drops_non_latin_only_final(self) -> None:
        self.assertEqual(
            _normalize_realtime_transcript(
                "嗯。",
                language="en",
                model="Qwen3-ASR",
            ),
            "",
        )
        self.assertEqual(
            _normalize_realtime_transcript(
                "أتميت.",
                language="en",
                model="Qwen3-ASR",
            ),
            "",
        )

    def test_odion_stt_advertises_non_streaming_capability(self) -> None:
        engine = OdionSTT(language="en")

        self.assertFalse(engine.capabilities.streaming)
        self.assertFalse(engine.capabilities.interim_results)

    def test_realtime_endpoint_advertises_streaming_capability(self) -> None:
        engine = OdionSTT(
            language="en",
            model="Qwen3-ASR",
            base_url="ws://102.88.137.124:8080/asr-rt/v1/realtime",
        )

        self.assertTrue(engine.capabilities.streaming)
        self.assertTrue(engine.capabilities.interim_results)
        self.assertEqual(engine.transport, "ws")
        self.assertEqual(
            engine.endpoint,
            "ws://102.88.137.124:8080/asr-rt/v1/realtime",
        )

    def test_endpoint_appends_default_path_for_host_only_base_url(self) -> None:
        engine = OdionSTT(
            language="en",
            base_url="https://eu-stt.odion.ai",
        )

        self.assertEqual(
            engine.endpoint,
            f"https://eu-stt.odion.ai{DEFAULT_ODION_STT_PATH}",
        )

    def test_endpoint_uses_full_endpoint_url_exactly(self) -> None:
        engine = OdionSTT(
            language="en",
            base_url="http://34.122.84.20/stt/v1/stt/stream",
        )

        self.assertEqual(engine.endpoint, "http://34.122.84.20/stt/v1/stt/stream")

    async def test_recognize_posts_audio_to_odion_endpoint(self) -> None:
        fake_session = _FakeSession(
            _FakeResponse(
                payload={
                    "request_id": "req-123",
                    "type": "final",
                    "timing": {"audio_s": 1.25},
                    "text": "Hello from Sales Girl",
                }
            )
        )
        engine = OdionSTT(
            language="en",
            base_url="https://eu-stt.odion.ai",
            http_session=fake_session,
        )

        with patch("agent.odion_stt.rtc.combine_audio_frames", return_value=_FakeAudioBuffer()):
            event = await engine.recognize(buffer=[_FakeFrame()])

        self.assertEqual(fake_session.calls[0]["url"], "https://eu-stt.odion.ai/stt/v1/stt/stream")
        self.assertEqual(fake_session.calls[0]["headers"]["Content-Type"], "application/json")
        self.assertEqual(fake_session.calls[0]["json"]["language"], "English")
        wav_bytes = base64.b64decode(fake_session.calls[0]["json"]["audio_base64"])
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            self.assertEqual(wav.getframerate(), 16000)
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
        self.assertEqual(event.request_id, "req-123")
        self.assertEqual(event.alternatives[0].text, "Hello from Sales Girl")
        self.assertAlmostEqual(event.recognition_usage.audio_duration, 1.25)

    async def test_recognize_posts_pidgin_model_and_language_hint(self) -> None:
        fake_session = _FakeSession(
            _FakeResponse(
                body=(
                    '{"type":"partial","text":"How"}\n'
                    '{"type":"final","request_id":"req-pidgin",'
                    '"timing":{"audio_s":1.0},"text":"How far"}\n'
                )
            )
        )
        engine = OdionSTT(
            language="en",
            model="odion-pidgin-asr",
            base_url="http://34.122.84.20/stt/v1/stt/stream",
            http_session=fake_session,
        )

        with patch("agent.odion_stt.rtc.combine_audio_frames", return_value=_FakeAudioBuffer()):
            event = await engine.recognize(buffer=[_FakeFrame()])

        self.assertEqual(fake_session.calls[0]["headers"]["Content-Type"], "application/json")
        self.assertEqual(fake_session.calls[0]["json"]["language"], "Pidgin")
        wav_bytes = base64.b64decode(fake_session.calls[0]["json"]["audio_base64"])
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            self.assertEqual(wav.getframerate(), 16000)
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
        self.assertEqual(event.request_id, "req-pidgin")
        self.assertEqual(event.alternatives[0].text, "How far")

    async def test_stream_posts_wav_base64_segments_to_odion_stream_endpoint(self) -> None:
        fake_response = _FakeResponse(
            stream_lines=[
                'event: partial\n',
                'data: {"request_id":"req-stream","text":"How"}\n',
                'event: final\n',
                'data: {"request_id":"req-stream","text":"How far"}\n',
            ]
        )
        fake_session = _FakeSession(fake_response)
        engine = OdionSTT(
            language="en",
            model="odion-pidgin-asr",
            base_url="http://34.122.84.20/stt/v1/stt/stream",
            http_session=fake_session,
        )
        frame = rtc.AudioFrame(
            data=b"\1\0" * 160,
            sample_rate=16000,
            num_channels=1,
            samples_per_channel=160,
        )

        stream = engine.stream()
        stream.push_frame(frame)
        stream.end_input()
        events = [event async for event in stream]

        self.assertEqual(fake_session.calls[0]["url"], "http://34.122.84.20/stt/v1/stt/stream")
        self.assertEqual(fake_response.streamed_chunks, [])
        self.assertEqual(fake_session.calls[0]["headers"]["Content-Type"], "application/json")
        self.assertEqual(fake_session.calls[0]["json"]["language"], "Pidgin")
        wav_bytes = base64.b64decode(fake_session.calls[0]["json"]["audio_base64"])
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            self.assertEqual(wav.getframerate(), 16000)
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.readframes(160), frame.data.tobytes())
        self.assertIn(stt.SpeechEventType.START_OF_SPEECH, [event.type for event in events])
        self.assertIn(stt.SpeechEventType.END_OF_SPEECH, [event.type for event in events])
        final_events = [
            event
            for event in events
            if event.type == stt.SpeechEventType.FINAL_TRANSCRIPT
        ]
        self.assertEqual(final_events[0].request_id, "req-stream")
        self.assertEqual(final_events[0].alternatives[0].text, "How far")

    async def test_realtime_stream_sends_pcm_and_maps_websocket_events(self) -> None:
        fake_websocket = _FakeWebSocket(
            [
                {"type": "transcription.delta", "delta": "language"},
                {"type": "transcription.delta", "delta": " English"},
                {"type": "transcription.delta", "delta": "<asr_text>"},
                {"type": "transcription.delta", "delta": "Hello"},
                {"type": "transcription.delta", "delta": " world"},
                {
                    "type": "transcription.done",
                    "text": "language English<asr_text>Hello world",
                },
            ]
        )
        fake_session = _FakeSession(websocket=fake_websocket)
        engine = OdionSTT(
            language="en",
            model="Qwen3-ASR",
            base_url="ws://102.88.137.124:8080/asr-rt/v1/realtime",
            transport="ws",
            http_session=fake_session,
        )
        frame = rtc.AudioFrame(
            data=b"\1\0" * 160,
            sample_rate=16000,
            num_channels=1,
            samples_per_channel=160,
        )

        stream = engine.stream()
        stream.push_frame(frame)
        stream.end_input()
        events = [event async for event in stream]

        self.assertEqual(
            fake_session.ws_calls[0]["url"],
            "ws://102.88.137.124:8080/asr-rt/v1/realtime",
        )
        self.assertEqual(
            fake_websocket.sent_json[0],
            {
                "type": "session.update",
                "model": "Qwen3-ASR",
                "language": "English",
            },
        )
        self.assertEqual(
            fake_websocket.sent_json[1],
            {"type": "input_audio_buffer.commit", "final": False},
        )
        append_event = next(
            item
            for item in fake_websocket.sent_json
            if item.get("type") == "input_audio_buffer.append"
        )
        self.assertEqual(base64.b64decode(append_event["audio"]), frame.data.tobytes())
        self.assertEqual(
            fake_websocket.sent_json[-1],
            {"type": "input_audio_buffer.commit", "final": True},
        )
        self.assertTrue(fake_websocket.closed)
        event_types = [event.type for event in events]
        self.assertIn(stt.SpeechEventType.START_OF_SPEECH, event_types)
        self.assertIn(stt.SpeechEventType.INTERIM_TRANSCRIPT, event_types)
        self.assertIn(stt.SpeechEventType.END_OF_SPEECH, event_types)
        final_events = [
            event
            for event in events
            if event.type == stt.SpeechEventType.FINAL_TRANSCRIPT
        ]
        self.assertEqual(final_events[0].request_id, "sess-123")
        self.assertEqual(final_events[0].alternatives[0].text, "Hello world")

    async def test_realtime_stream_drops_non_latin_only_final(self) -> None:
        fake_websocket = _FakeWebSocket(
            [
                {
                    "type": "transcription.done",
                    "text": "language English<asr_text>嗯。",
                }
            ]
        )
        engine = OdionSTT(
            language="en",
            model="Qwen3-ASR",
            base_url="ws://102.88.137.124:8080/asr-rt/v1/realtime",
            transport="ws",
            http_session=_FakeSession(websocket=fake_websocket),
        )
        frame = rtc.AudioFrame(
            data=b"\1\0" * 160,
            sample_rate=16000,
            num_channels=1,
            samples_per_channel=160,
        )

        stream = engine.stream()
        stream.push_frame(frame)
        stream.end_input()
        events = [event async for event in stream]
        event_types = [event.type for event in events]

        self.assertNotIn(stt.SpeechEventType.START_OF_SPEECH, event_types)
        self.assertNotIn(stt.SpeechEventType.FINAL_TRANSCRIPT, event_types)
        self.assertNotIn(stt.SpeechEventType.END_OF_SPEECH, event_types)

    async def test_realtime_stream_finalizes_on_endpointing_vad(self) -> None:
        fake_websocket = _FakeWebSocket(
            [
                {
                    "type": "transcription.done",
                    "text": "language English<asr_text>Hello world",
                }
            ]
        )
        engine = OdionSTT(
            language="en",
            model="Qwen3-ASR",
            base_url="ws://102.88.137.124:8080/asr-rt/v1/realtime",
            transport="ws",
            http_session=_FakeSession(websocket=fake_websocket),
            endpointing_vad=_FakeEndpointingVAD(),
        )
        frame = rtc.AudioFrame(
            data=b"\1\0" * 1600,
            sample_rate=16000,
            num_channels=1,
            samples_per_channel=1600,
        )
        stream = engine.stream()
        stream.push_frame(frame)

        final_event = None
        async for event in stream:
            if event.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                final_event = event
                break
        await stream.aclose()

        self.assertIsNotNone(final_event)
        self.assertEqual(final_event.alternatives[0].text, "Hello world")
        self.assertIn(
            {"type": "input_audio_buffer.commit", "final": True},
            fake_websocket.sent_json,
        )

    async def test_realtime_stream_does_not_send_idle_audio_to_asr(self) -> None:
        fake_websocket = _FakeWebSocket([])
        engine = OdionSTT(
            language="en",
            model="Qwen3-ASR",
            base_url="ws://102.88.137.124:8080/asr-rt/v1/realtime",
            transport="ws",
            http_session=_FakeSession(websocket=fake_websocket),
            endpointing_vad=_FakeIdleEndpointingVAD(),
        )
        frame = rtc.AudioFrame(
            data=b"\0\0" * 1600,
            sample_rate=16000,
            num_channels=1,
            samples_per_channel=1600,
        )

        stream = engine.stream()
        stream.push_frame(frame)
        stream.end_input()
        events = [event async for event in stream]

        self.assertFalse(
            any(
                item.get("type") == "input_audio_buffer.append"
                for item in fake_websocket.sent_json
            )
        )
        self.assertNotIn(
            {"type": "input_audio_buffer.commit", "final": True},
            fake_websocket.sent_json,
        )
        self.assertTrue(fake_websocket.closed)
        self.assertIn(
            stt.SpeechEventType.RECOGNITION_USAGE,
            [event.type for event in events],
        )

    def test_main_builds_odion_stt_stream_adapter_from_runtime_overrides(self) -> None:
        userdata = {
            "runtime_overrides": {
                "stt_provider": "odion_stt",
                "stt_model": "Qwen/Qwen3-ASR-1.7B",
            }
        }

        with (
            patch(
                "main._default_odion_stt_base_url",
                return_value=DEFAULT_ODION_STT_BASE_URL,
            ),
            patch("main._default_odion_stt_transport", return_value="http"),
            patch("main.silero.VAD.load", return_value=object()),
        ):
            engine = main._build_stt_engine_for_language(language="en", userdata=userdata)

        self.assertIsInstance(engine, stt.StreamAdapter)
        self.assertIsInstance(engine.wrapped_stt, OdionSTT)
        self.assertEqual(
            engine.wrapped_stt.endpoint,
            f"{DEFAULT_ODION_STT_BASE_URL}/stt/v1/stt/stream",
        )

    def test_main_builds_odion_stt_stream_adapter_with_full_endpoint_override(self) -> None:
        userdata = {
            "runtime_overrides": {
                "stt_provider": "odion_stt",
                "stt_model": "Qwen/Qwen3-ASR-1.7B",
                "stt_base_url": "http://34.122.84.20/stt/v1/stt/stream",
            }
        }

        with patch("main.silero.VAD.load", return_value=object()):
            engine = main._build_stt_engine_for_language(language="en", userdata=userdata)

        self.assertIsInstance(engine, stt.StreamAdapter)
        self.assertIsInstance(engine.wrapped_stt, OdionSTT)
        self.assertEqual(engine.wrapped_stt.endpoint, "http://34.122.84.20/stt/v1/stt/stream")

    def test_main_builds_native_realtime_stt_from_runtime_overrides(self) -> None:
        userdata = {
            "runtime_overrides": {
                "stt_provider": "odion_stt",
                "stt_model": "Qwen3-ASR",
                "stt_base_url": "ws://102.88.137.124:8080/asr-rt/v1/realtime",
                "stt_transport": "ws",
            }
        }

        with patch("main.silero.VAD.load") as vad_load:
            engine = main._build_stt_engine_for_language(language="en", userdata=userdata)

        self.assertIsInstance(engine, OdionSTT)
        self.assertTrue(engine.capabilities.streaming)
        self.assertEqual(engine.transport, "ws")
        self.assertEqual(
            engine.endpoint,
            "ws://102.88.137.124:8080/asr-rt/v1/realtime",
        )
        vad_load.assert_called_once_with(
            min_speech_duration=ODION_STT_REALTIME_MIN_SPEECH_SECONDS,
            min_silence_duration=ODION_STT_REALTIME_ENDPOINTING_SILENCE_SECONDS,
            activation_threshold=ODION_STT_REALTIME_VAD_ACTIVATION_THRESHOLD,
        )

    def test_main_builds_odion_stt_from_environment_defaults(self) -> None:
        userdata = {"runtime_overrides": {}}

        with patch.dict(
            "os.environ",
            {
                "VOICE_AGENT_STT_PROVIDER": "odion_stt",
                "VOICE_AGENT_STT_MODEL": "Qwen/Qwen3-ASR-1.7B",
                "ODION_STT_BASE_URL": "https://ng-stt.odion.ai/v1/stt/stream",
                "ODION_STT_TRANSPORT": "http",
            },
            clear=False,
        ):
            with patch("main.silero.VAD.load", return_value=object()):
                engine = main._build_stt_engine_for_language(language="en", userdata=userdata)

        self.assertIsInstance(engine, stt.StreamAdapter)
        self.assertIsInstance(engine.wrapped_stt, OdionSTT)
        self.assertEqual(engine.wrapped_stt.endpoint, "https://ng-stt.odion.ai/v1/stt/stream")
        self.assertEqual(engine.wrapped_stt.model, "Qwen/Qwen3-ASR-1.7B")

    def test_session_builder_uses_supplied_stt_engine(self) -> None:
        stt_engine = object()
        tts_engine = object()

        with (
            patch("main._build_llm_for_language", return_value=object()),
            patch(
                "main.AgentSession",
                side_effect=lambda **kwargs: SimpleNamespace(**kwargs),
            ),
        ):
            session = main._build_session_for_language(
                language="en",
                instructions="hello",
                userdata={},
                stt_engine=stt_engine,
                tts_engine=tts_engine,
            )

        self.assertIs(session.stt, stt_engine)
        self.assertIs(session.tts, tts_engine)

    def test_session_builder_configures_responsive_barge_in(self) -> None:
        with (
            patch.multiple(
                main,
                TURN_MIN_ENDPOINTING_DELAY=0.45,
                TURN_MAX_ENDPOINTING_DELAY=0.9,
                TURN_MIN_INTERRUPTION_DURATION=0.1,
                TURN_AEC_WARMUP_DURATION=0.1,
            ),
            patch("main._build_llm_for_language", return_value=object()),
            patch(
                "main.AgentSession",
                side_effect=lambda **kwargs: SimpleNamespace(**kwargs),
            ),
        ):
            session = main._build_session_for_language(
                language="en",
                instructions="hello",
                userdata={},
                stt_engine=object(),
                tts_engine=object(),
            )

        self.assertEqual(
            session.turn_handling["endpointing"],
            {"min_delay": 0.45, "max_delay": 0.9},
        )
        self.assertEqual(
            session.turn_handling["interruption"],
            {
                "enabled": True,
                "mode": "vad",
                "min_duration": 0.1,
                "min_words": 0,
                "resume_false_interruption": False,
                "false_interruption_timeout": None,
            },
        )
        self.assertEqual(session.aec_warmup_duration, 0.1)

    def test_session_builder_shares_odion_vad_for_immediate_barge_in(self) -> None:
        endpointing_vad = object()
        stt_engine = OdionSTT(
            base_url="ws://102.88.137.124:8080/asr-rt/v1/realtime",
            transport="ws",
            endpointing_vad=endpointing_vad,
        )

        with (
            patch("main._build_llm_for_language", return_value=object()),
            patch(
                "main.AgentSession",
                side_effect=lambda **kwargs: SimpleNamespace(**kwargs),
            ),
        ):
            session = main._build_session_for_language(
                language="en",
                instructions="hello",
                userdata={},
                stt_engine=stt_engine,
                tts_engine=object(),
            )

        self.assertIs(stt_engine.endpointing_vad, endpointing_vad)
        self.assertIs(session.vad, endpointing_vad)


if __name__ == "__main__":
    unittest.main()
