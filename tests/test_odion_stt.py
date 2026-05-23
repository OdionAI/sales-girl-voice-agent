from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

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
        payload: dict | list | None = None,
        headers: dict | None = None,
    ) -> None:
        self.status = status
        self._payload = payload or {}
        self._body = body
        self.headers = headers or {}

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


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls: list[dict] = []

    def post(self, url, **kwargs):  # noqa: ANN001
        self.calls.append({"url": url, **kwargs})
        return self.response

    async def close(self) -> None:
        return None


class OdionSTTTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(fake_session.calls[0]["data"], b"\0" * 4000)
        self.assertEqual(fake_session.calls[0]["headers"]["Content-Type"], "audio/pcm")
        self.assertEqual(fake_session.calls[0]["headers"]["X-Sample-Rate"], "16000")
        self.assertEqual(fake_session.calls[0]["headers"]["X-Channels"], "1")
        self.assertEqual(fake_session.calls[0]["headers"]["X-Language"], "English")
        self.assertEqual(fake_session.calls[0]["headers"]["X-Model"], "Qwen/Qwen3-ASR-1.7B")
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

        self.assertEqual(fake_session.calls[0]["headers"]["X-Language"], "Pidgin")
        self.assertEqual(fake_session.calls[0]["headers"]["X-Model"], "odion-pidgin-asr")
        self.assertEqual(event.request_id, "req-pidgin")
        self.assertEqual(event.alternatives[0].text, "How far")

    def test_main_builds_odion_stt_from_runtime_overrides(self) -> None:
        userdata = {
            "runtime_overrides": {
                "stt_provider": "odion_stt",
                "stt_model": "Qwen/Qwen3-ASR-1.7B",
            }
        }

        engine = main._build_stt_engine_for_language(language="en", userdata=userdata)

        self.assertIsInstance(engine, OdionSTT)
        self.assertEqual(engine.endpoint, f"{DEFAULT_ODION_STT_BASE_URL}/stt/v1/stt/stream")

    def test_main_builds_odion_stt_with_full_endpoint_override(self) -> None:
        userdata = {
            "runtime_overrides": {
                "stt_provider": "odion_stt",
                "stt_model": "Qwen/Qwen3-ASR-1.7B",
                "stt_base_url": "http://34.122.84.20/stt/v1/stt/stream",
            }
        }

        engine = main._build_stt_engine_for_language(language="en", userdata=userdata)

        self.assertIsInstance(engine, OdionSTT)
        self.assertEqual(engine.endpoint, "http://34.122.84.20/stt/v1/stt/stream")

    def test_session_builder_uses_supplied_stt_engine(self) -> None:
        stt_engine = object()
        tts_engine = object()

        with (
            patch("main.google.LLM", return_value=object()),
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


if __name__ == "__main__":
    unittest.main()
