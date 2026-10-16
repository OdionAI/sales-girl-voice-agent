from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from latency_lab.agentic import load_agent_runtime_context, session_routing_context
from latency_lab.config import LabConfig, apply_runtime_overrides, apply_saved_agent_voice
from latency_lab.pipeline import PhraseTokenizer
from latency_lab.providers import RTTSClient, tts_token_budget
from latency_lab.trace import TraceRecorder


class SavedVoiceTest(unittest.TestCase):
    def test_saved_voice_selects_yinka_without_mutating_defaults(self):
        base = LabConfig()
        selected = apply_saved_agent_voice(base, "odion_tts", "yinka-v7-test")
        self.assertTrue(selected.uses_yinka_v7)
        self.assertEqual(selected.tts_seed, 1)
        self.assertEqual(selected.tts_initial_codec_chunk_frames, 6)
        self.assertIn("/tts-yinka-v7/", selected.tts_url)
        self.assertEqual(base.tts_model, "Qwen3-TTS")
        self.assertIsNone(base.tts_seed)

    def test_unrelated_voices_and_providers_are_unchanged(self):
        base = LabConfig()
        for provider, voice in (("odion_tts", "existing-voice"), ("deepgram", "yinka-v7-test"), ("", "")):
            with self.subTest(provider=provider, voice=voice):
                self.assertIs(apply_saved_agent_voice(base, provider, voice), base)

    def test_explicit_session_selection_takes_precedence(self):
        base = LabConfig()
        for override in (
            {"tts_model": "Qwen3-TTS"}, {"tts_base_url": "http://test.invalid/speech"},
            {"tts_voice_id": "another-voice"}, {"tts_voice": "another-voice"},
            {"tts_provider": "deepgram"},
        ):
            with self.subTest(override=override):
                overridden = apply_runtime_overrides(base, override)
                self.assertIs(apply_saved_agent_voice(overridden, "odion_tts", "yinka-v7-test", override), overridden)

    def test_inherited_provider_and_tuning_keep_saved_voice(self):
        for provider in ("", "current", "odion_tts"):
            selected = apply_saved_agent_voice(LabConfig(), "odion_tts", "yinka-v7-test", {
                "tts_provider": provider, "tts_seed": 0,
            })
            self.assertTrue(selected.uses_yinka_v7)
            self.assertEqual(selected.tts_seed, 0)

    def test_model_switch_replaces_route_and_clears_stale_reference(self):
        base = LabConfig(tts_ref_audio="old.wav", tts_ref_text="Old voice")
        yinka = apply_runtime_overrides(base, {"tts_model": "yinka-v7-test"})
        self.assertTrue(yinka.uses_yinka_v7)
        self.assertEqual(yinka.tts_ref_audio, "")
        helen = apply_runtime_overrides(yinka, {"tts_model": "Qwen3-TTS"})
        self.assertEqual(helen.tts_voice, "helen-mavino-0030")
        self.assertEqual(helen.tts_task_type, "Base")
        self.assertIn("/tts/v1/", helen.tts_url)
        self.assertIsNone(helen.tts_max_new_tokens)
        self.assertFalse(helen.uses_yinka_v7)

    def test_explicit_route_and_tuning_override_profile(self):
        selected = apply_runtime_overrides(LabConfig(), {
            "tts_model": "yinka-v7-test", "tts_base_url": "http://test.invalid/speech",
            "tts_seed": "0", "tts_initial_codec_chunk_frames": "8",
        })
        self.assertEqual(selected.tts_url, "http://test.invalid/speech")
        self.assertEqual(selected.tts_seed, 0)
        self.assertEqual(selected.tts_initial_codec_chunk_frames, 8)

    def test_yinka_can_preserve_long_sentence_boundaries(self):
        text = " ".join(["word"] * 40)
        tokenizer = PhraseTokenizer(max_words=0)
        self.assertEqual(tokenizer.push(text), [])
        self.assertEqual(tokenizer.finish(), [text])
        self.assertEqual(len(PhraseTokenizer().push(text)), 1)


class VoiceRequestTest(unittest.IsolatedAsyncioTestCase):
    def session(self):
        async def chunks(*_args):
            yield b"\x01\x02"

        response = Mock()
        response.headers = {"x-sample-rate": "24000"}
        response.content.iter_chunked = chunks
        return SimpleNamespace(post=AsyncMock(return_value=response)), response

    async def test_every_yinka_request_streams_complete_profile(self):
        session, response = self.session()
        config = apply_saved_agent_voice(LabConfig(), "odion_tts", "yinka-v7-test")
        client = RTTSClient(config, session)
        for text in ("Hello.", "How may I help you today?"):
            rate, audio = await client.stream(text)
            self.assertEqual(rate, 24000)
            self.assertEqual(b"".join([chunk async for chunk in audio]), b"\x01\x02")
            self.assertEqual(session.post.call_args.args[0], config.tts_url)
            payload = session.post.call_args.kwargs["json"]
            self.assertEqual(payload["model"], "yinka-v7-test")
            self.assertEqual(payload["voice"], "yinka")
            self.assertEqual(payload["task_type"], "CustomVoice")
            self.assertEqual(payload["language"], "Auto")
            self.assertEqual(payload["seed"], 1)
            self.assertEqual(payload["initial_codec_chunk_frames"], 6)
            self.assertEqual(payload["max_new_tokens"], 4096)
            self.assertTrue(payload["stream"])
            self.assertFalse(payload["non_streaming_mode"])
            self.assertNotIn("x_vector_only_mode", payload)
            self.assertNotIn("ref_audio", payload)
        self.assertEqual(response.release.call_count, 2)

    async def test_existing_default_request_stays_unchanged(self):
        session, _ = self.session()
        config = LabConfig()
        _, audio = await RTTSClient(config, session).stream("Hello.")
        _ = [chunk async for chunk in audio]
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(payload["task_type"], "Base")
        self.assertEqual(payload["voice"], config.tts_voice)
        self.assertEqual(payload["max_new_tokens"], tts_token_budget("Hello."))
        self.assertNotIn("seed", payload)
        self.assertNotIn("non_streaming_mode", payload)

    async def test_scoped_runtime_loader_keeps_saved_voice(self):
        response = AsyncMock()
        response.status = 200
        response.json.return_value = {"name": "Test", "tts_provider": "odion_tts", "tts_voice_id": "yinka-v7-test"}
        response.__aenter__.return_value = response
        session = AsyncMock()
        session.__aenter__.return_value = session
        session.get = Mock(return_value=response)
        config = LabConfig(agent_config_api_base_url="http://config.invalid", agent_config_service_token="test-only")
        routing = session_routing_context({"business_id": "business-1", "config_agent_id": "agent-1"})
        with tempfile.TemporaryDirectory() as tmp, patch("latency_lab.agentic.aiohttp.ClientSession", return_value=session):
            context = await load_agent_runtime_context(
                room_name="", config=config, trace=TraceRecorder(Path(tmp)), routing_context=routing,
            )
        self.assertTrue(context.loaded)
        self.assertEqual(context.tts_provider, "odion_tts")
        self.assertEqual(context.tts_voice_id, "yinka-v7-test")
        self.assertEqual(session.get.call_args.kwargs["headers"]["X-Business-ID"], "business-1")
