from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from agent import salon_agent
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
            patch.object(main, "is_recording_enabled", return_value=True),
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


class VoiceLabMetricsPublishingTests(unittest.IsolatedAsyncioTestCase):
    async def test_publishes_stt_llm_and_tts_metrics_to_voice_lab_topic(self) -> None:
        registered_handlers: dict[str, object] = {}
        queued_coroutines: list[object] = []

        class FakeSession:
            current_agent = None

            def on(self, event_name: str):
                def decorator(fn):
                    registered_handlers[event_name] = fn
                    return fn

                return decorator

        publish_data = AsyncMock()
        room = SimpleNamespace(
            local_participant=SimpleNamespace(publish_data=publish_data)
        )
        transcript = "I would like to check my account balance."
        userdata = {
            "runtime_overrides": {
                "stt_provider": "odion_stt",
                "tts_transport": "http",
            },
            "turn_index": 0,
            "timeline_event_index": 0,
            "last_dynamic_knowledge_query": transcript,
            "last_user_transcript": "",
            "language": "en",
            "agent_id": "agent-1",
            "client_id": "client-1",
            "conversation_id": "conversation-1",
            "session_id": "session-1",
            "end_user_id": "caller@example.com",
        }

        with (
            patch.object(main, "trace_conversation_event"),
            patch.object(
                main,
                "_track_background_task",
                side_effect=lambda _userdata, coro: queued_coroutines.append(coro),
            ),
        ):
            main._wire_session_timeline(FakeSession(), userdata, room=room)
            registered_handlers["user_input_transcribed"](
                SimpleNamespace(
                    transcript=transcript,
                    is_final=True,
                    created_at=1234.5,
                    language="en",
                    speaker_id="speaker-1",
                )
            )
            registered_handlers["metrics_collected"](
                SimpleNamespace(
                    metrics=SimpleNamespace(
                        type="eou_metrics",
                        end_of_utterance_delay=0.45,
                        transcription_delay=0.32,
                        on_user_turn_completed_delay=0.01,
                    )
                )
            )
            registered_handlers["metrics_collected"](
                SimpleNamespace(
                    metrics=SimpleNamespace(
                        type="llm_metrics",
                        provider="qwen",
                        model="qwen3.8_27b",
                        ttft=0.25,
                        duration=0.75,
                        completion_tokens=12,
                        cancelled=False,
                    )
                )
            )
            registered_handlers["metrics_collected"](
                SimpleNamespace(
                    metrics=SimpleNamespace(
                        type="tts_metrics",
                        provider="odion",
                        model="Qwen3-TTS",
                        ttfb=0.4,
                        duration=1.2,
                        audio_duration=2.4,
                        cancelled=False,
                    )
                )
            )

            for coro in queued_coroutines:
                await coro

        self.assertEqual(publish_data.await_count, 4)
        payloads = [
            json.loads(call.args[0]) for call in publish_data.await_args_list
        ]
        self.assertEqual(
            [payload["event"] for payload in payloads],
            ["stt_final", "stt_timing", "llm_first_token", "tts_done"],
        )
        self.assertTrue(
            all(payload["type"] == "odion.voice_lab.metric" for payload in payloads)
        )
        self.assertTrue(all(payload["turn_index"] == 1 for payload in payloads))
        self.assertEqual(payloads[0]["transcript_preview"], transcript)
        self.assertEqual(payloads[1]["transcript_delay_ms"], 320.0)
        self.assertEqual(payloads[1]["endpointing_ms"], 450.0)
        self.assertEqual(payloads[2]["llm_ttft_ms"], 250.0)
        self.assertEqual(payloads[3]["ttfa_ms"], 400.0)
        self.assertEqual(payloads[3]["rtf"], 0.5)
        for call in publish_data.await_args_list:
            self.assertEqual(call.kwargs["topic"], main.VOICE_LAB_METRICS_TOPIC)
            self.assertTrue(call.kwargs["reliable"])


class TranscriptAndTicketGuardTests(unittest.IsolatedAsyncioTestCase):
    def test_skips_partial_assistant_fragment_when_full_reply_already_saved(self) -> None:
        userdata = {
            "last_persisted_assistant_content": (
                "Bonjour, je m'appelle Sonia. Merci de nous avoir contactés. "
                "L'IC correspond au numéro d'immatriculation consulaire."
            )
        }

        self.assertTrue(
            main._should_skip_assistant_message_persist(
                userdata, "Bonjour, je m'appelle Sonia."
            )
        )
        self.assertFalse(
            main._should_skip_assistant_message_persist(
                userdata,
                "Bonjour, je m'appelle Sonia. Merci de nous avoir contactés. "
                "L'IC correspond au numéro d'immatriculation consulaire. "
                "Souhaitez-vous plus de détails ?",
            )
        )
        self.assertTrue(
            main._should_skip_assistant_message_persist({}, "Bonjour ! Je")
        )

    async def test_does_not_reconcile_ticket_update_when_ticket_already_created(self) -> None:
        userdata = {
            "enabled_tool_names": ["create_ticket"],
            "turn_index": 4,
            "last_create_ticket_success_turn": 3,
            "recent_user_messages": [
                "Si cette information n'est pas disponible, pouvez-vous creer un ticket de suivi pour moi ?",
                "Mon nom complet est Claire Adjovi.",
            ],
        }

        with patch.object(main, "ops_create_ticket", AsyncMock()) as create_ticket_mock:
            await main._reconcile_ticket_claim_if_needed(
                userdata,
                "Parfait. J'ai mis à jour le ticket avec votre nom.",
            )

        create_ticket_mock.assert_not_awaited()

    def test_reuses_recent_ticket_for_same_title_on_next_turn(self) -> None:
        session_userdata = {
            "turn_index": 3,
            "last_create_ticket_success_turn": 2,
            "last_create_ticket_result": {
                "id": "ticket-1",
                "title": "Demande d'horaires du service consulaire à Paris pour samedi",
                "status": "success",
                "customer_name": "mfab.verify.ticket@example.com",
            },
        }

        reused = salon_agent._recent_ticket_reuse_result(
            session_userdata,
            title="Demande d'horaires du service consulaire à Paris pour samedi",
            customer_identifier="Claire Adjovi",
        )

        self.assertIsNotNone(reused)
        self.assertEqual(reused["id"], "ticket-1")
        self.assertTrue(reused["reused_existing_ticket"])


if __name__ == "__main__":
    unittest.main()
