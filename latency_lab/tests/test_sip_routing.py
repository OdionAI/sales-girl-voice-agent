from __future__ import annotations

import unittest
from types import SimpleNamespace

from latency_lab.sip_routing import (
    extract_sip_party_number,
    load_number_agent_map,
    normalize_phone,
    resolve_sip_agent_route,
)


class SipRoutingTest(unittest.TestCase):
    def test_nigerian_phone_variants_normalize_to_same_key(self) -> None:
        expected = "+2348112812216"
        self.assertEqual(normalize_phone("08112812216"), expected)
        self.assertEqual(normalize_phone("002348112812216"), expected)
        self.assertEqual(normalize_phone("sip:+2348112812216@aicc.test"), expected)

    def test_explicit_callee_header_routes_to_saved_agent(self) -> None:
        routes = load_number_agent_map(
            '{"08112812216":{"config_agent_id":"agent-2",'
            '"business_id":"business-1","configured_agent_name":"Sarah"}}'
        )
        participant = SimpleNamespace(
            identity="sip_call-1",
            attributes={"sip.h.X-Callee-Number": "+2348112812216"},
        )

        route = resolve_sip_agent_route(participant, number_map=routes)

        self.assertIsNotNone(route)
        self.assertEqual(route.agent_id, "agent-2")
        self.assertEqual(route.business_id, "business-1")
        self.assertEqual(extract_sip_party_number(participant), "+2348112812216")

    def test_non_sip_participant_cannot_trigger_number_map(self) -> None:
        routes = load_number_agent_map(
            '{"08112812216":{"config_agent_id":"agent-2",'
            '"business_id":"business-1"}}'
        )
        participant = SimpleNamespace(
            identity="voice_assistant_user", attributes={"phoneNumber": "08112812216"}
        )

        self.assertIsNone(resolve_sip_agent_route(participant, number_map=routes))
