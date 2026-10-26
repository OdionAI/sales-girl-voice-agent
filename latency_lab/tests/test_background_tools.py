"""Scenario tests: bank tools must never block or stall the conversation, the agent
must acknowledge and report in its own words (never a canned line), failures must be
handled gracefully, and the confirmation step must never require an exact phrase."""
import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from latency_lab.action_tools import ActionToolCall, PendingAction
from latency_lab.config import LabConfig
from latency_lab.pipeline import Attempt, ConversationPipeline
from latency_lab.trace import TraceRecorder


class ScriptedLLM:
    """Returns scripted text per call so we can prove wording is model-generated."""

    def __init__(self, *texts):
        self.texts = list(texts) or ["Sure, let me have a look at that for you."]
        self.calls = []

    async def stream(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        text = self.texts[min(len(self.calls) - 1, len(self.texts) - 1)]
        yield text


class FakeBanking:
    def __init__(self, body):
        self.body = body
        self.prepared = None
        self.profile = {"customer_id": "R008448055"}
        self.records = {"wema_execute_prepared": {"url": "http://127.0.0.1:8097/v1/tools/wema_execute_prepared", "headers": {}}}
        self.context = type("C", (), {"agent_id": "a", "business_id": "b", "end_user_id": "u"})()
        self.session_id = "s"
        self.calls = []

    async def execute(self, call, *, confirmed_operation="", on_result=None):
        self.calls.append(call.name)
        if on_result:
            on_result(self.body)
        if call.name.startswith("wema_prepare_") and self.body.get("status") == "prepared":
            self.prepared = self.body.get("data")
        return self.body

    async def emit(self, topic, payload):
        return None

    async def close(self):
        return None


class BackgroundToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sent = []

        async def send(message):
            self.sent.append(message)

        self.pipe = ConversationPipeline(
            replace(LabConfig(), engagement_enabled=True, memory_compaction_enabled=False,
                    background_tools=True),
            TraceRecorder(Path(self.tmp.name)), send, transport="livekit")
        self.pipe.llm = ScriptedLLM("Let me pull that up for you.")
        self.spoken, self.announced = [], []

        async def fake_speak(attempt, text, *, phase):
            self.spoken.append((text, phase))

        async def fake_announce(text):
            self.announced.append(text)

        self.pipe._speak_text = AsyncMock(side_effect=fake_speak)
        self.pipe.announce = AsyncMock(side_effect=fake_announce)

    async def asyncTearDown(self):
        await self.pipe.close()
        self.tmp.cleanup()

    def attempt(self):
        a = Attempt("one", "airtime please", False, authorized=True, turn_id="one")
        a.authorization_event.set()
        self.pipe.attempt = a
        self.pipe.turn_id = "one"
        return a

    async def dispatch(self, body, name="wema_prepare_airtime_purchase"):
        self.pipe.banking = FakeBanking(body)
        call = ActionToolCall("c1", name, {"amount": "100"})
        await self.pipe._dispatch_background_tool(self.attempt(), call)
        await asyncio.gather(*list(self.pipe._background_tasks), return_exceptions=True)

    async def test_acknowledges_naturally_and_does_not_block(self):
        self.pipe.banking = FakeBanking({"status": "ok"})
        self.pipe.engagement.enabled = False  # exercise the fallback ack line
        call = ActionToolCall("c1", "wema_get_balance", {"source_account": "0000000001"})
        gate = asyncio.Event()
        self.pipe.banking.gate = gate  # keep the tool running
        await self.pipe._dispatch_background_tool(self.attempt(), call)
        # the acknowledgement is spoken immediately, before the tool finishes
        self.assertEqual(self.spoken[0][0], "Let me pull that up for you.")
        self.assertEqual(self.spoken[0][1], "tool_acknowledged")
        # the conversation is free: the tool is running in the background
        self.assertEqual(len(self.pipe.inflight_tools), 1)
        self.assertIn("wema_get_balance", json.dumps(self.pipe.inflight_tools))
        gate.set()
        await asyncio.gather(*list(self.pipe._background_tasks), return_exceptions=True)
        self.assertEqual(self.pipe.inflight_tools, {})

    async def test_result_is_reported_naturally(self):
        await self.dispatch({"status": "ok", "message": "Balance 5,000", "data": {"balance": 5000}},
                            name="wema_get_balance")
        self.assertTrue(self.announced, "result must be announced")
        self.assertEqual(self.announced[0], "Let me pull that up for you.")

    async def test_failure_offers_a_ticket_gracefully(self):
        await self.dispatch({"status": "failed", "message": "The Wema service returned HTTP 500."})
        self.assertTrue(self.announced)
        self.assertNotIn("500", self.announced[0])  # no raw internals spoken

    async def test_prepare_result_triggers_confirmation_with_exact_digits(self):
        self.pipe.banking = FakeBanking({
            "status": "prepared",
            "data": {"operation_id": "op-1", "preview": {"package_name": "100 naira airtime",
                    "network": "MTN", "amount": "100", "phone_number": "08112812216"}},
        })
        call = ActionToolCall("c1", "wema_prepare_airtime_purchase", {"amount": "100"})
        await self.pipe._dispatch_background_tool(self.attempt(), call)
        await asyncio.gather(*list(self.pipe._background_tasks), return_exceptions=True)
        self.assertTrue(self.pipe.pending_action is not None)
        spoken = self.announced[-1]
        self.assertIn("0. 8. 1. 1. 2. 8. 1. 2. 2. 1. 6", spoken)  # read back digit by digit
        self.assertEqual(self.pipe.pending_action.call.arguments["operation_id"], "op-1")

    async def test_natural_line_rotates_when_the_model_is_unavailable(self):
        self.pipe.llm = ScriptedLLM()  # will be broken below
        self.pipe.llm.stream = AsyncMock(side_effect=RuntimeError("llm down"))
        lines = set()
        for _ in range(8):
            lines.add(await self.pipe._natural_line("acknowledge_tool_start", {}))
        self.assertGreater(len(lines), 1, "fallback must not always be the same phrase")

    async def test_background_tool_registers_with_engagement_and_stops_after(self):
        self.pipe.banking = FakeBanking({"status": "ok"})
        call = ActionToolCall("c1", "wema_get_balance", {"source_account": "0000000001"})
        await self.pipe._dispatch_background_tool(self.attempt(), call)
        # the engagement can see the in-flight tool, so it can speak into the gap
        self.assertIsNotNone(self.pipe.engagement.operation)
        candidate = self.pipe.engagement.opportunity()
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate[0], "tool_started")
        await asyncio.gather(*list(self.pipe._background_tasks), return_exceptions=True)
        # once it lands, no more interim updates are offered
        self.assertIsNone(self.pipe.engagement.operation)

    async def test_no_duplicate_ack_when_engagement_is_on(self):
        self.pipe.banking = FakeBanking({"status": "ok"})
        await self.pipe._dispatch_background_tool(
            self.attempt(), ActionToolCall("c1", "wema_get_balance", {}))
        # engagement owns the acknowledgement line, so _speak_text must stay quiet
        self.assertEqual(self.spoken, [])
        await asyncio.gather(*list(self.pipe._background_tasks), return_exceptions=True)


class ConfirmationLoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()

        async def send(message):
            pass

        self.pipe = ConversationPipeline(
            replace(LabConfig(), engagement_enabled=True, memory_compaction_enabled=False),
            TraceRecorder(Path(self.tmp.name)), send, transport="livekit")
        self.spoken = []

        async def fake_speak(attempt, text, *, phase):
            self.spoken.append(phase)

        self.pipe._speak_text = AsyncMock(side_effect=fake_speak)

    async def asyncTearDown(self):
        await self.pipe.close()
        self.tmp.cleanup()

    def pending(self):
        a = Attempt("one", "yes", False, authorized=True, turn_id="one")
        a.authorization_event.set()
        self.pipe.attempt = a
        self.pipe.turn_id = "one"
        self.pipe.final_text = "hmm, maybe later?"
        self.pipe.pending_action = PendingAction(
            ActionToolCall("c1", "wema_execute_prepared", {"operation_id": "op-1"}), "Do you confirm?")
        self.pipe.banking = FakeBanking({"status": "ok"})
        return a

    async def test_bank_confirmation_uses_the_natural_classifier(self):
        self.pending()
        with patch("latency_lab.pipeline.confirmation_decision",
                   new=AsyncMock(return_value="confirmed")) as classify:
            await self.pipe._handle_pending_action(self.pipe.attempt)
        self.assertEqual(classify.await_count, 1, "bank path must use the LLM classifier")

    async def test_unclear_is_bounded_and_never_loops(self):
        a = self.pending()
        with patch("latency_lab.pipeline.confirmation_decision",
                   new=AsyncMock(return_value="unclear")):
            for _ in range(3):
                self.pipe.final_text = "hmm"
                await self.pipe._handle_pending_action(a)
        self.assertIn("tool_confirmation_repeated", self.spoken)
        self.assertIn("tool_confirmation_abandoned", self.spoken)
        self.assertIsNone(self.pipe.pending_action, "must drop the pending action, not loop")


if __name__ == "__main__":
    unittest.main()
