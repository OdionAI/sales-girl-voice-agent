from __future__ import annotations

import asyncio
import builtins
import unittest
from collections import deque
from contextlib import asynccontextmanager
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp

from latency_lab.agentic import AgentRuntimeContext
from latency_lab.config import LabConfig
from latency_lab.conversation_persist import ConversationStore, caller_from_identity, to_e164
from latency_lab.pipeline import Attempt, ConversationPipeline


BUSINESS_ID = "d4ad3409-6e53-49ab-86be-40df27bc8592"
AGENT_ID = "f4fd4fa5-880d-4694-98c7-e50f3999d6bb"
SESSION_ID = "9a9ff650-42ae-4825-8910-65b5b4f20191"
CONVERSATION_ID = "91562301-23d5-4ca7-948d-e5673a9653bf"


class FakeSession:
    def __init__(self):
        self.calls = []
        self.responses = {}
        self.before_request = None
        self.closed = False

    @asynccontextmanager
    async def request(self, method, url, **kwargs):
        if self.closed:
            raise AssertionError("Persistence ran after HTTP close")
        path = url.removeprefix("https://conversations.example")
        self.calls.append((method, path, kwargs))
        if self.before_request:
            await self.before_request(path)
        response = self.responses.get(path)
        if response:
            result = response.popleft()
        elif path.endswith("/resolve"):
            result = (200, {"conversation_id": CONVERSATION_ID})
        elif path.endswith("/sessions/start"):
            result = (200, {"id": SESSION_ID})
        else:
            result = (200, {})
        if isinstance(result, Exception):
            raise result
        status, body = result
        json_result = AsyncMock(side_effect=body) if isinstance(body, Exception) else AsyncMock(return_value=body)
        yield SimpleNamespace(status=status, json=json_result)

    async def close(self):
        self.closed = True

    def payloads(self, suffix):
        return [kwargs["json"] for _, path, kwargs in self.calls if path.endswith(suffix)]


class CallerIdentityTest(unittest.TestCase):
    def test_email_with_digits_is_not_a_phone_or_room_fallback(self):
        self.assertEqual(caller_from_identity({"end_user_id": "caller2348012345678@example.com"},
                                             "call__2348099999999_x"), "caller2348012345678@example.com")

    def test_phone_normalization(self):
        for raw in ("08012345678", "+234 (801) 234-5678", "002348012345678", "2348012345678"):
            with self.subTest(raw=raw):
                self.assertEqual(to_e164(raw), "+2348012345678")

    def test_arbitrary_sdk_subject_never_becomes_phone(self):
        self.assertEqual(caller_from_identity({"end_user_id": "customer-2348012345678",
                                             "wema_context": {"customer_id": "2348099999999"}}),
                         "customer-2348012345678")
        self.assertEqual(to_e164("user-12345678"), "user-12345678")

    def test_email_and_signed_room_fallbacks(self):
        self.assertEqual(caller_from_identity({"end_user_email": "caller@example.com"}), "caller@example.com")
        self.assertEqual(caller_from_identity({}, "call__2348012345678_x"), "+2348012345678")
        self.assertEqual(caller_from_identity(None), "")


class ConversationStoreTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict("os.environ", {"LIVEKIT_RECORDING_ENABLED": "false"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.http = FakeSession()
        self.trace = Mock(session_id="client-trace")
        self.context = AgentRuntimeContext(business_id=BUSINESS_ID, agent_id=AGENT_ID)
        self.config = LabConfig(conversation_service_base_url="https://conversations.example",
                                conversation_service_token="service-token")
        self.stores = []

    async def asyncTearDown(self):
        for store in self.stores:
            await store.close()

    def store(self, caller="caller@example.com", **kwargs):
        store = ConversationStore(self.config, self.http, self.context, self.trace, caller=caller,
                                  client_session_id=kwargs.pop("client_session_id", "client-trace"), **kwargs)
        store.RETRY_DELAY_SECONDS = 0
        self.stores.append(store)
        return store

    def events(self):
        return [call.args[0] for call in self.trace.record.call_args_list]

    def recording_helper(self):
        helper = ModuleType("agent.livekit_recording")
        helper.start_room_recording = AsyncMock(return_value=SimpleNamespace(
            enabled=True, egress_id="egress-1", expected_url="local-recording://private/call.ogg", detail=None))
        helper.request_stop_room_recording = AsyncMock(return_value=True)
        helper.finalize_room_recording = AsyncMock(return_value=SimpleNamespace(
            status="available", recording_url="local-recording://private/call.ogg", duration_seconds=2, detail=None))
        package = ModuleType("agent")
        package.livekit_recording = helper
        modules = patch.dict("sys.modules", {"agent": package, "agent.livekit_recording": helper})
        modules.start()
        self.addCleanup(modules.stop)
        self.env.stop()
        self.env = patch.dict("os.environ", {"LIVEKIT_RECORDING_ENABLED": "true"})
        self.env.start()
        self.addCleanup(self.env.stop)
        return helper

    async def test_email_resolves_and_starts_web_session(self):
        store = self.store("Caller2348012345678@Example.com")
        await store.start()
        self.assertEqual(self.http.payloads("/resolve")[0]["external_id"], "caller2348012345678@example.com")
        self.assertEqual(self.http.payloads("/resolve")[0]["channel"], "web")
        self.assertEqual(self.http.payloads("/sessions/start")[0]["channel"], "web")
        self.assertEqual(store.session_id, SESSION_ID)

    async def test_phone_resolves_and_starts_voice_session(self):
        store = self.store("08012345678")
        await store.start()
        self.assertEqual(self.http.payloads("/resolve")[0]["external_id"], "+2348012345678")
        self.assertEqual(self.http.payloads("/sessions/start")[0]["channel"], "voice")

    async def test_sdk_and_missing_ids_have_collision_resistant_session_aliases(self):
        a = self.store("customer-1234567890")
        b = self.store("customer-1234567891")
        c = self.store("customer-1234567890", client_session_id="other-session")
        missing = self.store("", client_session_id="")
        missing2 = self.store("", client_session_id="")
        self.assertEqual(len({s.external_id for s in (a, b, c, missing, missing2)}), 5)
        self.assertEqual(a.external_id, self.store("customer-1234567890").external_id)
        for store in (a, b, c, missing, missing2):
            self.assertEqual(store.channel, "web")
            self.assertRegex(store.external_id, r"^session-[a-f0-9]{48}@sdk\.invalid$")
        self.context = AgentRuntimeContext(business_id=AGENT_ID, agent_id=AGENT_ID)
        self.assertNotEqual(a.external_id, self.store("customer-1234567890").external_id)

    async def test_queue_drains_in_order_with_stable_turn_dedupe(self):
        store = self.store()
        store.spawn(store.start())
        expected = []
        for index in range(10):
            for role in ("user", "assistant"):
                text = "Same words"
                expected.append(role)
                method = store.add_user if role == "user" else store.add_assistant
                store.spawn(method(text, turn_id=f"turn-{index}"))
                store.spawn(method(text, turn_id=f"turn-{index}"))
        await store.close()
        messages = self.http.payloads("/messages")
        self.assertEqual([p["role"] for p in messages], expected)
        self.assertEqual([p["metadata"]["sequence"] for p in messages], list(range(1, 21)))
        self.assertEqual(len({p["idempotency_key"] for p in messages}), 20)
        self.assertTrue(all(p["session_id"] == SESSION_ID for p in messages))
        self.assertEqual(self.http.calls[-1][1], "/v1/conversations/sessions/end")
        await store.close()
        self.assertEqual(len(self.http.payloads("/sessions/end")), 1)

    async def test_deployed_add_api_dedupes_adjacent_replay_but_retains_later_repeat(self):
        store = self.store()
        for role, text in (("user", "Hello"), ("user", "Hello"), ("assistant", "Hi"), ("user", "Hello")):
            store.spawn(store.add_user(text) if role == "user" else store.add_assistant(text))
        await store.close()
        self.assertEqual([p["content"] for p in self.http.payloads("/messages")], ["Hello", "Hi", "Hello"])

    async def test_interrupted_assistant_does_not_overwrite_completed_turn(self):
        store = self.store()
        await store.add_assistant("Completed answer", turn_id="one")
        await store.add_assistant("Partial answer", turn_id="one", interrupted=True)
        messages = self.http.payloads("/messages")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], "Completed answer")
        self.assertNotIn("interrupted", messages[0]["metadata"])

    async def test_transient_request_retries_with_same_message_key(self):
        path = f"/v1/conversations/{CONVERSATION_ID}/messages"
        self.http.responses[path] = deque([aiohttp.ClientConnectionError("lost response"), (200, {})])
        store = self.store()
        await store.add_user("Please help", turn_id="one")
        messages = self.http.payloads("/messages")
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0], messages[1])
        self.assertEqual(store.recent_user, ["Please help"])

    async def test_request_failure_does_not_stop_later_messages(self):
        path = f"/v1/conversations/{CONVERSATION_ID}/messages"
        self.http.responses[path] = deque([(503, {}), (503, {}), (200, {})])
        store = self.store()
        store.spawn(store.add_user("First", turn_id="one"))
        store.spawn(store.add_user("Second", turn_id="two"))
        await store.close()
        self.assertEqual([p["content"] for p in self.http.payloads("/messages")], ["First", "First", "Second"])
        self.assertIn("conversation_persist_message_failed", self.events())

    async def test_resolve_failure_is_nonfatal_traced_and_does_not_create_recording(self):
        helper = self.recording_helper()
        self.http.responses["/v1/conversations/resolve"] = deque([(503, {}), (503, {})])
        store = self.store(room_name="signed-room")
        await store.add_user("Hello")
        await store.start()
        self.assertEqual(len(self.http.payloads("/resolve")), 2)
        self.assertFalse(self.http.payloads("/sessions/start"))
        self.assertFalse(self.http.payloads("/messages"))
        helper.start_room_recording.assert_not_called()
        self.assertIn("conversation_persist_start_failed", self.events())

    async def test_rejected_or_malformed_start_never_uses_client_identity_as_session(self):
        helper = self.recording_helper()
        for response in ((400, {"id": SESSION_ID}), (200, {"session": []}), (200, ValueError("bad json"))):
            with self.subTest(response=response):
                self.http.responses["/v1/conversations/sessions/start"] = deque([response])
                store = self.store(room_name="signed-room")
                await store.add_user("Hello")
                self.assertEqual(store.session_id, "")
        helper.start_room_recording.assert_not_called()
        self.assertFalse(self.http.payloads("/messages"))

    async def test_start_retries_and_accepts_nested_server_session(self):
        self.http.responses["/v1/conversations/sessions/start"] = deque([
            (503, {}), (200, {"session": {"id": SESSION_ID}})])
        store = self.store()
        await store.start()
        await store.start()
        self.assertEqual(store.session_id, SESSION_ID)
        self.assertEqual(len(self.http.payloads("/sessions/start")), 2)

    async def test_handoff_preserves_fast_analysis_and_payload_semantics(self):
        store = self.store(room_name="signed-room")
        store.spawn(store.add_user("Please block my card", turn_id="one"))
        await store.mark_transfer("Urgent human support", {"custom": "value"})
        analysis = self.http.payloads("/analysis")[0]
        self.assertEqual(analysis["primary_intent"], "card_or_security_issue")
        self.assertEqual(analysis["resolution_status"], "escalated")
        self.assertIn("Please block my card", analysis["summary"])
        event = next(p for p in self.http.payloads("/events") if p["event_type"] == "aicc_handoff")
        self.assertEqual(event["payload"]["transfer_mode"], "sip_bridge")
        self.assertEqual(event["payload"]["snapshot_kind"], "fast")
        self.assertEqual(event["payload"]["custom"], "value")
        self.assertEqual(event["payload"]["room_name"], "signed-room")

    async def test_spawned_handoff_does_not_wait_on_itself(self):
        store = self.store()
        store.spawn(store.add_user("I need a person"))
        store.spawn(store.mark_transfer("Requested human"))
        await asyncio.wait_for(store.close(), 1)
        self.assertEqual(len(self.http.payloads("/analysis")), 1)

    async def test_disabled_recording_imports_no_agent_or_livekit_modules(self):
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "agent" or name.startswith(("agent.", "livekit")):
                raise AssertionError("Optional recording dependency imported")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded_import):
            store = self.store(room_name="signed-room")
            await store.start()
            await store.close()
        self.assertEqual(self.http.payloads("/recording")[0]["recording_status"], "disabled")
        self.assertIn("conversation_recording_status", self.events())

    async def test_browser_transport_never_records_even_with_room_and_enabled_flag(self):
        helper = self.recording_helper()
        store = self.store(room_name="signed-room", transport="browser")
        await store.start()
        await store.close()
        helper.start_room_recording.assert_not_called()
        self.assertEqual(store.recording_status, "disabled")

    async def test_recording_start_is_nonblocking_and_finalizes_before_http_close(self):
        helper = self.recording_helper()
        gate = asyncio.Event()
        begun = asyncio.Event()
        original = helper.start_room_recording.return_value

        async def start(**kwargs):
            begun.set()
            await gate.wait()
            return original

        helper.start_room_recording.side_effect = start
        store = self.store(room_name="signed-room", transport="livekit")
        await asyncio.wait_for(store.start(), 0.5)
        await begun.wait()
        await asyncio.wait_for(store.add_user("Do not block my transcript", turn_id="one"), 0.5)
        gate.set()
        await store.close()
        await self.http.close()
        helper.start_room_recording.assert_awaited_once()
        self.assertEqual(helper.start_room_recording.call_args.kwargs["session_id"], SESSION_ID)
        self.assertEqual(helper.start_room_recording.call_args.kwargs["room_name"], "signed-room")
        helper.finalize_room_recording.assert_awaited_once()
        helper.request_stop_room_recording.assert_awaited_once_with(egress_id="egress-1", timeout_seconds=5.0)
        self.assertFalse(helper.finalize_room_recording.call_args.kwargs["request_stop"])
        self.assertEqual(helper.finalize_room_recording.call_args.kwargs["timeout_seconds"], 45.0)
        statuses = self.http.payloads("/recording")
        self.assertEqual([p["recording_status"] for p in statuses], ["processing", "available"])
        self.assertIsNone(statuses[0]["recording_url"])
        self.assertEqual(statuses[-1]["recording_url"], "local-recording://private/call.ogg")
        self.assertEqual(self.http.calls[-1][1], "/v1/conversations/sessions/end")

    async def test_recording_start_failure_has_explicit_failed_status(self):
        helper = self.recording_helper()
        helper.start_room_recording.side_effect = RuntimeError("egress unavailable")
        store = self.store(room_name="signed-room")
        await store.start()
        await store.close()
        self.assertEqual(store.recording_status, "failed")
        helper.finalize_room_recording.assert_not_called()
        self.assertEqual(self.http.payloads("/recording")[-1]["recording_status"], "failed")

    async def test_recording_stop_runs_while_transcripts_are_still_draining(self):
        helper = self.recording_helper()
        store = self.store(room_name="signed-room")
        await store.start()
        await store._recording_task
        draining = asyncio.Event()
        release = asyncio.Event()
        stopped = asyncio.Event()

        async def pending_transcript():
            draining.set()
            await release.wait()
            await store.add_user("Final transcript", turn_id="one")

        async def stop(**kwargs):
            stopped.set()
            return True

        helper.request_stop_room_recording.side_effect = stop
        store.spawn(pending_transcript())
        await draining.wait()
        cleanup = asyncio.create_task(store.close())
        try:
            await asyncio.wait_for(stopped.wait(), 0.5)
            self.assertFalse(store._worker.done())
            helper.finalize_room_recording.assert_not_called()
            self.assertEqual(store.recording_status, "processing")
        finally:
            release.set()
            await cleanup
        helper.request_stop_room_recording.assert_awaited_once()
        self.assertEqual(len(self.http.payloads("/messages")), 1)

    async def test_late_recording_start_is_stopped_before_metadata_write_finishes(self):
        helper = self.recording_helper()
        store = self.store(room_name="signed-room")
        metadata_blocked = asyncio.Event()
        release = asyncio.Event()
        stopped = asyncio.Event()

        async def before_request(path):
            if path.endswith("/recording"):
                metadata_blocked.set()
                await release.wait()

        async def stop(**kwargs):
            stopped.set()
            return True

        helper.request_stop_room_recording.side_effect = stop
        self.http.before_request = before_request
        store.mark_disconnected()
        await store.start()
        try:
            await asyncio.wait_for(metadata_blocked.wait(), 0.5)
            await asyncio.wait_for(stopped.wait(), 0.5)
            self.assertFalse(store._recording_task.done())
        finally:
            release.set()
            await store.close()
        helper.request_stop_room_recording.assert_awaited_once()

    async def test_failed_stop_ack_does_not_claim_file_readiness(self):
        helper = self.recording_helper()
        helper.request_stop_room_recording.return_value = False
        helper.finalize_room_recording.return_value = SimpleNamespace(
            status="processing", recording_url=None, duration_seconds=0, detail="egress_completion_timeout")
        store = self.store(room_name="signed-room")
        await store.start()
        await store.close()
        self.assertEqual(store.recording_status, "processing")
        self.assertFalse(self.http.payloads("/events")[-1]["payload"]["stop_acknowledged"])
        self.assertFalse(helper.finalize_room_recording.call_args.kwargs["request_stop"])
        self.assertIsNone(store.recording_url)

    async def test_stop_timeout_is_bounded_and_cleanup_continues(self):
        helper = self.recording_helper()

        async def hang(**kwargs):
            await asyncio.Event().wait()

        helper.request_stop_room_recording.side_effect = hang
        store = self.store(room_name="signed-room")
        store.RECORDING_STOP_TIMEOUT_SECONDS = 0.01
        await store.start()
        await asyncio.wait_for(store.close(), 2)
        self.assertFalse(store._recording_stop_acknowledged)
        self.assertTrue(store._recording_stop_task.done())
        helper.finalize_room_recording.assert_awaited_once()

    async def test_call_duration_excludes_cleanup_and_recording_uses_actual_file_duration(self):
        helper = self.recording_helper()
        store = self.store(room_name="signed-room")
        await store.start()
        await store._recording_task
        clock = Mock(return_value=store.started_mono + 12)

        async def finalize(**kwargs):
            self.assertEqual(kwargs["duration_seconds"], 12)
            clock.return_value += 45
            return SimpleNamespace(status="available", recording_url="local-recording://private/call.ogg",
                                   duration_seconds=9, detail=None)

        helper.finalize_room_recording.side_effect = finalize
        with patch("latency_lab.conversation_persist.time", SimpleNamespace(monotonic=clock)):
            await store.close()
        self.assertEqual(self.http.payloads("/sessions/end")[0]["duration_seconds"], 12)
        self.assertEqual(self.http.payloads("/sessions/end")[0]["ended_at"], store._ended_at.isoformat())
        self.assertEqual(self.http.payloads("/recording")[-1]["recording_duration_seconds"], 9)

    async def test_recording_start_timeout_emits_room_reconciliation_details(self):
        helper = self.recording_helper()

        async def hang(**kwargs):
            await asyncio.Event().wait()

        helper.start_room_recording.side_effect = hang
        store = self.store(room_name="signed-room")
        store.RECORDING_START_TIMEOUT_SECONDS = 0.01
        await store.start()
        await asyncio.wait_for(store.close(), 0.5)
        self.assertEqual(store.recording_status, "failed")
        event = next(p for p in self.http.payloads("/events") if p["event_type"] == "recording_status")
        self.assertTrue(event["payload"]["reconciliation_required"])
        self.assertEqual(event["payload"]["room_name"], "signed-room")
        self.assertEqual(event["payload"]["detail"], "recording_start_timeout_reconcile_room")
        self.assertIsNone(event["payload"]["recording_url"])
        helper.finalize_room_recording.assert_not_called()

    async def test_failed_recording_status_write_is_retried_on_close(self):
        self.http.responses[f"/v1/conversations/sessions/{SESSION_ID}/recording"] = deque([
            (503, {}), (503, {}), (200, {})])
        store = self.store()
        await store.start()
        await store._recording_task
        self.assertTrue(store._recording_status_dirty)
        await store.close()
        self.assertFalse(store._recording_status_dirty)
        self.assertEqual(len(self.http.payloads("/recording")), 3)
        self.assertIn("conversation_recording_persist_failed", self.events())

    async def test_failed_recording_event_write_is_retried_on_close(self):
        store = self.store()
        await store.start()
        await store._recording_task
        self.http.responses[f"/v1/conversations/sessions/{SESSION_ID}/events"] = deque([
            (503, {}), (503, {}), (200, {})])
        await store._publish_recording_status()
        self.assertTrue(store._recording_status_dirty)
        await store.close()
        self.assertFalse(store._recording_status_dirty)

    async def test_request_deadline_is_explicit_and_non_json_errors_are_nonfatal(self):
        self.http.responses["/v1/conversations/resolve"] = deque([(502, ValueError("html proxy error")), (200, [])])
        store = self.store()
        await store.start()
        self.assertEqual(len(self.http.payloads("/resolve")), 2)
        self.assertEqual(self.http.calls[0][2]["timeout"].total, store.REQUEST_TIMEOUT_SECONDS)
        self.assertIn("conversation_persist_start_failed", self.events())

    async def test_recording_finalize_timeout_is_processing_not_available(self):
        helper = self.recording_helper()

        async def hang(**kwargs):
            await asyncio.Event().wait()

        helper.finalize_room_recording.side_effect = hang
        store = self.store(room_name="signed-room")
        store.RECORDING_FINALIZE_TIMEOUT_SECONDS = 0.01
        await store.start()
        await asyncio.wait_for(store.close(), 2)
        self.assertEqual(store.recording_status, "processing")
        self.assertIsNone(store.recording_url)
        self.assertEqual(self.http.payloads("/recording")[-1]["recording_status"], "processing")
        event = self.http.payloads("/events")[-1]
        self.assertEqual(event["payload"]["egress_id"], "egress-1")
        self.assertTrue(event["payload"]["reconciliation_required"])

    async def test_recording_finalize_failed_and_processing_results(self):
        helper = self.recording_helper()
        for status in ("failed", "processing", "available"):
            with self.subTest(status=status):
                helper.finalize_room_recording.return_value = SimpleNamespace(
                    status=status, recording_url=None, duration_seconds=0, detail="provider_result")
                store = self.store(room_name="signed-room")
                await store.start()
                await store.close()
                self.assertEqual(store.recording_status, "processing" if status == "processing" else "failed")
                self.assertIsNone(store.recording_url)

    async def test_queue_limit_and_shutdown_deadline_cancel_pending_work(self):
        store = self.store()
        store.MAX_PENDING_OPERATIONS = 2
        store.DRAIN_TIMEOUT_SECONDS = 0.01
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def hang():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        store.spawn(hang())
        await entered.wait()
        for index in range(3):
            store.spawn(store.add_user(str(index)))
        self.assertEqual(len(store._pending), 2)
        await asyncio.wait_for(store.close(), 0.5)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(store._pending)
        self.assertTrue(store._worker.done())
        self.assertIn("conversation_persist_dropped", self.events())
        self.assertIn("conversation_persist_drain_timeout", self.events())


class PipelinePersistenceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.http = FakeSession()
        self.config = LabConfig(conversation_service_base_url="https://conversations.example",
                                conversation_service_token="service-token", memory_compaction_enabled=False)
        self.identity = {"end_user_id": "caller@example.com", "room_name": "signed-room",
                         "wema_context": {"customer_id": "banking-customer"}}
        with patch("latency_lab.pipeline.aiohttp.ClientSession", return_value=self.http):
            self.pipe = ConversationPipeline(self.config, Mock(session_id="client-trace"), AsyncMock(),
                                             agent_context=AgentRuntimeContext(business_id=BUSINESS_ID, agent_id=AGENT_ID),
                                             session_identity=self.identity)
        self.pipe.banking.close = AsyncMock()
        self.pipe.stt.close = AsyncMock()
        self.pipe._generate = AsyncMock(side_effect=self.wait_for_cancel)
        self.env = patch.dict("os.environ", {"LIVEKIT_RECORDING_ENABLED": "false"})
        self.env.start()
        self.addCleanup(self.env.stop)

    async def wait_for_cancel(self, attempt):
        await asyncio.Event().wait()

    async def asyncTearDown(self):
        if not self.http.closed:
            await self.pipe.close()

    async def test_finalized_users_survive_interrupted_generation_and_disconnect(self):
        await self.pipe.text("First finalized request")
        self.assertFalse(self.pipe.attempt.audio_complete)
        await self.pipe.text("Second finalized request")
        await self.pipe.close()
        self.assertEqual([p["content"] for p in self.http.payloads("/messages")],
                         ["First finalized request", "Second finalized request"])
        self.assertTrue(self.http.closed)
        self.assertEqual(self.identity["wema_context"]["customer_id"], "banking-customer")

    async def test_final_capture_never_waits_for_storage_and_ignores_duplicate_final(self):
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def block(path):
            if path.endswith("/resolve"):
                entered.set()
                await gate.wait()

        self.http.before_request = block
        self.pipe.turn_id = "turn-0001"
        self.pipe.state = "awaiting_final"
        await asyncio.wait_for(self.pipe.on_final("Accepted final"), 0.5)
        await entered.wait()
        await self.pipe.on_final("Duplicate final")
        gate.set()
        await self.pipe.close()
        self.assertEqual([p["content"] for p in self.http.payloads("/messages")], ["Accepted final"])

    async def test_assistant_commit_does_not_reappend_user(self):
        await self.pipe.text("Hello")
        attempt = self.pipe.attempt
        attempt.answer = "Hi there"
        await self.pipe._commit_attempt_text(attempt)
        await self.pipe._commit_attempt_text(attempt)
        await self.pipe.close()
        self.assertEqual([p["role"] for p in self.http.payloads("/messages")], ["user", "assistant"])

    async def test_disconnect_clock_stops_before_pipeline_cleanup(self):
        await self.pipe.text("Hello")
        clock = Mock(return_value=self.pipe.conversations.started_mono + 10)

        async def banking_close():
            clock.return_value += 30

        self.pipe.banking.close.side_effect = banking_close
        with patch("latency_lab.conversation_persist.time", SimpleNamespace(monotonic=clock)):
            await self.pipe.close()
        self.assertEqual(self.http.payloads("/sessions/end")[0]["duration_seconds"], 10)

    async def test_uncommitted_speculation_is_not_persisted(self):
        self.pipe.attempt = Attempt("speculative", "Wrong hypothesis", True, turn_id="turn-0001")
        await self.pipe._cancel_attempt("speculation_replaced")
        await self.pipe.close()
        self.assertFalse(self.http.payloads("/messages"))

    async def test_barge_in_retains_generated_assistant_after_audio_was_sent(self):
        await self.pipe.text("First request")
        self.pipe.attempt.answer = "Some sent words and an unconfirmed generated tail"
        self.pipe.attempt.audio_sent = True
        await self.pipe.text("Interrupting request")
        await self.pipe.close()
        messages = self.http.payloads("/messages")
        self.assertEqual([p["role"] for p in messages], ["user", "assistant", "user"])
        self.assertEqual(messages[1]["content"], "Some sent words and an unconfirmed generated tail")
        self.assertEqual(messages[1]["metadata"]["delivery"], "generated_not_confirmed")
        self.assertTrue(messages[1]["metadata"]["interrupted"])

    async def test_disconnect_retains_audio_sent_even_when_authorization_is_cleared(self):
        await self.pipe.text("Hello")
        self.pipe.attempt.answer = "Response still generating"
        self.pipe.attempt.audio_sent = True
        await self.pipe.close()
        self.assertEqual([p["role"] for p in self.http.payloads("/messages")], ["user", "assistant"])
        self.assertTrue(self.http.payloads("/messages")[1]["metadata"]["interrupted"])

    async def test_cancel_does_not_save_unsent_generated_assistant(self):
        await self.pipe.text("Hello")
        self.pipe.attempt.answer = "Unsent answer"
        await self.pipe.close()
        self.assertEqual([p["role"] for p in self.http.payloads("/messages")], ["user"])

    async def test_cancel_does_not_duplicate_completed_assistant(self):
        await self.pipe.text("Hello")
        self.pipe.attempt.answer = "Completed answer"
        self.pipe.attempt.audio_sent = True
        await self.pipe._commit_attempt_text(self.pipe.attempt)
        await self.pipe.close()
        messages = self.http.payloads("/messages")
        self.assertEqual([p["role"] for p in messages], ["user", "assistant"])
        self.assertNotIn("interrupted", messages[1]["metadata"])

    async def test_pipeline_start_does_not_wait_for_resolve(self):
        gate = asyncio.Event()

        async def block(path):
            if path.endswith("/resolve"):
                await gate.wait()

        self.http.before_request = block
        self.pipe.stt.connect = AsyncMock()
        await asyncio.wait_for(self.pipe.start(), 0.5)
        gate.set()
        await self.pipe.close()


if __name__ == "__main__":
    unittest.main()
