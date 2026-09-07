from __future__ import annotations

import base64
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from latency_lab.livekit_worker import LiveKitPCMTransport, resolve_dispatch
from latency_lab.tests import test_banking_tools
from latency_lab.config import LabConfig
from latency_lab.pipeline import Attempt, ConversationPipeline
from latency_lab.trace import TraceRecorder


class FakeSource:
    def __init__(self) -> None:
        self.frames = []
        self.waited = False
        self.cleared = False
        self.queued_duration = 0.12

    async def capture_frame(self, frame) -> None:
        self.frames.append(frame)

    async def wait_for_playout(self) -> None:
        self.waited = True

    def clear_queue(self) -> None:
        self.cleared = True


class FakePipeline:
    def __init__(self) -> None:
        self.events = []
        self.turn_id = "turn-0001"
        self.attempt = SimpleNamespace(id="attempt-test")

    async def client_event(self, kind, data=None) -> None:
        self.events.append(kind)


class LiveKitPCMTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.trace = TraceRecorder(Path(self.temp.name))
        self.source = FakeSource()
        self.pipeline = FakePipeline()
        self.transport = LiveKitPCMTransport(self.source, self.trace)
        self.transport.bind(self.pipeline)  # type: ignore[arg-type]

    async def asyncTearDown(self) -> None:
        await self.transport.close()
        self.temp.cleanup()

    async def test_pcm_is_enqueued_and_playout_is_measured(self) -> None:
        pcm = b"\x01\x00" * 2400
        await self.transport.send(
            {
                "type": "tts_chunk",
                "content": base64.b64encode(pcm).decode("ascii"),
                "sample_rate": 24000,
            }
        )
        await self.transport.send({"type": "tts_end"})

        self.assertEqual(self.pipeline.events, ["tts_start", "tts_stop"])
        self.assertEqual(len(self.source.frames), 1)
        self.assertEqual(self.source.frames[0].samples_per_channel, 2400)
        self.assertTrue(self.source.waited)

    async def test_interruption_clears_livekit_queue(self) -> None:
        self.transport.playing = True
        await self.transport.send({"type": "stop_tts"})

        self.assertTrue(self.source.cleared)
        self.assertEqual(self.pipeline.events, ["tts_stop"])

    async def test_mismatched_sample_rate_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "RTTS returned 16000 Hz"):
            await self.transport.send(
                {
                    "type": "tts_chunk",
                    "content": base64.b64encode(b"\0\0" * 10).decode("ascii"),
                    "sample_rate": 16000,
                }
            )

    async def test_browser_playback_ack_is_recorded_once_per_turn(self) -> None:
        pcm = b"\x01\x00" * 2400
        await self.transport.send(
            {
                "type": "tts_chunk",
                "content": base64.b64encode(pcm).decode("ascii"),
                "sample_rate": 24000,
            }
        )
        payload = {
            "type": "rvc_browser_playback_start",
            "browser_turn_id": "browser-turn-1",
            "browser_wall_ms": 1234,
            "browser_mono_ms": 456,
            "user_final_wall_ms": 1000,
            "user_final_to_heard_ms": 234,
            "detection_source": "audio_energy",
        }
        self.transport.receive_browser_playback_ack(
            payload,
            participant_identity="voice_assistant_user_test",
        )
        self.transport.receive_browser_playback_ack(payload)

        events = [
            json.loads(line)
            for line in self.trace.path.read_text(encoding="utf-8").splitlines()
        ]
        acknowledgments = [
            event
            for event in events
            if event["event"] == "browser_playback_confirmed"
        ]
        self.assertEqual(len(acknowledgments), 1)
        self.assertEqual(acknowledgments[0]["turn_id"], "turn-0001")
        self.assertEqual(
            acknowledgments[0]["data"]["user_final_to_heard_ms"], 234.0
        )

    async def test_end_call_uses_transport_room_delete_handler(self) -> None:
        reasons = []

        async def end_call(reason: str) -> None:
            reasons.append(reason)

        transport = LiveKitPCMTransport(
            self.source, self.trace, end_call_handler=end_call
        )
        transport.bind(self.pipeline)  # type: ignore[arg-type]

        await transport.send(
            {"type": "end_call", "reason": "configured_end_call_after_goodbye"}
        )

        self.assertEqual(reasons, ["configured_end_call_after_goodbye"])

    async def test_private_events_keep_contract_and_target_only_caller(self):
        participant = SimpleNamespace(publish_data=AsyncMock())
        self.transport.local_participant = participant
        self.transport.user_identity = "intended-caller"
        for kind in ("odion.tool.activity", "odion.auth.status", "odion.auth.action_status", "odion.auth.action"):
            payload = {"type": kind, "status": "failed", "call_id": "one"}
            await self.transport.send({"type": kind, "payload": payload})
            args, kwargs = participant.publish_data.call_args
            self.assertEqual(json.loads(args[0]), payload)
            self.assertEqual(kwargs, {"reliable": True, "topic": kind, "destination_identities": ["intended-caller"]})

    async def test_old_playout_completion_cannot_close_replacement_turn(self):
        waiting = asyncio.Event()
        self.source.wait_for_playout = waiting.wait
        self.transport.playing = True
        drain = asyncio.create_task(self.transport.send({"type": "tts_end"}))
        await asyncio.sleep(0)
        self.pipeline.attempt = SimpleNamespace(id="replacement")
        waiting.set()
        await drain
        self.assertNotIn("tts_stop", self.pipeline.events)

    async def test_final_transcript_supports_web_and_swift_with_same_identity(self):
        writer = SimpleNamespace(write=AsyncMock(), aclose=AsyncMock())
        participant = SimpleNamespace(identity="agent-test", publish_transcription=AsyncMock(),
                                      stream_text=AsyncMock(return_value=writer))
        self.transport.local_participant = participant
        self.transport.user_identity = "caller"
        self.transport.user_track_sid = "mic"
        await self.transport.send({"type": "final_user_request", "content": "hello"})
        transcription = participant.publish_transcription.call_args.args[0]
        kwargs = participant.stream_text.call_args.kwargs
        self.assertEqual(transcription.participant_identity, "caller")
        self.assertEqual(kwargs["sender_identity"], "caller")
        self.assertEqual(kwargs["attributes"]["lk.segment_id"], transcription.segments[0].id)
        writer.write.assert_awaited_once_with("hello")


