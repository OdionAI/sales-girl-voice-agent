import unittest
from dataclasses import replace

import aiohttp

from latency_lab.agentic import AgentRuntimeContext
from latency_lab.config import LabConfig
from latency_lab.pidgin import SBPN_MODEL
from latency_lab.speech_languages import (
    ABDUL_CHECKPOINT, ABDUL_MODEL, HAUSA_TWB_VOICES,
    NativeLanguageTTSClient, apply_language_stack,
)
from latency_lab.tests.test_speech_languages import SpeechLanguageTests


class AbdulRoutingTests(unittest.TestCase):
    def test_saved_voice_and_overrides_preserve_asr_and_llm(self):
        context = AgentRuntimeContext(tts_language_hint="Hausa", tts_voice_id="abdul")
        for research_enabled in (False, True):
            base = LabConfig(llm_model="existing-llm", hausa_twb_voices_enabled=research_enabled)
            for voice in ("abdul", "Abdul", " ABDUL "):
                result = apply_language_stack(base, replace(context, tts_voice_id=voice))
                self.assertEqual(result.tts_voice, "abdul")
                self.assertEqual(result.tts_model, ABDUL_MODEL)
                self.assertEqual(result.stt_model, SBPN_MODEL)
                self.assertEqual(result.llm_model, "existing-llm")
            overridden = apply_language_stack(base, context, {"runtime_overrides": {"tts_voice_id": "abdul"}})
            self.assertEqual(overridden.tts_voice, "abdul")
        result = apply_language_stack(LabConfig(tts_language="ha", tts_voice="abdul"))
        self.assertEqual(result.tts_model, ABDUL_MODEL)

    def test_existing_speakers_and_research_gate_remain_unchanged(self):
        for voice in HAUSA_TWB_VOICES:
            context = AgentRuntimeContext(tts_language_hint="Hausa", tts_voice_id=voice)
            result = apply_language_stack(LabConfig(hausa_twb_voices_enabled=True), context)
            self.assertEqual((result.tts_model, result.tts_voice), ("hausa", voice))
            with self.assertRaisesRegex(ValueError, "not enabled"):
                apply_language_stack(LabConfig(hausa_twb_voices_enabled=False), context)
        with self.assertRaisesRegex(ValueError, "Unknown Hausa"):
            apply_language_stack(LabConfig(), AgentRuntimeContext(
                tts_language_hint="Hausa", tts_voice_id="missing-speaker"))
        result = apply_language_stack(LabConfig(), AgentRuntimeContext(
            tts_language_hint="Hausa", tts_voice_id="abdul"),
            {"runtime_overrides": {"tts_language_hint": "yo"}})
        self.assertEqual(result.tts_voice, "yoruba")


class AbdulStreamTests(SpeechLanguageTests):
    def configure_abdul(self):
        self.headers.update({"X-Model": ABDUL_MODEL, "X-Voice": "abdul",
                             "X-Speaker-ID": "abdul", "X-Checkpoint-SHA256": ABDUL_CHECKPOINT})
        return replace(apply_language_stack(LabConfig(), AgentRuntimeContext(
            tts_language_hint="Hausa", tts_voice_id="abdul")), tts_url=self.url)

    async def test_streams_before_eof_and_keeps_24k_pcm_unchanged(self):
        import asyncio
        config = self.configure_abdul()
        self.delay = True
        async with aiohttp.ClientSession() as session:
            rate, chunks = await NativeLanguageTTSClient(config, session).stream("Sannu da zuwa.")
            first = await asyncio.wait_for(anext(chunks), 2)
            self.assertEqual(rate, 24000)
            self.assertFalse(self.release.is_set())
            self.release.set()
            self.assertEqual(first + b"".join([chunk async for chunk in chunks]), self.audio)
        self.assertEqual(self.requests[0], {"model": ABDUL_MODEL, "voice": "abdul",
            "speaker_id": "abdul", "input": "Sannu da zuwa.", "language": "ha",
            "response_format": "pcm", "stream": True, "stream_format": "audio", "speed": 1, "seed": 17})

    async def test_all_parts_and_turns_use_abdul_and_cancellation_stops_more_work(self):
        config = self.configure_abdul()
        async with aiohttp.ClientSession() as session:
            client = NativeLanguageTTSClient(config, session)
            for text in ("Sannu da zuwa. " * 25, "Na gode sosai."):
                _, chunks = await client.stream(text)
                self.assertTrue(b"".join([chunk async for chunk in chunks]))
            self.assertGreater(len(self.requests), 2)
            self.assertTrue(all(row["voice"] == row["speaker_id"] == "abdul"
                                and row["model"] == ABDUL_MODEL for row in self.requests))
            self.requests.clear()
            _, chunks = await client.stream("Sannu da zuwa. " * 25)
            await anext(chunks)
            await chunks.aclose()
            self.assertEqual(len(self.requests), 1)

    async def test_wrong_speaker_checkpoint_or_native_rate_never_reaches_playback(self):
        config = self.configure_abdul()
        async with aiohttp.ClientSession() as session:
            for key, value in (("X-Voice", "muhammad"), ("X-Speaker-ID", "spk_m_2"),
                               ("X-Model", "hausa"), ("X-Checkpoint-SHA256", "other"),
                               ("X-Audio-Sample-Rate", "22050")):
                expected = self.headers[key]
                self.headers[key] = value
                _, chunks = await NativeLanguageTTSClient(config, session).stream("Sannu.")
                with self.assertRaises(ValueError):
                    await anext(chunks)
                self.headers[key] = expected

    async def test_failure_does_not_retry_or_substitute_an_existing_voice(self):
        config = self.configure_abdul()
        self.failure_status = 503
        async with aiohttp.ClientSession() as session:
            _, chunks = await NativeLanguageTTSClient(config, session).stream("Sannu.")
            with self.assertRaises(aiohttp.ClientResponseError):
                await anext(chunks)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]["voice"], "abdul")
