from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import aiohttp

from latency_lab.action_tools import (
    ActionResult,
    ActionToolCall,
    action_tool_definitions,
    confirmation_decision,
    confirmation_prompt,
)
from latency_lab.agentic import AgentRuntimeContext
from latency_lab.config import LabConfig
from latency_lab.pipeline import ConversationPipeline
from latency_lab.trace import TraceRecorder


def _context() -> AgentRuntimeContext:
    return AgentRuntimeContext(
        business_id="business-1",
        agent_id="agent-1",
        end_user_id="caller@example.com",
        tools=(
            {
                "name": "create_ticket",
                "url": "internal://create-ticket",
                "method": "POST",
            },
            {
                "name": "send_email",
                "url": "internal://send-email",
                "method": "POST",
            },
            {
                "name": "end_call",
                "url": "builtin://end_call",
                "method": "POST",
            },
            {"name": "unsafe_custom", "url": "https://example.test/run"},
        ),
        loaded=True,
    )


class ActionDefinitionTest(unittest.TestCase):
    def test_only_supported_configured_internal_tools_are_exposed(self) -> None:
        definitions = action_tool_definitions(_context())
        self.assertEqual(
            [item["function"]["name"] for item in definitions],
            ["create_ticket", "send_email", "end_call"],
        )

