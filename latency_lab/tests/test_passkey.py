"""Unit tests for the two-phase passkey flow (no LiveKit, no network)."""
from __future__ import annotations

import asyncio
import json
import os
import unittest

from latency_lab.passkey import TOPIC, PasskeyFlow


class Recorder:
    def __init__(self):
        self.sent: list[dict] = []
        self.posts: list[tuple[str, dict]] = []
        self.begin_reply = {
            "status": "ok",
            "data": {
                "authentication_id": "abc-123",
                "action": 1,
                "ceremony_mode": "authenticate",
                "options": {"challenge": "AQID", "rpId": "auth.bank.example"},
            },
        }
        self.execute_reply = {"status": "ok", "message": "Your transfer is on its way."}
        self.begin_error: Exception | None = None

    async def send(self, payload):
        self.sent.append(payload)

    async def post_json(self, path, payload):
        self.posts.append((path, payload))
        if path == "/v1/fido2/begin":
            if self.begin_error is not None:
                raise self.begin_error
            return self.begin_reply
        return self.execute_reply


def make(recorder, outcomes, *, timeout=90.0):
    async def on_outcome(text, meta):
        outcomes.append((text, meta))

    return PasskeyFlow(
        send=recorder.send, post_json=recorder.post_json, on_outcome=on_outcome, timeout=timeout
    )


def proof(authentication_id="abc-123", correlation="", body=None):
    return json.dumps(
        {
            "type": "fido2_proof",
            "authenticationId": authentication_id,
            "correlationId": correlation,
            "proof": body or {"id": "cred", "response": {"signature": "sig"}},
        }
    ).encode()


class StartTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_publishes_ceremony_on_the_private_topic(self):
        rec, out = Recorder(), []
        flow = make(rec, out)
        result = await flow.start(
            workflow="transfer", operation_id="op-1", customer_id="R008448055", display_name="Woron"
        )
        self.assertEqual(result["status"], "ceremony_started")
        self.assertNotIn("Face ID", json.dumps(rec.posts))
        path, body = rec.posts[0]
        self.assertEqual(path, "/v1/fido2/begin")
        self.assertEqual(body["feature"], "Transfer")
        self.assertEqual(body["display_name"], "Woron")
        ceremony = rec.sent[0]
        self.assertEqual(ceremony["type"], "fido2_ceremony")
        self.assertEqual(ceremony["authenticationId"], "abc-123")
        self.assertEqual(ceremony["options"]["challenge"], "AQID")
        self.assertTrue(ceremony["usePasskey"])
        self.assertTrue(ceremony["correlationId"].startswith("ceremony-"))
        self.assertTrue(flow.awaiting())

    async def test_retry_for_same_operation_uses_a_fresh_correlation_id(self):
        rec, out = Recorder(), []
        flow = make(rec, out)
        await flow.start(workflow="transfer", operation_id="op-1", customer_id="C", display_name="D")
        first_correlation = flow.pending.correlation_id
        await flow.receive(json.dumps({
            "type": "fido2_failed", "authenticationId": "abc-123",
            "correlationId": first_correlation, "reason": "presentation_unavailable",
        }).encode())
        rec.begin_reply["data"]["authentication_id"] = "abc-456"
        await flow.start(workflow="transfer", operation_id="op-1", customer_id="C", display_name="D")
        self.assertEqual(flow.pending.operation_id, "op-1")
        self.assertNotEqual(flow.pending.correlation_id, first_correlation)
        self.assertEqual(rec.sent[-1]["authenticationId"], "abc-456")

    async def test_airtime_maps_to_the_airtime_feature(self):
        rec, out = Recorder(), []
        flow = make(rec, out)
        await flow.start(workflow="airtime", operation_id="op-2", customer_id="C", display_name="D")
        self.assertEqual(rec.posts[0][1]["feature"], "Airtime")

    async def test_begin_failure_is_reported_and_publishes_nothing(self):
        rec, out = Recorder(), []
        rec.begin_error = RuntimeError("boom")
        flow = make(rec, out)
        result = await flow.start(
            workflow="transfer", operation_id="op-1", customer_id="C", display_name="D"
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(rec.sent, [])
        self.assertFalse(flow.awaiting())

    async def test_a_second_request_while_waiting_is_busy(self):
        rec, out = Recorder(), []
        flow = make(rec, out)
        await flow.start(workflow="transfer", operation_id="op-1", customer_id="C", display_name="D")
        again = await flow.start(
            workflow="transfer", operation_id="op-2", customer_id="C", display_name="D"
        )
        self.assertEqual(again["status"], "busy")

    async def test_device_id_from_session_context_is_sent_to_begin(self):
        rec, out = Recorder(), []
        flow = make(rec, out)
        await flow.receive(json.dumps({"type": "session_context", "deviceImei": "IMEI-9"}).encode())
        await flow.start(workflow="transfer", operation_id="op-1", customer_id="C", display_name="D")
        self.assertEqual(rec.posts[0][1]["device_imei"], "IMEI-9")


class ProofTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.rec, self.out = Recorder(), []
        self.flow = make(self.rec, self.out)
        await self.flow.start(
            workflow="transfer", operation_id="op-1", customer_id="R008448055", display_name="Woron"
        )
        self.correlation = self.flow.pending.correlation_id

    async def test_valid_proof_executes_with_the_ceremony(self):
        await self.flow.receive(proof(correlation=self.correlation))
        path, body = self.rec.posts[-1]
        self.assertEqual(path, "/v1/tools/wema_execute_prepared")
        self.assertEqual(body["operation_id"], "op-1")
        self.assertEqual(body["authentication_id"], "abc-123")
        self.assertEqual(body["fido2_credential_proof"]["id"], "cred")
        result = self.rec.sent[-1]
        self.assertEqual(result["type"], "fido2_result")
        self.assertTrue(result["accepted"])
        self.assertEqual(self.out[0][0], "Your transfer is on its way.")
        self.assertFalse(self.flow.awaiting())

    async def test_mismatched_authentication_id_is_ignored(self):
        before = len(self.rec.posts)
        await self.flow.receive(proof(authentication_id="other", correlation=self.correlation))
        self.assertEqual(len(self.rec.posts), before)
        self.assertTrue(self.flow.awaiting())

    async def test_proof_is_single_use(self):
        await self.flow.receive(proof(correlation=self.correlation))
        posts = len(self.rec.posts)
        await self.flow.receive(proof(correlation=self.correlation))
        self.assertEqual(len(self.rec.posts), posts)

    async def test_cancel_never_executes(self):
        await self.flow.receive(
            json.dumps(
                {"type": "fido2_cancelled", "authenticationId": "abc-123", "correlationId": self.correlation}
            ).encode()
        )
        paths = [p for p, _ in self.rec.posts]
        self.assertNotIn("/v1/tools/wema_execute_prepared", paths)
        self.assertEqual(self.out[0][1]["status"], "cancelled")

    async def test_expired_proof_never_executes(self):
        self.flow.pending.expires = 0
        await self.flow.receive(proof(correlation=self.correlation))
        paths = [p for p, _ in self.rec.posts]
        self.assertNotIn("/v1/tools/wema_execute_prepared", paths)
        self.assertEqual(self.out[0][1]["status"], "expired")

    async def test_bank_rejection_is_reported(self):
        self.rec.execute_reply = {"status": "failed", "message": "The bank declined the transfer."}
        await self.flow.receive(proof(correlation=self.correlation))
        self.assertEqual(self.rec.sent[-1]["accepted"], False)
        self.assertEqual(self.out[0][1]["status"], "rejected")
        self.assertIn("declined", self.out[0][0])


class CloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_drops_the_pending_ceremony(self):
        rec, out = Recorder(), []
        flow = make(rec, out)
        await flow.start(workflow="transfer", operation_id="op-1", customer_id="C", display_name="D")
        correlation = flow.pending.correlation_id
        await flow.close()
        self.assertFalse(flow.awaiting())
        await flow.receive(proof(correlation=correlation))
        self.assertEqual([p for p, _ in rec.posts], ["/v1/fido2/begin"])


class GateTests(unittest.TestCase):
    def test_enabled_matches_dashed_and_undashed_ids(self):
        from unittest.mock import patch as _patch

        from latency_lab.passkey import enabled

        env = {
            "ATTENTIVE_PASSKEY_REAL_BUSINESS_IDS": "a0c8cda23b464ee0898c48f9a4d3a444",
            "ATTENTIVE_PASSKEY_REAL_AGENT_IDS": "f90e2db36e124b4f94fa905b83a2adfd",
        }
        with _patch.dict(os.environ, env):
            self.assertTrue(enabled({
                "business_id": "a0c8cda2-3b46-4ee0-898c-48f9a4d3a444",
                "config_agent_id": "f90e2db3-6e12-4b4f-94fa-905b83a2adfd",
            }))
            self.assertFalse(enabled({
                "business_id": "a0c8cda2-3b46-4ee0-898c-48f9a4d3a444",
                "config_agent_id": "some-other-agent",
            }))


if __name__ == "__main__":
    unittest.main()
