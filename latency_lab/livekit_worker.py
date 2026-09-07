from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from livekit import rtc
from livekit.agents import AutoSubscribe, JobContext, WorkerOptions, cli

from .agentic import RoomAgentContext, load_agent_runtime_context, room_agent_context
from .config import LabConfig
from .pipeline import ConversationPipeline
from .report import generate_report
from .sip_routing import (
    extract_sip_party_number,
    is_sip_participant,
    load_number_agent_map,
    resolve_sip_agent_route,
)
from .trace import TraceRecorder


logger = logging.getLogger("rvc-livekit-transport")
ROOT = Path(__file__).parent
WORKSPACE = ROOT.parent.parent
DEFAULT_LIVEKIT_ENV = WORKSPACE / "sales-girl-dashboard" / ".env.local"
DEFAULT_AGENT_NAME = "sales-girl-agent-en-localhost-noclone"


def load_livekit_credentials() -> None:
    """Load only LiveKit connection values from the local dashboard contract."""
    env_path = Path(
        os.getenv("RVC_LAB_LIVEKIT_ENV_FILE", str(DEFAULT_LIVEKIT_ENV))
    ).expanduser()
    if env_path.is_file():
        values = dotenv_values(env_path)
        for key in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
            value = str(values.get(key) or "").strip()
            if value and not os.getenv(key):
                os.environ[key] = value
    missing = [
        key
        for key in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
        if not str(os.getenv(key) or "").strip()
    ]
    if missing:
        raise RuntimeError(f"Missing LiveKit configuration: {', '.join(missing)}")


