import asyncio
import io
import unittest
import wave
from array import array
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web

from latency_lab.config import LabConfig, apply_runtime_overrides
from latency_lab.agentic import AgentRuntimeContext
from latency_lab.pidgin import (
    EJIRO_MODEL, SBPN_MODEL, SBPN_WS_URL, EjiroTTSClient, SBPNClient,
    apply_pidgin_stack, ejiro_spoken_text, is_pidgin, split_text, wav_to_call_pcm,
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
            self.assertEqual(result.stt_ws_url, SBPN_WS_URL)
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

    def test_mms_numbers_preserve_every_digit_and_decimal(self):
        self.assertEqual(ejiro_spoken_text("006789"), " zero zero six seven eight nine ")
        self.assertIn("point", ejiro_spoken_text("12.50"))
        self.assertIn("naira", ejiro_spoken_text("\u20a612.50"))
        self.assertEqual(ejiro_spoken_text("How you dey?"), "How you dey?")

    async def test_mms_numeric_expansion_is_split_before_sending(self):
        text = "Account 0067891234. Amount 65.20 naira. " * 15
        async with aiohttp.ClientSession() as session:
            _, chunks = await EjiroTTSClient(self.config, session).stream(text)
            audio = b"".join([chunk async for chunk in chunks])
        sent = "".join(item["input"] for item in self.requests)
        self.assertEqual(sent, ejiro_spoken_text(text))
        self.assertFalse(any(c.isdecimal() for c in sent))
        self.assertGreater(len(audio), 0)

    def test_wav_resampling_preserves_duration_and_rejects_bad_audio(self):
        pcm = wav_to_call_pcm(pcm16_wav(b"\x01\x00" * 16000))
        self.assertEqual(len(pcm), 24000 * 2)
        for body in (b"not a WAV", pcm16_wav(b""), pcm16_wav(b"\0\0" * 10, sample_rate=24000), pcm16_wav(b"\0\0" * 100)[:-50]):
            with self.assertRaises((ValueError, wave.Error, EOFError)):
                wav_to_call_pcm(body)

    async def asyncSetUp(self):
        self.requests = []
        self.tts_release = asyncio.Event()
        self.tts_finished = asyncio.Event()
        self.delay_tts = False
        async def tts(request):
            body = await request.json()
            self.requests.append(body)
            self.assertLessEqual(len(body["input"]), 240)
            self.assertEqual(set(body), {"input", "model", "voice", "response_format", "stream"})
            self.assertTrue(body["stream"])
            self.assertEqual(body["response_format"], "pcm")
            self.assertNotIn("Authorization", request.headers)
            headers = {"Content-Type": "audio/pcm", "x-sample-rate": "16000", "x-channels": "1", "x-audio-format": "pcm_s16le"}
            if self.delay_tts:
                response = web.StreamResponse(headers=headers)
                await response.prepare(request)
                await response.write((b"\x01\x00" * 1600)[:3199])
                await self.tts_release.wait()
                await response.write(b"\x00")
                await response.write_eof()
                self.tts_finished.set()
                return response
            return web.Response(body=b"\x01\x00" * 1600, headers=headers)
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
        self.ws_audio = bytearray()
        self.ws_requests = []
        async def realtime(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({"type": "session.created"})
            async for message in ws:
                if message.type == aiohttp.WSMsgType.BINARY:
                    self.assertLessEqual(len(message.data), 64000)
                    self.ws_audio.extend(message.data)
                    try:
                        await ws.send_json({"type": "transcription.delta", "text": "I wan balance", "delta": "I wan balance"})
                        await ws.send_json({"type": "transcription.delta", "text": "I wan check balance", "delta": "check balance", "replace_from": 6})
                    except ConnectionResetError:
                        break
                elif message.type == aiohttp.WSMsgType.TEXT:
                    data = message.json()
                    self.ws_requests.append(data)
                    if data.get("final"):
                        await ws.send_json({"type": "transcription.done", "text": "I wan check my balance", "language": "pd"})
            return ws
        app.router.add_get("/realtime", realtime)
        app.router.add_post("/tts", tts)
        app.router.add_post("/asr", asr)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.config = replace(apply_pidgin_stack(LabConfig(tts_language="pcm")),
                              tts_url=f"http://127.0.0.1:{port}/tts", stt_batch_url=f"http://127.0.0.1:{port}/asr",
                              stt_ws_url=f"http://127.0.0.1:{port}/realtime")

    async def asyncTearDown(self):
        self.tts_release.set()
        await self.runner.cleanup()

    async def test_tts_yields_before_eof_and_preserves_odd_network_boundary(self):
        self.delay_tts = True
        async with aiohttp.ClientSession() as session:
            _, chunks = await EjiroTTSClient(self.config, session).stream("How you dey?")
            first = await asyncio.wait_for(anext(chunks), 2)
            self.assertGreater(len(first), 0)
            self.assertFalse(self.tts_finished.is_set())
            self.tts_release.set()
            pcm = first + b"".join([chunk async for chunk in chunks])
            expected = wav_to_call_pcm(pcm16_wav(b"\x01\x00" * 1600))
            self.assertEqual(len(pcm), len(expected))
            # Independent resamplers dither PCM16, differing by up to two steps.
            self.assertLessEqual(max(abs(a - b) for a, b in zip(array("h", pcm), array("h", expected))), 2)

    async def test_idle_silence_does_not_fill_sbpn_buffer(self):
        with TemporaryDirectory() as directory:
            pipeline = ConversationPipeline(self.config, TraceRecorder(Path(directory)), AsyncMock())
            pipeline.stt.append = AsyncMock()
            pipeline.stt.recover = AsyncMock()
            try:
                for _ in range(40):
                    await pipeline.audio(b"\0\0" * 16000)
                pipeline.stt.append.assert_not_awaited()
                await pipeline.audio(b"\xff\x7f" * 320)
                pipeline.stt.append.assert_awaited_once()
                await pipeline.audio(b"\0\0" * 320)
                pipeline.stt.recover.assert_awaited_once_with("speech_candidate_cancel")
            finally:
                await pipeline.close()

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

    async def test_live_partials_replace_prefix_and_final_has_no_batch_duplicate(self):
        partial, final = AsyncMock(), AsyncMock()
        client = SBPNClient(self.config, partial, final, lambda *args: None)
        try:
            await client.connect()
            pcm = b"\x01\x00" * 1600
            await client.append(pcm)
            async with asyncio.timeout(2):
                while partial.await_count < 2:
                    await asyncio.sleep(0.01)
            self.assertEqual(partial.await_args_list[-1].args, ("I wan check balance", "I wan check balance"))
            self.assertEqual(bytes(self.ws_audio), pcm)
            self.assertNotIn("language", self.ws_requests[0])
            await client.commit(pcm)
            await asyncio.gather(*client.tasks)
            final.assert_awaited_once_with("I wan check my balance")
            self.assertEqual(self.requests, [])
            await client.append(pcm)
            await client.commit(pcm)
            await asyncio.gather(*client.tasks)
            self.assertEqual(final.await_count, 2)
        finally:
            await client.close()

    async def test_long_live_turn_falls_back_without_dropping_audio(self):
        final = AsyncMock()
        client = SBPNClient(self.config, AsyncMock(), final, lambda *args: None)
        pcm = b"\x01\x00" * 16000 * 31
        try:
            await client.connect()
            for offset in range(0, len(pcm), 32000):
                await client.append(pcm[offset:offset + 32000])
            self.assertTrue(client.degraded)
            await client.commit(pcm)
            await asyncio.gather(*client.tasks)
            self.assertEqual(b"".join(self.requests), pcm)
            final.assert_awaited_once()
        finally:
            await client.close()

    async def test_stream_disconnect_uses_complete_turn_for_batch_recovery(self):
        final = AsyncMock()
        client = SBPNClient(self.config, AsyncMock(), final, lambda *args: None)
        pcm = b"\x01\x00" * 1600
        try:
            await client.connect()
            await client.append(pcm)
            await client._close_socket()
            await client.commit(pcm)
            await asyncio.gather(*client.tasks)
            self.assertEqual(b"".join(self.requests), pcm)
            final.assert_awaited_once()
        finally:
            await client.close()