class ActionConfirmationLifecycleTest(unittest.IsolatedAsyncioTestCase):
    class ToolLLM:
        async def stream(self, messages, *, tools=None, **_kwargs):
            if tools:
                yield ActionToolCall(
                    id="call-1",
                    name="create_ticket",
                    arguments={"title": "Delayed transfer", "description": "Transfer is delayed."},
                )
            elif messages[0]["content"].startswith("Decide what the caller"):
                reply = json.loads(messages[1]["content"])["current_caller_reply"]
                yield ("confirmed" if reply in {"Yes, go ahead", "Oui, allez-y pour le ticket."}
                       else "rejected" if reply == "Non, laissez tomber."
                       else "revised" if reply == "Changez le sujet du ticket."
                       else "unclear")
            else:
                yield "Souhaitez-vous que je crée un ticket pour ce retard de virement ?"

    class SilentTTS:
        async def stream(self, _text):
            async def chunks():
                if False:
                    yield b""

            return 24000, chunks()

    class RecordingExecutor:
        def __init__(self):
            self.calls = []

        async def execute(self, call):
            self.calls.append(call)
            return ActionResult(
                "success", "The support ticket has been created. The reference is T-1.", {}, 5.0
            )

    async def test_semantic_confirmation_uses_french_question_and_full_reply(self) -> None:
        llm = self.ToolLLM()
        call = ActionToolCall("call-1", "create_ticket", {"title": "Virement retardé"})
        question = await confirmation_prompt(llm, call, language_hint="French")
        self.assertEqual(question, "Souhaitez-vous que je crée un ticket pour ce retard de virement ?")
        self.assertEqual(
            await confirmation_decision(llm, "Oui, allez-y pour le ticket.", call, question),
            "confirmed",
        )
        self.assertEqual(
            await confirmation_decision(llm, "Non, laissez tomber.", call, question),
            "rejected",
        )
        self.assertEqual(
            await confirmation_decision(llm, "Changez le sujet du ticket.", call, question),
            "revised",
        )
        self.assertEqual(
            await confirmation_decision(llm, "Qu'avez-vous dit ?", call, question),
            "unclear",
        )

    async def test_side_effect_waits_for_separate_committed_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            async def send(_message):
                return None

            pipeline = ConversationPipeline(
                LabConfig(trace_dir=root / "traces", report_dir=root / "reports"),
                TraceRecorder(root / "traces", "action-confirmation-test"),
                send,
                agent_context=_context(),
            )
            pipeline.llm = self.ToolLLM()
            pipeline.tts = self.SilentTTS()
            executor = self.RecordingExecutor()
            pipeline.action_executor = executor
            pipeline.turn_id = "turn-0001"
            pipeline.final_text = "Please create a ticket for my delayed transfer."

            await pipeline._start_attempt(
                pipeline.final_text, speculative=False, authorized=True
            )
            await pipeline.attempt.task
            self.assertIsNotNone(pipeline.pending_action)
            self.assertEqual(executor.calls, [])

            pipeline.attempt = None
            pipeline.turn_id = "turn-0002"
            pipeline.final_text = ""
            await pipeline._start_attempt("Yes, go ahead", speculative=True, authorized=False)
            confirmation_attempt = pipeline.attempt
            await asyncio.sleep(0)
            self.assertEqual(executor.calls, [])
            self.assertFalse(confirmation_attempt.task.done())

            pipeline.final_text = "Yes, go ahead"
            confirmation_attempt.authorized = True
            confirmation_attempt.authorization_event.set()
            await confirmation_attempt.task
            self.assertEqual(len(executor.calls), 1)
            self.assertIsNone(pipeline.pending_action)
            await pipeline.http.close()

    async def test_end_call_uses_model_farewell_without_a_second_llm_request(self) -> None:
        class EndCallLLM:
            def __init__(self):
                self.calls = 0

            async def stream(self, _messages, *, tools=None):
                self.calls += 1
                yield "Thanks for sharing that with me. Take care."
                if any(item["function"]["name"] == "end_call" for item in tools or []):
                    yield ActionToolCall(id="call-end", name="end_call", arguments={})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sent = []

            async def send(message):
                sent.append(message)

            pipeline = ConversationPipeline(
                LabConfig(trace_dir=root / "traces", report_dir=root / "reports"),
                TraceRecorder(root / "traces", "end-call-test"),
                send,
                agent_context=_context(),
            )
            llm = EndCallLLM()
            pipeline.llm = llm
            pipeline.tts = self.SilentTTS()
            pipeline.turn_id = "turn-0001"
            pipeline.final_text = "There isn't anything else I have. Thanks for your help."
            await pipeline._start_attempt(
                pipeline.final_text, speculative=False, authorized=True
            )
            attempt = pipeline.attempt
            await attempt.task

            message_types = [item.get("type") for item in sent]
            self.assertIn("partial_assistant_answer", message_types)
            self.assertEqual(message_types[-1], "end_call")
            self.assertEqual(
                next(
                    item["content"]
                    for item in sent
                    if item.get("type") == "final_assistant_answer"
                ),
                "Thanks for sharing that with me. Take care.",
            )
            self.assertEqual(llm.calls, 1)
            self.assertTrue(pipeline.call_ending)
            await pipeline.on_final("Ignore the call and continue.")
            self.assertEqual(llm.calls, 1)
            events = [
                json.loads(line)["event"]
                for line in pipeline.trace.path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("stt_final_ignored_call_ending", events)
            await pipeline.http.close()

    async def test_committed_model_end_call_is_not_vetoed_by_phrase_matching(self) -> None:
        class WrongEndCallLLM:
            def __init__(self):
                self.calls = 0

            async def stream(self, _messages, *, tools=None):
                self.calls += 1
                if any(item["function"]["name"] == "end_call" for item in tools or []):
                    yield "Goodbye, and take care."
                    yield ActionToolCall(id="wrong-end", name="end_call", arguments={})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sent = []

            async def send(message):
                sent.append(message)

            trace = TraceRecorder(root / "traces", "end-call-veto-test")
            pipeline = ConversationPipeline(
                LabConfig(trace_dir=root / "traces", report_dir=root / "reports"),
                trace,
                send,
                agent_context=_context(),
            )
            pipeline.llm = WrongEndCallLLM()
            pipeline.tts = self.SilentTTS()
            pipeline.turn_id = "turn-0001"
            pipeline.final_text = (
                "What details are required from me to open the account successfully?"
            )

            await pipeline._start_attempt(
                pipeline.final_text, speculative=False, authorized=True
            )
            await pipeline.attempt.task

            message_types = [item.get("type") for item in sent]
            self.assertIn("end_call", message_types)
            self.assertIn("final_assistant_answer", message_types)
            self.assertIn(
                "Goodbye, and take care.",
                next(
                    item["content"]
                    for item in sent
                    if item.get("type") == "final_assistant_answer"
                ),
            )
            events = [
                json.loads(line)["event"]
                for line in trace.path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("end_call_authorized", events)
            self.assertNotIn("end_call_rejected", events)
            await pipeline.http.close()

    async def test_committed_llm_outage_speaks_fallback_instead_of_silence(self) -> None:
        class UnavailableLLM:
            async def stream(self, _messages, *, tools=None):
                raise aiohttp.ClientConnectionError("gateway unavailable")
                yield "unreachable"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sent = []

            async def send(message):
                sent.append(message)

            pipeline = ConversationPipeline(
                LabConfig(trace_dir=root / "traces", report_dir=root / "reports"),
                TraceRecorder(root / "traces", "llm-outage-fallback-test"),
                send,
                agent_context=_context(),
            )
            pipeline.llm = UnavailableLLM()
            pipeline.tts = self.SilentTTS()
            pipeline.turn_id = "turn-0001"
            pipeline.final_text = "Can you help me open an account?"

            await pipeline._start_attempt(
                pipeline.final_text, speculative=False, authorized=True
            )
            await pipeline.attempt.task

            answers = [
                item["content"]
                for item in sent
                if item.get("type") == "final_assistant_answer"
            ]
            self.assertEqual(len(answers), 1)
            self.assertIn("temporarily unavailable", answers[0])
            self.assertNotIn("end_call", [item.get("type") for item in sent])
            await pipeline.http.close()
