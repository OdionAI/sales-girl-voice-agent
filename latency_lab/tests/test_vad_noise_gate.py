"""Regression tests for the VAD noise-gate and empty-final recovery fixes.

Covers the 2026-10-31 Momo PSB silent-call defect:
  A) a loud non-speech burst (low RNNoise speech probability) must not start a
     caller turn even though it clears the loudness threshold;
  B) a committed buffer below the minimum utterance floor is discarded before
     ASR instead of finalizing and freezing the turn;
  C) an empty authoritative final makes the pipeline return to listening
     immediately instead of waiting out the full STT deadline.
"""
from __future__ import annotations

import asyncio
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from latency_lab.config import LabConfig
from latency_lab.pipeline import ConversationPipeline
from latency_lab.providers import RSTTClient
from latency_lab.trace import TraceRecorder


def loud_pcm(ms: int = 100, amp: int = 4000) -> bytes:
    """PCM16 mono 16 kHz square-ish wave with RMS well above speech_rms (280)."""
    n = int(16000 * ms / 1000)
    return b"".join(struct.pack("<h", amp if i % 2 else -amp) for i in range(n))


class _FakeSTT:
    def __init__(self):
        self.appended = 0
        self.committed = []

    async def append(self, _pcm):
        self.appended += 1

    async def commit(self, pcm):
        self.committed.append(len(pcm))

    def ensure_recovery(self, _reason):
        pass

    async def abandon_turn_audio(self, _reason):
        pass


def _build(session_id: str, **cfg):
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)

    async def send(_message):
        return None

    pipeline = ConversationPipeline(
        LabConfig(trace_dir=root / "traces", report_dir=root / "reports", **cfg),
        TraceRecorder(root / "traces", session_id),
        send,
    )
    return pipeline, tmp


def _events(pipeline) -> list[dict]:
    return [
        json.loads(line)
        for line in pipeline.trace.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class VADSppechProbabilityGateTest(unittest.IsolatedAsyncioTestCase):
    async def test_loud_noise_with_low_speech_prob_does_not_start_a_turn(self) -> None:
        pipeline, tmp = _build("noise-gate-low")
        pipeline.stt = _FakeSTT()
        try:
            for _ in range(4):  # > min_speech_ms of loud frames
                await pipeline.audio(loud_pcm(100), speech_prob=0.08)
            self.assertFalse(pipeline.speech_candidate_active)
            self.assertEqual(pipeline.state, "idle")
            self.assertIsNone(pipeline.turn_id)
        finally:
            await pipeline.http.close()
            tmp.cleanup()

    async def test_loud_speech_with_high_prob_starts_a_turn(self) -> None:
        pipeline, tmp = _build("noise-gate-high")
        pipeline.stt = _FakeSTT()
        try:
            for _ in range(4):
                await pipeline.audio(loud_pcm(100), speech_prob=0.9)
            self.assertEqual(pipeline.state, "speaking")
            self.assertIsNotNone(pipeline.turn_id)
        finally:
            await pipeline.http.close()
            tmp.cleanup()

    async def test_missing_speech_prob_falls_back_to_energy(self) -> None:
        # No probability supplied (APM / suppression off): behaviour is unchanged.
        pipeline, tmp = _build("noise-gate-none")
        pipeline.stt = _FakeSTT()
        try:
            for _ in range(4):
                await pipeline.audio(loud_pcm(100))
            self.assertEqual(pipeline.state, "speaking")
        finally:
            await pipeline.http.close()
            tmp.cleanup()


class MinUtteranceGuardTest(unittest.IsolatedAsyncioTestCase):
    async def test_short_committed_buffer_is_discarded_before_asr(self) -> None:
        pipeline, tmp = _build("min-utt-discard", min_utterance_ms=200.0)
        fake = _FakeSTT()
        pipeline.stt = fake
        try:
            pipeline.state = "speaking"
            pipeline.turn_id = "turn-0001"
            pipeline.turn_number = 1
            pipeline.turn_pcm16 = bytearray(loud_pcm(70))  # ~70 ms, below floor

            await pipeline._speech_ended()

            self.assertEqual(fake.committed, [])  # never sent to ASR
            self.assertEqual(pipeline.state, "idle")
            self.assertIsNone(pipeline.turn_id)
            reasons = [e["reason"] for e in _events(pipeline)
                       if e["event"] == "utterance_discarded_below_minimum"]
            self.assertEqual(reasons, ["committed_buffer_too_short_for_asr"])
        finally:
            await pipeline.http.close()
            tmp.cleanup()

    async def test_sufficient_buffer_still_commits(self) -> None:
        pipeline, tmp = _build("min-utt-commit", min_utterance_ms=50.0)
        fake = _FakeSTT()
        pipeline.stt = fake
        try:
            pipeline.state = "speaking"
            pipeline.turn_id = "turn-0001"
            pipeline.turn_number = 1
            pipeline.turn_pcm16 = bytearray(loud_pcm(200))  # 200 ms, above floor

            await pipeline._speech_ended()

            self.assertEqual(len(fake.committed), 1)  # committed to ASR
            if pipeline.final_timeout_task:
                pipeline.final_timeout_task.cancel()
        finally:
            await pipeline.http.close()
            tmp.cleanup()


class EmptyFinalRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_on_final_empty_returns_to_listening_and_cancels_timeout(self) -> None:
        pipeline, tmp = _build("empty-final-reset")
        try:
            pipeline.state = "awaiting_final"
            pipeline.turn_id = "turn-0001"
            pipeline.turn_number = 1
            pipeline.turn_pcm16 = bytearray(loud_pcm(100))
            pipeline.final_timeout_task = asyncio.create_task(asyncio.sleep(100))

            await pipeline.on_final_empty()

            self.assertEqual(pipeline.state, "idle")
            self.assertIsNone(pipeline.turn_id)
            self.assertIsNone(pipeline.final_timeout_task)
            reasons = [e["reason"] for e in _events(pipeline)
                       if e["event"] == "stt_final_empty_recovered"]
            self.assertEqual(reasons, ["empty_authoritative_final_return_to_listening"])
            self.assertEqual(len(pipeline.turn_pcm16), 0)
        finally:
            await pipeline.http.close()
            tmp.cleanup()

    async def test_rstt_empty_batch_notifies_pipeline(self) -> None:
        calls = []

        async def on_unavailable():
            calls.append("called")

        client = RSTTClient(
            LabConfig(), AsyncMock(), AsyncMock(), lambda _event, _data: None,
            on_final_unavailable=on_unavailable,
        )
        client._quiesce_realtime_locked = AsyncMock()
        client._open_socket_locked = AsyncMock()
        client._transcribe_batch = AsyncMock(return_value=False)

        await client._run_authoritative_batch(b"\x00\x00" * 10)

        self.assertEqual(calls, ["called"])

    async def test_rstt_successful_batch_does_not_notify(self) -> None:
        calls = []

        async def on_unavailable():
            calls.append("called")

        client = RSTTClient(
            LabConfig(), AsyncMock(), AsyncMock(), lambda _event, _data: None,
            on_final_unavailable=on_unavailable,
        )
        client._quiesce_realtime_locked = AsyncMock()
        client._open_socket_locked = AsyncMock()
        client._transcribe_batch = AsyncMock(return_value=True)

        await client._run_authoritative_batch(b"\x00\x00" * 10)

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
