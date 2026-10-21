import asyncio
import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from latency_lab import directory_locations as loc, provider_directory as directory
from latency_lab.agentic import AgentRuntimeContext
from latency_lab.action_tools import ActionToolCall
from latency_lab.config import LabConfig
from latency_lab.pipeline import ConversationPipeline
from latency_lab.trace import TraceRecorder


def semantic_reply(result):
    async def stream(*args, **kwargs):
        yield result
    return stream


class Locations(unittest.TestCase):
    def test_observed_call_errors_require_confirmation(self):
        args = {"intent":"lookup","network":"PROVIDER","plan":"PLOS","provider":"Kedja","state":"Lagos"}
        result = loc.clarify(args, "I am in a Kedja on the PLOS plan")
        self.assertEqual(result["status"], "needs_confirmation")
        proposed = result["data"]["suggested_filters"]
        self.assertEqual(proposed["town"], "Ikeja")
        self.assertEqual(proposed["plan"], "Plus")
        self.assertNotIn("provider", proposed)
        self.assertEqual(args["provider"], "Kedja")

    def test_even_llm_silent_correction_still_asks_caller(self):
        r = loc.clarify({"intent":"lookup","network":"PROVIDER","town":"Ikeja","plan":"Plus","state":"Lagos"}, "eat Kedja and PLOS plan")
        self.assertEqual(r["status"], "needs_confirmation")

    def test_no_correction_for_exact_or_non_lagos(self):
        self.assertIsNone(loc.clarify({"intent":"lookup","town":"Ikeja","plan":"Plus"}, "Ikeja on Plus"))
        self.assertIsNone(loc.clarify({"intent":"lookup","provider":"Kedja","state":"Oyo"}))
        self.assertIsNone(loc.clarify({"intent":"conversation"}, "a Kedja"))

    def test_unknown_plan_is_not_zero_matches(self):
        rows = [{"town":"IKEJA", "state":"LAGOS", "plan":"PLUS & HIGHER"}]
        r = loc.validate_labels({"network":"PROVIDER","plan":"PLOS","town":"Ikeja"}, rows)
        self.assertEqual(r["status"], "needs_input")  # low-score fuzzy isn't silently accepted
        r = loc.validate_labels({"network":"PROVIDER","plan":"Plus","town":"Ikejah"}, rows)
        self.assertEqual(r["status"], "needs_confirmation")
        self.assertEqual(r["data"]["suggested_filters"]["town"], "Ikeja")

    def test_nearby_preserves_eligibility(self):
        rows = [["CODE","ZONE","STATE","TOWN","PROVIDER","ADDRESS","CAT","PLAN","SPECIALTY"],
                ["1","","Lagos","Ikoyi","Nearby Plus","Road","D","PLUS & HIGHER","GENERAL MEDICINE"],
                ["2","","Lagos","Ikoyi","Wrong Plan","Road","D","MAX & HIGHER","GENERAL MEDICINE"]]
        args={"intent":"lookup","network":"PROVIDER","town":"Lagos Island","state":"Lagos","plan":"Plus"}
        self.assertEqual(directory.match_records("PROVIDER",rows,args),[])
        town=loc.NEARBY_LAGOS["lagos island"][0]
        found=directory.match_records("PROVIDER",rows,{**args,"town":town})
        self.assertEqual([r["provider"] for r in found],["Nearby Plus"])
        self.assertNotIn("Ikeja",loc.NEARBY_LAGOS["ikoyi"])


