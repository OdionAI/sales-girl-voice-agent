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
from latency_lab.action_tools import ActionToolCall, confirmation_prompt
from latency_lab.pidgin import (
    EJIRO_MODEL, LANGUAGE_RULES, PIDGIN_VOICE, SBPN_MODEL, SBPN_WS_URL, EjiroTTSClient, SBPNClient,
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
            self.assertEqual(result.tts_model, "qwen-pidgin-f1-cleaned-epoch5")
            self.assertEqual(result.tts_voice, "favour")
            self.assertEqual(result.tts_seed, "17")
            self.assertEqual(result.stt_model, SBPN_MODEL)
            self.assertEqual(result.stt_ws_url, SBPN_WS_URL)
            self.assertEqual(result.tts_ref_audio, "")
            for key in ("llm_model", "llm_base_url", "llm_api_key", "llm_enable_thinking"):
                self.assertEqual(getattr(base, key), getattr(result, key))
        for language in ("English", "French", "Yoruba", "Hausa", "Igbo", ""):
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

    def test_spoken_numbers_preserve_every_digit_and_decimal(self):
        self.assertEqual(ejiro_spoken_text("006789"), " zero zero six seven eight nine ")
        self.assertIn("point", ejiro_spoken_text("12.50"))
        self.assertIn("naira", ejiro_spoken_text("\u20a612.50"))
        self.assertEqual(ejiro_spoken_text("How you dey?"), "How you dey?")
        self.assertEqual(ejiro_spoken_text("Confirm Labs: https://example.com/confirm"),
                         "Confirm Labs: https://example.com/confirm")

    async def test_numeric_expansion_is_split_before_sending(self):
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
        self.asr_text = "I wan check my balance"
        self.ws_final_text = "I wan check my balance"
        async def tts(request):
            body = await request.json()
            self.requests.append(body)
            self.assertLessEqual(len(body["input"]), 240)
            self.assertEqual(set(body), {"input", "model", "voice", "response_format", "stream", "seed", "speed"})
            self.assertEqual(body["model"], "qwen-pidgin-f1-cleaned-epoch5")
            self.assertEqual(body["voice"], PIDGIN_VOICE)
            self.assertEqual(body["seed"], 17)
            self.assertEqual(body["speed"], 1)
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
            return web.json_response({"text": self.asr_text, "language": "pd"})
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
                        await ws.send_json({"type": "transcription.done", "text": self.ws_final_text, "language": "pd"})
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
                self.assertIn("konfam, with K", pipeline.system_prompt)
                self.assertNotIn("Do not imitate an accent with misspellings", pipeline.system_prompt)
                self.assertIn("How I fit help", pipeline.opening_greeting_text())
            finally:
                await pipeline.close()

    async def test_confirmation_has_untruncated_pidgin_rules_and_unchanged_structured_details(self):
        messages = []
        class LLM:
            async def stream(self, sent, **kwargs):
                messages.extend(sent)
                yield "Abeg, konfam say make I go ahead?"
        details = {"customer": "Confirm Labs", "account": "0012345678", "amount": 1250.50,
                   "status": "confirmed", "url": "https://example.com/confirm"}
        call = ActionToolCall("test-only", "transfer_money", details)
        question = await confirmation_prompt(LLM(), call, language_hint="pcm", instructions="x" * 5000)
        self.assertIn("konfam", question)
        self.assertIn(LANGUAGE_RULES, messages[0]["content"])
        import json
        payload = json.loads(messages[1]["content"])
        self.assertEqual(payload["details"], details)
        self.assertEqual(payload["action"], "transfer_money")

    async def test_pidgin_fallback_stays_pidgin_when_the_model_is_unavailable(self):
        class UnavailableLLM:
            async def stream(self, *args, **kwargs):
                raise RuntimeError("offline test")
                yield
        with TemporaryDirectory() as directory:
            pipeline = ConversationPipeline(self.config, TraceRecorder(Path(directory)), AsyncMock())
            pipeline.llm = UnavailableLLM()
            try:
                self.assertIn("konfam", await pipeline._natural_line("ask_confirmation", {}))
                self.assertIn(await pipeline._natural_line("acknowledge_tool_start", {}),
                              ["Make I check am for you.", "Give me small time, abeg."])
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

    async def test_empty_final_releases_pipeline_and_next_turn_is_accepted(self):
        with TemporaryDirectory() as directory:
            pipeline = ConversationPipeline(self.config, TraceRecorder(Path(directory)), AsyncMock())
            pipeline._start_attempt = AsyncMock()
            client = SBPNClient(self.config, pipeline.on_partial, pipeline.on_final,
                                pipeline._provider_event,
                                on_final_unavailable=pipeline.on_stt_final_unavailable)
            pipeline.stt = client
            try:
                await client.connect()
                for number, text in enumerate(("First question", "", "Next question"), 1):
                    self.ws_final_text = self.asr_text = text
                    pipeline.state = "speaking"
                    pipeline.turn_id = f"turn-{number:04d}"
                    pipeline.turn_number = number
                    pipeline.final_text = ""
                    pcm = b"\x01\x00" * 9600
                    pipeline.turn_pcm16.extend(pcm)
                    await client.append(pcm)
                    await pipeline._speech_ended()
                    async with asyncio.timeout(2):
                        while client.tasks:
                            await asyncio.gather(*client.tasks)
                    if text:
                        self.assertEqual(pipeline.final_text, text)
                        await pipeline.client_event("tts_stop")
                    else:
                        self.assertEqual(pipeline.state, "idle")
                        self.assertIsNone(pipeline.turn_id)
                        self.assertIsNone(pipeline.final_timeout_task)
                        self.assertFalse(client.pending_final)
                        self.assertIsNone(client.ws)
                        self.assertIn("turn-0002", pipeline.closed_turns)
                self.assertEqual([c.args[0] for c in pipeline._start_attempt.await_args_list],
                                 ["First question", "Next question"])
                events = pipeline.trace.path.read_text()
                self.assertIn('"event": "stt_final_unavailable"', events)
                self.assertNotIn('"event": "stt_final_timeout"', events)
            finally:
                await pipeline.close()

    async def test_empty_realtime_uses_batch_success_without_reporting_failure(self):
        final, unavailable = AsyncMock(), AsyncMock()
        self.ws_final_text = ""
        client = SBPNClient(self.config, AsyncMock(), final, lambda *args: None,
                            on_final_unavailable=unavailable)
        try:
            await client.connect()
            pcm = b"\x01\x00" * 1600
            await client.append(pcm)
            await client.commit(pcm)
            await asyncio.gather(*client.tasks)
            final.assert_awaited_once_with(self.asr_text)
            unavailable.assert_not_awaited()
        finally:
            await client.close()

    async def test_failed_batch_notifies_once_and_does_not_publish_partial_as_final(self):
        final, unavailable = AsyncMock(), AsyncMock()
        config = replace(self.config, stt_batch_url=self.config.stt_batch_url + '/missing')
        client = SBPNClient(config, AsyncMock(), final, lambda *args: None,
                            on_final_unavailable=unavailable)
        try:
            await client.connect()
            await client.commit(b"\x01\x00" * 1600)
            await asyncio.gather(*client.tasks)
            unavailable.assert_awaited_once_with()
            final.assert_not_awaited()
            self.assertFalse(client.pending_final)
        finally:
            await client.close()

    async def test_cancelled_final_does_not_report_a_failed_turn(self):
        unavailable = AsyncMock()
        client = SBPNClient(self.config, AsyncMock(), AsyncMock(), lambda *args: None,
                            on_final_unavailable=unavailable)
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked_transcription(_pcm):
            entered.set()
            await release.wait()
        client._transcribe = blocked_transcription
        try:
            await client.connect()
            await client.commit(b"\x01\x00" * 1600)
            await asyncio.wait_for(entered.wait(), 2)
            await client.recover("session_closed")
            unavailable.assert_not_awaited()
            self.assertFalse(client.pending_final)
            self.assertFalse(client.tasks)
        finally:
            release.set()
            await client.close()

    async def test_unavailable_callback_ignores_a_turn_that_already_closed(self):
        with TemporaryDirectory() as directory:
            pipeline = ConversationPipeline(self.config, TraceRecorder(Path(directory)), AsyncMock())
            pipeline.stt.recover = AsyncMock()
            try:
                await pipeline.on_stt_final_unavailable()
                self.assertEqual(pipeline.state, "idle")
                pipeline.stt.recover.assert_not_awaited()
            finally:
                await pipeline.close()
