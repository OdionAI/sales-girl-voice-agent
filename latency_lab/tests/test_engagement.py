import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from latency_lab.action_tools import ActionToolCall
from latency_lab.agentic import KnowledgeMatch, KnowledgeResult
from latency_lab.config import LabConfig, apply_runtime_overrides
from latency_lab.pipeline import Attempt, ConversationPipeline
from latency_lab.trace import TraceRecorder


class DecisionLLM:
    def __init__(self, *, speak=True, quiet="none", text="Let me check those accounts for you."):
        self.output = json.dumps({"speak": speak, "text": text if speak else "", "quiet": quiet})
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.requests = []

    async def stream(self, messages, **kwargs):
        self.requests.append((messages, kwargs))
        self.started.set()
        await self.release.wait()
        yield self.output


class TTS:
    def __init__(self):
        self.texts = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def stream(self, text):
        self.texts.append(text)
        self.started.set()

        async def chunks():
            await self.release.wait()
            yield b"\0\0" * 240
        return 24000, chunks()


class EngagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sent = []

        async def send(message):
            self.sent.append(message)
            if message["type"] == "tts_end":
                await self.pipe.client_event("tts_stop")
            elif message["type"] == "tts_segment_end":
                await self.pipe.client_event("tts_segment_stop")

        self.pipe = ConversationPipeline(
            replace(LabConfig(), engagement_enabled=True, memory_compaction_enabled=False),
            TraceRecorder(Path(self.temp.name)), send, transport="livekit")
        self.policy = self.pipe.engagement
        self.now = 100.0
        self.policy.clock = lambda: self.now
        self.policy.audio_until = self.now
        self.pipe.llm = DecisionLLM()
        self.pipe.tts = TTS()
        self.pipe.memory.user("old", "Please check all five account balances one by one.")
        self.pipe.memory.finish("old")
        self.release = asyncio.Event()
        self.tool_started = asyncio.Event()
        self.tasks = []

    async def asyncTearDown(self):
        self.release.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.pipe.close()
        self.temp.cleanup()

    def task(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    def assert_no_released_response(self):
        if isinstance(self.pipe._start_attempt, AsyncMock):
            self.assertTrue(all(not call.kwargs["authorized"] for call in self.pipe._start_attempt.call_args_list))
        self.assertFalse(any(message["type"] in {
            "tts_chunk", "partial_assistant_answer", "final_assistant_answer", "tool_activity"
        } for message in self.sent))

    def attempt(self):
        attempt = Attempt("one", "check balances", False, authorized=True, turn_id="one")
        attempt.authorization_event.set()
        self.pipe.attempt = attempt
        self.pipe.turn_id = "one"
        self.pipe.state = "releasing"
        return attempt

    async def test_reply_grace_waits_for_continuation_and_keeps_prior_words(self):
        self.pipe.config = replace(
            self.pipe.config,
            reply_grace_agent_ids=("leadway-amaka",),
            reply_grace_ms=500,
        )
        self.pipe.agent_context = replace(self.pipe.agent_context, agent_id="leadway-amaka")
        self.pipe._respond_to_final = AsyncMock()
        self.pipe.turn_id = "turn-0001"
        self.pipe.state = "awaiting_final"

        await self.pipe.on_final("It has been very good.")
        first_reply = self.pipe._final_response_task
        self.pipe._respond_to_final.assert_not_awaited()

        self.pipe.speech_candidate_active = True
        self.pipe.candidate_turn_id = "turn-0002"
        self.pipe.speech_ms = self.pipe.config.min_speech_ms
        await self.pipe._speech_started(1000.0, None, None)
        self.assertTrue(first_reply.cancelled())
        self.pipe._respond_to_final.assert_not_awaited()

        self.pipe.state = "awaiting_final"
        await self.pipe.on_final("You know.")
        await self.pipe._final_response_task
        self.pipe._respond_to_final.assert_awaited_once_with("You know.")
        caller_messages = [message["content"] for message in self.pipe.history
                           if message["role"] == "user"]
        self.assertEqual(caller_messages[-2:], ["It has been very good.", "You know."])

    async def test_reply_grace_does_not_delay_other_agents(self):
        self.pipe.config = replace(
            self.pipe.config,
            reply_grace_agent_ids=("leadway-amaka",),
            reply_grace_ms=500,
        )
        self.pipe.agent_context = replace(self.pipe.agent_context, agent_id="wema-saw")
        self.pipe._respond_to_final = AsyncMock()
        self.pipe.turn_id = "turn-0001"
        self.pipe.state = "awaiting_final"

        await self.pipe.on_final("Hello.")
        await asyncio.wait_for(self.pipe._final_response_task, timeout=0.1)
        self.pipe._respond_to_final.assert_awaited_once_with("Hello.")

    async def pending(self):
        attempt = self.attempt()
        call = ActionToolCall("balance", "wema_account_balance", {"account": "test-account"})

        async def execute():
            self.tool_started.set()
            await self.release.wait()
            body = {"status": "success", "data": {"balance": 1234}}
            self.pipe._retain_tool_result(attempt, call, body)
            return body

        task = self.task(self.policy.run_tool(attempt, call, execute))
        await self.tool_started.wait()
        return attempt, task

    async def test_slow_tool_gets_contextual_speech_without_closing_turn(self):
        attempt, tool = await self.pending()
        await self.policy.reconsider("tool_started", attempt)
        self.assertTrue(any(m["type"] == "tts_chunk" for m in self.sent))
        self.assertIn({"type": "tts_segment_end"}, self.sent)
        self.assertFalse(tool.done())
        self.assertIs(self.pipe.attempt, attempt)
        self.assertFalse(attempt.audio_complete)
        self.assertEqual(self.pipe.turn_number, 0)
        self.assertNotIn("tools", self.pipe.llm.requests[0][1])

    async def test_fast_tool_finishes_without_filler(self):
        attempt = self.attempt()
        call = ActionToolCall("fast", "read", {})
        invoke = AsyncMock(return_value={"status": "success"})
        await self.policy.run_tool(attempt, call, invoke)
        self.assertIsNone(self.policy.opportunity())
        self.assertEqual(self.pipe.tts.texts, [])

    async def test_result_during_llm_generation_discards_update(self):
        attempt, tool = await self.pending()
        self.pipe.llm.release.clear()
        decision = self.task(self.policy.reconsider("tool_started", attempt))
        self.policy.decision_task = decision
        await self.pipe.llm.started.wait()
        self.release.set()
        await tool
        self.assertTrue(decision.cancelled())
        self.assertEqual(self.pipe.tts.texts, [])

    async def test_result_during_tts_generation_discards_audio(self):
        attempt, tool = await self.pending()
        self.pipe.tts.release.clear()
        decision = self.task(self.policy.reconsider("tool_started", attempt))
        self.policy.decision_task = decision
        await self.pipe.tts.started.wait()
        self.release.set()
        await tool
        self.assertTrue(decision.cancelled())
        self.assertFalse(any(m["type"] == "tts_chunk" for m in self.sent))

    async def test_barge_in_keeps_tool_running_and_retains_result(self):
        attempt, tool = await self.pending()
        attempt.task = tool
        await self.pipe._cancel_attempt("user_barge_in")
        self.assertFalse(self.policy.operation.task.done())
        self.release.set()
        await self.policy.operation.task
        self.assertIn('"balance": 1234', json.dumps(self.pipe.history).replace('\\"', '"'))
        self.assertFalse(self.policy.operation.delivered)
        self.pipe.state = "idle"
        self.assertEqual(self.policy.opportunity(), ("tool_completed", None))

    async def test_duplicate_pending_operation_is_awaited_not_replayed(self):
        attempt, first = await self.pending()
        invoke = AsyncMock()
        second = self.task(self.policy.run_tool(attempt, self.policy.operation.call, invoke))
        await asyncio.sleep(0)
        self.release.set()
        self.assertEqual(await first, await second)
        invoke.assert_not_awaited()

    async def test_different_operation_cannot_run_while_previous_pending(self):
        attempt, _ = await self.pending()
        invoke = AsyncMock()
        body = await self.policy.run_tool(attempt, ActionToolCall("new", "transfer", {}), invoke)
        self.assertEqual(body["status"], "pending")
        invoke.assert_not_awaited()

    async def test_silence_request_pinned_outside_compacted_history(self):
        self.policy.set_speech_enabled(False)
        self.pipe.llm = DecisionLLM(speak=False, quiet="until_user")
        await self.policy.reconsider("idle", None)
        self.pipe.memory.clear()
        self.pipe.llm = DecisionLLM(speak=True, quiet="none")
        await self.policy.reconsider("idle", None)
        self.assertEqual(self.policy.quiet, "until_user")
        self.assertEqual(self.pipe.tts.texts, [])
        self.policy.set_speech_enabled(True)
        await self.policy.reconsider("idle", None)
        self.assertEqual(self.policy.quiet, "none")
        self.assertEqual(len(self.pipe.tts.texts), 1)

    async def test_until_result_suppresses_progress_but_allows_actual_outcome(self):
        attempt, tool = await self.pending()
        self.policy.quiet = "until_result"
        self.pipe.llm = DecisionLLM(quiet="until_result")
        await self.policy.reconsider("tool_wait", attempt)
        self.assertEqual(self.pipe.tts.texts, [])
        attempt.task = tool
        await self.pipe._cancel_attempt("barge_in")
        self.release.set()
        await self.policy.operation.task
        self.pipe.state = "idle"
        await self.policy.reconsider("tool_completed", None)
        self.assertEqual(len(self.pipe.tts.texts), 1)

    async def test_idle_speech_is_not_a_fake_customer_turn(self):
        before = [m for m in self.pipe.history if m["role"] == "user"]
        await self.policy.reconsider("idle", None)
        self.assertEqual(before, [m for m in self.pipe.history if m["role"] == "user"])
        self.assertIsNone(self.pipe.attempt)
        self.assertEqual(self.pipe.turn_number, 0)
        self.assertIsNone(self.policy.opportunity())

    async def test_invalid_decision_or_timeout_is_silent(self):
        self.pipe.llm.output = "not valid json"
        await self.policy.reconsider("idle", None)
        self.assertEqual(self.pipe.tts.texts, [])
        self.policy.config = replace(self.policy.config, engagement_generation_timeout_seconds=.01)
        self.pipe.llm.release.clear()
        await self.policy.reconsider("idle", None)
        self.assertEqual(self.pipe.tts.texts, [])

    async def test_new_caller_speech_invalidates_generated_audio(self):
        self.pipe.tts.release.clear()
        decision = self.task(self.policy.reconsider("idle", None))
        self.policy.decision_task = decision
        await self.pipe.tts.started.wait()
        self.policy.invalidate()
        await asyncio.gather(decision, return_exceptions=True)
        self.assertEqual(self.sent, [])

    async def test_audio_drain_does_not_close_unfinished_attempt(self):
        attempt = self.attempt()
        await self.pipe.client_event("tts_stop")
        self.assertIs(self.pipe.attempt, attempt)
        self.assertEqual(self.pipe.state, "releasing")
        attempt.audio_complete = True
        await self.pipe.client_event("tts_stop")
        self.assertIsNone(self.pipe.attempt)

    async def test_no_opportunity_during_customer_or_agent_audio(self):
        self.pipe.speech_candidate_active = True
        self.assertIsNone(self.policy.opportunity())
        self.pipe.speech_candidate_active = False
        self.policy.audio_sent(10)
        self.now += 9
        self.assertIsNone(self.policy.opportunity())
        self.now += 2
        self.assertEqual(self.policy.opportunity(), ("idle", None))

    async def test_close_cancels_policy_and_pending_operation(self):
        _, _ = await self.pending()
        self.policy.start()
        await self.policy.close()
        self.assertTrue(self.policy.operation.task.cancelled())
        self.assertTrue(self.policy.loop_task.cancelled())
        self.assertIsNone(self.policy.opportunity())

    async def test_tool_waits_until_initial_ack_audio_is_fully_played(self):
        attempt = self.attempt()
        call = ActionToolCall("read", "wema_get_balance", {})

        class ToolLLM:
            async def stream(self, messages, **kwargs):
                yield "I'll check that for you. "
                yield call

        self.pipe.llm = ToolLLM()
        self.pipe.tts.release.clear()
        calls = await self.pipe._stream_llm_and_tts(attempt, [], phase="prompt_first")
        await self.pipe.tts.started.wait()
        bank_started = asyncio.Event()

        async def execute(tool_call, *, on_result=None):
            self.assertIn({"type": "tts_segment_end"}, self.sent)
            bank_started.set()
            result = {"status": "success", "data": {"balance": "100.00"}}
            if on_result:
                on_result(result)
            return result

        self.pipe.banking.execute = execute
        self.pipe._stream_llm_and_tts = AsyncMock(return_value=[])
        bank_task = self.task(self.pipe._run_banking_tools(attempt, [], calls))
        await asyncio.sleep(0)
        self.assertFalse(bank_started.is_set())
        self.assertFalse(attempt.speech_tasks[0].done())
        self.pipe.tts.release.set()
        await bank_task
        self.assertTrue(bank_started.is_set())

    async def test_five_sequential_accounts_report_every_result(self):
        attempt = self.attempt()
        calls = [ActionToolCall(str(i), "wema_get_balance", {"account": str(i)}) for i in range(5)]
        completed = []

        async def execute(call, on_result):
            completed.append(call.arguments["account"])
            body = {"status": "success", "data": {"account": call.arguments["account"], "balance": len(completed) * 100}}
            on_result(body)
            return body

        async def followup(attempt, messages, **kwargs):
            result = json.loads(messages[-1]["content"])["data"]
            await self.pipe._speak_text(attempt, f"Account {result['account']}: {result['balance']}.", phase="bank_tool_result")
            return [calls[len(completed)]] if len(completed) < 5 else []

        self.pipe.banking.execute = execute
        self.pipe._stream_llm_and_tts = followup
        await self.pipe._run_banking_tools(attempt, [], [calls[0]])
        self.assertEqual(completed, ["0", "1", "2", "3", "4"])
        self.assertEqual(len(self.pipe.tts.texts), 10)
        self.assertEqual(
            self.pipe.tts.texts[::2],
            ["One moment while I check that for you."] * 5,
        )
        self.assertIn("Account 4: 500.", attempt.answer)

    async def test_owned_account_selection_keeps_balance_tool_available_once(self):
        attempt = self.attempt()
        call = ActionToolCall("list", "wema_get_balance", {})
        self.pipe.action_tools = [{"type": "function", "function": {"name": "wema_get_balance"}}]
        self.pipe.banking.execute = AsyncMock(return_value={"status": "needs_input", "data": {
            "accounts": [{"source_account": "0123456789", "currency": "NGN"}]}})
        self.pipe._stream_llm_and_tts = AsyncMock(side_effect=[[call], []])
        await self.pipe._run_banking_tools(attempt, [], [call])
        followups = self.pipe._stream_llm_and_tts.call_args_list
        self.assertEqual(followups[0].kwargs["tools"], self.pipe.action_tools)
        self.assertIsNone(followups[1].kwargs["tools"])

    async def test_auth_failure_or_invalid_selection_never_reenables_tools(self):
        for body in ({"status": "failed", "data": {"accounts": [{"source_account": "0123456789"}]}},
                     {"status": "needs_input", "data": {"accounts": [{"source_account": "invalid"}]}}):
            attempt = self.attempt()
            self.pipe.banking.execute = AsyncMock(return_value=body)
            self.pipe._stream_llm_and_tts = AsyncMock(return_value=[])
            await self.pipe._run_banking_tools(attempt, [], [ActionToolCall("list", "wema_get_balance", {})])
            self.assertIsNone(self.pipe._stream_llm_and_tts.call_args.kwargs["tools"])

    async def test_missing_next_call_is_reconsidered_read_only_without_replaying_completed_account(self):
        attempt = self.attempt()
        first = ActionToolCall("one", "wema_get_balance", {"source_account": "0123456789"})
        second = ActionToolCall("two", "wema_get_balance", {"source_account": "1123456789"})
        unsafe = ActionToolCall("unsafe", "wema_execute_prepared", {"operation_id": "not-authorized"})
        self.pipe.action_tools = [{"type": "function", "function": {"name": "wema_get_balance"}}]
        self.pipe.banking.execute = AsyncMock(return_value={"status": "success", "data": {"balance": 100}})
        self.pipe._stream_llm_and_tts = AsyncMock(side_effect=lambda *args, **kwargs: [])

        class ContinuationLLM:
            rounds = 0

            async def stream(inner, messages, **kwargs):
                self.assertEqual(messages[0]["role"], "system")
                self.assertFalse(any(message["role"] == "system" for message in messages[1:]))
                inner.rounds += 1
                if inner.rounds == 1:
                    for call in (unsafe, first, second):
                        yield call

        self.pipe.llm = ContinuationLLM()
        await self.pipe._run_banking_tools(attempt, [], [first])
        self.assertEqual([call.args[0] for call in self.pipe.banking.execute.call_args_list], [first, second])

    async def test_feature_off_preserves_legacy_tool_cancellation(self):
        self.policy.enabled = False
        attempt = self.attempt()
        call = ActionToolCall("legacy", "wema_get_balance", {})
        cancelled = asyncio.Event()

        async def execute():
            try:
                self.tool_started.set()
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        task = self.task(self.policy.run_tool(attempt, call, execute))
        await self.tool_started.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(self.policy.operation)

    async def test_unknown_operation_outcome_is_retained_and_never_retried(self):
        attempt = self.attempt()
        call = ActionToolCall("uncertain", "create_ticket", {})
        invoke = AsyncMock(side_effect=TimeoutError())
        body = await self.policy.run_tool(attempt, call, invoke)
        self.assertEqual(body["status"], "unknown")
        self.assertEqual(await self.policy.run_tool(attempt, call, invoke), body)
        invoke.assert_awaited_once()
        self.assertIn("unknown", json.dumps(self.policy.task_state()))

    async def test_silence_decision_does_not_poll_completed_operation_forever(self):
        attempt, tool = await self.pending()
        attempt.task = tool
        await self.pipe._cancel_attempt("barge_in")
        self.release.set()
        await self.policy.operation.task
        self.pipe.state = "idle"
        self.pipe.llm = DecisionLLM(speak=False, quiet="until_user")
        await self.policy.reconsider("tool_completed", None)
        self.assertNotEqual(self.policy.opportunity(), ("tool_completed", None))
        self.assertFalse(self.policy.operation.delivered)

    async def test_speculative_tool_never_executes_before_authorized_final(self):
        selected = asyncio.Event()
        call = ActionToolCall("read", "wema_get_balance", {"source_account": "0123456789"})

        async def generate(attempt, messages, **kwargs):
            selected.set()
            return [call]

        self.pipe._stream_llm_and_tts = generate
        self.pipe._run_banking_tools = AsyncMock()
        await self.pipe._start_attempt("check my balance", speculative=True, authorized=False)
        attempt = self.pipe.attempt
        await selected.wait()
        await asyncio.sleep(0)
        self.pipe._run_banking_tools.assert_not_awaited()
        await self.pipe._cancel_attempt("final_transcript_changed")
        self.pipe._run_banking_tools.assert_not_awaited()
        self.assertTrue(attempt.task.cancelled())

    async def test_continuation_failure_does_not_abort_already_spoken_results(self):
        attempt = self.attempt()
        call = ActionToolCall("read", "wema_get_balance", {"source_account": "0123456789"})
        self.pipe.action_tools = [{"type": "function", "function": {"name": "wema_get_balance"}}]
        self.pipe.banking.execute = AsyncMock(return_value={"status": "success", "data": {"balance": 100}})
        self.pipe._stream_llm_and_tts = AsyncMock(return_value=[])

        class FailingLLM:
            async def stream(self, *args, **kwargs):
                raise TimeoutError()
                yield

        self.pipe.llm = FailingLLM()
        await self.pipe._run_banking_tools(attempt, [], [call])
        self.pipe.banking.execute.assert_awaited_once()

    def responses(self, *outputs):
        class ScriptedLLM:
            def __init__(inner):
                inner.requests = []
                inner.outputs = iter(outputs)

            async def stream(inner, messages, **kwargs):
                inner.requests.append((messages, kwargs))
                for output in next(inner.outputs):
                    yield output

        self.pipe.llm = ScriptedLLM()
        return self.pipe.llm

    async def caller(self, text):
        await self.pipe.text(text)
        if self.pipe._final_response_task:
            await self.pipe._final_response_task
        attempt = self.pipe.attempt
        if attempt and attempt.task:
            await attempt.task

    async def test_normal_reply_has_one_generation_and_no_preference_review(self):
        llm = self.responses(["The answer is 4."])
        await self.caller("What is 2 plus 2?")
        self.assertEqual(len(llm.requests), 1)
        self.assertIn({"type": "final_assistant_answer", "content": "The answer is 4."}, self.sent)
        self.assertTrue(self.policy.speech_enabled)

    async def test_gate_tool_closes_without_tts_and_keeps_transcript(self):
        llm = self.responses([ActionToolCall("quiet", "set_speech_enabled",
                                             {"enabled": False, "response": ""})])
        self.pipe.turn_id = "quiet-turn"
        self.pipe.state = "awaiting_final"
        await self.pipe.on_final("I'd rather you just listen for the moment.")
        await self.pipe._final_response_task
        await self.pipe.attempt.task
        self.assertFalse(self.policy.speech_enabled)
        self.assertEqual(self.pipe.tts.texts, [])
        self.assertIn({"type": "final_user_request", "content": "I'd rather you just listen for the moment."}, self.sent)
        self.assertEqual(len(llm.requests), 1)
        self.assert_no_released_response()

    async def test_quiet_input_is_remembered_and_no_business_tool_can_run(self):
        self.policy.set_speech_enabled(False)
        self.pipe._run_banking_tools = AsyncMock()
        llm = self.responses(["An unwanted answer.", ActionToolCall("bank", "wema_account_balance", {})])
        await self.caller("My notebook is green.")
        self.assertIn("My notebook is green.", json.dumps(self.pipe.memory.context()))
        self.assertEqual([t["function"]["name"] for t in llm.requests[0][1]["tools"]], ["set_speech_enabled"])
        self.assertEqual(llm.requests[0][1]["tool_choice"], "required")
        self.pipe._run_banking_tools.assert_not_awaited()
        self.assertEqual(self.pipe.tts.texts, [])
        self.assert_no_released_response()

    async def test_resume_tool_speaks_its_model_reply_without_another_request(self):
        self.policy.set_speech_enabled(False)
        llm = self.responses([], [], [ActionToolCall("resume", "set_speech_enabled",
            {"enabled": True, "response": "Your notebook is green and the meeting is Friday."})])
        await self.caller("My notebook is green.")
        await self.caller("The meeting is Friday.")
        self.assert_no_released_response()
        await self.caller("Over to you now. What did you hear?")
        self.assertEqual(len(llm.requests), 3)
        self.assertIn("My notebook is green.", json.dumps(llm.requests[-1][0]))
        self.assertIn("The meeting is Friday.", json.dumps(llm.requests[-1][0]))
        self.assertTrue(self.policy.speech_enabled)
        self.assertEqual(self.pipe.tts.texts, ["Your notebook is green and the meeting is Friday."])
        self.assertEqual(sum(m["type"] == "final_assistant_answer" for m in self.sent), 1)

    async def test_speculative_gate_cannot_change_state_until_final(self):
        llm = self.responses([ActionToolCall("quiet", "set_speech_enabled",
                                             {"enabled": False, "response": ""})])
        self.pipe.turn_id = "spec"
        await self.pipe._start_attempt("Please listen.", speculative=True, authorized=False)
        attempt = self.pipe.attempt
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertTrue(self.policy.speech_enabled)
        self.assertIsNotNone(attempt.speech_control)
        self.pipe.final_text = "Please listen."
        await self.pipe._respond_to_final("Please listen.")
        await attempt.task
        self.assertFalse(self.policy.speech_enabled)
        self.assertEqual(len(llm.requests), 1)
        self.assert_no_released_response()

    async def test_cancelled_speculative_gate_never_applies(self):
        self.responses([ActionToolCall("quiet", "set_speech_enabled",
                                       {"enabled": False, "response": ""})])
        self.pipe.turn_id = "spec"
        await self.pipe._start_attempt("Please listen.", speculative=True, authorized=False)
        for _ in range(10):
            await asyncio.sleep(0)
        await self.pipe._cancel_attempt("changed transcript")
        self.assertTrue(self.policy.speech_enabled)

    async def test_invalid_gate_arguments_cannot_resume_or_speak(self):
        for args in ({"enabled": "true", "response": "Hello"},
                     {"enabled": True}, {"enabled": False, "response": "Hello"}):
            self.policy.set_speech_enabled(False)
            self.responses([ActionToolCall("bad", "set_speech_enabled", args)])
            await self.caller("More background speech.")
            self.assertFalse(self.policy.speech_enabled)
            self.assert_no_released_response()

    async def test_close_gate_does_not_wait_for_pending_tts(self):
        self.pipe.tts.release.clear()
        class PreambleLLM:
            async def stream(inner, *args, **kwargs):
                yield "An unwanted preamble."
                await self.pipe.tts.started.wait()
                yield ActionToolCall("quiet", "set_speech_enabled", {"enabled": False, "response": ""})
        self.pipe.llm = PreambleLLM()
        await asyncio.wait_for(self.caller("Please just listen."), 1)
        self.assertFalse(self.policy.speech_enabled)
        self.assertFalse(any(m["type"] == "tts_chunk" for m in self.sent))

    async def test_slow_quiet_generation_does_not_block_next_input(self):
        self.policy.set_speech_enabled(False)
        started = asyncio.Event()
        class SlowLLM:
            async def stream(inner, messages, **kwargs):
                if messages[-1]["content"] == "My notebook is green.":
                    started.set()
                    await asyncio.Event().wait()
                yield ActionToolCall("resume", "set_speech_enabled",
                    {"enabled": True, "response": "Your notebook is green."})
        self.pipe.llm = SlowLLM()
        await self.pipe.text("My notebook is green.")
        await started.wait()
        await asyncio.wait_for(self.caller("Your turn to speak."), 1)
        self.assertIn("My notebook is green.", json.dumps(self.pipe.memory.context()))
        self.assertTrue(self.policy.speech_enabled)

    async def test_timer_cannot_reopen_output_gate(self):
        self.policy.set_speech_enabled(False)
        self.policy.caller_final("A movie character asks a question.")
        self.pipe.llm = DecisionLLM(speak=True, quiet="none")
        await self.policy.reconsider("idle", None)
        self.assertFalse(self.policy.speech_enabled)
        self.assert_no_released_response()

    async def test_quiet_partials_are_still_published(self):
        self.policy.set_speech_enabled(False)
        self.pipe.turn_id = "partial"
        self.pipe.state = "speaking"
        await self.pipe.on_partial("I am explaining something", "")
        self.assertIn({"type": "partial_user_request", "content": "I am explaining something"}, self.sent)
        if self.pipe.stability_task:
            await self.pipe.stability_task
        self.assertEqual(self.pipe.tts.texts, [])

    async def test_output_boundary_drops_late_audio_after_gate_close(self):
        attempt = self.attempt()
        self.policy.set_speech_enabled(False)
        await self.pipe._send_audio(24000, b"\\0\\0" * 240, attempt)
        await self.pipe._speak_text(attempt, "Late result.", phase="late_tool")
        await self.pipe._commit_attempt_text(attempt)
        self.assert_no_released_response()
        self.assertEqual(self.pipe.tts.texts, [])

    async def test_gate_resume_does_not_confirm_pending_action(self):
        from latency_lab.action_tools import PendingAction
        pending = PendingAction(ActionToolCall("payment", "wema_execute_prepared",
            {"operation_id": "already-prepared"}), "Do you confirm?")
        self.pipe.pending_action = pending
        self.policy.set_speech_enabled(False)
        self.pipe.banking.execute = AsyncMock()
        llm = self.responses([ActionToolCall("resume", "set_speech_enabled",
            {"enabled": True, "response": "I'm listening."})])
        await self.caller("You may answer me again.")
        self.pipe.banking.execute.assert_not_awaited()
        self.assertIs(self.pipe.pending_action, pending)
        self.assertEqual(len(llm.requests), 1)

    async def test_feature_off_does_not_advertise_the_gate_tool(self):
        config = apply_runtime_overrides(self.pipe.config, {"engagement_enabled": "false"})
        self.pipe.config = config
        self.policy.enabled = False
        llm = self.responses(["4."])
        await self.caller("What is 2 plus 2?")
        self.assertEqual(len(llm.requests), 1)
        self.assertNotIn("set_speech_enabled", [t["function"]["name"] for t in llm.requests[0][1]["tools"]])

    def test_session_flag_cannot_enable_master_disabled_feature(self):
        disabled = replace(self.pipe.config, engagement_enabled=False)
        for value in (True, "true", "invalid", None):
            self.assertFalse(apply_runtime_overrides(disabled, {"engagement_enabled": value}).engagement_enabled)
        for value in (False, "false"):
            self.assertFalse(apply_runtime_overrides(self.pipe.config, {"engagement_enabled": value}).engagement_enabled)
        self.assertTrue(apply_runtime_overrides(self.pipe.config, {}).engagement_enabled)

    async def test_knowledge_result_survives_interruption_in_real_generation_path(self):
        class LookupLLM:
            async def stream(self, *args, **kwargs):
                yield "Let me check that for you."

        async def search(query):
            self.tool_started.set()
            await self.release.wait()
            return KnowledgeResult(query=query, status="success", matches=(
                KnowledgeMatch("Hours", "Verified hours are 9 AM to 5 PM", .9),))

        self.pipe.llm = LookupLLM()
        self.pipe.knowledge.availability = lambda: (True, "attached")
        self.pipe.knowledge.search = search
        self.pipe.final_text = "What are your opening hours?"
        await self.pipe._start_attempt(self.pipe.final_text, speculative=False, authorized=True)
        await asyncio.wait_for(self.tool_started.wait(), 1)
        await self.pipe._cancel_attempt("caller_interrupt")
        self.assertFalse(self.policy.operation.task.done())
        self.release.set()
        await self.policy.operation.task
        self.assertIn("Verified hours", json.dumps(self.pipe.history))
        self.assertEqual(self.policy.operation.snapshot()["result"]["status"], "success")


if __name__ == "__main__":
    unittest.main()
