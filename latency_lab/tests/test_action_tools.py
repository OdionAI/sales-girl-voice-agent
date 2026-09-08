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
    explicit_end_call_intent,
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

    def test_confirmation_classifier_is_conservative(self) -> None:
        self.assertEqual(confirmation_decision("Yes, go ahead"), "confirmed")
        self.assertEqual(confirmation_decision("No, cancel that"), "rejected")
        self.assertEqual(confirmation_decision("What did you say?"), "unclear")

    def test_end_call_intent_requires_an_explicit_closing_utterance(self) -> None:
        accepted = (
            "Goodbye",
            "Okay, bye",
            "Please end this call now",
            "You can hang up now",
            "No, that's all, thank you",
            "I have no more questions, thanks",
            "Nothing else, thank you",
        )
        rejected = (
            "What details are required to open the account successfully?",
            "Thank you",
            "Before we end the call, I have another question",
            "How do I end a call in the mobile application?",
            "Do not end the call",
            "I don't want to hang up yet",
            "What does goodbye mean?",
        )
        for text in accepted:
            with self.subTest(text=text):
                self.assertTrue(explicit_end_call_intent(text))
        for text in rejected:
            with self.subTest(text=text):
                self.assertFalse(explicit_end_call_intent(text))


class ActionConfirmationLifecycleTest(unittest.IsolatedAsyncioTestCase):
    class ToolLLM:
        async def stream(self, _messages, *, tools=None):
            assert tools
            yield ActionToolCall(
                id="call-1",
                name="create_ticket",
                arguments={"title": "Delayed transfer", "description": "Transfer is delayed."},
            )

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

    async def test_end_call_waits_for_committed_turn_and_follows_goodbye(self) -> None:
        class EndCallLLM:
            async def stream(self, _messages, *, tools=None):
                assert tools
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
            pipeline.llm = EndCallLLM()
            pipeline.tts = self.SilentTTS()
            pipeline.turn_id = "turn-0001"
            await pipeline._start_attempt(
                "Goodbye, please end the call", speculative=True, authorized=False
            )
            attempt = pipeline.attempt
            await asyncio.sleep(0)
            self.assertNotIn("end_call", [item.get("type") for item in sent])
            self.assertFalse(attempt.task.done())

            pipeline.final_text = "Goodbye, please end the call"
            attempt.authorized = True
            attempt.authorization_event.set()
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
                "Thank you for calling. Goodbye.",
            )
            await pipeline.http.close()

    async def test_end_call_tool_is_vetoed_for_non_closing_final_transcript(self) -> None:
        class WrongEndCallLLM:
            def __init__(self):
                self.calls = 0

            async def stream(self, _messages, *, tools=None):
                self.calls += 1
                if self.calls == 1:
                    yield ActionToolCall(id="wrong-end", name="end_call", arguments={})
                else:
                    yield "You need a valid BVN or NIN and the requested personal details."

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
            self.assertNotIn("end_call", message_types)
            self.assertIn("final_assistant_answer", message_types)
            self.assertIn(
                "valid BVN or NIN",
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
            self.assertIn("end_call_semantic_validation", events)
            self.assertIn("end_call_rejected", events)
            self.assertNotIn("end_call_authorized", events)
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
