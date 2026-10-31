from __future__ import annotations

import aiohttp
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

from .agentic import load_agent_runtime_context, session_routing_context
from .config import apply_agent_configured_voice, apply_runtime_overrides, for_inactivity_pilot, lab_config_from_environ, load_platform_env
from .pipeline import ConversationPipeline
from .passkey_demo import DemoSession, TOPIC as PASSKEY_TOPIC, enabled as passkey_demo_enabled
from .passkey import PasskeyFlow, TOPIC as PASSKEY_FLOW_TOPIC, enabled as passkey_flow_enabled
from .input_noise import InputNoiseFilter
from .report import generate_report
from .session_identity import verify_session_token
from .aicc_transfer import bridge_call
from .sip_bootstrap import resolve_route as resolve_sip_route, participant_identity as sip_participant_identity
from agent.agent_config_api import resolve_sip_route as resolve_dashboard_sip_route
from .trace import TraceRecorder
from .live_metrics import LiveMetricPublisher
from .outbound import apply_outbound_context, claim_for_route, sip_remote_headers
from agent import outbound_call_api
from agent import call_capacity_api


logger = logging.getLogger("rvc-livekit-transport")
ROOT = Path(__file__).parent
WORKSPACE = ROOT.parent.parent
DEFAULT_LIVEKIT_ENV = WORKSPACE / "sales-girl-dashboard" / ".env.local"
DEFAULT_AGENT_NAME = "rvc-livekit-comparison"


