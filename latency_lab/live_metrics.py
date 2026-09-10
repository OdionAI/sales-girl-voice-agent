"""Bounded, caller-scoped telemetry; never await network I/O in the audio path."""
from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from .trace import TraceEvent, TraceRecorder

TOPIC = "odion.voice_lab.metrics"
logger = logging.getLogger(__name__)


def elapsed(end: int, start: int | None) -> float | None:
    return round((end - start) / 1_000_000, 3) if start is not None and end >= start else None


@dataclass
class Turn:
    speech_end: int | None = None
    final: int | None = None
    first_output: bool = False
    requests: OrderedDict = field(default_factory=OrderedDict)
    first_tts: dict[str, Any] = field(default_factory=dict)


class MetricMapper:
    def __init__(self, max_turns: int = 50) -> None:
        self.turns: OrderedDict[str, Turn] = OrderedDict()
        self.max_turns = max_turns

    def map(self, item: TraceEvent) -> dict[str, Any] | None:
        # No raw trace forwarding: traces also contain credentials, tools and prompts.
        supported = {
            "user_speech_end", "stt_final", "llm_request_start", "llm_first_token",
            "tts_request_start", "tts_first_audio_chunk", "tts_request_complete",
            "livekit_audio_frame_enqueued", "speculation_start", "speculation_cancel",
            "playback_release_authorized",
            "generation_cancel", "turn_close", "tool_call_prepared",
        }
        if item.event not in supported or not item.turn_id or not item.turn_id.startswith("turn-"):
            return None
        turn = self.turns.setdefault(item.turn_id, Turn())
        self.turns.move_to_end(item.turn_id)
        while len(self.turns) > self.max_turns:
            self.turns.popitem(last=False)
        data = item.data
        event = item.event
        payload: dict[str, Any] = {
            "type": "odion.voice_lab.metric", "event": event,
            "run_id": item.session_id, "turn_id": item.turn_id,
            "attempt_id": item.attempt_id, "ts_ms": item.mono_ns / 1_000_000,
            "transport": "livekit", "measurement_source": "hybrid_worker",
        }
        if event == "user_speech_end":
            turn.speech_end = item.mono_ns
            return None
        if event == "stt_final":
            turn.final = item.mono_ns
            payload.update(transcript_preview=str(data.get("text") or "")[:500],
                           transcript_delay_ms=elapsed(item.mono_ns, turn.speech_end),
                           boundary="server_vad_commit_to_final")
        elif event in {"llm_request_start", "tts_request_start"}:
            kind = "llm" if event.startswith("llm") else "tts"
            key = (kind, item.attempt_id, data.get("phase"), data.get("phrase_index"))
            turn.requests[key] = item.mono_ns
            turn.requests.move_to_end(key)
            while len(turn.requests) > 64:
                turn.requests.popitem(last=False)
            payload["event"] = f"{kind}_request_started"
        elif event == "llm_first_token":
            key = ("llm", item.attempt_id, data.get("phase"), None)
            payload.update(llm_ttft_ms=elapsed(item.mono_ns, turn.requests.get(key)),
                           boundary="llm_request_to_first_text_delta")
        elif event in {"tts_first_audio_chunk", "tts_request_complete"}:
            key = ("tts", item.attempt_id, data.get("phase"), data.get("phrase_index"))
            start = turn.requests.get(key)
            request_id = f"{item.attempt_id}:{data.get('phase')}:{data.get('phrase_index')}:{start}"
            if event == "tts_first_audio_chunk":
                payload.update(event="tts_first_audio", request_id=request_id,
                               ttfa_ms=elapsed(item.mono_ns, start))
                turn.first_tts[item.attempt_id] = dict(payload)
                while len(turn.first_tts) > 16:
                    del turn.first_tts[next(iter(turn.first_tts))]
            else:
                total = elapsed(item.mono_ns, start)
                seconds = data.get("audio_seconds", 0)
                payload.update(event="tts_done", request_id=request_id,
                               total_ms=total, audio_seconds=seconds,
                               bytes=data.get("bytes"), pushes=data.get("chunks"),
                               ttfa_ms=elapsed(data["first_audio_ns"], start) if data.get("first_audio_ns") else None,
                               rtf=total / (seconds * 1000) if total is not None and seconds > 0 else None,
                               boundary="tts_stream_wall_including_output_backpressure")
        elif event == "livekit_audio_frame_enqueued":
            if turn.first_output or turn.final is None:
                return None
            turn.first_output = True
            payload.update({k: v for k, v in turn.first_tts.get(item.attempt_id, {}).items()
                            if k in {"request_id", "ttfa_ms"}})
            payload.update(event="tts_first_audio",
                           system_since_user_final_ms=elapsed(item.mono_ns, turn.final),
                           boundary="server_final_to_first_livekit_enqueue")
        elif event == "speculation_start":
            payload.update(event="preemptive_start")
        elif event == "speculation_cancel":
            payload.update(event="preemptive_discard", reason=item.reason)
        elif event == "playback_release_authorized":
            payload.update(event="preemptive_release")
        elif event == "generation_cancel":
            payload.update(event="generation_cancelled", reason=item.reason)
        elif event == "turn_close":
            payload.update(event="turn_closed", reason=item.reason)
        elif event == "tool_call_prepared":
            # A tool-only response has no spoken text token. Never invent a TTFT
            # from the fully assembled tool call or expose its arguments.
            payload.update(event="llm_tool_response")
        return payload


class LiveMetricPublisher:
    def __init__(self, trace: TraceRecorder, participant: Any, caller_identity: str,
                 *, queue_size: int = 128, publish_timeout: float = 1.0) -> None:
        if not caller_identity:
            raise ValueError("Metrics require a caller destination")
        self.participant = participant
        self.caller_identity = caller_identity
        self.publish_timeout = publish_timeout
        self.mapper = MetricMapper()
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_size)
        self.dropped = 0
        self.failed = 0
        self.published = 0
        self.unsubscribe = trace.subscribe(self.record)
        self.task = asyncio.create_task(self._publish(), name="hybrid-live-metrics")

    def record(self, item: TraceEvent) -> None:
        payload = self.mapper.map(item)
        if payload is None:
            return
        if self.queue.full():
            self.queue.get_nowait()
            self.queue.task_done()
            self.dropped += 1
        self.queue.put_nowait(payload)

    async def _publish(self) -> None:
        while True:
            payload = await self.queue.get()
            try:
                await asyncio.wait_for(self.participant.publish_data(
                    json.dumps(payload), reliable=True, topic=TOPIC,
                    destination_identities=[self.caller_identity],
                ), timeout=self.publish_timeout)
                self.published += 1
            except Exception:
                self.failed += 1
                if self.failed == 1:
                    logger.warning("Live metrics delivery failed; audio continues", exc_info=True)
            finally:
                self.queue.task_done()

    async def close(self) -> None:
        self.unsubscribe()
        try:
            await asyncio.wait_for(self.queue.join(), timeout=0.25)
        except asyncio.TimeoutError:
            pass
        finally:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        logger.info("Live metrics: published=%d failed=%d dropped=%d",
                    self.published, self.failed, self.dropped)
