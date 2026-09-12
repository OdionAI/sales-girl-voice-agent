import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from latency_lab.config import LabConfig
from latency_lab.providers import RLLMClient, RSTTClient, RTTSClient
from latency_lab.usage import UsageCollector, UsageReporter


class UsageTest(unittest.IsolatedAsyncioTestCase):
    def test_aggregates_real_usage_without_counting_duplicate_frames_twice(self):
        usage = UsageCollector()
        first = usage.llm_started()
        data = {'prompt_tokens': 100, 'completion_tokens': 20, 'prompt_tokens_details': {'cached_tokens': 10}}
        usage.llm_usage(first, data)
        usage.llm_usage(first, data)
        second = usage.llm_started()
        usage.llm_usage(second, {'prompt_tokens': 80, 'completion_tokens': 12})
        usage.stt_submitted(32000)
        usage.tts_accepted('Hello.')
        snapshot = usage.snapshot()
        self.assertEqual(snapshot['llm_input_tokens'], 180)
        self.assertEqual(snapshot['llm_cached_tokens'], 10)
        self.assertEqual(snapshot['measurement_status']['llm_cached_tokens'], 'partial')
        self.assertEqual(snapshot['stt_seconds'], 1)
        self.assertEqual(snapshot['tts_characters'], 6)
        usage.llm_started()  # Cancelled request with no provider usage frame.
        self.assertEqual(usage.snapshot()['measurement_status']['llm_input_tokens'], 'partial')

    async def test_usage_only_sse_frame_is_collected_without_changing_streamed_text(self):
        frames = [
            {'choices': [{'delta': {'content': 'Hello.'}}]},
            {'choices': [], 'usage': {'prompt_tokens': 42, 'completion_tokens': 3}},
        ]
        async def chunks():
            for frame in frames:
                for byte in ('data: ' + json.dumps(frame) + '\n\n').encode():
                    yield bytes([byte])
            yield b'data: [DONE]\n\n'
        response = Mock(status=200, content=SimpleNamespace(iter_any=chunks))
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)
        session = Mock(post=Mock(return_value=response))
        client = RLLMClient(LabConfig(), session)
        client.usage = UsageCollector()
        self.assertEqual([part async for part in client.stream([])], ['Hello.'])
        self.assertTrue(session.post.call_args.kwargs['json']['stream_options']['include_usage'])
        snapshot = client.usage.snapshot()
        self.assertEqual(snapshot['llm_input_tokens'], 42)
        self.assertIsNone(snapshot['llm_cached_tokens'])

    async def test_stt_counts_only_sent_pcm_and_tts_counts_accepted_text(self):
        usage = UsageCollector()
        stt = RSTTClient(LabConfig(), AsyncMock(), AsyncMock(), Mock())
        stt.usage = usage
        stt.ws = SimpleNamespace(closed=False, send_json=AsyncMock())
        await stt.append(b'\x00' * 6400)
        self.assertEqual(usage.snapshot()['stt_seconds'], .2)
        stt.ws.send_json.side_effect = OSError('disconnected')
        await stt.append(b'\x00' * 6400)
        self.assertEqual(usage.snapshot()['stt_seconds'], .2)
        response = Mock(headers={})
        tts = RTTSClient(LabConfig(tts_ref_audio=''), SimpleNamespace(post=AsyncMock(return_value=response)))
        tts.usage = usage
        await tts.stream('Read 0123456789.')
        self.assertEqual(usage.snapshot()['tts_characters'], 16)

    async def test_reporting_uses_canonical_session_and_retries_without_double_count(self):
        usage = UsageCollector()
        usage.tts_accepted('Hello')
        response = Mock()
        response.read = AsyncMock()
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)
        session = Mock(post=Mock(return_value=response))
        store = SimpleNamespace(session_id='', conversation_id='conversation', _headers=lambda: {'X-Business-ID': 'business'})
        reporter = UsageReporter(LabConfig(billing_service_base_url='http://billing', billing_service_token='test'), session, store, usage, Mock())
        await reporter.flush()
        session.post.assert_not_called()
        store.session_id = 'canonical'
        await reporter.flush()
        await reporter.flush()
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(session.post.call_args.kwargs['json']['session_id'], 'canonical')
        await reporter.close()
        self.assertTrue(session.post.call_args.kwargs['json']['usage']['final'])
        self.assertEqual(session.post.call_args.kwargs['json']['usage']['tts_characters'], 5)
