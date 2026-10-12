import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace
from latency_lab import directory_email as email
from latency_lab.action_tools import ActionToolCall, PendingAction
from latency_lab.tests.test_directory_locations import PipelineChecks, semantic_reply

class Confirmation(unittest.IsolatedAsyncioTestCase):
    async def test_passes_natural_language_and_context_to_model(self):
        seen=[]
        async def stream(messages, **kwargs):
            seen.extend(messages)
            yield "confirmed"
        text="That would really help, pop those over for me."
        result=await email.confirmation_decision(SimpleNamespace(stream=stream),text,"Send to your saved email?",history=[{"role":"user","content":"I need the hospital list"}])
        self.assertEqual(result,"confirmed")
        self.assertIn(text,seen[1]["content"])
        self.assertIn("I need the hospital list",seen[1]["content"])

    async def test_alternate_address_confirmation_is_explicitly_scoped(self):
        seen=[]
        async def stream(messages, **kwargs):
            seen.extend(messages)
            yield "confirmed"
        result=await email.confirmation_decision(SimpleNamespace(stream=stream),"yes, that is correct",
            "Send the hospital list to other@example.test instead of your saved email?",
            action="alternate_email")
        self.assertEqual(result,"confirmed")
        self.assertIn("different address",seen[0]["content"])
        self.assertIn("exact address",seen[0]["content"])
        self.assertIn("alternate_email",seen[1]["content"])

    async def test_invalid_output_and_failure_do_not_send(self):
        for output in ["", "yes", "confirmed because I agree", "rejected", "unclear"]:
            result=await email.confirmation_decision(SimpleNamespace(stream=semantic_reply(output)),"yes","Email?")
            self.assertNotEqual(result,"confirmed")
        async def fail(*args,**kwargs):
            raise RuntimeError("offline")
            yield "confirmed"
        self.assertEqual(await email.confirmation_decision(SimpleNamespace(stream=fail),"yes","Email?"),"unclear")

class ConfirmationFlow(PipelineChecks):
    async def setup_email(self,text,decision,alternate=False):
        self.p.final_text=text
        self.p.llm.stream=semantic_reply(decision)
        args={"recipient_email":"other@example.test"} if alternate else {}
        self.p.pending_action=PendingAction(ActionToolCall('mail',email.TOOL_NAME,args),
            'Send to other@example.test instead of your saved email?' if alternate else 'Send to your saved email?')
        self.p.directory_email.prepared={"alternate":alternate}
        self.p.directory_email.send=AsyncMock(return_value={'status':'success','message':'Submitted for sending.','data':{}})
        self.p._finish_speech=AsyncMock();self.p._commit_attempt_text=AsyncMock()

    async def test_natural_permission_executes_once(self):
        await self.setup_email("That would be lovely, get them across to me.","confirmed")
        await self.p._handle_pending_action(self.attempt)
        self.p.directory_email.send.assert_awaited_once()
        self.assertIsNone(self.p.pending_action)
        await self.p._handle_pending_action(self.attempt)
        self.p.directory_email.send.assert_awaited_once()

    async def test_confirmed_alternate_passes_one_shot_confirmation_to_sender(self):
        await self.setup_email("Yes, that is the right email. Send it there.","confirmed",alternate=True)
        await self.p._handle_pending_action(self.attempt)
        self.p.directory_email.send.assert_awaited_once_with(self.p.http, confirmed_alternate=True)
        self.assertIsNone(self.p.pending_action)

    async def test_withdrawal_does_not_send(self):
        await self.setup_email("Let's leave that for another time.","rejected")
        await self.p._handle_pending_action(self.attempt)
        self.p.directory_email.send.assert_not_awaited()
        self.assertIsNone(self.p.pending_action)

    async def test_uncertainty_does_not_send(self):
        await self.setup_email("Which address do you have?","unclear")
        await self.p._handle_pending_action(self.attempt)
        self.p.directory_email.send.assert_not_awaited()
        self.assertIsNotNone(self.p.pending_action)

    async def test_cancelled_during_classification_does_not_send(self):
        await self.setup_email("Go ahead","confirmed")
        async def stream(*args,**kwargs):
            self.attempt.cancelled=True
            yield "confirmed"
        self.p.llm.stream=stream
        await self.p._handle_pending_action(self.attempt)
        self.p.directory_email.send.assert_not_awaited()