def resolve_dispatch(metadata: str, room_name: str, service_token: str) -> dict[str, Any]:
    """Banking identity comes from server dispatch, never editable participant metadata."""
    payload = json.loads(metadata or "{}")
    identity = verify_session_token(str(payload.get("session_token") or ""), service_token)
    if identity.get("room_name") != room_name or not identity.get("participant_identity"):
        raise ValueError("Call bootstrap does not match this room and caller.")
    return identity


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
        transfer_handler: Callable[[dict[str, Any]], Awaitable[dict]] | None = None,
    ) -> None:
        self.source = source
        self.trace = trace
        self.sample_rate = sample_rate
        self.local_participant = local_participant
        self.end_call_handler = end_call_handler
        self.transfer_handler = transfer_handler
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
        self._agent_state = ""
        self.metrics: LiveMetricPublisher | None = None

    def start_metrics(self) -> None:
        if self.metrics is None and self.local_participant is not None and self.user_identity:
            self.metrics = LiveMetricPublisher(self.trace, self.local_participant, self.user_identity)

    def bind(self, pipeline: ConversationPipeline) -> None:
        self.pipeline = pipeline

    async def send(self, message: dict[str, Any]) -> None:
        kind = str(message.get("type") or "")
        if kind == "tts_chunk":
            await self._send_audio(message)
            return
        if kind == "tts_end":
            attempt = self.pipeline.attempt if self.pipeline else None
            if self.playing:
                await self.source.wait_for_playout()
            await self._finish_playout(attempt)
            return
        if kind == "tts_segment_end":
            attempt = self.pipeline.attempt if self.pipeline else None
            if self.playing:
                await self.source.wait_for_playout()
            if self.pipeline is not None and self.pipeline.attempt is attempt:
                self.playing = False
                await self.set_state("listening")
                await self.pipeline.client_event("tts_segment_stop")
            return
        if kind in {"stop_tts", "tts_interruption"}:
            self.source.clear_queue()
            if self.playing and self.pipeline is not None:
                await self.pipeline.client_event("tts_stop")
            self.playing = False
            await self.set_state("listening")
            self.trace.record(
                "livekit_playout_cleared",
                turn_id=self.pipeline.turn_id if self.pipeline else None,
                reason=kind,
            )
            return
        if kind in {"odion.tool.activity", "odion.auth.status", "odion.auth.action_status", "odion.auth.action"}:
            if self.local_participant is not None and self.user_identity:
                await self.local_participant.publish_data(
                    json.dumps(message.get("payload") or message), reliable=True,
                    topic=kind, destination_identities=[self.user_identity],
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
            if kind == "lab_ready":
                await self.set_state("listening")
            return
        if kind == "transfer_to_aicc":
            if self.transfer_handler is None:
                return {"status": "failed", "error": "transfer_handler_unavailable"}
            return await self.transfer_handler(message)
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

    async def set_state(self, state: str) -> None:
        if state != self._agent_state and self.local_participant is not None:
            await self.local_participant.set_attributes({"lk.agent.state": state})
            self._agent_state = state

    async def _finish_playout(self, attempt: Any) -> None:
        if self.pipeline is not None and self.pipeline.attempt is attempt:
            await self.pipeline.client_event("tts_stop")
        if self.pipeline is None or self.pipeline.attempt is None:
            self.playing = False
            await self.set_state("listening")

    async def close(self) -> None:
        if self.metrics is not None:
            await self.metrics.close()
        self.source.clear_queue()

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
            await self.set_state("speaking")
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
            # Web components use text streams; Swift also consumes the legacy
            # transcription packet above. Both carry the same final segment.
            writer = await self.local_participant.stream_text(
                topic="lk.transcription", destination_identities=[self.user_identity],
                sender_identity=identity,
                attributes={"lk.transcription_final": "true", "lk.segment_id": segment_id,
                            "lk.transcribed_track_id": track_sid},
            )
            await writer.write(text)
            await writer.aclose()

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
    load_platform_env()
    config = lab_config_from_environ()
    trace = TraceRecorder(config.trace_dir)
    pipeline: ConversationPipeline | None = None
    stream: rtc.AudioStream | None = None
    transport: LiveKitPCMTransport | None = None
    source = None
    tasks: set[asyncio.Task] = set()
    data_handler = None
    disconnected = asyncio.Event()
    participant_handler = None
    ready = asyncio.Event()
    input_lock = asyncio.Lock()
    outbound_call_ref = ""
    outbound_claim: dict[str, Any] = {}
    capacity_reservation_id = ""
    capacity_heartbeat_task: asyncio.Task | None = None
    capacity_heartbeat_stop = asyncio.Event()
    failure_reason = ""
    try:
        sip_route = resolve_sip_route(ctx.job.metadata, ctx.room.name)
        identity = {} if sip_route else resolve_dispatch(ctx.job.metadata, ctx.room.name, config.agent_config_service_token)
        config = apply_runtime_overrides(config, identity.get("runtime_overrides"), language=identity.get("language", "en"))
        trace.record(
            "livekit_job_received",
            reason="named_agent_dispatch",
            room_name=ctx.room.name,
            agent_name=os.getenv("RVC_LAB_LIVEKIT_AGENT_NAME", DEFAULT_AGENT_NAME),
        )
        await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
        if sip_route:
            participant = await asyncio.wait_for(
                ctx.wait_for_participant(kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP), timeout=30,
            )
            sip_headers = await sip_remote_headers(ctx, participant)
            participant_attributes = {
                str(key).lower(): value
                for key, value in dict(participant.attributes).items()
            }
            called_number = str(
                sip_headers.get("to")
                or next(
                    (
                        participant_attributes.get(key)
                        for key in (
                            "sip.to",
                            "sip.h.to",
                            "sip.h.x-to",
                            "sip.trunkphonenumber",
                            "sip.trunk_phone_number",
                            "sip.h.x-callee-number",
                            "sip.h.x-odion-callee-number",
                            "sip.x_callee_number",
                            "sip.calleenumber",
                            "sip.callee_number",
                            "sip.called",
                        )
                        if participant_attributes.get(key)
                    ),
                    "",
                )
                or ""
            ).strip()
            dashboard_route = await resolve_dashboard_sip_route(number=called_number) if called_number else {}
            dynamic_did_route = None
            if dashboard_route.get("status") == "success":
                dynamic_did_route = {
                    "business_id": dashboard_route.get("business_id"),
                    "config_agent_id": dashboard_route.get("agent_id"),
                    "configured_agent_name": dashboard_route.get("name"),
                }
            identity = sip_participant_identity(sip_route, participant, ctx.room.name,
                                               rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
                                               called_override=called_number,
                                               dynamic_did_route=dynamic_did_route)
            capacity_reservation_id = call_capacity_api.reservation_id_from_sip_headers(
                sip_headers, str(identity.get("business_id") or "")
            )
            if capacity_reservation_id:
                await call_capacity_api.claim_call(
                    reservation_id=capacity_reservation_id,
                    room_name=str(ctx.room.name or ""),
                )
            outbound_required = bool(identity.get("outbound_required"))
            outbound_recipient = str(identity.get("caller_number") or "").strip()
            if outbound_required and not outbound_recipient:
                raise RuntimeError("The SIP To route requires the customer's phone number on the INVITE.")
            if outbound_required:
                outbound_claim = await claim_for_route(
                    recipient_phone=outbound_recipient, room_name=ctx.room.name, identity=identity,
                )
                outbound_call_ref = str(outbound_claim.get("call_ref") or "")
                identity["end_user_id"] = str(outbound_claim.get("recipient_phone") or "")
                identity["caller_phone"] = identity["end_user_id"]
        else:
            participant = await asyncio.wait_for(
                ctx.wait_for_participant(identity=identity["participant_identity"]), timeout=30,
            )
        def on_participant_disconnected(remote: rtc.RemoteParticipant) -> None:
            if remote.identity == participant.identity:
                disconnected.set()
        participant_handler = on_participant_disconnected
        ctx.room.on("participant_disconnected", participant_handler)
        agent_context = await load_agent_runtime_context(
            room_name=str(ctx.room.name or ""),
            config=config,
            trace=trace,
            routing_context=session_routing_context(identity),
        )
        if not agent_context.loaded:
            raise RuntimeError("Configured agent could not be loaded; refusing an unconfigured call.")
        if outbound_claim:
            agent_context = apply_outbound_context(agent_context, outbound_claim)
        # The agent's own configured voice is the call's baseline (a session
        # override, e.g. the Wema accent/gender choice, still wins).
        config = apply_agent_configured_voice(
            config, agent_context, identity.get("runtime_overrides")
        )
        config = for_inactivity_pilot(
            config,
            agent_id=agent_context.agent_id,
            is_browser=(
                not sip_route
                and (identity.get("identity_type") == "web"
                     or str(ctx.room.name or "").startswith("rvc-livekit-"))
            ),
            is_sip=bool(sip_route),
        )
        if config.inactivity_pilot_enabled:
            trace.record(
                "inactivity_pilot_enabled",
                reason="configured_rvc_call",
                call_surface="sip" if sip_route else "browser",
                agent_id=agent_context.agent_id,
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
                reason="room_deleted",
            )
            disconnected.set()

        async def transfer_handler(message: dict[str, Any]) -> dict:
            from livekit import api
            if not any(
                t.get("name") == "transfer_to_aicc" and t.get("url") == "builtin://transfer_to_aicc"
                for t in (agent_context.tools or [])
            ):
                return {"status": "failed", "error": "tool_not_enabled"}
            reason = str(message.get("reason_summary") or "Caller requested a colleague.")[:240]
            await pipeline.conversations.mark_transfer(reason, {"status": "connecting"})
            transfer_config = None
            if identity.get("transfer_target_number") or identity.get("transfer_from_number"):
                transfer_config = dict(os.environ)
                if identity.get("transfer_target_number"):
                    transfer_config["AICC_TRANSFER_TARGET_NUMBER"] = identity["transfer_target_number"]
                if identity.get("transfer_from_number"):
                    transfer_config["AICC_TRANSFER_FROM_NUMBER"] = identity["transfer_from_number"]
            async with api.LiveKitAPI(
                os.environ["LIVEKIT_URL"], os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"]
            ) as client:
                result = await bridge_call(
                    client,
                    room_name=ctx.room.name,
                    session_id=pipeline.conversations.session_id or trace.session_id,
                    reason_summary=reason,
                    caller_phone=(
                        identity.get("caller_phone", "")
                        if sip_route and identity.get("sip_caller_verified")
                        else ""
                    ),
                    config=transfer_config,
                )
            await pipeline.conversations.mark_transfer(reason, result)
            pipeline.handoff_active = result.get("status") == "connected"
            if pipeline.handoff_active:
                await release_capacity_slot()
            trace.record("livekit_transfer_result", **result)
            return result

        async def release_capacity_slot() -> None:
            nonlocal capacity_reservation_id
            if not capacity_reservation_id:
                return
            reservation_id = capacity_reservation_id
            capacity_reservation_id = ""
            capacity_heartbeat_stop.set()
            try:
                await call_capacity_api.release_call(
                    reservation_id=reservation_id,
                    room_name=str(ctx.room.name or ""),
                )
            except Exception:
                logger.exception("Could not release MoMo call capacity reservation")

        async def maintain_capacity_lease() -> None:
            interval = max(10.0, float(os.getenv("MOMO_CALL_CAPACITY_HEARTBEAT_INTERVAL_SECONDS", "45")))
            while not capacity_heartbeat_stop.is_set():
                try:
                    await asyncio.wait_for(capacity_heartbeat_stop.wait(), timeout=interval)
                    return
                except asyncio.TimeoutError:
                    pass
                try:
                    await call_capacity_api.heartbeat_call(
                        reservation_id=capacity_reservation_id,
                        room_name=str(ctx.room.name or ""),
                    )
                except Exception:
                    logger.exception("Could not renew MoMo call capacity reservation")

        if capacity_reservation_id:
            capacity_heartbeat_task = asyncio.create_task(maintain_capacity_lease())

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
            transfer_handler=transfer_handler,
        )
        transport.publication_sid = str(publication.sid or "")
        pipeline = ConversationPipeline(
            config,
            trace,
            transport.send,
            transport="livekit",
            agent_context=agent_context,
            session_identity=identity,
        )
        if not sip_route and passkey_demo_enabled(identity):
            async def send_passkey(payload: dict) -> None:
                await ctx.room.local_participant.publish_data(
                    json.dumps(payload), topic=PASSKEY_TOPIC, reliable=True,
                    destination_identities=[participant.identity])
            pipeline.passkey_demo = DemoSession(
                send_passkey, rp_id="docs.odion.ai", origin="https://docs.odion.ai")
        if not sip_route and passkey_flow_enabled(identity):
            async def send_passkey_flow(payload: dict) -> None:
                await ctx.room.local_participant.publish_data(
                    json.dumps(payload), topic=PASSKEY_FLOW_TOPIC, reliable=True,
                    destination_identities=[participant.identity])

            async def post_passkey(path: str, payload: dict) -> dict:
                record = pipeline.banking.records.get("wema_execute_prepared") or {}
                base = str(record.get("url") or "").split("/v1/tools/")[0]
                if not base:
                    # Agents with no bank tool records (the isolated passkey agent)
                    # still reach the connector through the worker's own env.
                    base = os.getenv("WEMA_TOOLS_BASE_URL", "").rstrip("/")
                headers = {str(k): str(v) for k, v in (record.get("headers") or {}).items()}
                if not headers and os.getenv("WEMA_TOOLS_SERVICE_TOKEN"):
                    headers["X-Service-Token"] = os.environ["WEMA_TOOLS_SERVICE_TOKEN"]
                headers.update({
                    "X-Agent-Id": pipeline.banking.context.agent_id,
                    "X-Business-Id": pipeline.banking.context.business_id,
                    "X-Session-Id": pipeline.banking.session_id,
                    "X-End-User-Id": pipeline.banking.context.end_user_id,
                    "X-Wema-Customer-Id": str(pipeline.banking.profile.get("customer_id") or ""),
                })
                async with pipeline.http.post(base + path, json=payload, headers=headers,
                                              timeout=aiohttp.ClientTimeout(total=15)) as response:
                    return await response.json(content_type=None)

            async def speak_passkey_outcome(text: str, meta: dict) -> None:
                await pipeline.banking.emit("odion.auth.action_status", {
                    "status": "verified" if str(meta.get("status") or "") == "verified" else "failed",
                    "usePasskey": False})
                await pipeline.announce(text)

            pipeline.passkey = PasskeyFlow(
                send=send_passkey_flow, post_json=post_passkey,
                on_outcome=speak_passkey_outcome, trace=pipeline.trace)
            logger.info(
                "passkey flow enabled for this call (business=%s agent=%s)",
                identity.get("business_id"), identity.get("config_agent_id"))
        else:
            logger.info(
                "passkey flow not enabled (sip=%s business=%s agent=%s)",
                bool(sip_route), identity.get("business_id"), identity.get("config_agent_id"))
        _wc = identity.get("wema_context") or {}
        logger.info(
            "session identity context: wema_keys=%s customer=%r account_present=%s phone_present=%s",
            sorted(_wc), str(_wc.get("customer_id") or ""),
            bool(str(_wc.get("account_number") or "").strip()),
            bool(str(_wc.get("phone_number") or "").strip()))
        transport.bind(pipeline)
        transport.user_identity = participant.identity
        transport.user_participant = participant
        transport.user_track_sid = _microphone_track_sid(participant)
        transport.start_metrics()
        await transport.set_state("initializing")

        async def receive_text(reader: rtc.TextStreamReader, sender: str) -> None:
            if sender != participant.identity:
                return
            text = ""
            async with asyncio.timeout(10):
                async for chunk in reader:
                    text += chunk
                    if len(text) > 4096:
                        return
            if not text.strip():
                return
            await ready.wait()
            async with input_lock:
                await pipeline.text(text)

        def on_text(reader: rtc.TextStreamReader, sender: str) -> None:
            task = asyncio.create_task(receive_text(reader, sender))
            tasks.add(task)
            def completed(task: asyncio.Task) -> None:
                tasks.discard(task)
                if not task.cancelled() and task.exception() is not None:
                    logger.error("RVC caller text failed", exc_info=task.exception())
            task.add_done_callback(completed)
        ctx.room.register_text_stream_handler("lk.chat", on_text)

        def on_data_received(packet: rtc.DataPacket) -> None:
            if (packet.topic == PASSKEY_FLOW_TOPIC and getattr(pipeline, "passkey", None) is not None
                    and packet.participant is not None and packet.participant.identity == participant.identity):
                task = asyncio.create_task(pipeline.passkey.receive(packet.data))
                tasks.add(task)
                def passkey_flow_completed(task: asyncio.Task) -> None:
                    tasks.discard(task)
                    if not task.cancelled() and task.exception() is not None:
                        logger.error("Passkey flow packet handling failed")
                task.add_done_callback(passkey_flow_completed)
                return
            if (packet.topic == PASSKEY_TOPIC and getattr(pipeline, "passkey_demo", None) is not None
                    and packet.participant is not None and packet.participant.identity == participant.identity):
                task = asyncio.create_task(pipeline.passkey_demo.receive(packet.data))
                tasks.add(task)
                def passkey_completed(task: asyncio.Task) -> None:
                    tasks.discard(task)
                    if not task.cancelled() and task.exception() is not None:
                        logger.error("Passkey demo packet handling failed")
                task.add_done_callback(passkey_completed)
                return
            if (transport is None or packet.topic != "rvc_latency_ack" or
                    packet.participant is None or packet.participant.identity != participant.identity):
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
        ready.set()
        await pipeline.client_event(
            "client_ready",
            {
                "mic_sample_rate": 16000,
                "playback_sample_rate": 24000,
                "playback_prebuffer_ms": 0,
                "livekit_output_queue_ms": 240,
            },
        )
        trace.record(
            "livekit_participant_ready",
            reason="remote_participant_and_microphone_track_available",
            participant_identity=participant.identity,
            input_track_sid=transport.user_track_sid,
            output_track_sid=transport.publication_sid,
        )
        stream = rtc.AudioStream.from_participant(
            participant=participant,
            track_source=rtc.TrackSource.SOURCE_MICROPHONE,
            sample_rate=16000,
            num_channels=1,
            frame_size_ms=100,
        )
        noise_filter = InputNoiseFilter(agent_context, trace)
        async def receive_audio() -> None:
            first_frame = True
            async for event in stream:
                if first_frame:
                    first_frame = False
                    trace.record("livekit_first_audio_frame_received", reason="microphone_entered_rvc",
                                 bytes=len(event.frame.data), sample_rate=event.frame.sample_rate)
                async with input_lock:
                    pcm = noise_filter.process(bytes(event.frame.data))
                    if pcm:
                        await pipeline.audio(
                            pcm, speech_prob=noise_filter.last_speech_prob
                        )
            tail = noise_filter.finish()
            if tail:
                async with input_lock:
                    await pipeline.audio(tail)

        input_task = asyncio.create_task(receive_audio())
        stop_task = asyncio.create_task(disconnected.wait())
        tasks.update((input_task, stop_task))
        if config.opening_greeting_enabled:
            tasks.add(asyncio.create_task(pipeline.speak_opening(pipeline.opening_greeting_text())))
        done, _ = await asyncio.wait((input_task, stop_task), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        failure_reason = str(exc)[:255]
        trace.record(
            "livekit_transport_error",
            turn_id=pipeline.turn_id if pipeline else None,
            reason=type(exc).__name__,
            error=str(exc),
        )
        logger.exception("RVC LiveKit transport failed")
    finally:
        capacity_heartbeat_stop.set()
        if capacity_heartbeat_task is not None:
            capacity_heartbeat_task.cancel()
            await asyncio.gather(capacity_heartbeat_task, return_exceptions=True)
        if capacity_reservation_id:
            try:
                await call_capacity_api.release_call(
                    reservation_id=capacity_reservation_id,
                    room_name=str(ctx.room.name or ""),
                )
            except Exception:
                logger.exception("Could not release MoMo call capacity reservation during cleanup")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if data_handler is not None:
            try:
                ctx.room.off("data_received", data_handler)
            except Exception:
                pass
        if stream is not None:
            await stream.aclose()
        if participant_handler is not None:
            ctx.room.off("participant_disconnected", participant_handler)
        ctx.room.unregister_text_stream_handler("lk.chat")
        if pipeline is not None:
            if getattr(pipeline, "passkey", None) is not None:
                await pipeline.passkey.close()
            if getattr(pipeline, "passkey_demo", None) is not None:
                await pipeline.passkey_demo.close()
            await pipeline.close()
        else:
            trace.record("session_close", reason="livekit_startup_failed")
        if outbound_claim:
            try:
                await outbound_call_api.finish_call(
                    call_ref=outbound_call_ref,
                    status="failed" if failure_reason else "completed",
                    failure_reason=failure_reason or None,
                    conversation_id=(pipeline.conversations.conversation_id if pipeline else None),
                )
            except Exception:
                logger.exception("Could not finalize prepared outbound call")
        if transport is not None:
            await transport.close()
        if source is not None:
            await source.aclose()
        await ctx.room.disconnect()
        if trace.path.exists():
            report = generate_report(trace.path, config.report_dir)
            logger.info("RVC LiveKit latency report: %s", report)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    load_livekit_credentials()
    load_platform_env()
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
