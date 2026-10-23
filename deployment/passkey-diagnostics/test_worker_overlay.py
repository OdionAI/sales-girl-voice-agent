"""Run inside the candidate real-bank worker; no bank/network calls."""
import json
import unittest

from latency_lab.passkey import PasskeyFlow


class OverlayTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_failure_is_diagnosed_and_never_executes(self):
        events, sent, calls = [], [], []
        async def send(packet): sent.append(packet)
        async def post(path, payload):
            calls.append(path)
            return {"status": "ok", "data": {"authentication_id": "fixture-auth", "action": 1,
                "ceremony_mode": "authenticate", "options": {"challenge": "AQID", "rpId": "example.com"}}}
        flow = PasskeyFlow(send=send, post_json=post, diagnostic=lambda *event: events.append(event))
        await flow.receive(json.dumps({"type": "session_context", "deviceImei": "private-device"}).encode())
        await flow.start(workflow="airtime", operation_id="fixture-op", customer_id="fixture", display_name="Fixture")
        await flow.receive(json.dumps({"type": "fido2_failed", "authenticationId": "fixture-auth",
            "correlationId": "ceremony-fixture-op", "reason": "authorization_failed", "diagnostics": {
                "stage": "presentation", "systemDomain": "authorization", "systemCode": 1004,
                "proof": "private-proof"}}).encode())
        self.assertEqual(calls, ["/v1/fido2/begin"])
        self.assertIn(("ceremony_send", "requested"), events)
        failure = next(e for e in events if e[:2] == ("receipt", "failed"))
        self.assertEqual(failure[2], {"reason": "authorization_failed", "client_stage": "presentation",
                                      "system_domain": "authorization", "system_code": 1004})
        self.assertNotIn("private-", json.dumps(events))
        self.assertIsNone(flow.pending)
        await flow.close()

    async def test_legacy_client_reason_still_reaches_diagnostics(self):
        events = []
        async def send(packet): pass
        async def post(path, payload):
            return {"status": "ok", "data": {"authentication_id": "fixture-auth", "action": 1,
                "ceremony_mode": "authenticate", "options": {"challenge": "AQID", "rpId": "example.com"}}}
        flow = PasskeyFlow(send=send, post_json=post, diagnostic=lambda *event: events.append(event))
        await flow.start(workflow="airtime", operation_id="fixture-op", customer_id="fixture", display_name="Fixture")
        await flow.receive(json.dumps({"type": "fido2_failed", "authenticationId": "fixture-auth",
            "correlationId": "ceremony-fixture-op", "reason": "unsupported_options"}).encode())
        self.assertIn(("receipt", "failed", {"reason": "unsupported_options"}), events)
        await flow.close()
