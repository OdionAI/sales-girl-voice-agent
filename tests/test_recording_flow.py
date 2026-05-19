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
            '{\n  "type": "service_account",\n  "project_id": "sales-girl-staging-490417"\n}'
        )
        with patch.object(livekit_recording, "RECORDING_GCP_CREDENTIALS", encoded):
            payload = livekit_recording._serialize_credentials()

        parsed = json.loads(payload)
        self.assertEqual(parsed["type"], "service_account")
        self.assertEqual(parsed["project_id"], "sales-girl-staging-490417")

    def test_normalizes_shell_sourced_pretty_json_secret(self) -> None:
        shell_sourced = (
            '{\\n  "type": "service_account",\\n  "project_id": "sales-girl-staging-490417",'
            '\\n  "private_key": "-----BEGIN PRIVATE KEY-----\\\\nabc123\\\\n-----END PRIVATE KEY-----\\\\n"\\n}'
        )
        with patch.object(livekit_recording, "RECORDING_GCP_CREDENTIALS", shell_sourced):
            payload = livekit_recording._serialize_credentials()

        parsed = json.loads(payload)
        self.assertEqual(parsed["type"], "service_account")
        self.assertEqual(parsed["project_id"], "sales-girl-staging-490417")
        self.assertIn("\\nabc123\\n", parsed["private_key"])


class DynamicKnowledgeRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_updates_active_agent_instructions_without_agent_handoff(self) -> None:
        registered_handlers: dict[str, object] = {}

        class FakeSession:
            def __init__(self) -> None:
                self.current_agent = SimpleNamespace(update_instructions=AsyncMock())

            def on(self, event_name: str):
                def decorator(fn):
                    registered_handlers[event_name] = fn
                    return fn

                return decorator

        session = FakeSession()
        userdata = {
            "business_use_case": "generic",
            "base_instructions": "Base prompt",
            "turn_index": 1,
            "timeline_event_index": 0,
            "last_dynamic_knowledge_query": "",
            "last_user_transcript": "",
            "language": "en",
            "agent_id": "agent-1",
            "client_id": "client-1",
            "conversation_id": "conversation-1",
            "session_id": "session-1",
            "end_user_id": "caller@example.com",
        }

        with (
            patch.object(main, "ops_search_business_knowledge", AsyncMock(return_value={
                "matches": [{"source_name": "Knowledge", "text": "Use ALAT to open an account."}]
            })),
            patch.object(main, "trace_conversation_event"),
            patch.object(main, "_track_background_task") as track_task,
        ):
            main._wire_session_timeline(session, userdata)
            handler = registered_handlers["user_input_transcribed"]
            ev = SimpleNamespace(
                transcript="How do I open an account?",
                is_final=True,
                language="en",
                speaker_id="speaker-1",
            )
            handler(ev)

            self.assertEqual(track_task.call_count, 1)
            refresh_coro = track_task.call_args.args[1]
            await refresh_coro

        session.current_agent.update_instructions.assert_awaited_once()
        updated_instructions = session.current_agent.update_instructions.await_args.args[0]
        self.assertIn("Base prompt", updated_instructions)
        self.assertIn("Use ALAT to open an account.", updated_instructions)


if __name__ == "__main__":
    unittest.main()