class LiveReadChecks(unittest.IsolatedAsyncioTestCase):
    async def test_transient_retry_is_fresh_and_permanent_errors_do_not_retry(self):
        rows=[["CODE","ZONE","STATE","TOWN","PROVIDER","ADDRESS","CAT","PLAN","SPECIALTY"],
              ["1","","Lagos","Ikoyi","Current Hospital","Fresh Road","D","PLUS & HIGHER","GENERAL MEDICINE"]]
        good={"ok":True,"spreadsheetId":directory.SHEET_ID,"sheets":[{"name":"PROVIDER","values":rows}],"readCompletedAt":"now"}
        class Response:
            def __init__(self,body):self.body=body
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            def raise_for_status(self):pass
            async def json(self,**kwargs):return self.body
        class HTTP:
            def __init__(self,bodies):self.bodies=bodies;self.calls=0
            def post(self,*args,**kwargs):
                body=self.bodies[self.calls];self.calls+=1;return Response(body)
        with patch.dict(directory.os.environ,{"PROVIDER_DIRECTORY_ENDPOINT":"https://script.google.com/macros/s/test/exec","PROVIDER_DIRECTORY_TOKEN":"test"}):
            http=HTTP([{"ok":False,"error":"SOURCE_UNAVAILABLE"},good])
            r=await directory.lookup(http,{"intent":"lookup","network":"PROVIDER","plan":"Plus","town":"Lagos Island","state":"Lagos"})
            self.assertEqual(http.calls,2);self.assertEqual(r["status"],"ok")
            self.assertEqual(r["data"]["total_matches"],0)
            self.assertEqual(r["data"]["nearby_options"][0]["matches"][0]["address"],"Fresh Road")
            http=HTTP([{"ok":False,"error":"UNAUTHORIZED"}])
            r=await directory.lookup(http,{"intent":"lookup","network":"PROVIDER","provider":"Hospital"})
            self.assertEqual(http.calls,1);self.assertEqual(r["status"],"failed")


class PipelineChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        async def send(event): pass
        self.p=ConversationPipeline(LabConfig(trace_dir=root,report_dir=root),TraceRecorder(root,"lookup-test"),send,
            agent_context=AgentRuntimeContext(tools=({"name":directory.TOOL_NAME,"url":"builtin://provider-directory"},)))
        self.p._speak_text=AsyncMock();self.p._stream_llm_and_tts=AsyncMock(return_value=[])
        self.p.banking.activity=AsyncMock()
        self.p.turn_id="turn-1";self.p.final_text="Plus in Ikeja"
        from latency_lab.pipeline import Attempt
        self.attempt=Attempt(id="test",hypothesis=self.p.final_text,speculative=False,authorized=True,turn_id="turn-1")
        self.p.attempt=self.attempt

    async def asyncTearDown(self):
        await self.p.http.close();self.tmp.cleanup()

    async def test_acknowledgement_precedes_lookup(self):
        async def read(*args):
            self.assertTrue(self.p._speak_text.await_count)
            self.assertEqual(self.p._speak_text.call_args.kwargs["phase"],"provider_lookup_acknowledgement")
            return {"status":"ok","data":{}}
        call=ActionToolCall("a",directory.TOOL_NAME,{"intent":"lookup","network":"PROVIDER","plan":"Plus","town":"Ikeja"})
        with patch.object(directory,"lookup",read):
            await self.p._run_directory_lookup(self.attempt,[],call)

    async def test_mishearing_does_not_fetch_until_confirmation(self):
        self.p.final_text="eat Kedja on the PLOS plan"
        call=ActionToolCall("a",directory.TOOL_NAME,{"intent":"lookup","network":"PROVIDER","plan":"PLOS","provider":"Kedja"})
        with patch.object(directory,"lookup",AsyncMock()) as read:
            await self.p._run_directory_lookup(self.attempt,[],call)
            read.assert_not_awaited()
        self.assertIsNotNone(self.p.pending_directory)
        self.p.final_text="Yes, that’s correct."
        self.p.llm.stream = semantic_reply("confirmed")
        self.p._run_directory_lookup=AsyncMock();self.p._commit_attempt_text=AsyncMock()
        await self.p._handle_directory_confirmation(self.attempt)
        args=self.p._run_directory_lookup.call_args.args[2].arguments
        self.assertEqual(args["town"],"Ikeja");self.assertEqual(args["plan"],"Plus")
        self.assertTrue(self.p._run_directory_lookup.call_args.kwargs["confirmed"])

    async def test_unclear_reply_does_not_accept_correction(self):
        self.p.pending_directory=loc.clarify({"intent":"lookup","town":"Kedja"})
        self.p.final_text="Maybe, I am not sure"
        self.p.llm.stream = semantic_reply("unclear")
        self.p._run_directory_lookup=AsyncMock();self.p._commit_attempt_text=AsyncMock()
        await self.p._handle_directory_confirmation(self.attempt)
        self.p._run_directory_lookup.assert_not_awaited()


if __name__=="__main__":unittest.main()
