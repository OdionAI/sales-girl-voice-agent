from __future__ import annotations

import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import main
from agent.odion_tts import (
    DEFAULT_NG_TTS_BASE_URL,
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

    def test_ng_tts_base_url_is_default_and_preferred_over_legacy_alias(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "NG_TTS_BASE_URL": "https://ng-tts.example.test",
                "ODION_TTS_BASE_URL": "https://eu-tts.example.test",
            },
        ):
            engine = OdionTTS(owner_id="owner-123", voice_id=None)

        self.assertEqual(DEFAULT_ODION_TTS_BASE_URL, DEFAULT_NG_TTS_BASE_URL)
        self.assertEqual(
            engine._opts.endpoint_url,
            f"https://ng-tts.example.test{DEFAULT_ODION_TTS_STREAM_PATH}",
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

    def test_endpoint_rewrites_configured_host_to_direct_endpoint(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "ODION_TTS_ENDPOINT_REWRITE_HOSTS": "ng-tts.odion.ai",
                "ODION_TTS_ENDPOINT_REWRITE_URL": (
                    "http://102.140.102.211/tts/api/v1/tts/stream"
                ),
            },
            clear=False,
        ):
            engine = OdionTTS(
                owner_id="owner-123",
                voice_id=None,
                base_url="https://ng-tts.odion.ai/api/v1/tts/stream",
            )

        self.assertEqual(
            engine._opts.endpoint_url,
            "http://102.140.102.211/tts/api/v1/tts/stream",
        )

    def test_endpoint_rewrite_leaves_unlisted_host_unchanged(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "ODION_TTS_ENDPOINT_REWRITE_HOSTS": "ng-tts.odion.ai",
                "ODION_TTS_ENDPOINT_REWRITE_URL": (
                    "http://102.140.102.211/tts/api/v1/tts/stream"
                ),
            },
            clear=False,
        ):
            engine = OdionTTS(
                owner_id="owner-123",
                voice_id=None,
                base_url="https://unlisted-tts.example.test",
            )

        self.assertEqual(
            engine._opts.endpoint_url,
            f"https://unlisted-tts.example.test{DEFAULT_ODION_TTS_STREAM_PATH}",
        )

    def test_locale_language_codes_are_normalized_for_odion_tts(self) -> None:
        engine = OdionTTS(
            owner_id="owner-123",
            voice_id=None,
            language="en-NG",
            base_url="http://34.122.84.20/api/v1/tts/stream",
        )

        self.assertEqual(engine._opts.language, "English")

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
        self.assertEqual(engine._opts.model_profile, "odion-pidgin-tts")
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
                    "llm_provider": "qwen_openai",
                    "llm_model": "qwen3.8_27b",
                    "llm_base_url": "http://npu.test/v1/chat/completions",
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
                "llm_provider": "qwen_openai",
                "llm_model": "qwen3.8_27b",
                "llm_base_url": "http://npu.test/v1/chat/completions",
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

    def test_runtime_overrides_preserve_tts_api_key(self) -> None:
        metadata = json.dumps(
            {
                "runtime_overrides": {
                    "tts_base_url": "https://nockao1yom19xv.api.runpod.ai/api/v1/tts/stream",
                    "tts_api_key": "runpod-key",
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
                "tts_base_url": "https://nockao1yom19xv.api.runpod.ai/api/v1/tts/stream",
                "tts_api_key": "runpod-key",
            },
        )


class _FakeVoiceLabContext:
    def __init__(
        self,
        *,
        room_name: str,
        metadata: str,
        job_metadata: str = "",
    ) -> None:
        self.job = SimpleNamespace(
            room=SimpleNamespace(name=room_name),
            metadata=job_metadata,
        )
        self.room = SimpleNamespace(name=room_name, remote_participants={})
        self._metadata = metadata
        self.wait_count = 0

    async def wait_for_participant(self):  # noqa: ANN201
        self.wait_count += 1
        participant = SimpleNamespace(metadata=self._metadata)
        self.room.remote_participants["voice-lab-user"] = participant
        return participant


class VoiceLabMetadataHydrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_metadata_hydrates_overrides_before_participant_joins(
        self,
    ) -> None:
        runtime_overrides = {
            "stt_provider": "odion_stt",
            "stt_base_url": "ws://102.88.137.124:8080/asr-rt/v1/realtime",
            "stt_transport": "ws",
            "tts_provider": "odion_tts",
            "tts_base_url": "http://102.88.137.124:8080/tts/v1/audio/speech",
            "tts_mode": "default_voice",
            "tts_initial_codec_chunk_frames": "2",
            "tts_http_chunk_bytes": "4096",
            "tts_initial_buffer_ms": "0",
            "llm_provider": "qwen_openai",
            "llm_base_url": "http://102.88.137.124:8080/qwen38-standard/v1/chat/completions",
            "llm_disable_thinking": "true",
        }
        metadata = json.dumps(
            {
                "end_user_email": "research@odion.ai",
                "identity_type": "web",
                "runtime_overrides": runtime_overrides,
            }
        )
        room_name = (
            f"voice_assistant_room_eid{_room_token('research@odion.ai')}"
            f"_bid{_room_token('business-123')}"
            f"_aid{_room_token('agent-123')}"
            f"_nid{_room_token('Jane')}_9876"
        )
        ctx = _FakeVoiceLabContext(
            room_name=room_name,
            metadata=metadata,
            job_metadata=metadata,
        )

        userdata = await main._init_session_userdata(ctx, language="en")

        self.assertEqual(ctx.wait_count, 0)
        self.assertEqual(userdata["runtime_overrides"], runtime_overrides)
        self.assertEqual(userdata["tts_mode"], "default_voice")
        self.assertEqual(
            userdata["runtime_overrides"]["tts_initial_codec_chunk_frames"],
            "2",
        )

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

    async def test_web_room_hydrates_validated_wema_context(self) -> None:
        metadata = json.dumps(
            {
                "end_user_phone": "+2348161540638",
                "identity_type": "web",
                "wema_context": {
                    "customer_id": "R008448055",
                    "account_number": "0125679408",
                    "phone_number": "08161540638",
                    "ignored": "not copied",
                },
            }
        )
        room_name = (
            f"voice_assistant_room_eid{_room_token('+2348161540638')}"
            f"_bid{_room_token('business-123')}"
            f"_aid{_room_token('agent-123')}"
            f"_nid{_room_token('SAW')}_4321"
        )
        ctx = _FakeVoiceLabContext(room_name=room_name, metadata=metadata)

        userdata = await main._init_session_userdata(ctx, language="en")

        self.assertEqual(ctx.wait_count, 1)
        self.assertEqual(userdata["wema_customer_id"], "R008448055")
        self.assertEqual(userdata["wema_account_number"], "0125679408")
        self.assertEqual(userdata["wema_phone_number"], "08161540638")
        self.assertNotIn("ignored", userdata)

    def test_invalid_wema_context_values_are_not_hydrated(self) -> None:
        self.assertEqual(
            main._normalize_wema_context(
                {
                    "customer_id": "bad header\nvalue",
                    "account_number": "123",
                    "phone_number": "0816",
                }
            ),
            {},
        )

    async def test_tool_wait_speech_mode_reaches_userdata_without_runtime_override(self) -> None:
        for requested, expected in (
            ("llm_generated", "llm_generated"),
            ("tool_specific", "tool_specific"),
            ("invalid", "tool_specific"),
            (None, "tool_specific"),
        ):
            with self.subTest(requested=requested):
                metadata = {"end_user_email": "research@odion.ai", "identity_type": "web"}
                if requested is not None:
                    metadata["tool_wait_speech_mode"] = requested
                ctx = _FakeVoiceLabContext(
                    room_name=f"voice_assistant_room_eid{_room_token('research@odion.ai')}_1234",
                    metadata=json.dumps(metadata),
                )
                userdata = await main._init_session_userdata(ctx, language="en")
                self.assertEqual(userdata["tool_wait_speech_mode"], expected)
                self.assertEqual(userdata["runtime_overrides"], {})


class _FakeTTSContent:
    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self.chunks = chunks or [b"\x00" * 16]

    async def iter_chunked(self, size):  # noqa: ANN001, ARG002
        for chunk in self.chunks:
            yield chunk


class _IncompleteTTSContent:
    async def iter_chunked(self, size):  # noqa: ANN001, ARG002
        yield b"\x00" * 16
        raise aiohttp.ClientPayloadError("response payload is not completed")


class _FakeTTSResponse:
    status = 200

    def __init__(self, *, headers: dict[str, str] | None = None, chunks: list[bytes] | None = None) -> None:
        self.headers = headers or {
            "x-request-id": "tts-req",
            "x-sample-rate": "24000",
            "x-channels": "1",
            "x-audio-format": "pcm_s16le",
        }
        self.content = _FakeTTSContent(chunks)

    async def __aenter__(self) -> "_FakeTTSResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None


class _IncompleteTTSResponse(_FakeTTSResponse):
    def __init__(self) -> None:
        super().__init__()
        self.content = _IncompleteTTSContent()


class _FakeTTSSession:
    def __init__(
        self,
        chunks: list[bytes] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self.chunks = chunks
        self.headers = headers

    def post(self, url, **kwargs):  # noqa: ANN001
        self.calls.append({"url": url, **kwargs})
        return _FakeTTSResponse(headers=self.headers, chunks=self.chunks)

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


class _FallbackThenIncompleteSuccessSession:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._call_count = 0

    def post(self, url, **kwargs):  # noqa: ANN001
        self._call_count += 1
        self.calls.append({"url": url, **kwargs})
        if self._call_count == 1:
            return _Fake500TTSResponse()
        return _IncompleteTTSResponse()

    async def close(self) -> None:
        return None


class _ServerErrorThenSuccessSession:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._call_count = 0

    def post(self, url, **kwargs):  # noqa: ANN001
        self._call_count += 1
        self.calls.append({"url": url, **kwargs})
        if self._call_count == 1:
            return _Fake500TTSResponse()
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


class _Fake500TTSResponse:
    status = 500
    headers = {}

    async def __aenter__(self) -> "_Fake500TTSResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None

    async def text(self) -> str:
        return "Internal Server Error"


class _FakeAudioEmitter:
    def initialize(self, **kwargs) -> None:  # noqa: ANN001
        self.initialized = kwargs
        self.data_chunks = []
        self.pushed = self.data_chunks

    def push(self, data) -> None:  # noqa: ANN001
        self.data = data
        self.data_chunks.append(data)

    def flush(self) -> None:
        self.flushed = True

    def end_input(self) -> None:
        self.ended = True


class OdionTTSPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_tts_request_posts_runtime_model_profile(self) -> None:
        fake_session = _FakeTTSSession()
        engine = OdionTTS(
            owner_id="owner-123",
            voice_id=None,
            language="Pidgin",
            model="pidgin_custom",
            base_url="http://34.122.84.20/api/v1/tts/stream",
            http_session=fake_session,
        )

        stream = engine.synthesize("How far")
        emitter = _FakeAudioEmitter()
        await stream._run(emitter)

        self.assertEqual(fake_session.calls[0]["url"], "http://34.122.84.20/api/v1/tts/stream")
        self.assertEqual(fake_session.calls[0]["json"]["language"], "Pidgin")
        self.assertEqual(fake_session.calls[0]["json"]["model_profile"], "pidgin_custom")
        self.assertNotIn("model", fake_session.calls[0]["json"])
        self.assertEqual(emitter.initialized["mime_type"], "audio/pcm")
        self.assertEqual(emitter.initialized["frame_size_ms"], 200)
        self.assertEqual(emitter.initialized["sample_rate"], 24000)
        self.assertEqual(emitter.initialized["num_channels"], 1)
        self.assertEqual(emitter.data_chunks, [b"\x00" * 16])

    async def test_tts_request_includes_bearer_token_for_runpod_endpoint(self) -> None:
        fake_session = _FakeTTSSession()
        engine = OdionTTS(
            owner_id="owner-123",
            voice_id=None,
            language="English",
            base_url="https://nockao1yom19xv.api.runpod.ai/api/v1/tts/stream",
            api_key="runpod-secret",
            http_session=fake_session,
        )

        stream = engine.synthesize("Hello there")
        await stream._run(_FakeAudioEmitter())

        self.assertEqual(
            fake_session.calls[0]["headers"]["Authorization"],
            "Bearer runpod-secret",
        )

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

    async def test_clone_server_error_switches_remaining_session_to_default_voice(self) -> None:
        fake_session = _ServerErrorThenSuccessSession()
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
        self.assertNotIn("voice_id", fake_session.calls[1]["json"])
        self.assertNotIn("voice_id", fake_session.calls[2]["json"])
        self.assertIsNone(engine._opts.voice_id)
        self.assertEqual(engine._opts.mode, "default_voice")

    async def test_fallback_uses_received_audio_when_default_stream_closes_early(self) -> None:
        fake_session = _FallbackThenIncompleteSuccessSession()
        engine = OdionTTS(
            owner_id="mavinomichael@gmail.com",
            voice_id="46f5ac744a504023b93c6dd8ddd46ac6",
            language="English",
            seed=0,
            mode="cloned_voice",
            base_url="http://34.122.84.20/api/v1/tts/stream",
            http_session=fake_session,
        )

        stream = engine.synthesize("First reply")
        emitter = _FakeAudioEmitter()
        await stream._run(emitter)

        self.assertEqual(fake_session.calls[0]["json"]["voice_id"], "46f5ac744a504023b93c6dd8ddd46ac6")
        self.assertNotIn("voice_id", fake_session.calls[1]["json"])
        self.assertEqual(emitter.data_chunks, [b"\x00" * 16])
        self.assertTrue(emitter.ended)
        self.assertIsNone(engine._opts.voice_id)

    async def test_tts_initializes_livekit_with_default_pcm_frames(self) -> None:
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

        self.assertEqual(emitter.initialized["frame_size_ms"], 200)
        self.assertEqual(emitter.initialized["sample_rate"], 24000)
        self.assertEqual(emitter.initialized["mime_type"], "audio/pcm")

    async def test_tts_allows_low_frame_size_override(self) -> None:
        fake_session = _FakeTTSSession()
        emitter = _FakeAudioEmitter()
        with patch.dict("os.environ", {"ODION_TTS_FRAME_SIZE_MS": "20"}, clear=True):
            engine = OdionTTS(
                owner_id="owner-123",
                voice_id=None,
                language="English",
                base_url=DEFAULT_ODION_TTS_BASE_URL,
                http_session=fake_session,
            )

        await engine.synthesize("Hello")._run(emitter)

        self.assertEqual(emitter.initialized["frame_size_ms"], 20)

    async def test_tts_can_request_48khz_output_for_livekit(self) -> None:
        fake_session = _FakeTTSSession()
        emitter = _FakeAudioEmitter()
        with patch.dict("os.environ", {"ODION_TTS_OUTPUT_SAMPLE_RATE": "48000"}, clear=True):
            engine = OdionTTS(
                owner_id="owner-123",
                voice_id=None,
                language="English",
                base_url=DEFAULT_ODION_TTS_BASE_URL,
                http_session=fake_session,
            )

        await engine.synthesize("Hello")._run(emitter)

        self.assertEqual(engine._opts.output_sample_rate, 48000)
        self.assertEqual(fake_session.calls[0]["json"]["output_sample_rate"], 48000)

    async def test_tts_omits_default_output_sample_rate(self) -> None:
        fake_session = _FakeTTSSession()
        emitter = _FakeAudioEmitter()
        with patch.dict("os.environ", {}, clear=True):
            engine = OdionTTS(
                owner_id="owner-123",
                voice_id=None,
                language="English",
                base_url=DEFAULT_ODION_TTS_BASE_URL,
                http_session=fake_session,
            )

        await engine.synthesize("Hello")._run(emitter)

        self.assertEqual(engine._opts.output_sample_rate, 24000)
        self.assertNotIn("output_sample_rate", fake_session.calls[0]["json"])

    async def test_tts_npu_endpoint_defaults_to_cuda_pcm_contract(self) -> None:
        fake_session = _FakeTTSSession(
            chunks=[b"\x01\x02", b"\x03" * 4096],
            headers={
                "x-request-id": "tts-req",
                "x-sample-rate": "24000",
                "x-channels": "1",
            },
        )
        emitter = _FakeAudioEmitter()
        with patch.dict("os.environ", {}, clear=True):
            engine = OdionTTS(
                owner_id="owner-123",
                voice_id=None,
                language="English",
                base_url="https://ng-tts.odion.ai",
                http_session=fake_session,
            )

        await engine.synthesize("Hello")._run(emitter)

        self.assertEqual(engine._opts.output_sample_rate, 24000)
        self.assertEqual(engine._opts.frame_size_ms, 200)
        self.assertEqual(engine._opts.http_chunk_bytes, 4096)
        self.assertNotIn("output_sample_rate", fake_session.calls[0]["json"])
        self.assertEqual(emitter.initialized["sample_rate"], 24000)
        self.assertEqual(emitter.initialized["frame_size_ms"], 200)

    async def test_tts_does_not_buffer_initial_audio_by_default_for_npu_endpoint(self) -> None:
        fake_session = _FakeTTSSession(
            chunks=[
                b"\x01\x02",
                b"\x03" * 4096,
                b"\x04" * 4096,
                b"\x05" * 4096,
                b"\x06" * 4096,
            ],
            headers={
                "x-request-id": "tts-req",
                "x-sample-rate": "24000",
                "x-channels": "1",
            },
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

        self.assertEqual(engine._opts.initial_buffer_ms, 0)
        self.assertEqual(emitter.pushed[0], b"\x01\x02")
        self.assertNotIn("output_sample_rate", fake_session.calls[0]["json"])
        self.assertEqual(emitter.initialized["sample_rate"], 24000)
        self.assertEqual(emitter.initialized["frame_size_ms"], 200)

    async def test_tts_allows_initial_buffer_override(self) -> None:
        fake_session = _FakeTTSSession(
            chunks=[
                b"\x01\x02",
                b"\x03" * 4096,
                b"\x04" * 4096,
            ]
        )
        emitter = _FakeAudioEmitter()
        with patch.dict("os.environ", {"ODION_TTS_INITIAL_BUFFER_MS": "100"}, clear=True):
            engine = OdionTTS(
                owner_id="owner-123",
                voice_id=None,
                language="English",
                base_url="http://102.140.102.211/api/v1/tts/stream",
                http_session=fake_session,
            )

        await engine.synthesize("Hello")._run(emitter)

        self.assertEqual(engine._opts.initial_buffer_ms, 100)
        self.assertGreaterEqual(len(emitter.pushed[0]), 4800)

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
