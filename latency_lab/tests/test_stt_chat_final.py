import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from aiohttp import web

from latency_lab.config import LabConfig
from latency_lab.providers import RSTTClient


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
