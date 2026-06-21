import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent import ops_api


class _FakeLiveKitApi:
    def __init__(self, *, trunks=None, participant=None):
        self.sip = SimpleNamespace(
            list_sip_outbound_trunk=AsyncMock(
                return_value=SimpleNamespace(items=trunks or [])
            ),
            create_sip_participant=AsyncMock(return_value=participant or SimpleNamespace()),
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class AiccTransferToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_transfer_to_aicc_fails_without_room_name(self) -> None:
        result = await ops_api.transfer_to_aicc(metadata={"session_id": "sess-1"})
        self.assertEqual(result["status"], "failed")
        self.assertIn("room context", result["message"])

    async def test_transfer_to_aicc_places_outbound_sip_participant(self) -> None:
        fake_trunk = SimpleNamespace(
            sip_trunk_id="trunk-123",
            name="Huawei AICC Outbound Test",
        )
        fake_participant = SimpleNamespace(
            participant_identity="aicc_bridge_sess-1_3",
            participant_id="sip-participant-1",
            sip_call_id="call-123",
        )
        fake_api = _FakeLiveKitApi(trunks=[fake_trunk], participant=fake_participant)

        with (
            patch.object(ops_api, "_livekit_api", return_value=fake_api),
            patch.object(ops_api, "AICC_OUTBOUND_TRUNK_NAME", "Huawei AICC Outbound Test"),
            patch.object(ops_api, "AICC_OUTBOUND_TRUNK_ID", ""),
            patch.object(ops_api, "AICC_TRANSFER_TARGET_NUMBER", "02014114559"),
            patch.object(ops_api, "AICC_TRANSFER_FROM_NUMBER", "02014114559"),
        ):
            result = await ops_api.transfer_to_aicc(
                reason_summary="customer asked for a human",
                metadata={"room_name": "room-1", "session_id": "sess-1", "turn_index": 3},
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["room_name"], "room-1")
        self.assertEqual(result["target_number"], "02014114559")

        request = fake_api.sip.create_sip_participant.await_args.args[0]
        self.assertEqual(request.sip_trunk_id, "trunk-123")
        self.assertEqual(request.sip_call_to, "02014114559")
        self.assertEqual(request.sip_number, "02014114559")
        self.assertEqual(request.room_name, "room-1")
        self.assertEqual(request.participant_identity, "aicc_bridge_sess-1_3")


if __name__ == "__main__":
    unittest.main()
