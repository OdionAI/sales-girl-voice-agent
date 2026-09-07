import asyncio
import base64
import unittest
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web

from latency_lab.config import LabConfig, apply_runtime_overrides
from latency_lab.providers import RSTTClient


class WhisperProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_two_utterances_rearm_same_socket_without_http_batch(self):
        events, received, finals = [], [], asyncio.Queue()
        connected, release_hello = asyncio.Event(), asyncio.Event()
        armed = asyncio.Queue()
        sockets, batch_requests = [], []

        async def realtime(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            sockets.append(ws)
            connected.set()
            await release_hello.wait()
            await ws.send_json({"type": "session.created"})
            turn = 0
            async for message in ws:
                payload = message.json()
                received.append(payload)
                if payload["type"] == "input_audio_buffer.commit":
                    if payload["final"]:
                        turn += 1
                        # No partial required: Whisper can finalize only on commit.
                        await ws.send_json({"type": "transcription.done", "text": f"Turn {turn}"})
                    else:
                        await armed.put(True)
            return ws

        async def batch(request):
            batch_requests.append(request.path)
            return web.Response(status=404)

        app = web.Application()
        app.router.add_get("/realtime", realtime)
        app.router.add_post("/batch", batch)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        origin = f"127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        client = RSTTClient(
            LabConfig(stt_model="whisper-large-v3-turbo", stt_ws_url=f"ws://{origin}/realtime",
                      stt_batch_url=f"http://{origin}/batch"),
            AsyncMock(), finals.put, lambda event, data: events.append((event, data)),
        )
        connect_task = asyncio.create_task(client.connect())
        try:
            await asyncio.wait_for(connected.wait(), 2)
            self.assertFalse(client.ready)
            self.assertEqual(received, [])
            release_hello.set()
            await asyncio.wait_for(connect_task, 2)
            await asyncio.wait_for(armed.get(), 2)
            self.assertEqual(received[0]["type"], "session.update")
            self.assertEqual(received[0]["model"], "whisper-large-v3-turbo")
            self.assertEqual(received[0]["language"], "en")
            for turn in (1, 2):
                audio = bytes([turn, 0]) * 1600
                await client.append(audio)
                await client.commit(audio)
                self.assertEqual(await asyncio.wait_for(finals.get(), 2), f"Turn {turn}")
                await asyncio.wait_for(armed.get(), 2)
                self.assertTrue(client.ready)
            self.assertEqual(len(sockets), 1)
            self.assertEqual(batch_requests, [])
            self.assertFalse(any(name.startswith("stt_batch_") for name, _ in events))
            self.assertEqual([p["final"] for p in received if p["type"] == "input_audio_buffer.commit"],
                             [False, True, False, True, False])
            self.assertEqual([len(base64.b64decode(p["audio"])) for p in received
                              if p["type"] == "input_audio_buffer.append"], [3200, 3200])
        finally:
            connect_task.cancel()
            await asyncio.gather(connect_task, return_exceptions=True)
            await client.close()
            await runner.cleanup()

    async def test_failed_whisper_connection_does_not_pretend_batch_is_available(self):
        events = []
        client = RSTTClient(LabConfig(stt_model="whisper-large-v3-turbo"), AsyncMock(), AsyncMock(),
                            lambda name, data: events.append((name, data)))
        client._open_socket = AsyncMock(side_effect=aiohttp.ClientConnectionError("unavailable"))
        try:
            with self.assertRaises(aiohttp.ClientConnectionError):
                await client.connect()
            self.assertFalse(events[-1][1]["batch_final_available"])
        finally:
            await client.close()

    def test_model_only_override_disables_existing_batch_endpoint(self):
        config = apply_runtime_overrides(LabConfig(), {"stt_model": "whisper-large-v3-turbo"})
        self.assertEqual(config.stt_batch_url, "")
