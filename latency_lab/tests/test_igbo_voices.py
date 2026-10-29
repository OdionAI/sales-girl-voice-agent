import unittest
from dataclasses import replace

import aiohttp

from latency_lab.agentic import AgentRuntimeContext
from latency_lab.config import LabConfig
from latency_lab.pidgin import SBPN_MODEL
from latency_lab.speech_languages import IGBO_VOICES, apply_language_stack, igbo_voice
from latency_lab.tests.test_speech_languages import SpeechLanguageTests


class IgboVoiceRoutingTests(unittest.TestCase):
    def test_named_saved_voices_and_native_speaker_aliases(self):
        for name, speaker in IGBO_VOICES.items():
            for value in (name, name.title(), speaker.upper()):
                result = apply_language_stack(LabConfig(llm_model="unchanged"),
                    AgentRuntimeContext(tts_language_hint="Igbo", tts_voice_id=value))
                self.assertEqual(result.tts_voice, name)
                self.assertEqual(result.tts_model, "igbo-sorotts-three-voices-v1")
                self.assertEqual(result.stt_model, SBPN_MODEL)
                self.assertEqual(result.llm_model, "unchanged")

    def test_override_and_language_isolation(self):
        saved = AgentRuntimeContext(tts_language_hint="Igbo", tts_voice_id="eze")
        result = apply_language_stack(LabConfig(), saved,
            {"runtime_overrides": {"tts_voice_id": "ada"}})
        self.assertEqual(result.tts_voice, "ada")
        result = apply_language_stack(LabConfig(tts_voice="muhammad"),
            AgentRuntimeContext(tts_language_hint="Igbo"))
        self.assertEqual(result.tts_voice, "chinedu")
        self.assertEqual(apply_language_stack(LabConfig(), saved,
            {"runtime_overrides": {"tts_language_hint": "yo"}}).tts_voice, "yoruba")
        for value in ("unknown", "muhammad", "ejiro"):
            with self.assertRaisesRegex(ValueError, "Unknown Igbo"):
                igbo_voice(value)
        for value in ("", "default", "igbo", "qwen-igbo-cleaned5h-epoch20"):
            self.assertEqual(igbo_voice(value), "chinedu")


class IgboVoiceStreamTests(SpeechLanguageTests):
    async def test_saved_named_voice_reaches_all_streaming_requests(self):
        from latency_lab.speech_languages import NativeLanguageTTSClient
        for name in IGBO_VOICES:
            self.requests.clear()
            config = replace(apply_language_stack(LabConfig(),
                AgentRuntimeContext(tts_language_hint="Igbo", tts_voice_id=name)), tts_url=self.url)
            async with aiohttp.ClientSession() as session:
                client = NativeLanguageTTSClient(config, session)
                for text in ("Ndeewo. " * 30, "Daalụ."):
                    _, chunks = await client.stream(text)
                    self.assertTrue(b"".join([chunk async for chunk in chunks]))
            self.assertGreater(len(self.requests), 2)
            self.assertTrue(all(p["voice"] == name and p["model"] == config.tts_model
                                and p["language"] == "ig" for p in self.requests))

    async def test_voice_failure_never_falls_back_to_a_different_speaker(self):
        from latency_lab.speech_languages import NativeLanguageTTSClient
        self.failure_status = 429
        config = replace(apply_language_stack(LabConfig(),
            AgentRuntimeContext(tts_language_hint="Igbo", tts_voice_id="ada")), tts_url=self.url)
        async with aiohttp.ClientSession() as session:
            _, chunks = await NativeLanguageTTSClient(config, session).stream("Ndeewo. " * 30)
            with self.assertRaises(aiohttp.ClientResponseError):
                _ = [chunk async for chunk in chunks]
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]["voice"], "ada")
