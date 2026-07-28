import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent import ops_api


class _FakeLiveKitApi:
    def __init__(
        self,
        *,
        trunks=None,
        participant=None,
        create_side_effect=None,
        transfer_participant=None,
        transfer_side_effect=None,
    ):
        create_mock = AsyncMock(return_value=participant or SimpleNamespace())
        if create_side_effect is not None:
            create_mock = AsyncMock(side_effect=create_side_effect)
        transfer_mock = AsyncMock(
            return_value=transfer_participant or SimpleNamespace()
        )
        if transfer_side_effect is not None:
            transfer_mock = AsyncMock(side_effect=transfer_side_effect)
        self.sip = SimpleNamespace(
            list_sip_outbound_trunk=AsyncMock(
                return_value=SimpleNamespace(items=trunks or [])
            ),
            create_sip_participant=create_mock,
            transfer_sip_participant=transfer_mock,
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
            patch.object(ops_api, "AICC_TRANSFER_CALLER_ID_MODE", "caller_then_configured"),
            patch.object(ops_api, "AICC_TRANSFER_STRATEGY", "bridge"),
            patch.object(ops_api, "AICC_HANDOFF_DELAY_SECONDS", 0),
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
        self.assertEqual(request.participant_identity, "aicc_bridge_sess-1_3_1")

    async def test_transfer_to_aicc_retries_with_configured_number(self) -> None:
        fake_trunk = SimpleNamespace(
            sip_trunk_id="trunk-123",
            name="Huawei AICC Outbound Test",
        )
        fake_participant = SimpleNamespace(
            participant_identity="aicc_bridge_sess-1_3_2",
            participant_id="sip-participant-1",
            sip_call_id="call-123",
        )
        fake_api = _FakeLiveKitApi(
            trunks=[fake_trunk],
            create_side_effect=[
                RuntimeError("SIP call failed: 500 Server Internal Error"),
                fake_participant,
            ],
        )

        with (
            patch.object(ops_api, "_livekit_api", return_value=fake_api),
            patch.object(ops_api, "AICC_OUTBOUND_TRUNK_NAME", "Huawei AICC Outbound Test"),
            patch.object(ops_api, "AICC_OUTBOUND_TRUNK_ID", ""),
            patch.object(ops_api, "AICC_TRANSFER_TARGET_NUMBER", "02014114559"),
            patch.object(ops_api, "AICC_TRANSFER_FROM_NUMBER", "02014114559"),
            patch.object(ops_api, "AICC_TRANSFER_CALLER_ID_MODE", "caller_then_configured"),
            patch.object(ops_api, "AICC_TRANSFER_NORMALIZE_NG_CALLER", False),
            patch.object(ops_api, "AICC_TRANSFER_STRATEGY", "bridge"),
            patch.object(ops_api, "AICC_HANDOFF_DELAY_SECONDS", 0),
        ):
            result = await ops_api.transfer_to_aicc(
                reason_summary="customer asked for a human",
                metadata={
                    "room_name": "room-1",
                    "session_id": "sess-1",
                    "turn_index": 3,
                    "sip_caller_number": "7033590787",
                },
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["from_number"], "02014114559")
        self.assertEqual(result["attempted_from_numbers"], ["7033590787", "02014114559"])

        first_request = fake_api.sip.create_sip_participant.await_args_list[0].args[0]
        second_request = fake_api.sip.create_sip_participant.await_args_list[1].args[0]
        self.assertEqual(first_request.sip_number, "7033590787")
        self.assertEqual(second_request.sip_number, "02014114559")

    async def test_transfer_to_aicc_prefers_refer_when_sip_participant_is_known(self) -> None:
        fake_trunk = SimpleNamespace(
            sip_trunk_id="trunk-123",
            name="Huawei AICC Outbound Test",
            address="159.138.166.114:65108",
            transport="udp",
        )
        fake_api = _FakeLiveKitApi(
            trunks=[fake_trunk],
            transfer_participant=SimpleNamespace(
                participant_identity="sip_7033590787",
                participant_id="sip-participant-1",
                sip_call_id="call-123",
            ),
        )

        with (
            patch.object(ops_api, "_livekit_api", return_value=fake_api),
            patch.object(ops_api, "AICC_OUTBOUND_TRUNK_NAME", "Huawei AICC Outbound Test"),
            patch.object(ops_api, "AICC_OUTBOUND_TRUNK_ID", ""),
            patch.object(ops_api, "AICC_TRANSFER_TARGET_NUMBER", "02014114559"),
            patch.object(ops_api, "AICC_TRANSFER_STRATEGY", "refer_then_bridge"),
            patch.object(ops_api, "AICC_HANDOFF_DELAY_SECONDS", 0),
        ):
            result = await ops_api.transfer_to_aicc(
                reason_summary="customer asked for a human",
                metadata={
                    "room_name": "room-1",
                    "session_id": "sess-1",
                    "turn_index": 3,
                    "sip_caller_number": "7033590787",
                },
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["transfer_mode"], "sip_refer")
        self.assertEqual(result["transfer_to"], "sip:02014114559@159.138.166.114:65108;transport=udp")
        fake_api.sip.create_sip_participant.assert_not_awaited()

        request = fake_api.sip.transfer_sip_participant.await_args.args[0]
        self.assertEqual(request.participant_identity, "sip_7033590787")
        self.assertEqual(request.room_name, "room-1")
        self.assertEqual(request.transfer_to, "sip:02014114559@159.138.166.114:65108;transport=udp")


if __name__ == "__main__":
    unittest.main()