class DispatchScopeTest(unittest.TestCase):
    def test_signed_room_and_caller_are_required(self):
        signer = test_banking_tools.SessionTokenTest()
        claims = {**signer.claims(), "room_name": "room-one", "participant_identity": "caller-one",
                  "runtime_overrides": {"stt_model": "whisper-large-v3-turbo"}}
        resolved = resolve_dispatch(json.dumps({"session_token": signer.token(claims)}), "room-one", "test-service-token")
        self.assertEqual(resolved, claims)
        for changes, room in (({"nonce": "different-nonce-value"}, "other-room"),
                              ({"nonce": "yet-another-nonce-value", "participant_identity": ""}, "room-one")):
            with self.assertRaises(ValueError):
                resolve_dispatch(json.dumps({"session_token": signer.token({**claims, **changes})}), room, "test-service-token")


class PipelineTransportTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.pipeline = ConversationPipeline(LabConfig(), TraceRecorder(Path(self.temp.name)), AsyncMock(), transport="livekit")

    async def asyncTearDown(self):
        await self.pipeline.close()
        self.temp.cleanup()

    async def test_greeting_is_cancellable_and_not_committed_after_interruption(self):
        started = asyncio.Event()
        async def speak(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        self.pipeline._speak_text = speak
        greeting = asyncio.create_task(self.pipeline.speak_opening("Hello"))
        await started.wait()
        await self.pipeline._cancel_attempt("user_barge_in")
        self.assertTrue(greeting.cancelled())
        self.assertEqual(self.pipeline.history, [])
        self.assertIsNone(self.pipeline.attempt)

    async def test_chat_uses_authoritative_final_without_voice_verification(self):
        self.pipeline.on_final = AsyncMock()
        self.pipeline.banking.utterance = AsyncMock()
        await self.pipeline.text("Check my balance")
        self.pipeline.on_final.assert_awaited_once_with("Check my balance", publish_transcript=False)
        self.pipeline.banking.utterance.assert_not_called()
        self.assertEqual(self.pipeline.state, "awaiting_final")
        self.assertEqual(self.pipeline.banking.session_status, "pending")

    async def test_completed_speculation_commits_before_playout_closes_attempt(self):
        attempt = Attempt(id="prepared", hypothesis="Hello", speculative=True)
        attempt.audio_complete = True
        attempt.answer = "How can I help?"
        self.pipeline.attempt = attempt
        self.pipeline.turn_id = "turn-0001"
        self.pipeline.state = "awaiting_final"
        events = []

        async def send(message):
            events.append(message["type"])
            if message["type"] == "tts_end":
                self.assertEqual(self.pipeline.history[-1]["content"], attempt.answer)
                await self.pipeline.client_event("tts_stop")

        self.pipeline.send = send
        await self.pipeline.on_final("Hello")
        self.assertEqual(events, ["final_user_request", "final_assistant_answer", "tts_end"])
        self.assertIsNone(self.pipeline.attempt)
        self.assertEqual(self.pipeline.state, "idle")
        self.assertEqual(len(self.pipeline.history), 2)