class LiveKitPCMTransport:
    """Translate RVC messages to LiveKit PCM without owning conversation state."""

    def __init__(
        self,
        source: Any,
        trace: TraceRecorder,
        *,
        sample_rate: int = 24000,
        local_participant: Any | None = None,
        end_call_handler: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.source = source
        self.trace = trace
        self.sample_rate = sample_rate
        self.local_participant = local_participant
        self.end_call_handler = end_call_handler
        self.pipeline: ConversationPipeline | None = None
        self.playing = False
        self.frame_count = 0
        self.publication_sid = ""
        self.user_identity = ""
        self.user_track_sid = ""
        self.user_participant: rtc.RemoteParticipant | None = None
        self._segment_ids: dict[str, str] = {}
        self._active_playback_turn_id = ""
        self._active_playback_attempt_id = ""
        self._active_playback_started_ns = 0
        self._acknowledged_turns: set[str] = set()

    def bind(self, pipeline: ConversationPipeline) -> None:
        self.pipeline = pipeline

    async def send(self, message: dict[str, Any]) -> None:
        kind = str(message.get("type") or "")
        if kind == "tts_chunk":
            await self._send_audio(message)
            return
        if kind == "tts_end":
            if self.playing:
                await self.source.wait_for_playout()
                if self.playing:
                    assert self.pipeline is not None
                    await self.pipeline.client_event("tts_stop")
                    self.playing = False
            return
        if kind in {"stop_tts", "tts_interruption"}:
            self.source.clear_queue()
            if self.playing and self.pipeline is not None:
                await self.pipeline.client_event("tts_stop")
            self.playing = False
            self.trace.record(
                "livekit_playout_cleared",
                turn_id=self.pipeline.turn_id if self.pipeline else None,
                reason=kind,
            )
            return
        if kind in {
            "partial_user_request",
            "final_user_request",
            "partial_assistant_answer",
            "final_assistant_answer",
        }:
            await self._publish_transcription(kind, str(message.get("content") or ""))
            return
        if kind in {"lab_status", "lab_ready"}:
            self.trace.record(
                "livekit_transport_status",
                turn_id=self.pipeline.turn_id if self.pipeline else None,
                reason=kind,
                content=message.get("content"),
            )
            return
        if kind == "end_call":
            reason = str(message.get("reason") or "configured_end_call")
            self.trace.record(
                "livekit_room_delete_requested",
                turn_id=(
                    getattr(self.pipeline, "last_closed_turn_id", None)
                    or getattr(self.pipeline, "turn_id", None)
                    if self.pipeline
                    else None
                ),
                reason=reason,
            )
            if self.end_call_handler is None:
                raise RuntimeError("LiveKit end-call handler is not configured")
            await self.end_call_handler(reason)

    async def _send_audio(self, message: dict[str, Any]) -> None:
        pcm = base64.b64decode(str(message.get("content") or ""))
        sample_rate = int(message.get("sample_rate") or self.sample_rate)
        if sample_rate != self.sample_rate:
            raise ValueError(
                f"LiveKit output is {self.sample_rate} Hz but RTTS returned {sample_rate} Hz"
            )
        if len(pcm) % 2:
            raise ValueError("RTTS returned an odd PCM16 byte count")
        samples = len(pcm) // 2
        if not samples:
            return
        if not self.playing:
            self.playing = True
            assert self.pipeline is not None
            self._active_playback_turn_id = str(self.pipeline.turn_id or "")
            self._active_playback_attempt_id = str(
                self.pipeline.attempt.id if self.pipeline.attempt else ""
            )
            self._active_playback_started_ns = time.perf_counter_ns()
            await self.pipeline.client_event("tts_start")
        frame = rtc.AudioFrame(
            data=pcm,
            sample_rate=sample_rate,
            num_channels=1,
            samples_per_channel=samples,
        )
        await self.source.capture_frame(frame)
        self.frame_count += 1
        self.trace.record(
            "livekit_audio_frame_enqueued",
            turn_id=self.pipeline.turn_id if self.pipeline else None,
            attempt_id=(
                self.pipeline.attempt.id
                if self.pipeline and self.pipeline.attempt
                else None
            ),
            reason="authorized_rtts_pcm_captured_by_livekit_audio_source",
            frame_index=self.frame_count,
            bytes=len(pcm),
            sample_rate=sample_rate,
            audio_ms=round(samples / sample_rate * 1000, 3),
            queued_duration_ms=round(
                max(0.0, float(getattr(self.source, "queued_duration", 0.0))) * 1000,
                3,
            ),
        )

    async def _publish_transcription(self, kind: str, text: str) -> None:
        if not text or self.local_participant is None:
            return
        is_user = "user" in kind
        final = kind.startswith("final_")
        role = "user" if is_user else "assistant"
        identity = self.user_identity if is_user else self.local_participant.identity
        track_sid = self.user_track_sid if is_user else self.publication_sid
        if is_user and not track_sid and self.user_participant is not None:
            track_sid = _microphone_track_sid(self.user_participant)
            self.user_track_sid = track_sid
        if not identity or not track_sid:
            return
        segment_id = self._segment_ids.setdefault(role, uuid.uuid4().hex)
        await self.local_participant.publish_transcription(
            rtc.Transcription(
                participant_identity=identity,
                track_sid=track_sid,
                segments=[
                    rtc.TranscriptionSegment(
                        id=segment_id,
                        text=text,
                        start_time=0,
                        end_time=0,
                        language="en",
                        final=final,
                    )
                ],
            )
        )
        if final:
            self._segment_ids.pop(role, None)

    def receive_browser_playback_ack(
        self,
        payload: dict[str, Any],
        *,
        participant_identity: str = "",
    ) -> None:
        if payload.get("type") != "rvc_browser_playback_start":
            return
        turn_id = self._active_playback_turn_id
        if not turn_id or turn_id in self._acknowledged_turns:
            return
        self._acknowledged_turns.add(turn_id)
        received_ns = time.perf_counter_ns()
        enqueue_to_ack_ms = (
            (received_ns - self._active_playback_started_ns) / 1_000_000
            if self._active_playback_started_ns
            else None
        )
        self.trace.record(
            "browser_playback_confirmed",
            turn_id=turn_id,
            attempt_id=self._active_playback_attempt_id or None,
            reason="browser_remote_audio_energy_crossed_playback_threshold",
            participant_identity=participant_identity,
            browser_turn_id=str(payload.get("browser_turn_id") or ""),
            browser_wall_ms=float(payload.get("browser_wall_ms") or 0),
            browser_mono_ms=float(payload.get("browser_mono_ms") or 0),
            user_final_wall_ms=float(payload.get("user_final_wall_ms") or 0),
            user_final_to_heard_ms=float(
                payload.get("user_final_to_heard_ms") or 0
            ),
            detection_source=str(payload.get("detection_source") or ""),
            livekit_enqueue_to_ack_ms=(
                round(enqueue_to_ack_ms, 3)
                if enqueue_to_ack_ms is not None
                else None
            ),
        )


def _microphone_track_sid(participant: rtc.RemoteParticipant) -> str:
    for publication in participant.track_publications.values():
        if publication.source == rtc.TrackSource.SOURCE_MICROPHONE:
            return str(publication.sid or "")
    return ""


async def entrypoint(ctx: JobContext) -> None:
    config = LabConfig()
    trace = TraceRecorder(config.trace_dir)
    pipeline: ConversationPipeline | None = None
    stream: rtc.AudioStream | None = None
    transport: LiveKitPCMTransport | None = None
    started = False
    data_handler = None
    try:
        trace.record(
            "livekit_job_received",
            reason="named_agent_dispatch",
            room_name=ctx.room.name,
            agent_name=os.getenv("RVC_LAB_LIVEKIT_AGENT_NAME", DEFAULT_AGENT_NAME),
            sip_number_map_count=len(load_number_agent_map()),
        )
        await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
        participant = await ctx.wait_for_participant()
        base_room_context = room_agent_context(str(ctx.room.name or ""))
        sip_route = resolve_sip_agent_route(participant)
        routing_context: RoomAgentContext | None = None
        sip_party_number = extract_sip_party_number(participant)
        if sip_route is not None:
            routing_context = RoomAgentContext(
                business_id=sip_route.business_id,
                agent_id=sip_route.agent_id,
                configured_name=sip_route.configured_name,
                end_user_id=sip_route.party_number,
            )
            trace.record(
                "sip_number_agent_route",
                reason="configured_number_map_hit",
                original_agent_id=base_room_context.agent_id or None,
                routed_agent_id=sip_route.agent_id,
                routed_business_id=sip_route.business_id,
                configured_agent_name=sip_route.configured_name or None,
                party_number_suffix=sip_route.party_number[-4:],
            )
        elif is_sip_participant(participant):
            trace.record(
                "sip_number_agent_route",
                reason=(
                    "sip_party_number_not_mapped"
                    if sip_party_number
                    else "sip_party_number_missing"
                ),
                original_agent_id=base_room_context.agent_id or None,
                party_number_suffix=(sip_party_number[-4:] if sip_party_number else None),
            )
        agent_context = await load_agent_runtime_context(
            room_name=str(ctx.room.name or ""),
            config=config,
            trace=trace,
            routing_context=routing_context,
        )

        async def end_call_handler(reason: str) -> None:
            trace.record(
                "livekit_room_delete_start",
                turn_id=(
                    pipeline.last_closed_turn_id or pipeline.turn_id
                    if pipeline
                    else None
                ),
                reason=reason,
                sip_participant=is_sip_participant(participant),
            )
            deleted = ctx.delete_room()
            if hasattr(deleted, "__await__"):
                await deleted
            trace.record(
                "livekit_room_delete_complete",
                turn_id=(
                    pipeline.last_closed_turn_id or pipeline.turn_id
                    if pipeline
                    else None
                ),
                reason="room_deleted_sip_bye_expected",
                sip_participant=is_sip_participant(participant),
            )

        source = rtc.AudioSource(sample_rate=24000, num_channels=1, queue_size_ms=240)
        output_track = rtc.LocalAudioTrack.create_audio_track("rvc-tts", source)
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.SOURCE_MICROPHONE
        publication = await ctx.room.local_participant.publish_track(output_track, options)
        transport = LiveKitPCMTransport(
            source,
            trace,
            sample_rate=24000,
            local_participant=ctx.room.local_participant,
            end_call_handler=end_call_handler,
        )
        transport.publication_sid = str(publication.sid or "")
        pipeline = ConversationPipeline(
            config,
            trace,
            transport.send,
            transport="livekit",
            agent_context=agent_context,
        )
        transport.bind(pipeline)

        def on_data_received(packet: rtc.DataPacket) -> None:
            if transport is None or packet.topic != "rvc_latency_ack":
                return
            try:
                payload = json.loads(packet.data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return
            transport.receive_browser_playback_ack(
                payload,
                participant_identity=str(
                    packet.participant.identity if packet.participant else ""
                ),
            )

        data_handler = on_data_received
        ctx.room.on("data_received", data_handler)
        await pipeline.start()
        started = True
        await pipeline.client_event(
            "client_ready",
            {
                "mic_sample_rate": 16000,
                "playback_sample_rate": 24000,
                "playback_prebuffer_ms": 0,
                "livekit_output_queue_ms": 240,
            },
        )
        transport.user_identity = participant.identity
        transport.user_participant = participant
        transport.user_track_sid = _microphone_track_sid(participant)
        trace.record(
            "livekit_participant_ready",
            reason="remote_participant_and_microphone_track_available",
            participant_identity=participant.identity,
            input_track_sid=transport.user_track_sid,
            output_track_sid=transport.publication_sid,
        )
        if config.opening_greeting_enabled:
            opening_text = config.opening_greeting_text or (
                f"Hello! This is {agent_context.name}. How may I help you today?"
                if agent_context.name
                else "Hello! How may I help you today?"
            )
            await pipeline.speak_opening(opening_text)
        stream = rtc.AudioStream.from_participant(
            participant=participant,
            track_source=rtc.TrackSource.SOURCE_MICROPHONE,
            sample_rate=16000,
            num_channels=1,
            frame_size_ms=100,
        )
        first_frame = True
        async for event in stream:
            received_ns = time.perf_counter_ns()
            pcm = bytes(event.frame.data)
            if first_frame:
                first_frame = False
                trace.record(
                    "livekit_first_audio_frame_received",
                    reason="subscribed_microphone_pcm_entered_rvc_coordinator",
                    bytes=len(pcm),
                    sample_rate=event.frame.sample_rate,
                    received_ns=received_ns,
                )
            await pipeline.audio(pcm)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        trace.record(
            "livekit_transport_error",
            turn_id=pipeline.turn_id if pipeline else None,
            reason=type(exc).__name__,
            error=str(exc),
        )
        logger.exception("RVC LiveKit transport failed")
    finally:
        if data_handler is not None:
            try:
                ctx.room.off("data_received", data_handler)
            except Exception:
                pass
        if stream is not None:
            await stream.aclose()
        if pipeline is not None:
            await pipeline.close()
        elif not started:
            trace.record("session_close", reason="livekit_startup_failed")
        if trace.path.exists():
            report = generate_report(trace.path, config.report_dir)
            logger.info("RVC LiveKit latency report: %s", report)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    load_livekit_credentials()
    agent_name = os.getenv("RVC_LAB_LIVEKIT_AGENT_NAME", DEFAULT_AGENT_NAME)
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=agent_name,
            ws_url=os.environ["LIVEKIT_URL"],
            api_key=os.environ["LIVEKIT_API_KEY"],
            api_secret=os.environ["LIVEKIT_API_SECRET"],
            port=int(os.getenv("RVC_LAB_LIVEKIT_WORKER_PORT", "8084")),
        )
    )


if __name__ == "__main__":
    main()
