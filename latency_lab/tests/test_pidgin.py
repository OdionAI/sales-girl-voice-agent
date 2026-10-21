import asyncio
import io
import unittest
import wave
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web

from latency_lab.config import LabConfig, apply_runtime_overrides
from latency_lab.agentic import AgentRuntimeContext
from latency_lab.pidgin import (
    EJIRO_MODEL, SBPN_MODEL, EjiroTTSClient, SBPNClient,
    apply_pidgin_stack, is_pidgin, split_text, wav_to_call_pcm,
)
from latency_lab.pipeline import ConversationPipeline
from latency_lab.providers import pcm16_wav
from latency_lab.trace import TraceRecorder
from tempfile import TemporaryDirectory
from pathlib import Path


class PidginTests(unittest.IsolatedAsyncioTestCase):
    def test_saved_language_routes_both_providers_without_changing_llm(self):
        base = LabConfig(llm_model="unchanged", llm_api_key="test-only", tts_ref_audio="old-clone.wav")
        for language in ("pcm", "pd", "Pidgin", "Nigerian Pidgin"):
            result = apply_pidgin_stack(base, SimpleNamespace(tts_language_hint=language))
            self.assertEqual(result.tts_model, EJIRO_MODEL)
            self.assertEqual(result.stt_model, SBPN_MODEL)
            self.assertEqual(result.stt_ws_url, "")
            self.assertEqual(result.tts_ref_audio, "")
            for key in ("llm_model", "llm_base_url", "llm_api_key", "llm_enable_thinking"):
                self.assertEqual(getattr(base, key), getattr(result, key))
        for language in ("English", "French", ""):
            self.assertIs(apply_pidgin_stack(base, SimpleNamespace(tts_language_hint=language)), base)

    def test_explicit_lab_language_overrides_saved_agent_language(self):
        for selected, saved in (("pcm", "English"), ("en", "Nigerian Pidgin")):
            config = apply_runtime_overrides(LabConfig(), {"tts_language_hint": selected}, language=selected)
            result = apply_pidgin_stack(config, SimpleNamespace(tts_language_hint=saved))
            self.assertEqual(is_pidgin(result.tts_language), selected == "pcm")

    def test_text_splitting_is_bounded_and_lossless(self):
        for text in ("", "Hello", "a" * 241, " ", "Abeg check am. " * 100, "1234567890 " * 100):
            parts = split_text(text)
            self.assertEqual("".join(parts), text)
            self.assertTrue(all(0 < len(part) <= 240 for part in parts))

    def test_wav_resampling_preserves_duration_and_rejects_bad_audio(self):
        pcm = wav_to_call_pcm(pcm16_wav(b"\x01\x00" * 16000))
        self.assertEqual(len(pcm), 24000 * 2)
        for body in (b"not a WAV", pcm16_wav(b""), pcm16_wav(b"\0\0" * 10, sample_rate=24000), pcm16_wav(b"\0\0" * 100)[:-50]):
            with self.assertRaises((ValueError, wave.Error, EOFError)):
                wav_to_call_pcm(body)

    async def asyncSetUp(self):
        self.requests = []
        async def tts(request):
            body = await request.json()
            self.requests.append(body)
            self.assertLessEqual(len(body["input"]), 240)
            self.assertEqual(set(body), {"input", "model", "voice", "response_format"})
            self.assertNotIn("Authorization", request.headers)
            return web.Response(body=pcm16_wav(b"\x01\x00" * 1600), content_type="audio/wav")
        async def asr(request):
            data = await request.post()
            self.assertNotIn("language", data)
            self.assertEqual(data["model"], SBPN_MODEL)
            self.assertNotIn("Authorization", request.headers)
            audio = data["file"].file.read()
            with wave.open(io.BytesIO(audio)) as reader:
                self.assertLessEqual(reader.getnframes() / reader.getframerate(), 30)
                self.requests.append(reader.readframes(reader.getnframes()))
            return web.json_response({"text": "I wan check my balance", "language": "pd"})
        app = web.Application(client_max_size=10 * 1024 * 1024)
        app.router.add_post("/tts", tts)
        app.router.add_post("/asr", asr)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.config = replace(apply_pidgin_stack(LabConfig(tts_language="pcm")),
                              tts_url=f"http://127.0.0.1:{port}/tts", stt_batch_url=f"http://127.0.0.1:{port}/asr")

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def test_long_tts_complete_and_next_request_not_started_after_cancel(self):
        text = "Abeg give me small time make I check am for you. " * 12
        async with aiohttp.ClientSession() as session:
            client = EjiroTTSClient(self.config, session)
            rate, chunks = await client.stream(text)
            pcm = b"".join([chunk async for chunk in chunks])
            self.assertEqual(rate, 24000)
            self.assertEqual("".join(item["input"] for item in self.requests), text)
            self.assertEqual(len(pcm), len(self.requests) * 4800)
            self.requests.clear()
            _, chunks = await client.stream(text)
            await anext(chunks)
            await chunks.aclose()
            self.assertEqual(len(self.requests), 1)

    async def test_long_asr_submits_all_audio_as_one_final(self):
        final = AsyncMock()
        client = SBPNClient(self.config, AsyncMock(), final, lambda *args: None)
        pcm = b"\x01\x00" * 16000 * 61
        try:
            await client.connect()
            await client.commit(pcm)
            await asyncio.gather(*client.tasks)
            self.assertEqual(b"".join(self.requests), pcm)
            self.assertEqual(len(self.requests), 3)
            final.assert_awaited_once_with(" ".join(["I wan check my balance"] * 3))
        finally:
            await client.close()
        self.assertFalse(client.ready)

    async def test_pipeline_saved_agent_uses_pidgin_prompt_and_greetings(self):
        with TemporaryDirectory() as directory:
            pipeline = ConversationPipeline(LabConfig(), TraceRecorder(Path(directory)), AsyncMock(),
                agent_context=AgentRuntimeContext(name="Nova", tts_language_hint="Nigerian Pidgin"))
            try:
                self.assertIsInstance(pipeline.stt, SBPNClient)
                self.assertIsInstance(pipeline.tts, EjiroTTSClient)
                self.assertIn("not standard English", pipeline.system_prompt)
                self.assertIn("How I fit help", pipeline.opening_greeting_text())
                self.assertIn("well well", pipeline.closing_goodbye_text())
            finally:
                await pipeline.close()
