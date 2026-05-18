from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
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


if __name__ == "__main__":
    unittest.main()
