import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp import web

from latency_lab.agentic import AgentRuntimeContext
from latency_lab.config import LabConfig
from latency_lab.pipeline import ConversationPipeline
from latency_lab.providers import RSTTClient, is_asr_context_echo
from latency_lab.trace import TraceRecorder


class FakeBackend:
    def __init__(self, chat_text="language English<asr_text>CHAT HINTED"):
        self.chat_text = chat_text
        self.chat_bodies = []
        self.ws_hooks = []

    async def realtime(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "session.created"})
        async for message in ws:
            payload = message.json()
            if payload["type"] == "input_audio_buffer.commit" and payload["final"]:
                await ws.send_json({"type": "transcription.done", "text": "REALTIME"})
        return ws

    async def chat(self, request):
        body = await request.json()
        self.chat_bodies.append(body)
        content = self.chat_text
        if not content:
            return web.json_response({"choices": [{"message": {"content": ""}}]})
        return web.json_response({"choices": [{"message": {"content": content}}]})


async def start_backend(backend):
    app = web.Application()
    app.router.add_get("/realtime", backend.realtime)
    app.router.add_post("/chat", backend.chat)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    origin = f"127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    return runner, origin


class SttChatFinalTest(unittest.IsolatedAsyncioTestCase):
    async def test_wema_hint_includes_opay_and_spoken_digits_only_for_wema(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = TraceRecorder(Path(directory), "wema-hint")
            config = LabConfig(stt_chat_url="http://localhost/chat", stt_context="Wema Bank")
            wema = ConversationPipeline(
                config, trace, AsyncMock(),
                agent_context=AgentRuntimeContext(business_id="a0c8cda2-3b46-4ee0-898c-48f9a4d3a444"),
            )
            other = ConversationPipeline(
                config, trace, AsyncMock(),
                agent_context=AgentRuntimeContext(business_id="another-business"),
            )
            try:
                self.assertIn("OPay", wema.stt.config.stt_context)
                self.assertIn("0 zero", wema.stt.config.stt_context)
                self.assertIn("9 nine", wema.stt.config.stt_context)
                self.assertEqual(other.stt.config.stt_context, "Wema Bank")
            finally:
                await wema.http.close()
                await other.http.close()

    async def _run(self, chat_text, context="Wema hint: Glo, OPay, Wema Bank, naira"):
        backend = FakeBackend(chat_text)
        runner, origin = await start_backend(backend)
        finals = asyncio.Queue()
        events = []
        config = LabConfig(
            stt_model="whisper-large-v3-turbo",
            stt_ws_url=f"ws://{origin}/realtime",
            stt_chat_url=f"http://{origin}/chat",
            stt_chat_model="Qwen3-ASR",
            stt_context=context,
        )
        client = RSTTClient(config, AsyncMock(), finals.put, lambda e, d: events.append((e, d)))
        try:
            await client.connect()
            await client.commit(bytes(32000))  # 1 s of PCM
            final = await asyncio.wait_for(finals.get(), 5)
        finally:
            await client.close()
            await runner.cleanup()
        return final, backend, events

    async def test_committed_final_comes_from_the_hinted_chat_endpoint(self):
        final, backend, events = await self._run("language English<asr_text>Glo, one hundred naira")
        self.assertEqual(final, "Glo, one hundred naira")
        self.assertEqual(len(backend.chat_bodies), 1)
        body = backend.chat_bodies[0]
        self.assertEqual(body["model"], "Qwen3-ASR")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "Wema hint: Glo, OPay, Wema Bank, naira"})
        self.assertEqual(body["messages"][1]["content"][0]["type"], "input_audio")
        names = [n for n, _ in events]
        self.assertIn("stt_chat_final", names)
        self.assertFalse(any(n == "stt_realtime_final" and d.get("authoritative") for n, d in events))

    async def test_context_echo_is_suppressed_then_realtime_fallback_is_used(self):
        context = (
            "Listen on this call. The bank is Wema Bank; the phone networks are MTN, "
            "Glo (spelled G-l-o), Airtel, 9mobile; the currency is naira."
        )
        final, _, events = await self._run(context, context=context)
        self.assertEqual(final, "REALTIME")
        names = [name for name, _ in events]
        self.assertIn("stt_chat_context_echo_suppressed", names)
        self.assertNotIn("stt_chat_final", names)
        suppressed = next(data for name, data in events if name == "stt_chat_context_echo_suppressed")
        self.assertNotIn("text", suppressed)

    def test_context_echo_detector_keeps_normal_banking_requests(self):
        context = "Listen on this call. The bank is Wema Bank; network names include MTN and Glo."
        self.assertTrue(is_asr_context_echo("Listen on this call.", context))
        self.assertTrue(is_asr_context_echo("The bank is Wema Bank; network names include MTN and Glo.", context))
        self.assertFalse(is_asr_context_echo("Please check my Wema Bank balance.", context))

    async def test_pipeline_does_not_publish_or_persist_context_echoes(self):
        context = "Listen on this call. The bank is Wema Bank; network names include MTN and Glo."
        with tempfile.TemporaryDirectory() as directory:
            trace = TraceRecorder(Path(directory), "context-echo")
            send = AsyncMock()
            pipeline = ConversationPipeline(LabConfig(stt_context=context), trace, send)
            try:
                await pipeline.on_partial(context, context)
                await pipeline.on_final(context)
            finally:
                await pipeline.close()

            events = [json.loads(line) for line in trace.path.read_text().splitlines()]
            self.assertEqual(
                [event["reason"] for event in events if event["event"] == "stt_context_echo_suppressed"],
                ["partial_echoed_asr_context", "transcript_echoed_asr_context"],
            )
            self.assertFalse(any(event["event"] == "stt_final" for event in events))
            self.assertFalse(any(event["event"] == "stt_partial" for event in events))
            self.assertFalse(any(event["event"] == "conversation_persist_user" for event in events))
            self.assertFalse(any(call.args[0].get("type") == "final_user_request" for call in send.await_args_list))

    async def test_empty_chat_final_falls_back_to_realtime(self):
        final, backend, events = await self._run("")
        self.assertEqual(final, "REALTIME")
        names = [n for n, _ in events]
        self.assertIn("stt_chat_final_fallback_realtime", names)
        self.assertTrue(any(n == "stt_realtime_final" and d.get("authoritative") for n, d in events))

    async def test_no_chat_url_keeps_realtime_final(self):
        backend = FakeBackend()
        runner, origin = await start_backend(backend)
        finals = asyncio.Queue()
        config = LabConfig(stt_model="whisper-large-v3-turbo", stt_ws_url=f"ws://{origin}/realtime")
        client = RSTTClient(config, AsyncMock(), finals.put, lambda e, d: None)
        try:
            await client.connect()
            await client.commit(bytes(32000))
            final = await asyncio.wait_for(finals.get(), 5)
        finally:
            await client.close()
            await runner.cleanup()
        self.assertEqual(final, "REALTIME")
        self.assertEqual(backend.chat_bodies, [])


if __name__ == "__main__":
    unittest.main()
