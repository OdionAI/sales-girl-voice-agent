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
        self.policy.caller_final("Please keep quiet.")
        self.pipe.llm = DecisionLLM(speak=False, quiet="until_user")
        await self.policy.reconsider("idle", None)
        self.pipe.memory.clear()
        self.pipe.llm = DecisionLLM(speak=True, quiet="none")
        await self.policy.reconsider("idle", None)
        self.assertEqual(self.policy.quiet, "until_user")
        self.assertEqual(self.pipe.tts.texts, [])
        self.policy.caller_final("You can talk again, please.")
        await self.policy.reconsider("idle", None)
        self.assertEqual(self.policy.quiet, "none")
        self.assertEqual(len(self.pipe.tts.texts), 1)

    async def test_until_result_suppresses_progress_but_allows_actual_outcome(self):
        attempt, tool = await self.pending()
        self.policy.caller_final("Just tell me when finished.")
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

    async def test_tool_starts_while_initial_ack_audio_is_still_generating(self):
        attempt = self.attempt()
        call = ActionToolCall("read", "wema_account_balance", {})

        class ToolLLM:
            async def stream(self, messages, **kwargs):
                yield "I'll check that for you. "
                yield call

        self.pipe.llm = ToolLLM()
        self.pipe.tts.release.clear()
        calls = await self.pipe._stream_llm_and_tts(attempt, [], phase="prompt_first")
        await self.pipe.tts.started.wait()
        invoke = AsyncMock(return_value={"status": "success"})
        await self.policy.run_tool(attempt, calls[0], invoke)
        invoke.assert_awaited_once()
        self.assertFalse(attempt.speech_tasks[0].done())
        self.pipe.tts.release.set()
        await self.pipe._finish_speech(attempt)

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
        self.assertEqual(len(self.pipe.tts.texts), 5)
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
        self.assertIsNone(self.policy.opportunity())
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

    async def test_quiet_request_blocks_output_but_not_transcript_or_asr_callback(self):
        self.pipe.llm.output = json.dumps({"addressed": True, "quiet": "until_user"})
        self.pipe.llm.release.clear()
        self.pipe.turn_id, self.pipe.state = "quiet", "awaiting_final"
        self.pipe._start_attempt = AsyncMock()
        self.pipe.conversations.add_user = AsyncMock()
        await asyncio.wait_for(self.pipe.on_final("Please stay quiet."), .1)
        await self.pipe.llm.started.wait()
        self.assertFalse(self.policy.preference_task.done())
        self.assertIn({"type": "final_user_request", "content": "Please stay quiet."}, self.sent)
        self.assertIn("Please stay quiet.", json.dumps(self.pipe.history))
        self.pipe.conversations.add_user.assert_awaited_once_with("Please stay quiet.", turn_id="quiet")
        self.assert_no_released_response()
        self.pipe.llm.release.set()
        await self.pipe._final_response_task
        self.assertEqual(self.policy.quiet, "until_user")
        self.assert_no_released_response()
        self.assertEqual(self.pipe.tts.texts, [])
        self.assertTrue(self.pipe.memory.turn("quiet").closed)

    async def test_background_dialogue_is_retained_without_resuming_or_starting_tools(self):
        self.policy.quiet = "until_user"
        self.pipe.llm.output = json.dumps({"addressed": False, "quiet": "none"})
        self.pipe.turn_id, self.pipe.state = "movie", "awaiting_final"
        self.pipe._start_attempt = AsyncMock()
        await self.pipe.on_final("I will have Stevens draw up the contract at once.")
        await self.pipe._final_response_task
        self.assert_no_released_response()
        self.assertEqual(self.policy.quiet, "until_user")
        self.assertEqual(self.pipe.state, "idle")
        self.assertEqual(self.sent, [{"type": "final_user_request",
                                     "content": "I will have Stevens draw up the contract at once."}])
        self.assertIn("Stevens", json.dumps(self.pipe.history))
        self.assertTrue(self.pipe.memory.turn("movie").listening_only)
        self.assertIn("Listening-only speech", json.dumps(self.pipe._recent_history()))
        self.assertIsNone(self.policy.opportunity())

    async def test_resume_generation_receives_all_quiet_speech_once(self):
        self.policy.quiet = "until_user"
        self.pipe.llm.output = json.dumps({"addressed": False, "quiet": "until_user"})
        self.pipe._stream_llm_and_tts = AsyncMock(return_value=[])
        facts = ["My notebook is green.", "The meeting starts at 3 PM.", "There are 5 guests."]
        for text in facts:
            await self.pipe.text(text)
            await self.pipe._final_response_task
        self.assertEqual(self.pipe._stream_llm_and_tts.await_count, 3)
        self.assert_no_released_response()
        self.assertEqual(self.pipe.tts.texts, [])
        self.pipe.llm.output = json.dumps({"addressed": True, "quiet": "none"})
        resume = "Okay, now you can talk. What did I just tell you?"
        await self.pipe.text(resume)
        await self.pipe._final_response_task
        if self.pipe.attempt:
            await self.pipe.attempt.task
        messages = self.pipe._stream_llm_and_tts.call_args.args[1]
        for text in facts:
            self.assertEqual(sum(text in str(message.get("content")) for message in messages), 1)
        self.assertEqual(messages[-1], {"role": "user", "content": resume})
        self.assertEqual(self.policy.quiet, "none")
        self.assertEqual([m["content"] for m in self.pipe.history if m["role"] == "user"][-4:], facts + [resume])

    async def test_failed_review_still_publishes_and_retains_input(self):
        self.policy.quiet = "until_user"
        self.pipe.llm.output = "invalid"
        self.pipe.turn_id, self.pipe.state = "failed", "awaiting_final"
        self.pipe._start_attempt = AsyncMock()
        await self.pipe.on_final("The meeting is tomorrow.")
        await self.pipe._final_response_task
        self.assertIn({"type": "final_user_request", "content": "The meeting is tomorrow."}, self.sent)
        self.assertIn("The meeting is tomorrow.", json.dumps(self.pipe.history))
        self.assert_no_released_response()
        self.assertTrue(self.policy.preference_uncertain)

    async def test_quiet_partials_are_published_without_speculative_tts(self):
        self.policy.quiet = "until_user"
        self.pipe.config = replace(self.pipe.config, stable_partial_ms=0)
        self.pipe.turn_id, self.pipe.state = "partial", "speaking"
        self.pipe._start_attempt = AsyncMock()
        await self.pipe.on_partial("My notebook is green", "My notebook is green")
        await self.pipe.stability_task
        self.assertIn({"type": "partial_user_request", "content": "My notebook is green"}, self.sent)
        self.pipe._start_attempt.assert_not_awaited()

    async def test_next_utterance_is_retained_while_prior_review_pending(self):
        self.policy.quiet = "until_user"
        self.pipe.llm.output = json.dumps({"addressed": False, "quiet": "until_user"})
        self.pipe.llm.release.clear()
        self.pipe._start_attempt = AsyncMock()
        await self.pipe.text("My notebook is green.")
        await self.pipe.llm.started.wait()
        first_response = self.pipe._final_response_task
        await asyncio.wait_for(self.pipe.text("The meeting starts at 3 PM."), .1)
        self.assertTrue(first_response.cancelled())
        self.assertTrue(self.pipe.memory.turn("turn-0001").closed)
        self.assertIn("My notebook is green.", json.dumps(self.pipe.history))
        self.assertIn("The meeting starts at 3 PM.", json.dumps(self.pipe.history))
        self.pipe.llm.release.set()
        await self.pipe._final_response_task
        self.assert_no_released_response()

    async def test_new_microphone_speech_can_interrupt_pending_review(self):
        self.policy.quiet = "until_user"
        self.pipe.llm.output = json.dumps({"addressed": True, "quiet": "none"})
        self.pipe.llm.release.clear()
        self.pipe._start_attempt = AsyncMock()
        self.pipe.stt.append = AsyncMock()
        self.pipe.stt.commit = AsyncMock()
        await self.pipe.text("Okay, now you can talk.")
        await self.pipe.llm.started.wait()
        response = self.pipe._final_response_task
        await self.pipe.audio(b"\xff\x3f" * 8000)
        self.pipe.stt.append.assert_awaited_once()
        self.assertTrue(response.cancelled())
        self.assertEqual(self.pipe.state, "speaking")
        await self.pipe.on_partial("Actually, one more thing", "Actually, one more thing")
        self.pipe.llm.release.set()
        await self.policy.preference_task
        self.assert_no_released_response()
        self.assertIn({"type": "partial_user_request", "content": "Actually, one more thing"}, self.sent)

    async def test_shutdown_cancels_pending_response_review(self):
        self.pipe.llm.release.clear()
        await self.pipe.text("Stay quiet.")
        await self.pipe.llm.started.wait()
        response = self.pipe._final_response_task
        await self.pipe.close()
        self.assertTrue(response.cancelled())
        self.assertTrue(self.policy.preference_task.cancelled())

    async def test_clear_request_can_resume_after_background_dialogue(self):
        self.policy.quiet = "until_user"
        self.pipe.llm.output = json.dumps({"addressed": True, "quiet": "none"})
        self.assertTrue(await self.policy.accept_caller_turn("SAW, can you help me with my account again?"))
        self.assertEqual(self.policy.quiet, "none")

    async def test_failed_review_preserves_quiet_and_blocks_response(self):
        self.policy.quiet = "until_user"
        self.pipe.llm.output = "invalid"
        self.assertFalse(await self.policy.accept_caller_turn("unrelated speech"))
        self.assertEqual(self.policy.quiet, "until_user")
        self.assertTrue(self.policy.preference_uncertain)

    async def test_resume_does_not_queue_behind_slow_background_review(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        class LatestLLM:
            async def stream(inner, messages, **kwargs):
                if json.loads(messages[-1]["content"])["utterance"] == "movie dialogue":
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()
                yield json.dumps({"addressed": True, "quiet": "none"})

        self.policy.quiet = "until_user"
        self.pipe.llm = LatestLLM()
        first = self.task(self.policy.accept_caller_turn("movie dialogue"))
        await started.wait()
        self.assertTrue(await asyncio.wait_for(self.policy.accept_caller_turn("Now you can talk."), .1))
        await asyncio.gather(first, return_exceptions=True)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(first.cancelled())
        self.assertEqual(self.policy.quiet, "none")

    async def test_buffered_answer_is_ready_before_review_without_leaking_output(self):
        review_started, allow = asyncio.Event(), asyncio.Event()

        class ParallelLLM:
            async def stream(inner, messages, **kwargs):
                if kwargs.get("max_tokens") == 80:
                    review_started.set()
                    await allow.wait()
                    yield json.dumps({"addressed": True, "quiet": "none"})
                else:
                    yield "Your notebook is green."

        self.pipe.llm = ParallelLLM()
        self.policy.quiet = "until_user"
        await self.pipe.text("Now you can talk. What colour was it?")
        await review_started.wait()
        await self.pipe.tts.started.wait()
        attempt = self.pipe.attempt
        await attempt.task
        self.assertTrue(attempt.held_audio)
        self.assertFalse(attempt.authorized)
        self.assert_no_released_response()
        allow.set()
        await self.pipe._final_response_task
        self.assertTrue(any(message["type"] == "tts_chunk" for message in self.sent))
        self.assertIn({"type": "final_assistant_answer", "content": "Your notebook is green."}, self.sent)
        self.assertEqual(len(self.pipe.tts.texts), 1)

    async def test_rejected_review_discards_prepared_audio_and_native_tool(self):
        allow, prepared = asyncio.Event(), asyncio.Event()
        self.pipe._run_banking_tools = AsyncMock()

        class ParallelLLM:
            async def stream(inner, messages, **kwargs):
                if kwargs.get("max_tokens") == 80:
                    await allow.wait()
                    yield json.dumps({"addressed": False, "quiet": "until_user"})
                else:
                    yield "I will check that."
                    yield ActionToolCall("blocked", "wema_get_balance", {})
                    prepared.set()

        self.policy.quiet = "until_user"
        self.pipe.llm = ParallelLLM()
        await self.pipe.text("background account discussion")
        await prepared.wait()
        await self.pipe.tts.started.wait()
        self.assert_no_released_response()
        self.pipe._run_banking_tools.assert_not_awaited()
        allow.set()
        await self.pipe._final_response_task
        self.pipe._run_banking_tools.assert_not_awaited()
        self.assert_no_released_response()
        self.assertIsNone(self.pipe.attempt)
        self.assertIn("background account discussion", json.dumps(self.pipe.history))

    async def test_session_opt_out_bypasses_review_entirely(self):
        config = apply_runtime_overrides(self.pipe.config, {"engagement_enabled": "false"})
        self.pipe.config = config
        self.policy.enabled = config.engagement_enabled
        self.pipe._start_attempt = AsyncMock()
        self.policy.accept_caller_turn = AsyncMock()
        await self.pipe.text("What is 2 plus 2?")
        self.policy.accept_caller_turn.assert_not_awaited()
        self.pipe._start_attempt.assert_awaited_once_with("What is 2 plus 2?", speculative=False, authorized=True)
        self.assertIsNone(self.pipe._final_response_task)

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
