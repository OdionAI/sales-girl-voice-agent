import asyncio
import base64
import hmac
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web

from latency_lab.action_tools import ActionToolCall, action_tool_definitions
from latency_lab.agentic import AgentRuntimeContext
from latency_lab.banking_tools import BANK_TOOLS, BankingTools
from latency_lab.config import LabConfig
from latency_lab.pipeline import ConversationPipeline
from latency_lab.session_identity import verify_session_token
from latency_lab.trace import TraceRecorder


def identity():
    return {"business_id": "business", "config_agent_id": "agent", "end_user_id": "caller@example.test",
            "voice_auth_owner": "caller@example.test", "wema_context": {
                "customer_id": "test-customer", "account_number": "0123456789", "phone_number": "08000000000"}}


def record(name, url):
    properties = {"source_account": {"type": "string", "pattern": "^[0-9]{10}$"}}
    required = []
    if name == "wema_prepare_data_purchase":
        properties["phone_number"] = {"type": "string", "pattern": "^0[0-9]{10}$"}
        required = ["phone_number"]
    if name == "wema_execute_prepared":
        properties = {"operation_id": {"type": "string"}}
        required = ["operation_id"]
    return {"name": name, "url": url, "method": "POST", "request_schema": {
        "type": "object", "properties": properties, "required": required, "additionalProperties": False}}


