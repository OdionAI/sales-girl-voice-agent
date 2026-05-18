from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from agent import livekit_recording
from agent.livekit_recording import RecordingStartResult


class StartSessionRecordingCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_persists_recording_metadata_when_egress_starts(self) -> None:
        ctx = SimpleNamespace(room=SimpleNamespace(name="room-123"))
        userdata: dict[str, object] = {"session_id": "client-session-1"}
        started = RecordingStartResult(
            enabled=True,
            egress_id="EG_123",
            filepath="livekit-recordings/biz/session.mp3",
            expected_url="https://storage.googleapis.com/example/session.mp3",
        )

        with (
            patch.object(main, "start_room_recording", AsyncMock(return_value=started)) as start_mock,
            patch.object(main, "update_session_recording_remote", AsyncMock()) as update_mock,
            patch.object(main, "_persist_session_event_async") as event_mock,
        ):
            await main._start_session_recording_capture(
                ctx=ctx,
                userdata=userdata,
                business_id="business-1",
                session_tracker_id="session-1",
                started_at="2026-05-18T00:00:00Z",
            )

        start_mock.assert_awaited_once()
        update_mock.assert_awaited_once_with(
            session_id="session-1",
            recording_status="recording",
            recording_url="https://storage.googleapis.com/example/session.mp3",
            business_id="business-1",
        )
        event_mock.assert_called_once()
        self.assertEqual(userdata["recording_egress_id"], "EG_123")
        self.assertEqual(
            userdata["recording_expected_url"],
            "https://storage.googleapis.com/example/session.mp3",
        )


class RecordingCredentialSerializationTests(unittest.TestCase):
    def test_normalizes_json_encoded_secret_string(self) -> None:
        encoded = json.dumps(
            '{\n  "type": "service_account",\n  "project_id": "sales-girl-prod-490417"\n}'
        )
        with patch.object(livekit_recording, "RECORDING_GCP_CREDENTIALS", encoded):
            payload = livekit_recording._serialize_credentials()

        parsed = json.loads(payload)
        self.assertEqual(parsed["type"], "service_account")
        self.assertEqual(parsed["project_id"], "sales-girl-prod-490417")

    def test_normalizes_shell_sourced_pretty_json_secret(self) -> None:
        shell_sourced = (
            '{\\n  "type": "service_account",\\n  "project_id": "sales-girl-prod-490417",'
            '\\n  "private_key": "-----BEGIN PRIVATE KEY-----\\\\nabc123\\\\n-----END PRIVATE KEY-----\\\\n"\\n}'
        )
        with patch.object(livekit_recording, "RECORDING_GCP_CREDENTIALS", shell_sourced):
            payload = livekit_recording._serialize_credentials()

        parsed = json.loads(payload)
        self.assertEqual(parsed["type"], "service_account")
        self.assertEqual(parsed["project_id"], "sales-girl-prod-490417")
        self.assertIn("\\nabc123\\n", parsed["private_key"])


if __name__ == "__main__":
    unittest.main()
