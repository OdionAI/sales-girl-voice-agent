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

    def test_specific_area_can_match_only_the_address(self):
        rows = [
            ["CODE", "ZONE", "STATE", "TOWN", "PROVIDER", "ADDRESS", "CAT", "PLAN", "SPECIALTY"],
            ["1", "Lagos", "Lagos", "Lagos Island", "Victoria Dental Centre", "12 Adeola Odeku Street, Victoria Island", "Dental Clinic", "PLUS & HIGHER", "Dental"],
            ["2", "Lagos", "Lagos", "Ikeja", "Ikeja General Hospital", "Ikeja", "General Hospital", "PLUS & HIGHER", "General Medicine"],
        ]
        args = {"intent": "lookup", "network": "PROVIDER", "plan": "Plus", "town": "Lagos", "area": "Victoria Island"}
        matches = d.match_records("PROVIDER", rows, args)
        self.assertEqual([row["provider"] for row in matches], ["Victoria Dental Centre"])

    def test_dental_clinic_matches_sheet_specialty_or_category(self):
        rows = [
            ["CODE", "ZONE", "STATE", "TOWN", "PROVIDER", "ADDRESS", "CAT", "PLAN", "SPECIALTY"],
            ["1", "Lagos", "Lagos", "Ikeja", "Opebi Medical Centre", "Opebi, Ikeja", "Hospital", "PLUS & HIGHER", "Dental"],
            ["2", "Lagos", "Lagos", "Ikeja", "Ikeja General Hospital", "Ikeja", "General Hospital", "PLUS & HIGHER", "General Medicine"],
            ["3", "Lagos", "Lagos", "Ikeja", "Ikeja Dental Clinic", "Ikeja", "Clinic", "PLUS & HIGHER", ""],
        ]
        args = {"intent": "lookup", "network": "PROVIDER", "plan": "Plus", "town": "Ikeja", "specialty": "dental clinic"}
        matches = d.match_records("PROVIDER", rows, args)
        self.assertEqual({row["provider"] for row in matches}, {"Opebi Medical Centre", "Ikeja Dental Clinic"})

    def test_area_ranking_prefers_address_match_and_no_proximity_claim(self):
        rows = [
            ["CODE", "ZONE", "STATE", "TOWN", "PROVIDER", "ADDRESS", "CAT", "PLAN", "SPECIALTY"],
            ["1", "Lagos", "Lagos", "Ikeja", "Broad Hospital", "Ikeja", "Hospital", "PLUS & HIGHER", "Dental"],
            ["2", "Lagos", "Lagos", "Lagos", "Opebi Clinic", "Opebi Road, Ikeja", "Clinic", "PLUS & HIGHER", "Dental"],
        ]
        args = {"intent": "lookup", "network": "PROVIDER", "plan": "Plus", "town": "Ikeja", "area": "Opebi", "specialty": "Dental"}
        matches = d.match_records("PROVIDER", rows, args)
        self.assertEqual(matches[0]["provider"], "Opebi Clinic")

    async def test_lookup_caps_results_even_if_caller_requests_more(self):
        class Response:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def raise_for_status(self): pass
            async def json(self, **kwargs):
                rows = [["CODE", "ZONE", "STATE", "TOWN", "PROVIDER", "ADDRESS", "CAT", "PLAN", "SPECIALTY"]]
                rows += [[str(i), "Lagos", "Lagos", "Ikeja", f"Dental Clinic {i}", f"Opebi Road, Ikeja", "Dental Clinic", "PLUS & HIGHER", "Dental"] for i in range(1, 13)]
                return {"ok": True, "spreadsheetId": d.SHEET_ID, "readCompletedAt": "2026-10-11T00:00:00Z",
                        "sheets": [{"name": "PROVIDER", "values": rows}]}
        class HTTP:
            def post(self, *args, **kwargs): return Response()
        env = patch.dict(d.os.environ, {"PROVIDER_DIRECTORY_ENDPOINT": "https://script.google.com/macros/s/test/exec", "PROVIDER_DIRECTORY_TOKEN": "test"})
        with env:
            result = await d.lookup(HTTP(), {"intent": "lookup", "plan": "Plus", "town": "Ikeja", "area": "Opebi", "specialty": "dental clinic"}, limit=70)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["total_matches"], 12)
        self.assertEqual(len(result["data"]["matches"]), 7)
        self.assertIn("not distance-ranked", result["message"])

    async def test_other_explicit_schemes_are_preserved(self):
        self.assertEqual(d.prepare_args({'intent':'lookup','network':'FBN'})['network'],'FBN')
        result=await d.lookup(None,{'intent':'lookup','network':'invalid','town':'Ikeja','plan':'Max'})
        self.assertEqual(result['status'],'failed')
