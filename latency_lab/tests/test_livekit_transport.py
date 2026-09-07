from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from latency_lab.livekit_worker import LiveKitPCMTransport
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
