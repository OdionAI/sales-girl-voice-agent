import asyncio
from unittest.mock import AsyncMock, patch
from latency_lab import provider_directory as d
from latency_lab.action_tools import ActionToolCall
from latency_lab.tests.test_directory_locations import PipelineChecks


class SearchFlow(PipelineChecks):
    async def test_omitted_network_reaches_standard_provider_search(self):
        async def read(http,args):
            self.assertEqual(args['network'],'PROVIDER')
            self.assertEqual(args['plan'],'Max')
            return {'status':'ok','data':{'matches':[], 'total_matches':0}}
        with patch.object(d,'lookup',read):
            await self.p._run_directory_lookup(self.attempt,[],ActionToolCall('a',d.TOOL_NAME,{'intent':'lookup','plan':'Max','town':'Ikeja'}))
        self.p._stream_llm_and_tts.assert_awaited_once()

    async def test_missing_details_ask_directly_without_search_or_llm(self):
        for args,field in [({'intent':'lookup','town':'Ikeja'},'plan'),({'intent':'lookup','plan':'Plus'},'area')]:
            self.p._speak_text.reset_mock()
            with patch.object(d,'lookup',AsyncMock()) as read:
                await self.p._run_directory_lookup(self.attempt,[],ActionToolCall('a',d.TOOL_NAME,args))
            read.assert_not_awaited()
            self.p._stream_llm_and_tts.assert_not_awaited()
            self.assertEqual(self.p._speak_text.call_args.kwargs['phase'],'provider_lookup_clarification')
            self.assertIn(field,self.p._speak_text.call_args.args[1])
            self.assertNotIn('network',self.p._speak_text.call_args.args[1])

    async def test_read_runs_while_acknowledgement_is_speaking(self):
        reading=asyncio.Event()
        async def read(*args):
            reading.set()
            return {'status':'ok','data':{}}
        async def speak(*args,**kwargs):
            await asyncio.wait_for(reading.wait(),1)
        self.p._speak_text=AsyncMock(side_effect=speak)
        with patch.object(d,'lookup',read):
            await self.p._run_directory_lookup(self.attempt,[],ActionToolCall('a',d.TOOL_NAME,{'intent':'lookup','plan':'Max','town':'Ikeja'}))
        self.assertEqual(self.p._speak_text.await_count,1)

    async def test_slow_search_gets_real_progress(self):
        async def read(*args):
            await asyncio.sleep(5.1)
            return {'status':'ok','data':{}}
        with patch.object(d,'lookup',read):
            await self.p._run_directory_lookup(self.attempt,[],ActionToolCall('a',d.TOOL_NAME,{'intent':'lookup','plan':'Max','town':'Ikeja'}))
        self.assertEqual([c.kwargs['phase'] for c in self.p._speak_text.await_args_list],['provider_lookup_acknowledgement','provider_lookup_wait'])

    async def test_failed_search_never_becomes_no_matches(self):
        with patch.object(d,'lookup',AsyncMock(return_value={'status':'failed','data':{}})):
            await self.p._run_directory_lookup(self.attempt,[],ActionToolCall('a',d.TOOL_NAME,{'intent':'lookup','plan':'Max','town':'Ikeja'}))
        self.p._stream_llm_and_tts.assert_not_awaited()
        self.assertEqual(self.p._speak_text.call_args.kwargs['phase'],'provider_lookup_failure')
        self.assertIn("doesn't mean",self.p._speak_text.call_args.args[1])

    async def test_source_clarification_cannot_become_fake_waiting(self):
        with patch.object(d,'lookup',AsyncMock(return_value={'status':'needs_input','message':'Which plan name is on your card?','data':{}})):
            await self.p._run_directory_lookup(self.attempt,[],ActionToolCall('a',d.TOOL_NAME,{'intent':'lookup','plan':'Unclear','town':'Ikeja'}))
        self.p._stream_llm_and_tts.assert_not_awaited()
        self.assertEqual(self.p._speak_text.call_args.args[1],'Which plan name is on your card?')

    async def test_interruption_cancels_read_and_no_waiting_speech_leaks(self):
        reading=asyncio.Event();cancelled=asyncio.Event()
        async def read(*args):
            reading.set()
            try: await asyncio.sleep(20)
            finally: cancelled.set()
        async def speak(*args,**kwargs):
            await reading.wait();self.attempt.cancelled=True
        self.p._speak_text=AsyncMock(side_effect=speak)
        with patch.object(d,'lookup',read),self.assertRaises(asyncio.CancelledError):
            await self.p._run_directory_lookup(self.attempt,[],ActionToolCall('a',d.TOOL_NAME,{'intent':'lookup','plan':'Max','town':'Ikeja'}))
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.p._speak_text.await_count,1)

    def test_spelling_variants_do_not_change_plan(self):
        self.assertEqual(d.prepare_args({'intent':'lookup','plan':'Pro Max'})['plan'],'Promax')
        self.assertEqual(d.prepare_args({'intent':'lookup','plan':'Max'})['plan'],'Max')
        self.assertEqual(d.preflight(d.prepare_args({'intent':'lookup','plan':' ','town':'Ikeja'}))['data']['missing_fields'],['plan'])

    async def test_other_explicit_schemes_are_preserved(self):
        self.assertEqual(d.prepare_args({'intent':'lookup','network':'FBN'})['network'],'FBN')
        result=await d.lookup(None,{'intent':'lookup','network':'invalid','town':'Ikeja','plan':'Max'})
        self.assertEqual(result['status'],'failed')
