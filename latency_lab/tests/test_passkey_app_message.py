import asyncio
import json
import unittest

from latency_lab.passkey import PasskeyFlow, supported_options


class FakeTrace:
    def __init__(self):
        self.events = []

    def record(self, event, **data):
        self.events.append((event, data))

    def find(self, name):
        for event, data in self.events:
            if event == name:
                return data
        return None


def build():
    sent, outcomes = [], []

    async def send(payload):
        sent.append(payload)

    async def post(path, payload):
        return {"status": "ok", "data": {"authentication_id": "auth-1", "action": 0,
                                          "ceremony_mode": "authenticate",
                                          "options": {"challenge": "abc"}}}

    async def on_outcome(text, meta):
        outcomes.append((text, meta))

    trace = FakeTrace()
    flow = PasskeyFlow(send=send, post_json=post, on_outcome=on_outcome, trace=trace)
    return flow, sent, outcomes, trace


class PasskeyAppMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_app_failure_is_recorded_with_its_reason(self):
        flow, sent, outcomes, trace = build()
        started = await flow.start(workflow="transfer", operation_id="op-1",
                                   customer_id="R008448055", display_name="x")
        self.assertEqual(started["status"], "ceremony_started")
        ids = (flow.pending.authentication_id, flow.pending.correlation_id)

        await flow.receive(json.dumps({
            "type": "fido2_failed",
            "authenticationId": ids[0],
            "correlationId": ids[1],
            "errorCode": -1004,
            "message": "The operation couldn't be completed.",
        }).encode())

        message = trace.find("passkey_app_message")
        self.assertIsNotNone(message, "the app's raw message must be traced")
        self.assertEqual(message["kind"], "fido2_failed")
        self.assertTrue(message["authentication_id_match"])
        self.assertTrue(message["correlation_id_match"])
        self.assertEqual(message["errorCode"], "-1004")
        self.assertIn("couldn't be completed", message["message"])
        # every top-level key the app sent is recorded, so nothing is silently dropped
        self.assertIn("errorCode", message["all_keys"])
        self.assertIn("message", message["all_keys"])

        outcome = trace.find("passkey_outcome")
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcomes[0][1]["status"], "failed")

    async def test_proof_material_is_never_traced(self):
        flow, sent, outcomes, trace = build()
        await flow.start(workflow="transfer", operation_id="op-2",
                         customer_id="R008448055", display_name="x")
        ids = (flow.pending.authentication_id, flow.pending.correlation_id)
        await flow.receive(json.dumps({
            "type": "fido2_proof",
            "authenticationId": ids[0],
            "correlationId": ids[1],
            "proof": {"signature": "SECRET", "authenticatorData": "SECRET2"},
        }).encode())
        message = trace.find("passkey_app_message") or {}
        self.assertNotIn("signature", json.dumps(message))
        self.assertNotIn("authenticatorData", json.dumps(message))
        self.assertNotIn("SECRET", json.dumps(trace.events))

    async def test_session_context_handshake_is_recorded(self):
        flow, sent, outcomes, trace = build()
        await flow.receive(json.dumps({"type": "session_context", "deviceImei": "imei-1"}).encode())
        context = trace.find("passkey_session_context")
        self.assertIsNotNone(context)
        self.assertTrue(context["device_present"])
        self.assertNotIn("imei-1", json.dumps(trace.events))


class SupportedOptionsTests(unittest.IsolatedAsyncioTestCase):
    """The SDK rejects options containing fields its checker does not model
    (reason `unsupported_options`, check=options), so we forward only those it does."""

    REAL = {
        "rp": {"id": "nexus.alat.ng", "name": "AlatAuthNexus"},
        "user": {"name": "r008448055dev", "id": "UN60GTVOPkWVR65zfoiedg", "displayName": "x"},
        "challenge": "LW3P0cKzQBKFlSV_CNVCMw",
        "pubKeyCredParams": [{"type": "public-key", "alg": -7}],
        "timeout": 60000,
        "attestation": "none",
        "attestationFormats": [],
        "authenticatorSelection": {"residentKey": "preferred", "requireResidentKey": False,
                                   "userVerification": "discouraged", "unknownExtra": 1},
        "hints": [],
        "excludeCredentials": [],
        "extensions": {"credProps": True},
    }

    def test_unsupported_fields_are_removed(self):
        trimmed = supported_options(self.REAL)
        for gone in ("attestationFormats", "hints", "extensions"):
            self.assertNotIn(gone, trimmed)
        for kept in ("challenge", "rp", "user", "pubKeyCredParams", "attestation", "timeout",
                     "excludeCredentials"):
            self.assertIn(kept, trimmed)
        self.assertEqual(trimmed["authenticatorSelection"],
                         {"residentKey": "required", "userVerification": "required"})

    def test_off_switch_passes_options_through(self):
        import os
        os.environ["ATTENTIVE_PASSKEY_TRIM_OPTIONS"] = "0"
        try:
            self.assertEqual(supported_options(self.REAL), self.REAL)
        finally:
            os.environ.pop("ATTENTIVE_PASSKEY_TRIM_OPTIONS", None)

    async def test_start_sends_trimmed_options(self):
        sent = []

        async def send(payload):
            sent.append(payload)

        async def post(path, payload):
            return {"status": "ok", "data": {"authentication_id": "a1", "action": 1,
                                              "ceremony_mode": "register", "options": self.REAL}}

        flow = PasskeyFlow(send=send, post_json=post)
        await flow.start(workflow="transfer", operation_id="op", customer_id="R008448055",
                         display_name="x")
        options = sent[0]["options"]
        self.assertNotIn("hints", options)
        self.assertNotIn("extensions", options)
        self.assertIn("challenge", options)


    async def test_nested_diagnostic_is_recorded(self):
        flow, sent, outcomes, trace = build()
        await flow.start(workflow="transfer", operation_id="op-3",
                         customer_id="R008448055", display_name="x")
        ids = (flow.pending.authentication_id, flow.pending.correlation_id)
        await flow.receive(json.dumps({
            "type": "fido2_failed",
            "authenticationId": ids[0],
            "correlationId": ids[1],
            "reason": {"stage": "validation", "outcome": "failed",
                       "reason": "unsupported_options", "check": "options"},
            "diagnostics": {"fields": ["hints"], "systemError": {"domain": "x", "code": 1}},
        }).encode())
        message = trace.find("passkey_app_message")
        # `reason` is stored as `app_reason` (the trace event's own `reason` field is reserved)
        self.assertIn("unsupported_options", message["app_reason"])
        self.assertIn('"check":"options"', message["app_reason"])
        self.assertIn("hints", message["diagnostics"])
        self.assertIn("unsupported_options", json.dumps(message))


if __name__ == "__main__":
    unittest.main()
