"""Bounded, best-effort call persistence without storage waits in audio callbacks.

Capture begins when egress receives media, not at session creation or start API
acknowledgement. Startup stays in the background to preserve conversation latency,
so an opening greeting's first samples can be absent; full-call audio from the
first sample is not guaranteed. Transcript capture is independent of that boundary.

There is no post-close polling in this worker. Reconcile processing recordings
from recording_status events using egress_id, expected_url, and canonical session
ID. For an uncertain start timeout, first list egress for room_name and correlate
the session/time before stopping anything. Only a confirmed completed file should
be promoted to available through the conversation service's recording endpoint.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import os
import re
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Coroutine

import aiohttp

from .agentic import AgentRuntimeContext
from .config import LabConfig
from .trace import TraceRecorder


def as_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, AttributeError):
        return str(value or "").strip()


def to_e164(value: str) -> str:
    raw = str(value or "").strip()
    # Never mine digits from an email, SDK subject, or participant identifier.
    if not re.fullmatch(r"\+?[0-9 ()\-.]+", raw):
        return raw
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = "234" + digits[1:]
    if len(digits) == 10 and digits.startswith("8"):
        digits = "234" + digits
    return "+" + digits if re.fullmatch(r"[1-9]\d{7,14}", digits) else raw


def caller_from_identity(identity: dict[str, Any] | None, room_name: str = "") -> str:
    """Read server-issued call claims, without changing banking identity semantics."""
    identity = identity or {}
    for key in ("end_user_id", "end_user_email", "participant_identity"):
        value = str(identity.get(key) or "").strip()
        if value:
            return to_e164(value)
    match = re.search(r"__(\d{10,14})(?:_|$)", str(room_name or ""))
    return to_e164(match.group(1)) if match else ""


def build_fast_handoff_analysis(
    *,
    reason_summary: str = "",
    last_user_transcript: str = "",
    recent_user_messages: list[str] | None = None,
) -> dict[str, Any]:
    reason = str(reason_summary or "").strip()
    last_user = str(last_user_transcript or "").strip()
    recent = [str(item or "").strip() for item in (recent_user_messages or []) if str(item or "").strip()]
    if not recent and last_user:
        recent = [last_user]
    summary_parts: list[str] = []
    if reason:
        summary_parts.append(f"Handoff reason: {reason}")
    if recent:
        summary_parts.append("Caller said: " + " | ".join(recent[-3:]))
    if not summary_parts:
        summary_parts.append("Caller requested a live human agent during the AI call.")
    blob = f"{reason} {' '.join(recent)}".lower()
    intent = "request_human_agent"
    if any(token in blob for token in ("card", "block", "pin", "otp")):
        intent = "card_or_security_issue"
    elif any(token in blob for token in ("loan", "account", "balance", "statement")):
        intent = "account_inquiry"
    elif any(token in blob for token in ("complaint", "fraud", "unauthorized")):
        intent = "complaint_or_fraud"
    elif any(token in blob for token in ("transaction", "payment", "debit", "failed transfer")):
        intent = "transaction_issue"
    return {
        "analysis_status": "ready",
        "summary": " ".join(summary_parts)[:900],
        "primary_intent": intent,
        "intent_confidence": 0.9 if reason or recent else 0.7,
        "sentiment": "urgent" if any(token in blob for token in ("urgent", "fraud", "immediately")) else "frustrated",
        "resolution_status": "escalated",
    }


class ConversationStore:
    """Best-effort call history; enqueue from the pipeline, never await storage there."""

    REQUEST_TIMEOUT_SECONDS = 4.0
    RETRY_DELAY_SECONDS = 0.1
    MAX_PENDING_OPERATIONS = 256
    OPERATION_TIMEOUT_SECONDS = 30.0
    DRAIN_TIMEOUT_SECONDS = 15.0
    RECORDING_START_TIMEOUT_SECONDS = 10.0
    RECORDING_STOP_TIMEOUT_SECONDS = 5.0
    RECORDING_FINALIZE_TIMEOUT_SECONDS = 45.0

    def __init__(
        self,
        config: LabConfig,
        session: aiohttp.ClientSession,
        context: AgentRuntimeContext,
        trace: TraceRecorder,
        *,
        caller: str,
        client_session_id: str,
        room_name: str = "",
        transport: str | None = None,
        channel: str | None = None,
    ) -> None:
        self.config = config
        self.session = session
        self.context = context
        self.trace = trace
        self.caller = str(caller or "").strip()
        self.client_session_id = str(client_session_id or "").strip() or uuid.uuid4().hex
        self.room_name = room_name
        # Deployed callers predate transport; a supplied room preserves that API.
        self.transport = transport or ("livekit" if room_name else "browser")
        normalized = to_e164(self.caller)
        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", self.caller):
            self.channel, self.external_id = "web", self.caller.lower()
        elif channel != "web" and re.fullmatch(r"\+[1-9]\d{7,14}", normalized):
            self.channel, self.external_id = "voice", normalized
        else:
            # The web resolver requires email. Scope aliases to this session and
            # tenant, keeping arbitrary customer IDs out of real phone identities.
            parts = (as_uuid(context.business_id), as_uuid(context.agent_id), self.caller, self.client_session_id)
            digest = hashlib.sha256(repr(parts).encode()).hexdigest()
            self.channel, self.external_id = "web", f"session-{digest[:48]}@sdk.invalid"
        self.conversation_id = ""
        self.session_id = ""
        self.recent_user: list[str] = []
        self.last_user = ""
        self.started_mono = time.monotonic()
        self.started_at = datetime.now(timezone.utc)
        self._duration_seconds: int | None = None
        self._ended_at: datetime | None = None
        self._pending: deque[Coroutine[Any, Any, Any]] = deque()
        self._worker: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._start_attempted = False
        self._closing = False
        self._closed = False
        self._sequence = 0
        self._message_keys: dict[tuple[str, str], str] = {}
        self._last_message: tuple[str, str] | None = None
        self._recording_task: asyncio.Task[None] | None = None
        self._recording_stop_task: asyncio.Task[None] | None = None
        self._recording_stop_acknowledged: bool | None = None
        self._recording_helper: Any = None
        self._egress_id: str | None = None
        self._expected_url: str | None = None
        self.recording_status = "disabled"
        self.recording_url: str | None = None
        self._recording_duration_seconds: int | None = None
        self._recording_detail = "recording_disabled"
        self._recording_status_dirty = False

    def enabled(self) -> bool:
        return bool(self.config.conversation_service_base_url and self.config.conversation_service_token
                    and self.context.business_id and self.context.agent_id)

    def mark_disconnected(self) -> None:
        if self._duration_seconds is None:
            self._duration_seconds = max(0, int(time.monotonic() - self.started_mono))
            self._ended_at = datetime.now(timezone.utc)
        self._schedule_recording_stop()

    def _schedule_recording_stop(self) -> None:
        if self._ended_at is not None and self._egress_id and self._recording_stop_task is None:
            self._recording_stop_task = asyncio.create_task(self._request_recording_stop())

    async def _request_recording_stop(self) -> None:
        try:
            self._recording_stop_acknowledged = bool(await asyncio.wait_for(
                self._recording_helper.request_stop_room_recording(
                    egress_id=self._egress_id, timeout_seconds=self.RECORDING_STOP_TIMEOUT_SECONDS,
                ), timeout=self.RECORDING_STOP_TIMEOUT_SECONDS + 1.0,
            ))
        except Exception:
            self._recording_stop_acknowledged = False
        self.trace.record(
            "conversation_recording_stop", reason="stop_acknowledged" if self._recording_stop_acknowledged
            else "stop_unconfirmed", session_id=self.session_id, egress_id=self._egress_id,
            room_name=self.room_name,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Service-Token": self.config.conversation_service_token,
            "X-Service-Name": self.config.agent_client_id or "rvc-livekit",
            "X-Business-ID": as_uuid(self.context.business_id),
        }

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        if self._closing or len(self._pending) >= self.MAX_PENDING_OPERATIONS:
            coro.close()
            self.trace.record("conversation_persist_dropped", reason="store_closing" if self._closing else "queue_full")
            return
        self._pending.append(coro)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_pending())

    async def _run_pending(self) -> None:
        while self._pending:
            coro = self._pending.popleft()
            try:
                async with asyncio.timeout(self.OPERATION_TIMEOUT_SECONDS):
                    await coro
            except Exception as exc:
                self.trace.record("conversation_persist_error", reason=type(exc).__name__, error=str(exc))

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None = None,
                       *, retry: bool = False) -> dict[str, Any]:
        if not self.enabled():
            return {"status": "disabled"}
        for attempt in range(2 if retry else 1):
            transient = False
            try:
                async with self.session.request(
                    method, f"{self.config.conversation_service_base_url.rstrip('/')}{path}",
                    json=payload, headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=self.REQUEST_TIMEOUT_SECONDS),
                ) as response:
                    transient = response.status == 429 or response.status >= 500
                    body = await response.json(content_type=None)
                    if not isinstance(body, dict):
                        raise ValueError("Expected a JSON object")
                    if response.status < 400 and body.get("status") not in {"failed", "error"}:
                        return {**body, "status": body.get("status") or "success"}
                    reason = f"http_{response.status}"
            except Exception as exc:
                transient = transient or isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError, OSError))
                reason = type(exc).__name__
            self.trace.record("conversation_persist_failed", reason=reason, path=path, attempt=attempt + 1)
            if not retry or not transient or attempt:
                break
            await asyncio.sleep(self.RETRY_DELAY_SECONDS)
        return {"status": "failed"}

    async def start(self) -> None:
        async with self._start_lock:
            if self._start_attempted or self._closed:
                return
            self._start_attempted = True
            if not self.enabled():
                self.trace.record("conversation_persist_skipped", reason="conversation_service_not_configured")
                return
            resolved = await self._request("POST", "/v1/conversations/resolve", {
                "agent_id": as_uuid(self.context.agent_id), "external_id": self.external_id,
                "external_name": (self.caller or "Anonymous caller")[:255], "channel": self.channel,
            }, retry=True)
            if resolved.get("status") != "failed":
                self.conversation_id = str(resolved.get("conversation_id") or resolved.get("id") or "").strip()
            if not self.conversation_id:
                self.trace.record("conversation_persist_start_failed", reason="conversation_resolve_failed")
                return
            started = await self._request("POST", "/v1/conversations/sessions/start", {
                "conversation_id": self.conversation_id, "channel": self.channel,
                "client_session_id": self.client_session_id, "started_at": self.started_at.isoformat(),
            }, retry=True)
            nested = started.get("session")
            if started.get("status") != "failed":
                self.session_id = str(started.get("id") or started.get("session_id")
                                      or (nested.get("id") if isinstance(nested, dict) else "") or "").strip()
            if not self.session_id:
                self.trace.record("conversation_persist_start_failed", reason="canonical_session_missing",
                                  conversation_id=self.conversation_id)
                return
            self.trace.record("conversation_persist_started", reason=f"{self.channel}_session_opened",
                              conversation_id=self.conversation_id, session_id=self.session_id,
                              channel=self.channel, room_name=self.room_name)
            self._recording_task = asyncio.create_task(self._start_recording())
            await self._request("POST", f"/v1/conversations/sessions/{self.session_id}/events", {
                "event_type": "session_started", "role": "system", "title": "Session started",
                "body": f"Inbound call from {self.caller or 'anonymous caller'}",
                "payload": {"room_name": self.room_name, "caller": self.caller, "channel": self.channel},
            })

    async def add_user(self, text: str, *, turn_id: str | None = None) -> None:
        await self._add_message("user", text, turn_id)

    async def add_assistant(self, text: str, *, turn_id: str | None = None, interrupted: bool = False) -> None:
        await self._add_message("assistant", text, turn_id, interrupted=interrupted)

    async def _add_message(self, role: str, text: str, turn_id: str | None, *, interrupted: bool = False) -> None:
        clean = str(text or "").strip()
        if not clean or self._closed:
            return
        async with self._write_lock:
            await self.start()
            if not self.session_id:
                self.trace.record("conversation_persist_dropped", reason="canonical_session_missing", role=role)
                return
            key = (role, turn_id) if turn_id else None
            if (key and key in self._message_keys) or (not key and self._last_message == (role, clean)):
                return
            self._sequence += 1
            identity = f"{self.session_id}:{role}:{turn_id or self._sequence}"
            idempotency_key = "rvc:" + hashlib.sha256(identity.encode()).hexdigest()
            if key:
                self._message_keys[key] = idempotency_key
            self._last_message = (role, clean)
            if role == "user":
                self.last_user = clean
                self.recent_user = [*self.recent_user[-5:], clean]
            result = await self._request("POST", f"/v1/conversations/{self.conversation_id}/messages", {
                "role": role, "content": clean, "session_id": self.session_id,
                "idempotency_key": idempotency_key,
                "metadata": {"turn_id": turn_id, "sequence": self._sequence,
                             **({"interrupted": True, "delivery": "generated_not_confirmed"} if interrupted else {})},
            }, retry=True)
            if result.get("status") == "failed":
                self.trace.record("conversation_persist_message_failed", reason="retries_exhausted",
                                  role=role, turn_id=turn_id, sequence=self._sequence)

    async def mark_transfer(self, reason_summary: str, transfer_payload: dict[str, Any] | None = None) -> None:
        # A deployed handoff can call directly while transcript writes are queued.
        if self._worker and self._worker is not asyncio.current_task() and not self._worker.done():
            await asyncio.wait({self._worker}, timeout=self.DRAIN_TIMEOUT_SECONDS)
        async with self._write_lock:
            await self.start()
            if not self.session_id or self._closed:
                return
            draft = build_fast_handoff_analysis(reason_summary=reason_summary,
                                               last_user_transcript=self.last_user,
                                               recent_user_messages=self.recent_user)
            await self._request("POST", f"/v1/conversations/sessions/{self.session_id}/analysis", draft)
            await self._request("POST", f"/v1/conversations/sessions/{self.session_id}/events", {
                "event_type": "aicc_handoff", "role": "system", "title": "Transferred to human",
                "body": str(reason_summary or "Caller handed off to a human agent via AICC.").strip(),
                "payload": {
                    "handoff_kind": "human_handoff", "reason_summary": str(reason_summary or "").strip() or None,
                    "transfer_mode": "sip_bridge", "room_name": self.room_name, "snapshot_kind": "fast",
                    **(transfer_payload or {}),
                },
            })
            self.trace.record("conversation_handoff_snapshot", reason="aicc_handoff_analysis_ready",
                              session_id=self.session_id, primary_intent=draft["primary_intent"],
                              summary=draft["summary"][:180])

    async def _start_recording(self) -> None:
        try:
            if self.transport != "livekit" or not self.room_name:
                self._recording_detail = "not_a_livekit_room"
            elif os.getenv("LIVEKIT_RECORDING_ENABLED", "false").strip().lower() == "true":
                # Optional LiveKit/provider packages must not affect disabled calls.
                async with asyncio.timeout(self.RECORDING_START_TIMEOUT_SECONDS):
                    self._recording_helper = await asyncio.to_thread(
                        importlib.import_module, "agent.livekit_recording",
                    )
                    result = await self._recording_helper.start_room_recording(
                        room_name=self.room_name, business_id=as_uuid(self.context.business_id),
                        session_id=self.session_id, started_at=self.started_at,
                    )
                self._egress_id = result.egress_id
                self._expected_url = result.expected_url
                self._schedule_recording_stop()
                self.recording_status = "processing" if result.egress_id else "failed"
                self._recording_detail = result.detail or ("recording_started" if result.egress_id else "recording_start_failed")
        except asyncio.TimeoutError:
            # Cancellation cannot prove that the server did not start egress.
            self.recording_status = "failed"
            self._recording_detail = "recording_start_timeout_reconcile_room"
        except Exception as exc:
            self.recording_status = "failed"
            self._recording_detail = type(exc).__name__
        await self._publish_recording_status()

    async def _publish_recording_status(self) -> None:
        payload = {"recording_status": self.recording_status, "recording_url": self.recording_url,
                   "recording_duration_seconds": self._recording_duration_seconds}
        self.trace.record("conversation_recording_status", reason=self._recording_detail,
                          session_id=self.session_id, room_name=self.room_name, **payload)
        updated = await self._request("POST", f"/v1/conversations/sessions/{self.session_id}/recording", payload, retry=True)
        event = await self._request("POST", f"/v1/conversations/sessions/{self.session_id}/events", {
            "event_type": "recording_ready" if self.recording_status == "available" else "recording_status",
            "role": "system", "title": f"Recording {self.recording_status}",
            "body": self._recording_detail,
            "payload": {**payload, "egress_id": self._egress_id, "room_name": self.room_name,
                        "detail": self._recording_detail, "expected_url": self._expected_url,
                        "started_at": self.started_at.isoformat(),
                        "stop_acknowledged": self._recording_stop_acknowledged,
                        "reconciliation_required": self.recording_status == "processing"
                        or "reconcile_room" in self._recording_detail},
        }, retry=True)
        self._recording_status_dirty = updated.get("status") == "failed" or event.get("status") == "failed"
        if self._recording_status_dirty:
            self.trace.record("conversation_recording_persist_failed", reason="retries_exhausted",
                              session_id=self.session_id, room_name=self.room_name, egress_id=self._egress_id,
                              **payload)

    async def _finalize_recording(self, duration: int) -> None:
        if not self._egress_id:
            return
        try:
            result = await asyncio.wait_for(self._recording_helper.finalize_room_recording(
                egress_id=self._egress_id, expected_url=self._expected_url, duration_seconds=duration,
                request_stop=False, timeout_seconds=self.RECORDING_FINALIZE_TIMEOUT_SECONDS,
            ), timeout=self.RECORDING_FINALIZE_TIMEOUT_SECONDS + 1.0)
            self.recording_status = result.status if result.status in {"available", "processing", "failed"} else "failed"
            self.recording_url = result.recording_url if self.recording_status == "available" else None
            self._recording_duration_seconds = result.duration_seconds
            self._recording_detail = result.detail or f"recording_{self.recording_status}"
            if self.recording_status == "available" and not self.recording_url:
                self.recording_status, self._recording_detail = "failed", "missing_recording_url"
        except asyncio.TimeoutError:
            self.recording_status, self._recording_detail = "processing", "recording_finalize_timeout"
        except Exception as exc:
            self.recording_status, self._recording_detail = "failed", type(exc).__name__
        await self._publish_recording_status()

    async def close(self) -> None:
        self.mark_disconnected()
        async with self._close_lock:
            if self._closed:
                return
            self._closing = True
            try:
                if self._worker:
                    try:
                        await asyncio.wait_for(asyncio.shield(self._worker), timeout=self.DRAIN_TIMEOUT_SECONDS)
                    except asyncio.TimeoutError:
                        self.trace.record("conversation_persist_drain_timeout", reason="shutdown_budget_exhausted",
                                          pending=len(self._pending))
                        self._worker.cancel()
                        await asyncio.gather(self._worker, return_exceptions=True)
                if self._recording_task:
                    try:
                        await asyncio.wait_for(self._recording_task, timeout=self.RECORDING_START_TIMEOUT_SECONDS + 17)
                    except asyncio.TimeoutError:
                        self.recording_status, self._recording_detail = "failed", "recording_start_timeout_reconcile_room"
                        await self._publish_recording_status()
                if self.session_id:
                    duration = self._duration_seconds
                    if self._recording_stop_task:
                        await self._recording_stop_task
                    await self._finalize_recording(duration)
                    if self._recording_status_dirty:
                        await self._publish_recording_status()
                    ended = await self._request("POST", "/v1/conversations/sessions/end", {
                        "session_id": self.session_id, "duration_seconds": duration,
                        "ended_at": self._ended_at.isoformat(),
                    }, retry=True)
                    self.trace.record("conversation_persist_ended" if ended.get("status") != "failed"
                                      else "conversation_persist_end_failed", reason=f"{self.channel}_session_closed",
                                      session_id=self.session_id, duration_seconds=duration)
            finally:
                self._closed = True
                tasks = [task for task in (self._worker, self._recording_task, self._recording_stop_task)
                         if task and not task.done()]
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                while self._pending:
                    self._pending.popleft().close()
