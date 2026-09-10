from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from latency_lab.live_metrics import LiveMetricPublisher, MetricMapper, TOPIC
from latency_lab.trace import TraceEvent, TraceRecorder


def event(name, ms, turn="turn-0001", attempt="a", **data):
    return TraceEvent(1, "session", turn, attempt, name, int(ms * 1_000_000),
                      "", None, None, "", data)


class MapperTests(unittest.TestCase):
    def test_measured_clocks_and_caller_contract(self):
        mapper = MetricMapper()
        mapper.map(event("user_speech_end", 100))
        final = mapper.map(event("stt_final", 250, text="Hello", secret="not public"))
        self.assertEqual(final["transcript_delay_ms"], 150)
        self.assertNotIn("secret", json.dumps(final))
        mapper.map(event("llm_request_start", 270, phase="prompt_first"))
        llm = mapper.map(event("llm_first_token", 570, phase="prompt_first"))
        self.assertEqual(llm["llm_ttft_ms"], 300)
        mapper.map(event("tts_request_start", 600, phase="prompt_first", phrase_index=1))
        tts = mapper.map(event("tts_first_audio_chunk", 900, phase="prompt_first", phrase_index=1))
        self.assertEqual(tts["ttfa_ms"], 300)
        self.assertNotIn("system_since_user_final_ms", tts)
        audio = mapper.map(event("livekit_audio_frame_enqueued", 920))
        self.assertEqual(audio["system_since_user_final_ms"], 670)
        self.assertEqual(audio["ttfa_ms"], 300)
        self.assertEqual(audio["type"], "odion.voice_lab.metric")
        self.assertEqual(audio["transport"], "livekit")
        self.assertIsNone(mapper.map(event("livekit_audio_frame_enqueued", 1000)))
        done = mapper.map(event("tts_request_complete", 2600, phase="prompt_first",
                                phrase_index=1, first_audio_ns=900_000_000,
                                audio_seconds=4, bytes=192000, chunks=20))
        self.assertEqual(done["rtf"], 0.5)
        self.assertEqual(done["ttfa_ms"], 300)

    def test_missing_boundaries_never_become_zero(self):
        mapper = MetricMapper()
        self.assertIsNone(mapper.map(event("stt_final", 100, text="Typed"))["transcript_delay_ms"])
        self.assertIsNone(mapper.map(event("llm_first_token", 200))["llm_ttft_ms"])
        self.assertIsNone(mapper.map(event("tts_first_audio_chunk", 300))["ttfa_ms"])
        self.assertIsNone(mapper.map(event("tts_request_complete", 400, audio_seconds=0))["rtf"])
        self.assertIsNone(mapper.map(event("tts_request_start", 50, turn="opening-0000")))

    def test_followups_attempts_and_turns_have_independent_clocks(self):
        mapper = MetricMapper()
        mapper.map(event("llm_request_start", 10, phase="first"))
        mapper.map(event("llm_request_start", 100, phase="tool"))
        mapper.map(event("llm_request_start", 200, phase="first", attempt="b"))
        self.assertEqual(mapper.map(event("llm_first_token", 210, phase="tool"))["llm_ttft_ms"], 110)
        self.assertEqual(mapper.map(event("llm_first_token", 220, phase="first", attempt="b"))["llm_ttft_ms"], 20)
        self.assertIsNone(mapper.map(event("llm_first_token", 230, turn="turn-0002", phase="first"))["llm_ttft_ms"])

    def test_speculative_audio_is_not_reported_as_caller_output(self):
        mapper = MetricMapper()
        mapper.map(event("tts_request_start", 100, phase="first", phrase_index=1))
        held = mapper.map(event("tts_first_audio_chunk", 200, phase="first", phrase_index=1, held=True))
        self.assertNotIn("system_since_user_final_ms", held)
        mapper.map(event("stt_final", 500, text="Final"))
        audio = mapper.map(event("livekit_audio_frame_enqueued", 550))
        self.assertEqual(audio["system_since_user_final_ms"], 50)

    def test_retention_and_request_state_are_bounded(self):
        mapper = MetricMapper(max_turns=3)
        for index in range(1000):
            mapper.map(event("stt_final", index, turn=f"turn-{index:04d}"))
        self.assertEqual(len(mapper.turns), 3)
        for index in range(1000):
            mapper.map(event("tts_request_start", index, turn="turn-0999", phrase_index=index))
        self.assertEqual(len(mapper.turns["turn-0999"].requests), 64)


class PublisherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.trace = TraceRecorder(Path(self.temp.name))

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_private_delivery_and_unsubscribe(self):
        participant = SimpleNamespace(publish_data=AsyncMock())
        publisher = LiveMetricPublisher(self.trace, participant, "caller")
        self.trace.record("stt_final", turn_id="turn-0001", text="Hello")
        await publisher.queue.join()
        call = participant.publish_data.call_args
        self.assertEqual(call.kwargs["topic"], TOPIC)
        self.assertEqual(call.kwargs["destination_identities"], ["caller"])
        self.assertTrue(call.kwargs["reliable"])
        self.assertEqual(json.loads(call.args[0])["transcript_preview"], "Hello")
        await publisher.close()
        self.trace.record("stt_final", turn_id="turn-0002", text="After close")
        self.assertEqual(participant.publish_data.call_count, 1)
        self.assertTrue(publisher.task.done())

    async def test_slow_network_cannot_block_trace_or_grow_queue(self):
        waiting = asyncio.Event()
        async def blocked(*args, **kwargs):
            await waiting.wait()
        publisher = LiveMetricPublisher(self.trace, SimpleNamespace(publish_data=blocked),
                                        "caller", queue_size=2, publish_timeout=0.01)
        for i in range(30):
            self.trace.record("stt_final", turn_id=f"turn-{i:04d}", text="Test")
        self.assertEqual(publisher.queue.qsize(), 2)
        self.assertEqual(publisher.dropped, 28)
        with self.assertLogs("latency_lab.live_metrics", level="WARNING"):
            await asyncio.wait_for(publisher.queue.join(), timeout=1)
        self.assertEqual(publisher.failed, 2)
        await publisher.close()

    async def test_failure_does_not_stop_later_delivery(self):
        participant = SimpleNamespace(publish_data=AsyncMock(side_effect=[RuntimeError("offline"), None]))
        publisher = LiveMetricPublisher(self.trace, participant, "caller")
        for i in range(2):
            self.trace.record("stt_final", turn_id=f"turn-{i:04d}", text="Test")
        with self.assertLogs("latency_lab.live_metrics", level="WARNING"):
            await publisher.queue.join()
        self.assertEqual((publisher.failed, publisher.published), (1, 1))
        await publisher.close()

    async def test_listener_failure_cannot_break_recording(self):
        def broken(item):
            raise RuntimeError("observer")
        self.trace.subscribe(broken)
        with self.assertLogs("latency_lab.trace", level="ERROR"):
            result = self.trace.record("stt_final", turn_id="turn-0001")
        self.assertEqual(result.event, "stt_final")
        self.assertTrue(self.trace.path.exists())


if __name__ == "__main__":
    unittest.main()