class SessionTokenTest(unittest.TestCase):
    def token(self, claims):
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        key = hmac.digest(b"test-service-token", b"odion-rvc-session-v1", "sha256")
        signature = base64.urlsafe_b64encode(hmac.digest(key, payload.encode(), "sha256")).decode().rstrip("=")
        return payload + "." + signature

    def claims(self):
        return {**identity(), "aud": "odion-rvc-tools", "exp": time.time() + 120, "nonce": str(uuid.uuid4())}

    def test_valid_once_only(self):
        claims = self.claims()
        token = self.token(claims)
        self.assertEqual(verify_session_token(token, "test-service-token"), claims)
        with self.assertRaises(ValueError):
            verify_session_token(token, "test-service-token")

    def test_rejects_tampering_expiry_wrong_audience_and_secret(self):
        for updates in ({"exp": 1}, {"aud": "other"}, {"nonce": "x"}, {"business_id": ""}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                verify_session_token(self.token({**self.claims(), **updates}), "test-service-token")
        with self.assertRaises(ValueError):
            verify_session_token(self.token(self.claims()), "wrong-secret")
        with self.assertRaises(ValueError):
            verify_session_token("not.a.valid.token", "test-service-token")

    def test_voice_policy_cannot_be_changed_without_resigning(self):
        claims = {**self.claims(), "voice_auth_required": True}
        signature = self.token(claims).split(".")[1]
        altered = self.token({**claims, "voice_auth_required": False}).split(".")[0]
        with self.assertRaises(ValueError):
            verify_session_token(altered + "." + signature, "test-service-token")


class BankingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.events = []
        self.response = {"status": "ok", "message": "Balance retrieved", "data": {"availableBalance": "100.00"}}
        async def handle(request):
            self.requests.append((dict(request.headers), await request.json()))
            return web.json_response(self.response)
        app = web.Application()
        app.router.add_post("/tool", handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/tool"
        self.context = AgentRuntimeContext(business_id="business", agent_id="agent", end_user_id="caller@example.test",
                                           tools=tuple(record(name, url) for name in sorted(BANK_TOOLS)), loaded=True)
        self.http = aiohttp.ClientSession()
        async def send(event):
            self.events.append(event)
        self.send = send
        self.bank = BankingTools(self.http, self.context, "session", send, identity())
        self.bank.generation = 1
        self.bank.clip = b"\x01\x00" * 24000
        self.bank.compare = AsyncMock(return_value={"matched": True, "score": .9})

    async def asyncTearDown(self):
        await self.bank.close()
        await self.http.close()
        await self.runner.cleanup()

    def call(self, name="wema_get_balance", **args):
        return ActionToolCall(str(uuid.uuid4()), name, args)

    async def test_both_voice_checks_then_scoped_http_and_activity(self):
        result = await self.bank.execute(self.call())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.bank.compare.await_count, 2)
        headers, args = self.requests[0]
        self.assertEqual(headers["X-Wema-Customer-Id"], "test-customer")
        self.assertEqual(headers["X-Session-Id"], "session")
        self.assertEqual(args["source_account"], "0123456789")
        self.assertEqual([e["payload"]["event"] for e in self.events if e["type"] == "odion.tool.activity"], ["started", "completed"])

    async def test_signed_exemption_runs_all_bank_tools_without_voice_or_auth_events(self):
        bank = BankingTools(self.http, self.context, "momo-session", self.send,
                            {**identity(), "voice_auth_required": False})
        bank.compare = AsyncMock(return_value={"matched": False})
        try:
            bank.ingest_pcm(b"\x01\x00" * 24000)
            bank.utterance(b"\x01\x00" * 24000)
            await bank.check_session(1, b"audio")
            for name in sorted(BANK_TOOLS):
                with self.subTest(name=name):
                    if name == "wema_execute_prepared":
                        bank.prepared = {"operation_id": "latest", "preview": {}}
                        call = self.call(name, operation_id="latest")
                        result = await bank.execute(call, confirmed_operation="latest")
                    else:
                        result = await bank.execute(self.call(name))
                    self.assertEqual(result["status"], "ok")
                    self.assertEqual(result["auth_status"], "not_required")
            bank.compare.assert_not_awaited()
            self.assertEqual(len(self.requests), len(BANK_TOOLS))
            self.assertEqual(len(self.events), 2 * len(BANK_TOOLS))
            self.assertTrue(all(event["type"] == "odion.tool.activity" for event in self.events))
            self.assertFalse(bank.tasks)
            self.assertFalse(bank.audio_buffer)
            self.assertIn("disabled voice verification", bank.prompt())
        finally:
            await bank.close()

    async def test_exemption_keeps_identity_schema_and_confirmation_guards(self):
        bank = BankingTools(self.http, self.context, "momo-session", self.send,
                            {**identity(), "voice_auth_required": False})
        try:
            bank.prepared = {"operation_id": "latest", "preview": {}}
            result = await bank.execute(self.call("wema_execute_prepared", operation_id="latest"))
            self.assertEqual(result["status"], "failed")
            result = await bank.execute(self.call("wema_execute_prepared", operation_id="old"), confirmed_operation="old")
            self.assertEqual(result["status"], "failed")
            self.assertEqual((await bank.execute(self.call(skip_auth=True)))["status"], "needs_input")
            self.assertEqual((await bank.execute(self.call(source_account="invalid")))["status"], "needs_input")
            self.assertEqual((await bank.execute(self.call("unconfigured")))["status"], "failed")
            bank.identity = {}
            self.assertFalse(await bank.authorize())
            self.assertEqual((await bank.execute(self.call()))["status"], "failed")
            self.assertFalse(self.requests)
        finally:
            await bank.close()

    async def test_only_explicit_signed_false_disables_voice_checks(self):
        for value in (True, None, 0, "false", ""):
            bank = BankingTools(self.http, self.context, "saw-session", self.send,
                                {**identity(), "voice_auth_required": value})
            try:
                self.assertTrue(bank.voice_auth_required)
                self.assertIn("Both voice checks must pass", bank.prompt())
                self.assertTrue((await bank.execute(self.call()))["auth_required"])
            finally:
                await bank.close()
        self.assertTrue(self.bank.voice_auth_required)
        self.assertFalse(self.requests)

    async def test_failure_of_either_check_blocks_every_banking_tool(self):
        for name in BANK_TOOLS - {"wema_execute_prepared"}:
            for matches in ((False, True), (True, False)):
                with self.subTest(name=name, matches=matches):
                    self.bank.session_status = "pending"
                    self.bank.checked_generation = 0
                    self.bank.compare = AsyncMock(side_effect=[{"matched": value} for value in matches])
                    result = await self.bank.execute(self.call(name))
                    self.assertTrue(result["auth_required"])
        self.assertEqual(self.requests, [])

    async def test_next_utterance_retries_failed_session_then_fresh_check_each_tool(self):
        self.bank.compare = AsyncMock(side_effect=[{"matched": False}, {"matched": False}, {"matched": True}, {"matched": True}, {"matched": False}])
        await self.bank.execute(self.call())
        self.bank.generation += 1
        self.assertEqual((await self.bank.execute(self.call()))["status"], "ok")
        self.assertTrue((await self.bank.execute(self.call("wema_get_transactions")))["auth_required"])
        self.assertEqual(len(self.requests), 1)

    async def test_unsigned_session_and_model_auth_flags_are_rejected(self):
        self.bank.identity = {}
        self.assertEqual((await self.bank.execute(self.call()))["status"], "failed")
        self.bank.identity = identity()
        result = await self.bank.execute(self.call(skip_auth=True))
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(self.requests, [])

    async def test_own_line_defaults_other_line_requires_recipient(self):
        own = self.bank.arguments(self.call("wema_prepare_data_purchase"))
        self.assertEqual(own["phone_number"], "08000000000")
        with self.assertRaises(ValueError):
            self.bank.arguments(self.call("wema_prepare_data_purchase", recipient_scope="other"))
        other = self.bank.arguments(self.call("wema_prepare_data_purchase", recipient_scope="other", phone_number="08111111111"))
        self.assertEqual(other["phone_number"], "08111111111")
        self.assertNotIn("recipient_scope", other)

    async def test_execute_requires_latest_operation_and_explicit_confirmation(self):
        self.bank.prepared = {"operation_id": "latest", "preview": {}}
        self.assertEqual((await self.bank.execute(self.call("wema_execute_prepared", operation_id="latest")))["status"], "failed")
        self.assertEqual((await self.bank.execute(self.call("wema_execute_prepared", operation_id="old"), confirmed_operation="old"))["status"], "failed")
        self.assertEqual(self.requests, [])
        result = await self.bank.execute(self.call("wema_execute_prepared", operation_id="latest"), confirmed_operation="latest")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.bank.compare.await_count, 2)
        self.assertIsNone(self.bank.prepared)

    async def test_preparation_failure_invalidates_previous_preview(self):
        self.bank.prepared = {"operation_id": "old"}
        self.bank.compare = AsyncMock(return_value={"matched": False})
        await self.bank.execute(self.call("wema_prepare_transfer"))
        self.assertIsNone(self.bank.prepared)

    async def test_schema_and_config_restrict_tools(self):
        names = {item["function"]["name"] for item in action_tool_definitions(self.context)}
        self.assertEqual(names, BANK_TOOLS)
        with self.assertRaises(ValueError):
            self.bank.arguments(self.call(source_account="one two three"))
        result = await self.bank.execute(self.call("unconfigured"))
        self.assertEqual(result["status"], "failed")

    async def test_capture_keeps_existing_rolling_window_for_short_confirmation(self):
        self.bank.session_status = "verified"
        self.bank.ingest_pcm(b"\x01\x00" * 16000 * 10)
        self.bank.ingest_pcm(b"\x02\x00" * 4000)
        self.bank.utterance(b"\x02\x00" * 4000)
        self.assertEqual(len(self.bank.clip), 8 * 16000 * 2)
        self.assertTrue(self.bank.clip.endswith(b"\x02\x00" * 4000))
        self.bank.ingest_pcm(bytes(8 * 16000 * 2))
        self.bank.utterance(b"\x02\x00" * 4000)
        self.assertFalse(any(self.bank.clip))

    async def test_pipeline_uses_actual_result_and_preserves_tool_history(self):
        events = []
        call = self.call()
        class LLM:
            async def stream(inner, messages, *, tools=None):
                events.append(messages.copy())
                if not any(m["role"] == "tool" for m in messages):
                    yield call
                else:
                    self.assertIn("100.00", messages[-1]["content"])
                    yield "Your balance is one hundred naira."
        class TTS:
            async def stream(inner, text):
                async def chunks():
                    if False:
                        yield b""
                return 24000, chunks()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pipe = ConversationPipeline(LabConfig(trace_dir=root, report_dir=root), TraceRecorder(root), self.send,
                                        agent_context=self.context, session_identity=identity())
            pipe.banking.compare = self.bank.compare
            pipe.banking.generation = 1
            pipe.banking.clip = self.bank.clip
            pipe.llm, pipe.tts = LLM(), TTS()
            pipe.turn_id, pipe.final_text = "turn-1", "Check my balance"
            try:
                await pipe._start_attempt(pipe.final_text, speculative=False, authorized=True)
                await pipe.attempt.task
                self.assertEqual(len(self.requests), 1)
                self.assertEqual([m["role"] for m in pipe.history], ["user", "assistant", "tool", "assistant"])
                self.assertIsNone(pipe.pending_action)
            finally:
                await pipe.close()

    async def test_similar_bank_partial_is_not_authorized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pipe = ConversationPipeline(LabConfig(trace_dir=root, report_dir=root), TraceRecorder(root), self.send, agent_context=self.context)
            from latency_lab.pipeline import Attempt
            pipe.turn_id = "turn-1"
            pipe.state = "awaiting_final"
            attempt = Attempt("a", "Transfer 100 naira to 0123456789", True)
            pipe.attempt = attempt
            pipe._start_attempt = AsyncMock()
            try:
                await pipe.on_final("Transfer 100 naira to 0123456788")
                self.assertFalse(attempt.authorized)
                self.assertTrue(attempt.cancelled)
                pipe._start_attempt.assert_awaited_once()
                self.assertEqual(self.requests, [])
            finally:
                await pipe.close()

    async def test_knowledge_followup_keeps_bank_tools_and_both_voice_checks(self):
        for matched in (False, True):
            with self.subTest(matched=matched), tempfile.TemporaryDirectory() as directory:
                self.requests.clear()
                pipe = ConversationPipeline(LabConfig(), TraceRecorder(Path(directory)), self.send,
                                            agent_context=self.context, session_identity=identity())
                pipe.banking.compare = AsyncMock(return_value={"matched": matched, "score": .9 if matched else .1})
                pipe.banking.generation = 1
                pipe.banking.clip = self.bank.clip

                async def no_audio(_text):
                    async def chunks():
                        if False:
                            yield b""
                    return 24000, chunks()

                pipe.tts.stream = no_audio
                calls = []
                owner = self

                class LLM:
                    async def stream(self, messages, *, tools=None):
                        calls.append((messages.copy(), tools))
                        if len(calls) == 1:
                            yield "Let me check that for you."
                        elif len(calls) == 2:
                            owner.assertIn("wema_get_balance", {t["function"]["name"] for t in tools})
                            owner.assertIn("never invent values or placeholders", messages[0]["content"])
                            owner.assertIn("Both voice checks must pass", messages[0]["content"])
                            owner.assertIn("test-customer", messages[0]["content"])
                            yield owner.call()
                        else:
                            owner.assertEqual(messages[-1]["role"], "tool")
                            yield "Your balance is one hundred naira." if matched else "Please speak again to verify your voice."

                pipe.llm = LLM()
                pipe.turn_id, pipe.final_text = "turn-1", "Check my balance"
                try:
                    await pipe._start_attempt(pipe.final_text, speculative=False, authorized=True)
                    await pipe.attempt.task
                    self.assertEqual(len(calls), 3)
                    self.assertEqual(len(self.requests), 1 if matched else 0)
                    self.assertEqual(pipe.banking.compare.await_count, 2)
                    self.assertEqual([m["role"] for m in pipe.history], ["user", "assistant", "tool", "assistant"])
                finally:
                    await pipe.close()

    def test_qualified_yes_does_not_execute(self):
        self.assertEqual(ConversationPipeline._bank_confirmation("Yes, but change the amount to 500"), "unclear")
        self.assertEqual(ConversationPipeline._bank_confirmation("Yes, go ahead"), "confirmed")
