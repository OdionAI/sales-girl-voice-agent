from __future__ import annotations

import base64
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import wave

from livekit import rtc
from livekit.agents import stt

import main
from agent.odion_stt import DEFAULT_ODION_STT_BASE_URL, DEFAULT_ODION_STT_PATH, OdionSTT


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


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls: list[dict] = []

    def post(self, url, **kwargs):  # noqa: ANN001
        self.calls.append({"url": url, **kwargs})
        self.response.request_data = kwargs.get("data")
        return self.response

    async def close(self) -> None:
        return None


class OdionSTTTests(unittest.IsolatedAsyncioTestCase):
    def test_odion_stt_advertises_non_streaming_capability(self) -> None:
        engine = OdionSTT(language="en")

        self.assertFalse(engine.capabilities.streaming)
        self.assertFalse(engine.capabilities.interim_results)

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

    def test_main_builds_odion_stt_stream_adapter_from_runtime_overrides(self) -> None:
        userdata = {
            "runtime_overrides": {
                "stt_provider": "odion_stt",
                "stt_model": "Qwen/Qwen3-ASR-1.7B",
            }
        }

        with patch("main.silero.VAD.load", return_value=object()):
            engine = main._build_stt_engine_for_language(language="en", userdata=userdata)

        self.assertIsInstance(engine, stt.StreamAdapter)
        self.assertIsInstance(engine.wrapped_stt, OdionSTT)
        self.assertEqual(engine.wrapped_stt.endpoint, f"{DEFAULT_ODION_STT_BASE_URL}/stt/v1/stt/stream")

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

    def test_main_builds_odion_stt_from_environment_defaults(self) -> None:
        userdata = {"runtime_overrides": {}}

        with patch.dict(
            "os.environ",
            {
                "VOICE_AGENT_STT_PROVIDER": "odion_stt",
                "VOICE_AGENT_STT_MODEL": "Qwen/Qwen3-ASR-1.7B",
                "ODION_STT_BASE_URL": "https://ng-stt.odion.ai/v1/stt/stream",
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
            patch("main.openai.LLM", return_value=object()),
            patch.object(main, "MAAS_API_KEY", "test-maas-key"),
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
        llm_mock.assert_called_once()
        self.assertEqual(llm_mock.call_args.kwargs["model"], main.MAAS_LLM_MODEL_EN)
        self.assertEqual(llm_mock.call_args.kwargs["base_url"], main.MAAS_BASE_URL)
        self.assertEqual(llm_mock.call_args.kwargs["api_key"], "test-maas-key")
        self.assertEqual(
            llm_mock.call_args.kwargs["extra_body"],
            {"chat_template_kwargs": {"thinking": False}},
        )


if __name__ == "__main__":
    unittest.main()
