from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path

from latency_lab.agentic import (
    AgentRuntimeContext,
    KNOWLEDGE_ACKNOWLEDGEMENT,
    KnowledgeClient,
    KnowledgeMatch,
    KnowledgeResult,
    load_agent_runtime_context,
    requests_knowledge_lookup,
    room_agent_context,
    session_routing_context,
    spoken_knowledge_acknowledgement,
)
from latency_lab.config import LabConfig
from latency_lab.pipeline import ConversationPipeline
from latency_lab.trace import TraceRecorder


def _token(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


class RoomAgentContextTest(unittest.TestCase):
    def test_room_scope_decodes_business_agent_and_name(self) -> None:
        room = (
            f"voice_assistant_room_eid{_token('caller@example.com')}"
            f"_bid{_token('business-1')}_aid{_token('agent-1')}"
            f"_nid{_token('Helen')}_1234"
        )

        context = room_agent_context(room)

        self.assertEqual(context.business_id, "business-1")
        self.assertEqual(context.agent_id, "agent-1")
        self.assertEqual(context.configured_name, "Helen")
        self.assertEqual(context.end_user_id, "caller@example.com")

    def test_spoken_knowledge_acknowledgements_rotate_by_turn(self) -> None:
        first = spoken_knowledge_acknowledgement(1)
        second = spoken_knowledge_acknowledgement(2)
        fifth = spoken_knowledge_acknowledgement(5)

        self.assertNotEqual(first, second)
        self.assertEqual(first, fifth)
        self.assertNotEqual(first, KNOWLEDGE_ACKNOWLEDGEMENT)

    def test_session_scope_reads_voice_lab_ids(self) -> None:
        context = session_routing_context(
            {
                "business_id": "business-1",
                "config_agent_id": "agent-1",
                "agent_id": "ignored-agent",
                "agent_name": "Helen",
                "end_user_email": "caller@example.com",
            }
        )

        self.assertEqual(context.business_id, "business-1")
        self.assertEqual(context.agent_id, "agent-1")
        self.assertEqual(context.configured_name, "Helen")
        self.assertEqual(context.end_user_id, "caller@example.com")

    def test_session_scope_is_empty_without_ids(self) -> None:
        context = session_routing_context({"type": "session_config", "language": "en"})

        self.assertEqual(context.business_id, "")
        self.assertEqual(context.agent_id, "")


class LoadAgentRuntimeContextTest(unittest.IsolatedAsyncioTestCase):
    async def test_skips_when_voice_lab_session_has_no_agent_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace = TraceRecorder(Path(tmp))
            context = await load_agent_runtime_context(
                room_name="",
                config=LabConfig(),
                trace=trace,
                routing_context=session_routing_context({"language": "en"}),
            )
            events = [
                json.loads(line)
                for line in Path(trace.path).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        self.assertFalse(context.loaded)
        skipped = next(
            event for event in events if event.get("event") == "agent_config_load_skipped"
        )
        self.assertEqual(skipped["reason"], "session_missing_business_or_agent_scope")

    async def test_skips_when_agent_config_service_is_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace = TraceRecorder(Path(tmp))
            context = await load_agent_runtime_context(
                room_name="",
                config=LabConfig(
                    agent_config_api_base_url="",
                    agent_config_service_token="",
                ),
                trace=trace,
                routing_context=session_routing_context(
                    {
                        "business_id": "business-1",
                        "config_agent_id": "agent-1",
                        "agent_name": "Helen",
                    }
                ),
            )
            events = [
                json.loads(line)
                for line in Path(trace.path).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        self.assertFalse(context.loaded)
        self.assertEqual(context.agent_id, "agent-1")
        skipped = next(
            event for event in events if event.get("event") == "agent_config_load_skipped"
        )
        self.assertEqual(skipped["reason"], "agent_config_service_not_configured")


class KnowledgeLookupIntentTest(unittest.TestCase):
    def test_accepts_exact_and_natural_self_directed_lookup_acknowledgements(self) -> None:
        acknowledgements = (
            KNOWLEDGE_ACKNOWLEDGEMENT,
            "I’d be happy to help. May I have a moment while I check the current account-opening requirements for you?",
            "One moment, please, while I confirm that.",
            "I'll quickly verify that for you.",
            "Allow me to look that up.",
            "I'd be happy to check that for you.",
        )

        for acknowledgement in acknowledgements:
            with self.subTest(acknowledgement=acknowledgement):
                self.assertTrue(requests_knowledge_lookup(acknowledgement))

    def test_rejects_customer_advice_and_direct_answers_that_contain_check(self) -> None:
        direct_answers = (
            "You can check your balance using the Zenith Mobile App.",
            "Please check the app for your current balance.",
            "Let me know if you want to check your balance.",
            "I can show you how to check your account balance.",
            "May I ask you to check the app for the current status?",
            "I can confirm that your application was received.",
            "I checked and found the account-opening requirements for you.",
            "The bank may check your credit history during the application.",
        )

        for direct_answer in direct_answers:
            with self.subTest(direct_answer=direct_answer):
                self.assertFalse(requests_knowledge_lookup(direct_answer))


class KnowledgeClientTest(unittest.IsolatedAsyncioTestCase):
    def _config(self) -> LabConfig:
        return LabConfig(
            knowledge_service_base_url="http://knowledge.test",
            knowledge_service_token="secret",
            knowledge_top_k=4,
        )

    async def test_availability_requires_scope_and_service_configuration(self) -> None:
        no_scope = KnowledgeClient(
            self._config(),
            object(),
            AgentRuntimeContext(business_id="business-1", agent_id="agent-1"),
        )
        self.assertEqual(
            no_scope.availability(),
            (False, "agent_has_no_attached_knowledge_bases"),
        )

        scoped = KnowledgeClient(
            self._config(),
            object(),
            AgentRuntimeContext(
                business_id="business-1",
                agent_id="agent-1",
                knowledge_base_ids=("kb-1",),
            ),
        )
        self.assertEqual(
            scoped.availability(),
            (True, "attached_knowledge_available_on_explicit_llm_request"),
        )

    async def test_search_is_scoped_to_attached_knowledge_bases(self) -> None:
        class FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def json(self, **_kwargs):
                return {
                    "matches": [
                        {"source_name": "Loans", "text": "Salary advance details", "score": 0.91}
                    ]
                }

        class FakeSession:
            def __init__(self):
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return FakeResponse()

        session = FakeSession()
        client = KnowledgeClient(
            self._config(),
            session,
            AgentRuntimeContext(
                business_id="business-1",
                agent_id="agent-1",
                knowledge_base_ids=("kb-1", "kb-2"),
            ),
        )

        result = await client.search("What loans are available?")

        self.assertEqual(result.status, "success")
        self.assertEqual(result.matches[0].source_name, "Loans")
        _, request = session.calls[0]
        self.assertEqual(request["json"]["knowledge_base_ids"], ["kb-1", "kb-2"])
        self.assertEqual(request["headers"]["X-Business-ID"], "business-1")
        self.assertEqual(request["headers"]["X-Agent-ID"], "agent-1")


class ExplicitKnowledgeLifecycleTest(unittest.IsolatedAsyncioTestCase):
    class FakeTTS:
        def __init__(self):
            self.texts = []

        async def stream(self, _text):
            self.texts.append(_text)
            async def chunks():
                if False:
                    yield b""

            return 24000, chunks()

    class ScriptedLLM:
        def __init__(self, answers):
            self.answers = list(answers)
            self.calls = []
            self.first_complete = asyncio.Event()

        async def stream(self, messages, *, tools=None):
            self.calls.append(messages)
            answer = self.answers[len(self.calls) - 1]
            yield answer
            if len(self.calls) == 1:
                self.first_complete.set()

    class ControlledKnowledge:
        def __init__(self):
            self.searches = []

        def availability(self):
            return True, "attached_knowledge_available_on_explicit_llm_request"

        async def search(self, query):
            self.searches.append(query)
            return KnowledgeResult(
                query=query,
                status="success",
                matches=(KnowledgeMatch("USSD", "Verified emergency code", 0.9),),
                elapsed_ms=25.0,
            )

    async def test_direct_conversation_never_searches_knowledge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            async def send(_message):
                return None

            pipeline = ConversationPipeline(
                LabConfig(trace_dir=root / "traces", report_dir=root / "reports"),
                TraceRecorder(root / "traces", "knowledge-direct-test"),
                send,
                agent_context=AgentRuntimeContext(
                    business_id="business-1",
                    agent_id="agent-1",
                    knowledge_base_ids=("kb-1",),
                    loaded=True,
                ),
            )
            llm = self.ScriptedLLM(["Good morning! I can hear you clearly."])
            knowledge = self.ControlledKnowledge()
            pipeline.llm = llm
            pipeline.tts = self.FakeTTS()
            pipeline.knowledge = knowledge
            pipeline.turn_id = "turn-0001"
            pipeline.final_text = "Hello. Good morning. Can you hear me?"

            await pipeline._start_attempt(
                pipeline.final_text, speculative=False, authorized=True
            )
            await pipeline.attempt.task

            self.assertEqual(len(llm.calls), 1)
            self.assertEqual(knowledge.searches, [])
            events = [
                json.loads(line)
                for line in pipeline.trace.path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("knowledge_retrieval_deferred", [event["event"] for event in events])
            self.assertNotIn("knowledge_retrieval_start", [event["event"] for event in events])
            await pipeline.http.close()

    async def test_explicit_request_waits_for_final_then_retrieves(self) -> None:

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            async def send(_message):
                return None

            pipeline = ConversationPipeline(
                LabConfig(trace_dir=root / "traces", report_dir=root / "reports"),
                TraceRecorder(root / "traces", "knowledge-explicit-test"),
                send,
                agent_context=AgentRuntimeContext(
                    business_id="business-1",
                    agent_id="agent-1",
                    knowledge_base_ids=("kb-1",),
                    loaded=True,
                ),
            )
            controlled = self.ControlledKnowledge()
            natural_lookup_acknowledgement = (
                "I’d be happy to help. May I have a moment while I check the current "
                "account-opening requirements for you?"
            )
            llm = self.ScriptedLLM(
                [natural_lookup_acknowledgement, "The verified emergency code is available."]
            )
            pipeline.knowledge = controlled
            pipeline.llm = llm
            tts = self.FakeTTS()
            pipeline.tts = tts
            pipeline.turn_id = "turn-0001"
            pipeline.turn_number = 2

            await pipeline._start_attempt(
                "What is the emergency self-service code?",
                speculative=True,
                authorized=False,
            )
            attempt = pipeline.attempt
            self.assertIsNotNone(attempt)
            await llm.first_complete.wait()
            await asyncio.sleep(0)

            self.assertEqual(controlled.searches, [])
            self.assertFalse(attempt.task.done())

            pipeline.final_text = "What is the emergency self-service code?"
            attempt.authorized = True
            attempt.authorization_event.set()
            await attempt.task

            self.assertEqual(controlled.searches, [pipeline.final_text])
            self.assertEqual(len(llm.calls), 2)
            self.assertEqual(attempt.knowledge_result.status, "success")
            self.assertIn(spoken_knowledge_acknowledgement(2), tts.texts)
            self.assertNotIn(
                "May I have a moment while I check the current account-opening requirements for you?",
                tts.texts,
            )
            for messages in llm.calls:
                self.assertEqual(
                    sum(message["role"] == "system" for message in messages), 1
                )
            self.assertIn("Verified emergency code", llm.calls[1][0]["content"])
            self.assertIn("KNOWLEDGE RETRIEVAL IS COMPLETE", llm.calls[1][0]["content"])
            self.assertNotIn(
                "Reply with exactly these six words", llm.calls[1][0]["content"]
            )
            self.assertNotIn(
                "The application will perform the read-only lookup",
                llm.calls[1][0]["content"],
            )
            events = [
                json.loads(line)
                for line in pipeline.trace.path.read_text(encoding="utf-8").splitlines()
            ]
            names = [event["event"] for event in events]
            self.assertIn("knowledge_lookup_waiting_for_final", names)
            self.assertIn("knowledge_retrieval_start", names)
            self.assertIn("knowledge_retrieval_complete", names)
            await pipeline.http.close()


if __name__ == "__main__":
    unittest.main()
