import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from latency_lab import aicc_transfer
from latency_lab.action_tools import (
    TRANSFER_TO_AICC_TOOL,
    action_tool_definitions,
    transfer_tool_definitions,
)
from latency_lab.livekit_worker import LiveKitPCMTransport


class FakeSip:
    def __init__(self, participant=None, error=None):
        self._participant = participant
        self._error = error
        self.create_sip_participant = AsyncMock(side_effect=self._run)

    async def _run(self, request):
        if self._error is not None:
            raise self._error
        return self._participant


class FakeRoom:
    def __init__(self):
        self.remove_participant = AsyncMock()


class FakeClient:
    def __init__(self, participant=None, error=None):
        self.sip = FakeSip(participant, error)
        self.room = FakeRoom()


def _cfg(**over):
    base = {
        "AICC_OUTBOUND_TRUNK_ID": "ST_test",
        "AICC_TRANSFER_TARGET_NUMBER": "02017004200",
        "AICC_TRANSFER_FROM_NUMBER": "02017004200",
        "AICC_TRANSFER_PRESERVE_CALLER_ID": "true",
    }
    base.update(over)
    return base


class BridgeCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_not_configured_fails_fast(self):
        # Preserve-caller-id off and nothing configured: the destination check fires.
        result = await aicc_transfer.bridge_call(
            FakeClient(), room_name="", session_id="s1", reason_summary="x",
            caller_phone="", config=_cfg(AICC_OUTBOUND_TRUNK_ID="",
                                         AICC_TRANSFER_PRESERVE_CALLER_ID="false"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "aicc_destination_not_configured")

    async def test_preserve_caller_id_without_verified_phone_is_refused(self):
        result = await aicc_transfer.bridge_call(
            FakeClient(), room_name="room", session_id="s1", reason_summary="x",
            caller_phone="", config=_cfg())
        self.assertEqual(result["error"], "verified_sip_caller_required")

    async def test_connects_and_preserves_the_callers_number(self):
        client = FakeClient(participant=SimpleNamespace(
            participant_identity="aicc_bridge_s1", sip_call_id="SCL_abc"))
        result = await aicc_transfer.bridge_call(
            client, room_name="room", session_id="s1", reason_summary="x",
            caller_phone="+233201234567", config=_cfg())
        self.assertEqual(result["status"], "connected")
        self.assertEqual(result["sip_call_id"], "SCL_abc")
        self.assertEqual(result["target_number"], "02017004200")
        request = client.sip.create_sip_participant.await_args.args[0]
        self.assertEqual(request.sip_number, aicc_transfer.normalize_phone("+233201234567"))
        self.assertEqual(request.sip_trunk_id, "ST_test")
        self.assertEqual(request.sip_call_to, "02017004200")
        self.assertTrue(request.wait_until_answered)

    async def test_carrier_rejection_is_reported_and_orphan_leg_removed(self):
        class Boom(Exception):
            metadata = {"sip_status_code": "503"}

        client = FakeClient(error=Boom("503"))
        result = await aicc_transfer.bridge_call(
            client, room_name="room", session_id="s1", reason_summary="x",
            caller_phone="233201234567", config=_cfg())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "Boom")
        self.assertEqual(result["sip_status_code"], "503")
        client.room.remove_participant.assert_awaited()


class TransportDelegationTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_routes_transfer_messages_to_the_handler(self):
        handler = AsyncMock(return_value={"status": "connected"})
        transport = LiveKitPCMTransport(
            SimpleNamespace(), SimpleNamespace(record=lambda *a, **k: None),
            transfer_handler=handler)
        result = await transport.send({"type": "transfer_to_aicc", "reason_summary": "why"})
        self.assertEqual(result["status"], "connected")
        handler.assert_awaited_once()

    async def test_missing_handler_fails_honestly(self):
        transport = LiveKitPCMTransport(
            SimpleNamespace(), SimpleNamespace(record=lambda *a, **k: None))
        result = await transport.send({"type": "transfer_to_aicc", "reason_summary": "why"})
        self.assertEqual(result["error"], "transfer_handler_unavailable")


class TransferDefinitionTests(unittest.TestCase):
    def test_advertised_only_when_configured(self):
        self.assertEqual(transfer_tool_definitions(SimpleNamespace(tools=[])), [])
        off = SimpleNamespace(tools=[{"name": TRANSFER_TO_AICC_TOOL, "is_active": False}])
        self.assertEqual(transfer_tool_definitions(off), [])

    def test_advertised_with_a_required_reason(self):
        ctx = SimpleNamespace(tools=[{
            "name": TRANSFER_TO_AICC_TOOL, "url": "builtin://transfer_to_aicc"}])
        definitions = transfer_tool_definitions(ctx)
        self.assertEqual(definitions[0]["function"]["name"], TRANSFER_TO_AICC_TOOL)
        self.assertEqual(definitions[0]["function"]["parameters"]["required"], ["reason_summary"])

    def test_supported_tools_accept_the_handoff(self):
        from latency_lab.action_tools import SUPPORTED_ACTION_TOOLS
        self.assertIn(TRANSFER_TO_AICC_TOOL, SUPPORTED_ACTION_TOOLS)


if __name__ == "__main__":
    unittest.main()
