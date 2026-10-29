import asyncio
import json
import os
import unittest
from unittest.mock import patch

from agent import call_capacity_api
from latency_lab.outbound import sip_remote_headers


class CallCapacityApiTests(unittest.TestCase):
    def test_capacity_api_claim_sends_worker_auth_and_room(self):
        self._test_capacity_api_claim_sends_worker_auth_and_room()

    def _test_capacity_api_claim_sends_worker_auth_and_room(self):
        env = {
            "CONVERSATION_SERVICE_BASE_URL": "http://conversation.internal",
            "CONVERSATION_SERVICE_TOKEN": "service-secret",
        }
        with patch.dict(os.environ, env):
            self._assert_capacity_api_claim()

    def _assert_capacity_api_claim(self):
        requests = []

        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {"reservation_id": "opaque", "status": "active"}

        class Client:
            def __init__(self, *, timeout):
                assert timeout == 8

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, url, *, json, headers):
                requests.append((url, json, headers))
                return Response()

        with patch.object(call_capacity_api.httpx, "AsyncClient", Client):
            result = asyncio.run(call_capacity_api.claim_call(
                reservation_id="opaque-token", room_name="momo-room-1"
            ))
        self.assertEqual(result["status"], "active")
        self.assertEqual(requests, [(
            "http://conversation.internal/v1/internal/aicc/capacity/reservations/opaque-token/claim",
            {"room_name": "momo-room-1"},
            {
                "Content-Type": "application/json",
                "X-Service-Token": "service-secret",
                "X-Service-Name": "sales-girl-voice-agent",
            },
        )])

    def test_capacity_gate_is_scoped_to_configured_momo_business(self):
        env = {"MOMO_CALL_CAPACITY_BUSINESS_ID": "momo-business"}
        with patch.dict(os.environ, env):
            self.assertTrue(call_capacity_api.is_enabled_for_business("momo-business"))
            self.assertFalse(call_capacity_api.is_enabled_for_business("another-business"))
            self.assertEqual(call_capacity_api.reservation_id_from_sip_headers({}, "another-business"), "")
            with self.assertRaises(call_capacity_api.CallCapacityError):
                call_capacity_api.reservation_id_from_sip_headers({}, "momo-business")
            self.assertEqual(call_capacity_api.reservation_id_from_sip_headers(
                {"x-odion-capacity-reservation": "reserved"}, "momo-business"
            ), "reserved")

    def test_sip_remote_header_request_includes_capacity_reservation(self):
        self._test_sip_remote_header_request_includes_capacity_reservation()

    def _test_sip_remote_header_request_includes_capacity_reservation(self):
        requested = []

        class LocalParticipant:
            async def perform_rpc(self, **kwargs):
                requested.append(kwargs)
                return json.dumps({"headers": {"X-Odion-Capacity-Reservation": "reserve-123"}})

        class Room:
            local_participant = LocalParticipant()

        class Context:
            room = Room()

        class Participant:
            identity = "sip-caller"

        headers = asyncio.run(sip_remote_headers(Context(), Participant()))
        self.assertEqual(headers["x-odion-capacity-reservation"], "reserve-123")
        included = json.loads(requested[0]["payload"])["include"]
        self.assertIn("X-Odion-Capacity-Reservation", included)
