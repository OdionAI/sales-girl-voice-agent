import asyncio
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web

from latency_lab.agentic import AgentRuntimeContext, load_agent_runtime_context, session_routing_context
from latency_lab.config import LabConfig, apply_runtime_overrides
from latency_lab.pidgin import EjiroTTSClient, SBPNClient, SBPN_MODEL
from latency_lab.pipeline import ConversationPipeline
from latency_lab.speech_languages import CoquiTTSClient, NativeLanguageTTSClient, HAUSA_TWB_VOICES, LANGUAGES, apply_language_stack, language_code
from latency_lab.trace import TraceRecorder


class SpeechLanguageTests(unittest.IsolatedAsyncioTestCase):
    def test_saved_language_selects_both_providers_and_retains_llm(self):
        base = LabConfig(llm_model="existing-llm", tts_ref_audio="old-clone.wav")
        for code, aliases in (("yo", ("yo", "yor", "Yoruba", "Yorùbá")), ("ha", ("ha", "hau", "Hausa")),
                              ("ig", ("ig", "ibo", "Igbo", " IGBO "))):
            for alias in aliases:
                with self.subTest(alias=alias):
                    result = apply_language_stack(base, SimpleNamespace(tts_language_hint=alias))
                    self.assertEqual(result.tts_model, LANGUAGES[code]["model"])
                    self.assertEqual(result.tts_url, LANGUAGES[code]["url"])
                    self.assertEqual(result.tts_voice, LANGUAGES[code]["voice"])
                    self.assertEqual(result.stt_model, SBPN_MODEL)
                    self.assertEqual(result.tts_ref_audio, "")
                    for key in ("llm_model", "llm_base_url", "llm_api_key", "llm_enable_thinking"):
                        self.assertEqual(getattr(result, key), getattr(base, key))
        for language in ("English", "French"):
            self.assertIs(apply_language_stack(base, SimpleNamespace(tts_language_hint=language)), base)

    def test_voice_lab_language_override_wins_and_can_leave_sbpn(self):
        for selected, saved in (("yo", "Hausa"), ("ha", "Yoruba"), ("ig", "English"),
                                ("en", "Igbo"), ("pcm", "Igbo")):
            config = apply_runtime_overrides(LabConfig(), {"tts_language_hint":selected}, language=selected)
            result = apply_language_stack(config, SimpleNamespace(tts_language_hint=saved))
            self.assertEqual(language_code(result.tts_language), selected if selected != "en" else "")

    async def test_pipeline_uses_native_greetings_prompt_and_sbpn_input_gate(self):
        for code, profile in LANGUAGES.items():
            with TemporaryDirectory() as directory:
                pipeline = ConversationPipeline(LabConfig(), TraceRecorder(Path(directory)), AsyncMock(),
                    agent_context=AgentRuntimeContext(name="Nova", tts_language_hint=profile["name"]))
                try:
                    self.assertIsInstance(pipeline.tts, NativeLanguageTTSClient)
                    self.assertIsInstance(pipeline.stt, SBPNClient)
                    self.assertIn(f"CALL LANGUAGE: {profile['name']} ({code})", pipeline.system_prompt)
                    self.assertEqual(pipeline.opening_greeting_text(), profile["opening"])
                    pipeline.stt.append = AsyncMock()
                    pipeline.stt.recover = AsyncMock()
                    await pipeline.audio(b"\0\0" * 16000)
                    pipeline.stt.append.assert_not_awaited()
                    await pipeline.audio(b"\xff\x7f" * 320)
                    pipeline.stt.append.assert_awaited_once()
                    await pipeline.audio(b"\0\0" * 320)
                    pipeline.stt.recover.assert_awaited_once_with("speech_candidate_cancel")
                finally:
                    await pipeline.close()

    async def test_pidgin_keeps_its_existing_client(self):
        with TemporaryDirectory() as directory:
            pipeline = ConversationPipeline(LabConfig(), TraceRecorder(Path(directory)), AsyncMock(),
                agent_context=AgentRuntimeContext(tts_language_hint="pcm"))
            try:
                self.assertIsInstance(pipeline.tts, EjiroTTSClient)
            finally:
                await pipeline.close()

    async def asyncSetUp(self):
        self.requests = []
        self.release = asyncio.Event()
        self.delay = False
        self.failure_status = 0
        self.audio = b"\x01\x00" * 4800
        self.headers = {"Content-Type":"audio/pcm", "X-Audio-Sample-Rate":"24000", "X-Audio-Channels":"1"}
        async def speech(request):
            body = await request.json()
            self.requests.append(body)
            self.assertLessEqual(len(body["input"]), 140)
            self.assertTrue(body["stream"])
            self.assertEqual(body["response_format"], "pcm")
            if self.failure_status:
                return web.json_response({"detail": "Speaker unavailable"}, status=self.failure_status)
            response = web.StreamResponse(headers=self.headers)
            await response.prepare(request)
            if self.delay:
                await response.write(self.audio[:9599])
                await self.release.wait()
                await response.write(self.audio[9599:])
            else:
                await response.write(self.audio)
            await response.write_eof()
            return response
        app = web.Application()
        app.router.add_post("/speech", speech)
        async def runtime_config(request):
            return web.json_response(self.runtime_payload)
        app.router.add_get("/v1/agents/internal/agents/{agent_id}/runtime-config", runtime_config)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/speech"

    async def asyncTearDown(self):
        self.release.set()
        await self.runner.cleanup()

    async def test_streams_without_waiting_for_eof_and_preserves_pcm_boundaries(self):
        self.delay = True
        config = replace(apply_language_stack(LabConfig(tts_language="yo")), tts_url=self.url)
        async with aiohttp.ClientSession() as session:
            rate, chunks = await CoquiTTSClient(config, session).stream("Ẹ káàárọ̀.")
            first = await asyncio.wait_for(anext(chunks), 2)
            self.assertEqual(rate, 24000)
            self.assertFalse(self.release.is_set())
            self.release.set()
            self.assertEqual(first + b"".join([part async for part in chunks]), self.audio)

    async def test_long_text_complete_and_cancellation_stops_later_requests(self):
        for code, profile in LANGUAGES.items():
            self.requests.clear()
            config = replace(apply_language_stack(LabConfig(tts_language=code)), tts_url=self.url)
            text = (profile["opening"] + " ") * 20
            async with aiohttp.ClientSession() as session:
                client = CoquiTTSClient(config, session)
                _, chunks = await client.stream(text)
                result = b"".join([part async for part in chunks])
                self.assertEqual("".join(item["input"] for item in self.requests), text)
                self.assertEqual(result, self.audio * len(self.requests))
                self.assertTrue(all(item["model"] == profile["model"] and item["voice"] == profile["voice"] for item in self.requests))
                if code == "ig":
                    self.assertTrue(all(item["language"] == "ig" for item in self.requests))
                else:
                    self.assertTrue(all("language" not in item for item in self.requests))
                self.requests.clear()
                _, chunks = await client.stream(text)
                await anext(chunks)
                await chunks.aclose()
                self.assertEqual(len(self.requests), 1)

    async def test_rejects_wrong_format_empty_or_truncated_audio(self):
        config = replace(apply_language_stack(LabConfig(tts_language="ha")), tts_url=self.url)
        async with aiohttp.ClientSession() as session:
            for rate, audio in (("16000", self.audio), ("24000", b""), ("24000", b"\0")):
                self.headers["X-Audio-Sample-Rate"] = rate
                self.audio = audio
                _, chunks = await CoquiTTSClient(config, session).stream("Sannu.")
                with self.assertRaises(ValueError):
                    _ = [part async for part in chunks]

    async def test_saved_yoruba_voices_route_every_turn_to_balogun_without_changing_llm(self):
        for saved_voice in ("balogun", "coqui-yoruba-vits", "yoruba", "yoruba-ljspeech-vits-epoch92"):
            with self.subTest(saved_voice=saved_voice):
                base = LabConfig(llm_model="unchanged-llm")
                context = SimpleNamespace(tts_language_hint="Yoruba", tts_voice_id=saved_voice)
                config = replace(apply_language_stack(base, context), tts_url=self.url)
                self.assertEqual(config.tts_model, "coqui-yoruba-vits")
                self.assertEqual(config.tts_voice, "balogun")
                self.assertEqual(config.stt_model, SBPN_MODEL)
                self.assertEqual(config.llm_model, "unchanged-llm")
                self.requests.clear()
                async with aiohttp.ClientSession() as session:
                    client = NativeLanguageTTSClient(config, session)
                    for text in ("Ẹ káàbọ̀. " * 20, "Báwo ni mo ṣe lè ràn yín lọ́wọ́?"):
                        _, chunks = await client.stream(text)
                        self.assertTrue(b"".join([part async for part in chunks]))
                self.assertGreater(len(self.requests), 2)
                self.assertTrue(all(item["model"] == "coqui-yoruba-vits"
                                    and item["voice"] == "balogun" for item in self.requests))

    async def test_saved_hausa_speaker_reaches_every_streaming_turn(self):
        for voice, speaker_id in HAUSA_TWB_VOICES.items():
            with self.subTest(voice=voice), TemporaryDirectory() as directory:
                self.runtime_payload = {"name": "Research agent", "tts_language_hint": "Hausa",
                                        "tts_voice_id": voice, "instructions": "Speak Hausa."}
                base = LabConfig(hausa_twb_voices_enabled=True, llm_model="unchanged-llm",
                                 agent_config_api_base_url=self.url.removesuffix("/speech"),
                                 agent_config_service_token="test-service-token")
                context = await load_agent_runtime_context(room_name="", config=base,
                    trace=TraceRecorder(Path(directory)),
                    routing_context=session_routing_context({"business_id": "test-business", "config_agent_id": "test-agent"}))
                self.assertTrue(context.loaded)
                self.assertEqual(context.tts_voice_id, voice)
                config = replace(apply_language_stack(base, context), tts_url=self.url)
                self.assertEqual(config.llm_model, "unchanged-llm")
                self.assertEqual(config.stt_model, SBPN_MODEL)
                self.requests.clear()
                async with aiohttp.ClientSession() as session:
                    client = NativeLanguageTTSClient(config, session)
                    for text in ("Sannu da zuwa. " * 20, "Ta yaya zan iya taimaka muku?"):
                        _, chunks = await client.stream(text)
                        self.assertTrue(b"".join([block async for block in chunks]))
                self.assertGreater(len(self.requests), 2)
                for payload in self.requests:
                    self.assertEqual(payload["model"], "hausa")
                    self.assertEqual(payload["voice"], voice)
                    self.assertEqual(payload["speaker_id"], speaker_id)
                    self.assertEqual(payload["seed"], 17)
                    self.assertEqual(payload["speed"], 1)
                    self.assertEqual(payload["stream_format"], "audio")

    def test_hausa_opt_in_legacy_and_override_selection(self):
        for legacy in ("", "default", "hausa", "musa", "coqui-hausa-vits"):
            context = AgentRuntimeContext(tts_language_hint="Hausa", tts_voice_id=legacy)
            self.assertEqual(apply_language_stack(LabConfig(hausa_twb_voices_enabled=False), context).tts_voice, "hausa")
            self.assertEqual(apply_language_stack(LabConfig(hausa_twb_voices_enabled=True), context).tts_voice, "haisha")
        for voice, native_id in HAUSA_TWB_VOICES.items():
            context = AgentRuntimeContext(tts_language_hint="Hausa", tts_voice_id=voice)
            with self.assertRaisesRegex(ValueError, "not enabled"):
                apply_language_stack(LabConfig(hausa_twb_voices_enabled=False), context)
            identity = {"runtime_overrides": {"tts_voice_id": native_id, "tts_language_hint": "ha"}}
            selected = apply_language_stack(LabConfig(hausa_twb_voices_enabled=True), context, identity)
            self.assertEqual(selected.tts_voice, voice)
        saved = AgentRuntimeContext(tts_language_hint="Hausa", tts_voice_id="adamu")
        result = apply_language_stack(LabConfig(hausa_twb_voices_enabled=True), saved,
                                     {"runtime_overrides": {"tts_voice_id": "muhammad"}})
        self.assertEqual(result.tts_voice, "muhammad")
        with self.assertRaisesRegex(ValueError, "Unknown Hausa"):
            apply_language_stack(LabConfig(hausa_twb_voices_enabled=True), saved,
                                 {"runtime_overrides": {"tts_voice_id": "not-a-speaker"}})
        with self.assertRaisesRegex(ValueError, "Unknown Hausa"):
            apply_language_stack(LabConfig(hausa_twb_voices_enabled=True),
                                 AgentRuntimeContext(tts_language_hint="Hausa", tts_voice_id="not-a-speaker"))

    async def test_hausa_error_does_not_retry_another_speaker(self):
        self.failure_status = 400
        config = replace(apply_language_stack(LabConfig(hausa_twb_voices_enabled=True),
            AgentRuntimeContext(tts_language_hint="Hausa", tts_voice_id="adamu")), tts_url=self.url)
        async with aiohttp.ClientSession() as session:
            _, chunks = await NativeLanguageTTSClient(config, session).stream("Sannu. " * 50)
            with self.assertRaises(aiohttp.ClientResponseError):
                _ = [part async for part in chunks]
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]["voice"], "adamu")
        self.assertEqual(self.requests[0]["speaker_id"], "spk_m_1")
