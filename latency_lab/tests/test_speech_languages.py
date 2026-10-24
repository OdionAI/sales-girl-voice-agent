import asyncio
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web

from latency_lab.agentic import AgentRuntimeContext
from latency_lab.config import LabConfig, apply_runtime_overrides
from latency_lab.pidgin import EjiroTTSClient, SBPNClient, SBPN_MODEL
from latency_lab.pipeline import ConversationPipeline
from latency_lab.speech_languages import CoquiTTSClient, NativeLanguageTTSClient, LANGUAGES, apply_language_stack, language_code
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
                    self.assertEqual(pipeline.closing_goodbye_text(), profile["goodbye"])
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
        self.audio = b"\x01\x00" * 4800
        self.headers = {"Content-Type":"audio/pcm", "X-Audio-Sample-Rate":"24000", "X-Audio-Channels":"1"}
        async def speech(request):
            body = await request.json()
            self.requests.append(body)
            self.assertLessEqual(len(body["input"]), 140)
            self.assertTrue(body["stream"])
            self.assertEqual(body["response_format"], "pcm")
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
