from __future__ import annotations

import json
from types import SimpleNamespace
import unittest

import main


class SawwSessionProfileTests(unittest.TestCase):
    def _ctx(self, metadata: dict) -> SimpleNamespace:
        participant = SimpleNamespace(metadata=json.dumps(metadata))
        return SimpleNamespace(
            room=SimpleNamespace(remote_participants={"caller": participant})
        )

    def test_reads_profile_only_for_the_saww_public_agent(self) -> None:
        extras = main._extract_session_extras_from_ctx(
            self._ctx(
                {
                    "agent_public_id": "agt_bedb4b6498",
                    "configured_agent_name": "SAWW",
                    "wema_context": {
                        "customer_id": "demo-customer-01",
                        "phone_number": "08012345678",
                        "account_number": "1234567890",
                    },
                }
            )
        )

        self.assertEqual(
            extras["wema_context"],
            {
                "customer_id": "demo-customer-01",
                "phone_number": "08012345678",
                "account_number": "1234567890",
            },
        )

    def test_rejects_profile_for_other_public_agent_or_invalid_fields(self) -> None:
        other_agent = main._extract_session_extras_from_ctx(
            self._ctx(
                {
                    "agent_public_id": "agt-another",
                    "configured_agent_name": "SAWW",
                    "wema_context": {
                        "customer_id": "demo-customer-01",
                        "phone_number": "08012345678",
                    },
                }
            )
        )
        invalid_phone = main._extract_session_extras_from_ctx(
            self._ctx(
                {
                    "agent_public_id": "agt_bedb4b6498",
                    "configured_agent_name": "SAWW",
                    "wema_context": {
                        "customer_id": "demo-customer-01",
                        "phone_number": "invalid",
                    },
                }
            )
        )

        self.assertNotIn("wema_context", other_agent)
        self.assertNotIn("wema_context", invalid_phone)


if __name__ == "__main__":
    unittest.main()
