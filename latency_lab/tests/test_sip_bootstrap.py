"""Tests for AICC inbound number routing (sip_bootstrap).

Loaded directly from file so the test does not import the heavy latency_lab
package (and its runtime dependencies).
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import unittest
from types import SimpleNamespace

_SPEC = importlib.util.spec_from_file_location(
    "sip_bootstrap", pathlib.Path(__file__).resolve().parents[1] / "sip_bootstrap.py"
)
assert _SPEC is not None and _SPEC.loader is not None
sip_bootstrap = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sip_bootstrap)

TRUNK = "ST_TEST"
ROUTE = {
    "trunk_ids": [TRUNK],
    "room_prefix": "aicc-lara-",
    "business_id": "biz-default",
    "config_agent_id": "agent-default",
    "configured_agent_name": "Default",
    "did_routes": {
        "+233308048122": {"business_id": "biz-ghana", "config_agent_id": "agent-sonia",
                          "configured_agent_name": "Sonia"},
        "0301051": {"business_id": "biz-momo", "config_agent_id": "agent-feedback",
                     "configured_agent_name": "MoMo Feedback", "outbound_required": True},
        "0301050": {"business_id": "biz-momo", "config_agent_id": "agent-momo",
                    "configured_agent_name": "Amaka"},
    },
    "caller_routes": {
        "301050": {"business_id": "biz-momo", "config_agent_id": "agent-momo",
                   "configured_agent_name": "Amaka"},
        "+2347000000787": {"business_id": "biz-teni", "config_agent_id": "agent-teni",
                           "configured_agent_name": "Teni"},
        "+2348112812216": {
            "business_id": "biz-leadway", "config_agent_id": "agent-leadway",
            "configured_agent_name": "Amaka", "override_did_routes": True,
        },
    },
}


def _participant(caller="", called="", trunk=TRUNK, kind="sip"):
    attrs = {"sip.trunkID": trunk, "sip.phoneNumber": caller}
    if called:
        attrs["sip.h.X-Callee-Number"] = called
    return SimpleNamespace(identity=f"sip_{caller or 'x'}", kind=kind, attributes=attrs)


class AiccNumberRoutingTest(unittest.TestCase):
    def _identity(self, participant):
        route = {"trunk_ids": ROUTE["trunk_ids"], "room_prefix": ROUTE["room_prefix"],
                 "expected_trunk_id": TRUNK, **{k: v for k, v in ROUTE.items()}}
        return sip_bootstrap.participant_identity(route, participant, "aicc-lara-1", "sip")

    def test_caller_from_number_301050_routes_to_momo(self) -> None:
        ident = self._identity(_participant(caller="301050"))
        self.assertEqual(ident["business_id"], "biz-momo")
        self.assertEqual(ident["config_agent_id"], "agent-momo")

    def test_called_did_takes_precedence_over_caller_route(self) -> None:
        ident = self._identity(_participant(caller="301050", called="+233308048122"))
        self.assertEqual(ident["config_agent_id"], "agent-sonia")

    def test_dashboard_route_overrides_static_did_route(self) -> None:
        p = _participant(caller="+2348012345678", called="0301051")
        ident = sip_bootstrap.participant_identity(
            {"trunk_ids": ROUTE["trunk_ids"], "room_prefix": ROUTE["room_prefix"],
             "expected_trunk_id": TRUNK, **ROUTE},
            p,
            "aicc-lara-1",
            "sip",
            dynamic_did_route={
                "business_id": "biz-momo",
                "config_agent_id": "agent-hausa",
                "configured_agent_name": "Abdul",
            },
        )
        self.assertEqual(ident["config_agent_id"], "agent-hausa")
        self.assertEqual(ident["configured_agent_name"], "Abdul")

    def test_explicit_caller_override_still_precedes_dashboard_did_route(self) -> None:
        p = _participant(caller="08112812216", called="0301051")
        ident = sip_bootstrap.participant_identity(
            {"trunk_ids": ROUTE["trunk_ids"], "room_prefix": ROUTE["room_prefix"],
             "expected_trunk_id": TRUNK, **ROUTE},
            p,
            "aicc-lara-1",
            "sip",
            dynamic_did_route={
                "business_id": "biz-momo",
                "config_agent_id": "agent-hausa",
                "configured_agent_name": "Abdul",
            },
        )
        self.assertEqual(ident["config_agent_id"], "agent-leadway")

    def test_sip_to_short_code_routes_to_feedback_agent(self) -> None:
        p = _participant(caller="+2348012345678")
        p.attributes["sip.To"] = "sip:0301051@aicc.example"
        ident = self._identity(p)
        self.assertEqual(ident["config_agent_id"], "agent-feedback")
        self.assertTrue(ident["outbound_required"])
        # The customer's (from) number is exposed for outbound context matching.
        self.assertEqual(ident["caller_number"], "+2348012345678")

    def test_remote_to_header_overrides_trunk_number(self) -> None:
        p = _participant(caller="+2348012345678", called="030105")
        route = {"trunk_ids": ROUTE["trunk_ids"], "room_prefix": ROUTE["room_prefix"],
                 "expected_trunk_id": TRUNK, **ROUTE}
        ident = sip_bootstrap.participant_identity(
            route, p, "aicc-lara-1", "sip", called_override="<sip:0301051@aicc.example>"
        )
        self.assertEqual(ident["config_agent_id"], "agent-feedback")

    def test_caller_number_cannot_select_outbound_to_route(self) -> None:
        ident = self._identity(_participant(caller="0301051", called="030105"))
        self.assertNotEqual(ident["config_agent_id"], "agent-feedback")

    def test_explicit_leadway_caller_override_wins_over_rewritten_momo_did(self) -> None:
        ident = self._identity(_participant(caller="08112812216", called="0301050"))
        self.assertEqual(ident["business_id"], "biz-leadway")
        self.assertEqual(ident["config_agent_id"], "agent-leadway")

    def test_known_caller_routes_to_its_agent(self) -> None:
        ident = self._identity(_participant(caller="+2347000000787"))
        self.assertEqual(ident["config_agent_id"], "agent-teni")

    def test_unknown_numbers_fall_back_to_default(self) -> None:
        ident = self._identity(_participant(caller="+2348000000000"))
        self.assertEqual(ident["config_agent_id"], "agent-default")

    def test_called_number_matches_on_last_ten_digits(self) -> None:
        ident = self._identity(_participant(called="233308048122"))
        self.assertEqual(ident["config_agent_id"], "agent-sonia")

    def test_dialed_number_found_in_an_unexpected_attribute(self) -> None:
        # The dialed number arrives under an attribute name we do not know about:
        # the configured DID must still be found by scanning the call's values.
        p = SimpleNamespace(
            kind="sip",
            identity="sip_+2330000000000",
            attributes={"sip.trunkID": TRUNK, "sip.phoneNumber": "+2330000000000",
                        "sip.h.X-Some-Forwarded-To": "233308048122"},
        )
        ident = self._identity(p)
        self.assertEqual(ident["config_agent_id"], "agent-sonia")

    def test_dialed_number_from_livekit_trunk_attribute(self) -> None:
        p = SimpleNamespace(
            kind="sip",
            identity="sip_+2330000000000",
            attributes={"sip.trunkID": TRUNK, "sip.phoneNumber": "+2330000000000",
                        "sip.trunkPhoneNumber": "+233308048122"},
        )
        ident = self._identity(p)
        self.assertEqual(ident["config_agent_id"], "agent-sonia")

    def test_non_trunk_participant_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._identity(_participant(caller="301050", trunk="ST_OTHER"))

    def test_normalize_number(self) -> None:
        self.assertEqual(sip_bootstrap.normalize_number("sip:301050@aicc.test"), "301050")
        self.assertEqual(sip_bootstrap.normalize_number("+233 30 804 8122"), "233308048122")


if __name__ == "__main__":
    unittest.main()
