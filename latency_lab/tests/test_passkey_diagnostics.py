import json
import unittest
from unittest.mock import Mock, patch

from latency_lab.passkey_diagnostics import client_failure, record


class DiagnosticTests(unittest.TestCase):
    def test_sdk_reason_and_system_code_are_preserved(self):
        self.assertEqual(client_failure({"reason": "authorization_failed", "diagnostics": {
            "stage": "presentation", "check": "presentationAnchor",
            "systemDomain": "authorization", "systemCode": 1004}}),
            {"reason": "authorization_failed", "client_stage": "presentation",
             "check": "presentationAnchor", "system_domain": "authorization", "system_code": 1004})

    def test_untrusted_values_never_enter_logs_or_trace(self):
        trace = Mock()
        fields = {"reason": "private-device", "client_stage": "private-customer", "check": "private-challenge",
                  "system_domain": "private-domain", "system_code": True, "http_status": 500,
                  "proof": "private-proof", "deviceImei": "private-device"}
        with patch.dict("os.environ", {"ATTENTIVE_PASSKEY_DIAGNOSTICS_ENABLED": "1"}), \
             self.assertLogs("latency_lab.passkey_diagnostics", level="INFO") as logs:
            record("receipt", "failed", fields, trace=trace)
        serialized = json.dumps(trace.record.call_args.kwargs) + " ".join(logs.output)
        self.assertNotIn("private-", serialized)
        self.assertNotIn("proof", serialized)
        self.assertNotIn("deviceImei", serialized)
        self.assertNotIn("system_code", serialized)
        self.assertEqual(trace.record.call_args.kwargs["http_status"], 500)

    def test_malformed_client_fields_are_bounded(self):
        for details in [None, [], "private-proof", {"stage": [], "check": {}, "systemCode": 2**65}]:
            self.assertEqual(client_failure({"reason": {"private-proof": True}, "diagnostics": details}), {"reason": "unknown"})

    def test_disabled_has_no_observable_output(self):
        trace = Mock()
        with patch.dict("os.environ", {"ATTENTIVE_PASSKEY_DIAGNOSTICS_ENABLED": "0"}), \
             patch("latency_lab.passkey_diagnostics.logger.info") as log:
            record("receipt", "failed", {"reason": "invalid_request"}, trace=trace)
        trace.record.assert_not_called(); log.assert_not_called()
