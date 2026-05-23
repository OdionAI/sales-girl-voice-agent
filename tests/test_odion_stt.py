from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import main
from agent.odion_stt import DEFAULT_ODION_STT_BASE_URL, DEFAULT_ODION_STT_PATH, OdionSTT


class _FakeAudioBuffer:
    def to_wav_bytes(self) -> bytes:
        return b"RIFFfakewav"


class _FakeFrame:
    duration = 0.125


class _FakeResponse:
    def __init__(self, *, status: int = 200, payload: dict | None = None, headers: dict | None = None) -> None:
        self.status = status
        self._payload = payload or {}
        self.headers = headers or {}

    async def json(self, content_type=None):  # noqa: ANN001
        return self._payload

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


def _form_text_fields(form) -> dict[str, str]:  # noqa: ANN001
    return {
        field[0]["name"]: field[2]
        for field in form._fields
        if isinstance(field[2], str)
    }


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
            base_url="http://34.122.84.20/stt/v1/stt",
        )

        self.assertEqual(engine.endpoint, "http://34.122.84.20/stt/v1/stt")

    async def test_recognize_posts_audio_to_odion_endpoint(self) -> None:
        fake_session = _FakeSession(
            _FakeResponse(
                payload={
                    "request_id": "req-123",
                    "audio_seconds": 1.25,
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

        self.assertEqual(fake_session.calls[0]["url"], "https://eu-stt.odion.ai/api/v1/stt/transcribe-file")
        fields = _form_text_fields(fake_session.calls[0]["data"])
        self.assertEqual(fields["language"], "English")
        self.assertEqual(fields["model"], "Qwen/Qwen3-ASR-1.7B")
        self.assertEqual(event.request_id, "req-123")
        self.assertEqual(event.alternatives[0].text, "Hello from Sales Girl")
        self.assertAlmostEqual(event.recognition_usage.audio_duration, 1.25)

    async def test_recognize_posts_pidgin_model_and_language_hint(self) -> None:
        fake_session = _FakeSession(
            _FakeResponse(
                payload={
                    "request_id": "req-pidgin",
                    "audio_seconds": 1.0,
                    "text": "How far",
                }
            )
        )
        engine = OdionSTT(
            language="en",
            model="odion-pidgin-asr",
            base_url="http://34.122.84.20/stt/v1/stt",
            http_session=fake_session,
        )

        with patch("agent.odion_stt.rtc.combine_audio_frames", return_value=_FakeAudioBuffer()):
            await engine.recognize(buffer=[_FakeFrame()])

        fields = _form_text_fields(fake_session.calls[0]["data"])
        self.assertEqual(fields["language"], "Pidgin")
        self.assertEqual(fields["model"], "odion-pidgin-asr")

    def test_main_builds_odion_stt_from_runtime_overrides(self) -> None:
        userdata = {
            "runtime_overrides": {
                "stt_provider": "odion_stt",
                "stt_model": "Qwen/Qwen3-ASR-1.7B",
            }
        }

        engine = main._build_stt_engine_for_language(language="en", userdata=userdata)

        self.assertIsInstance(engine, OdionSTT)
        self.assertEqual(engine.endpoint, f"{DEFAULT_ODION_STT_BASE_URL}/api/v1/stt/transcribe-file")

    def test_main_builds_odion_stt_with_full_endpoint_override(self) -> None:
        userdata = {
            "runtime_overrides": {
                "stt_provider": "odion_stt",
                "stt_model": "Qwen/Qwen3-ASR-1.7B",
                "stt_base_url": "http://34.122.84.20/stt/v1/stt",
            }
        }

        engine = main._build_stt_engine_for_language(language="en", userdata=userdata)

        self.assertIsInstance(engine, OdionSTT)
        self.assertEqual(engine.endpoint, "http://34.122.84.20/stt/v1/stt")

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
