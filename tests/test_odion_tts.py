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
    chunks = [b"\x00" * 16]

    async def iter_chunked(self, size):  # noqa: ANN001, ARG002
        for chunk in self.chunks:
            yield chunk


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
    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self.calls: list[dict] = []
        self.chunks = chunks

    def post(self, url, **kwargs):  # noqa: ANN001
        self.calls.append({"url": url, **kwargs})
        response = _FakeTTSResponse()
        if self.chunks is not None:
            response.content = _FakeTTSContent()
            response.content.chunks = self.chunks
        return response

    async def close(self) -> None:
        return None


class _FallbackThenSuccessSession:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._call_count = 0

    def post(self, url, **kwargs):  # noqa: ANN001
        self._call_count += 1
        self.calls.append({"url": url, **kwargs})
        if self._call_count == 1:
            return _Fake404TTSResponse()
        return _FakeTTSResponse()

    async def close(self) -> None:
        return None


class _Fake404TTSResponse:
    status = 404
    headers = {}

    async def __aenter__(self) -> "_Fake404TTSResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None

    async def text(self) -> str:
        return "voice_id not found"


class _FakeAudioEmitter:
    def initialize(self, **kwargs) -> None:  # noqa: ANN001
        self.initialized = kwargs
        self.pushed: list[bytes] = []

    def push(self, data) -> None:  # noqa: ANN001
        self.data = data
        self.pushed.append(data)

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

    async def test_tts_initializes_livekit_with_small_pcm_frames(self) -> None:
        fake_session = _FakeTTSSession()
        emitter = _FakeAudioEmitter()
        engine = OdionTTS(
            owner_id="owner-123",
            voice_id=None,
            language="English",
            base_url=DEFAULT_ODION_TTS_BASE_URL,
            http_session=fake_session,
        )

        await engine.synthesize("Hello")._run(emitter)

        self.assertEqual(emitter.initialized["frame_size_ms"], 20)
        self.assertEqual(emitter.initialized["sample_rate"], 24000)
        self.assertEqual(emitter.initialized["mime_type"], "audio/pcm")

    async def test_tts_buffers_initial_audio_for_npu_endpoint(self) -> None:
        fake_session = _FakeTTSSession(
            chunks=[
                b"\x01\x02",
                b"\x03" * 4096,
                b"\x04" * 4096,
                b"\x05" * 4096,
                b"\x06" * 4096,
            ]
        )
        emitter = _FakeAudioEmitter()
        with patch.dict("os.environ", {}, clear=True):
            engine = OdionTTS(
                owner_id="owner-123",
                voice_id=None,
                language="English",
                base_url="http://102.140.102.211/api/v1/tts/stream",
                http_session=fake_session,
            )

        await engine.synthesize("Hello")._run(emitter)

        self.assertEqual(engine._opts.initial_buffer_ms, 250)
        self.assertGreaterEqual(len(emitter.pushed[0]), 12000)
        self.assertEqual(emitter.initialized["frame_size_ms"], 20)

    async def test_tts_allows_disabling_initial_buffer(self) -> None:
        fake_session = _FakeTTSSession(chunks=[b"\x01\x02", b"\x03" * 4096])
        emitter = _FakeAudioEmitter()
        with patch.dict("os.environ", {"ODION_TTS_INITIAL_BUFFER_MS": "0"}, clear=True):
            engine = OdionTTS(
                owner_id="owner-123",
                voice_id=None,
                language="English",
                base_url="http://102.140.102.211/api/v1/tts/stream",
                http_session=fake_session,
            )

        await engine.synthesize("Hello")._run(emitter)

        self.assertEqual(engine._opts.initial_buffer_ms, 0)
        self.assertEqual(emitter.pushed[0], b"\x01\x02")

    async def test_missing_clone_switches_remaining_session_to_default_voice(self) -> None:
        fake_session = _FallbackThenSuccessSession()
        engine = OdionTTS(
            owner_id="mavinomichael@gmail.com",
            voice_id="46f5ac744a504023b93c6dd8ddd46ac6",
            language="English",
            seed=0,
            mode="cloned_voice",
            base_url="http://34.122.84.20/api/v1/tts/stream",
            http_session=fake_session,
        )

        first_stream = engine.synthesize("First reply")
        await first_stream._run(_FakeAudioEmitter())
        second_stream = engine.synthesize("Second reply")
        await second_stream._run(_FakeAudioEmitter())

        self.assertEqual(fake_session.calls[0]["json"]["voice_id"], "46f5ac744a504023b93c6dd8ddd46ac6")
        self.assertEqual(fake_session.calls[0]["json"]["seed"], 0)
        self.assertNotIn("voice_id", fake_session.calls[1]["json"])
        self.assertEqual(fake_session.calls[1]["json"]["seed"], 0)
        self.assertNotIn("voice_id", fake_session.calls[2]["json"])
        self.assertEqual(fake_session.calls[2]["json"]["seed"], 0)
        self.assertIsNone(engine._opts.voice_id)
        self.assertEqual(engine._opts.mode, "default_voice")


if __name__ == "__main__":
    unittest.main()
