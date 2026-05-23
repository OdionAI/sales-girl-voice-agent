from __future__ import annotations

import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import main
from agent.odion_tts import (
    DEFAULT_ODION_TTS_BASE_URL,
    DEFAULT_ODION_TTS_STREAM_PATH,
    OdionTTS,
)


def _room_token(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("utf-8").rstrip("=")


class OdionTTSTests(unittest.TestCase):
    def test_endpoint_appends_stream_path_for_host_only_base_url(self) -> None:
        engine = OdionTTS(
            owner_id="owner-123",
            voice_id=None,
            base_url=DEFAULT_ODION_TTS_BASE_URL,
        )

        self.assertEqual(
            engine._opts.endpoint_url,
            f"{DEFAULT_ODION_TTS_BASE_URL}{DEFAULT_ODION_TTS_STREAM_PATH}",
        )

    def test_endpoint_uses_full_stream_endpoint_url_exactly(self) -> None:
        engine = OdionTTS(
            owner_id="owner-123",
            voice_id=None,
            base_url="http://34.122.84.20/api/v1/tts/stream",
        )

        self.assertEqual(
            engine._opts.endpoint_url,
            "http://34.122.84.20/api/v1/tts/stream",
        )

    def test_tts_builder_passes_runtime_model_and_bypasses_experiment_clone(self) -> None:
        with (
            patch("main.deepgram.TTS", return_value=object()),
            patch.object(main, "ENABLE_ODION_TTS_EN", False),
            patch.object(main, "ODION_TTS_EXPERIMENT_OWNER_ID", "experiment-owner"),
            patch.object(main, "ODION_TTS_EXPERIMENT_VOICE_ID", "english-voice"),
        ):
            engine = main._build_tts_engine_for_language(
                language="en",
                active_agent_config={},
                userdata={
                    "runtime_overrides": {
                        "tts_provider": "custom",
                        "tts_model": "odion-pidgin-tts",
                        "tts_base_url": "http://34.122.84.20/api/v1/tts/stream",
                    }
                },
                business_id="business-123",
            )

        self.assertIsInstance(engine, OdionTTS)
        self.assertEqual(engine._opts.endpoint_url, "http://34.122.84.20/api/v1/tts/stream")
        self.assertEqual(engine._opts.model, "odion-pidgin-tts")
        self.assertEqual(engine._opts.language, "Pidgin")
        self.assertEqual(engine._opts.mode, "default_voice")
        self.assertIsNone(engine._opts.voice_id)

    def test_participant_metadata_preserves_runtime_overrides(self) -> None:
        metadata = json.dumps(
            {
                "runtime_overrides": {
                    "stt_provider": "odion_stt",
                    "stt_base_url": "http://34.122.84.20/stt/v1/stt/stream",
                    "tts_base_url": "http://34.122.84.20/api/v1/tts/stream",
                    "ignored": "not copied",
                }
            }
        )
        ctx = SimpleNamespace(
            room=SimpleNamespace(
                remote_participants={
                    "user": SimpleNamespace(metadata=metadata),
                }
            )
        )

        overrides = main._extract_tts_overrides_from_ctx(ctx)

        self.assertEqual(
            overrides["runtime_overrides"],
            {
                "stt_provider": "odion_stt",
                "stt_base_url": "http://34.122.84.20/stt/v1/stt/stream",
                "tts_base_url": "http://34.122.84.20/api/v1/tts/stream",
            },
        )

    def test_tts_builder_uses_runtime_tts_base_url_without_top_level_endpoint(self) -> None:
        with (
            patch("main.deepgram.TTS", return_value=object()),
            patch.object(main, "ENABLE_ODION_TTS_EN", True),
            patch.object(main, "ODION_TTS_EXPERIMENT_OWNER_ID", ""),
            patch.object(main, "ODION_TTS_EXPERIMENT_VOICE_ID", ""),
        ):
            engine = main._build_tts_engine_for_language(
                language="en",
                active_agent_config={},
                userdata={
                    "runtime_overrides": {
                        "tts_base_url": "http://34.122.84.20/api/v1/tts/stream",
                    }
                },
                business_id="business-123",
            )

        self.assertIsInstance(engine, OdionTTS)
        self.assertEqual(
            engine._opts.endpoint_url,
            "http://34.122.84.20/api/v1/tts/stream",
        )


class _FakeVoiceLabContext:
    def __init__(self, *, room_name: str, metadata: str) -> None:
        self.job = SimpleNamespace(room=SimpleNamespace(name=room_name))
        self.room = SimpleNamespace(name=room_name, remote_participants={})
        self._metadata = metadata
        self.wait_count = 0

    async def wait_for_participant(self):  # noqa: ANN201
        self.wait_count += 1
        participant = SimpleNamespace(metadata=self._metadata)
        self.room.remote_participants["voice-lab-user"] = participant
        return participant


class VoiceLabMetadataHydrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_room_waits_for_runtime_overrides_before_billing(self) -> None:
        endpoint = "http://34.122.84.20/api/v1/tts/stream"
        runtime_overrides = {
            "stt_provider": "odion_stt",
            "stt_model": "odion-pidgin-asr",
            "stt_base_url": "http://34.122.84.20/stt/v1/stt/stream",
            "tts_provider": "custom",
            "tts_model": "odion-pidgin-tts",
            "tts_base_url": endpoint,
        }
        metadata = json.dumps(
            {
                "end_user_email": "research@odion.ai",
                "identity_type": "web",
                "tts_endpoint": endpoint,
                "runtime_overrides": runtime_overrides,
            }
        )
        room_name = (
            f"voice_assistant_room_eid{_room_token('research@odion.ai')}"
            f"_bid{_room_token('business-123')}"
            f"_aid{_room_token('agent-123')}"
            f"_nid{_room_token('Sharon')}_1234"
        )
        ctx = _FakeVoiceLabContext(room_name=room_name, metadata=metadata)

        userdata = await main._init_session_userdata(ctx, language="en")

        self.assertEqual(ctx.wait_count, 1)
        self.assertEqual(userdata["runtime_overrides"], runtime_overrides)
        self.assertEqual(userdata["tts_endpoint"], endpoint)
        self.assertEqual(
            main._billing_bypass_reason(userdata, "web"),
            "voice_lab_runtime_overrides",
        )


class _FakeTTSContent:
    async def iter_chunked(self, size):  # noqa: ANN001, ARG002
        yield b"\x00" * 16


class _FakeTTSResponse:
    status = 200
    headers = {
        "x-request-id": "tts-req",
        "x-sample-rate": "24000",
        "x-channels": "1",
    }
    content = _FakeTTSContent()

    async def __aenter__(self) -> "_FakeTTSResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None


class _FakeTTSSession:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, url, **kwargs):  # noqa: ANN001
        self.calls.append({"url": url, **kwargs})
        return _FakeTTSResponse()

    async def close(self) -> None:
        return None


class _FakeAudioEmitter:
    def initialize(self, **kwargs) -> None:  # noqa: ANN001
        self.initialized = kwargs

    def push(self, data) -> None:  # noqa: ANN001
        self.data = data

    def flush(self) -> None:
        self.flushed = True


class OdionTTSPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_tts_request_posts_runtime_model(self) -> None:
        fake_session = _FakeTTSSession()
        engine = OdionTTS(
            owner_id="owner-123",
            voice_id=None,
            language="Pidgin",
            model="odion-pidgin-tts",
            base_url="http://34.122.84.20/api/v1/tts/stream",
            http_session=fake_session,
        )

        stream = engine.synthesize("How far")
        await stream._run(_FakeAudioEmitter())

        self.assertEqual(fake_session.calls[0]["url"], "http://34.122.84.20/api/v1/tts/stream")
        self.assertEqual(fake_session.calls[0]["json"]["language"], "Pidgin")
        self.assertEqual(fake_session.calls[0]["json"]["model"], "odion-pidgin-tts")


if __name__ == "__main__":
    unittest.main()
