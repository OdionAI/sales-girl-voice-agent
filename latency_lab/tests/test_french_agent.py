import asyncio,unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from aiohttp import web
from latency_lab.config import LabConfig,apply_runtime_overrides
from latency_lab.providers import RSTTClient
from latency_lab.pipeline import ConversationPipeline

class FrenchAgent(unittest.IsolatedAsyncioTestCase):
    def test_greetings_follow_session_language_and_keep_english(self):
        for language,expected in [('fr','Bonjour'),('en','Hello')]:
            cfg=apply_runtime_overrides(LabConfig(opening_greeting_text=''),{},language=language)
            fake=SimpleNamespace(config=cfg,agent_context=SimpleNamespace(name='Amaka'))
            greeting=ConversationPipeline.opening_greeting_text(fake)
            self.assertTrue(greeting.startswith(expected));self.assertIn('Amaka',greeting)

    async def test_french_recognition_does_not_request_english_translation(self):
        received=asyncio.Queue()
        async def realtime(request):
            ws=web.WebSocketResponse();await ws.prepare(request)
            await ws.send_json({'type':'session.created'})
            async for msg in ws:
                body=msg.json()
                if body.get('type')=='session.update':await received.put(body)
            return ws
        app=web.Application();app.router.add_get('/realtime',realtime)
        runner=web.AppRunner(app);await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        port=site._server.sockets[0].getsockname()[1]
        cfg=apply_runtime_overrides(LabConfig(stt_model='whisper-large-v3-turbo',stt_ws_url=f'ws://127.0.0.1:{port}/realtime'),{},language='fr')
        client=RSTTClient(cfg,AsyncMock(),AsyncMock(),lambda *a:None)
        try:
            await client.connect();message=await asyncio.wait_for(received.get(),2)
            self.assertEqual(message['language'],'fr');self.assertEqual(message['to_language'],'fr')
        finally:await client.close();await runner.cleanup()
