import asyncio
import json
import logging
import os
from typing import Any, Callable

import numpy as np

from .voice_auth import (
    DEFAULT_SAMPLE_RATE,
    compare_pcm,
    normalize_owner_email,
    resample_mono,
)

logger = logging.getLogger(__name__)

AUTH_PENDING = "pending"
AUTH_VERIFIED = "verified"
AUTH_FAILED = "failed"
AUTH_STATUS_TOPIC = "odion.auth.status"
AUTH_ACTION_STATUS_TOPIC = "odion.auth.action_status"
AUTH_ACTION_TOPIC = "odion.auth.action"
ACTION_AIRTIME_PURCHASE = "complete_airtime_purchase"
ACTION_FUNDS_TRANSFER = "complete_funds_transfer"
PRIVILEGED_ACTIONS = (ACTION_AIRTIME_PURCHASE, ACTION_FUNDS_TRANSFER)
ACTION_LABELS = {
    ACTION_AIRTIME_PURCHASE: "Airtime top-up",
    ACTION_FUNDS_TRANSFER: "Funds transfer",
}
DEMO_EMAIL_TO = str(os.getenv("AUTH_OBSERVER_EMAIL_TO") or "woron@odion.ai").strip() or "woron@odion.ai"
PCM_BUFFER_SECONDS = 8.0
CompareFn = Callable[..., dict[str, Any]]
WEMA_DEMO_PROMPT = (
    "You are Wema Bank's customer care voice assistant in Nigeria.\n"
    "- Help with everyday Wema Bank requests: airtime top-up, funds transfer, "
    "account questions, cards, and general banking help.\n"
    "- Be warm, clear, and concise. Speak like a helpful bank agent, not a generic chatbot.\n"
    "- Do not mention tools, observers, authentication systems, embeddings, or badges.\n"
    "- If you do not have a live backend for a request, explain the next step the customer "
    "should take instead of inventing balances, account data, or transaction references.\n"
    "- Airtime top-up is a transaction. First collect the amount and the destination phone "
    "number. Read both back and wait for the caller to confirm they are correct. Only then "
    "call complete_airtime_purchase.\n"
    "- Funds transfer is a transaction. First collect the amount, the destination account "
    "number, and the destination bank. Read those details back and wait for confirmation. "
    "Only then call complete_funds_transfer.\n"
    "- Never call a transaction tool as soon as you hear an amount or number.\n"
    "- Both transaction tools run a second voice check. If a tool is blocked, tell the "
    "caller you could not recognize their voice, so you cannot complete the transaction. "
    "Do not pretend it went through.\n"
    "- If the caller says they are done or have nothing else to talk about, offer to send "
    "an email recap of this conversation. For this demo, do not actually send email; "
    "if they accept, say you will send the recap after the call.\n"
    "- Never ask the caller for an email address."
)
AUTH_VERIFIED_HINT = (
    "[AUTH: VERIFIED] Session voice authentication passed. "
    "Continue the Wema Bank conversation normally. For airtime top-up or funds transfer, "
    "collect the details, confirm them, then use the matching tool. "
    "Do not mention the checks."
)
AUTH_UNVERIFIED_HINT = (
    "[AUTH: UNAUTHENTICATED] Session voice authentication has not passed yet. "
    "Continue answering general Wema Bank questions. If they ask for a transaction, collect "
    "and confirm the details first, then call the matching tool so the action check can "
    "record the block. If it is blocked, say you could not recognize their voice."
)
AUTH_FAILED_HINT = (
    "[AUTH: FAILED] Session voice authentication failed. "
    "Continue helping with general Wema Bank questions. If they ask for a transaction, collect "
    "and confirm the details first, then call the matching tool so the action check can "
    "record the block. If it is blocked, say you could not recognize their voice."
)
INJECTED_USER_PREFIXES = (
    "start the conversation now",
    "greet the caller first",
)


def auth_observer_enabled() -> bool:
    raw = str(os.getenv("AUTH_OBSERVER_ENABLED", "true") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def auth_observer_delay_seconds() -> float:
    raw = str(os.getenv("AUTH_OBSERVER_DELAY_SECONDS", "1.5") or "1.5").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 1.5


def action_label(action: str) -> str:
    return ACTION_LABELS.get(str(action or "").strip(), "Bank transaction")


def auth_observer_instructions() -> str:
    return (
        "Background voice authentication uses two cosine voice checks and must not interrupt the call.\n"
        "- Keep talking normally. Do not mention the checks, observers, embeddings, or badges.\n"
        "- A session check runs once in the background after the caller has spoken enough.\n"
        "- An action check runs again when complete_airtime_purchase or "
        "complete_funds_transfer is used.\n"
        "- Call those tools only after the caller has confirmed the transaction details.\n"
        "- A transaction completes only when both voice checks pass.\n"
        "- If a tool returns that the voice was not recognized, say that to the caller."
    )


def apply_auth_observer_session(userdata: dict[str, Any], instructions: str) -> str:
    if not auth_observer_enabled():
        userdata["auth_observer_enabled"] = False
        userdata.setdefault("auth_status", AUTH_VERIFIED)
        return instructions

    enabled_tool_names = list(userdata.get("enabled_tool_names") or [])
    for tool_name in PRIVILEGED_ACTIONS:
        if tool_name not in enabled_tool_names:
            enabled_tool_names.append(tool_name)
    userdata["enabled_tool_names"] = enabled_tool_names
    userdata["auth_observer_enabled"] = True
    userdata["auth_status"] = AUTH_PENDING
    userdata["action_auth_status"] = AUTH_PENDING
    userdata["configured_agent_name"] = userdata.get("configured_agent_name") or "Wema Care"
    logger.info(
        "Auth observer enabled: cosine voice checks delay_seconds=%s owner=%s",
        auth_observer_delay_seconds(),
        normalize_owner_email(str(userdata.get("end_user_id") or "")),
    )
    return (
        f"{WEMA_DEMO_PROMPT}\n\n"
        f"{auth_observer_instructions()}\n"
    )


def is_injected_user_text(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    return any(lowered.startswith(prefix) or prefix in lowered[:120] for prefix in INJECTED_USER_PREFIXES)


def _item_role(item: Any) -> str:
    return str(getattr(item, "role", "") or "").strip().lower()


def _item_text(item: Any) -> str:
    content = getattr(item, "content", None)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str) and part.strip():
                parts.append(part.strip())
            else:
                text = getattr(part, "text", None) or getattr(part, "transcript", None)
                if str(text or "").strip():
                    parts.append(str(text).strip())
        return " ".join(parts).strip()
    text = getattr(item, "text_content", None) or getattr(item, "text", None)
    return str(text or "").strip()


def _status_from_compare(result: dict[str, Any]) -> str:
    if result.get("matched"):
        return AUTH_VERIFIED
    if str(result.get("reason") or "") in {"audio_too_short", "not_speech"}:
        return AUTH_PENDING
    return AUTH_FAILED


class FakeVoiceAuthObserver:
    """Session cosine while talking, plus a second cosine before a privileged action."""

    def __init__(
        self,
        session: Any,
        *,
        room: Any | None = None,
        delay_seconds: float | None = None,
        compare_fn: CompareFn | None = None,
    ) -> None:
        self.session = session
        self.room = room
        self.delay_seconds = (
            auth_observer_delay_seconds() if delay_seconds is None else max(0.0, float(delay_seconds))
        )
        self.compare_fn = compare_fn or compare_pcm
        self._pcm_lock = asyncio.Lock()
        self._pcm = np.zeros(0, dtype=np.float32)
        self._pcm_rate = DEFAULT_SAMPLE_RATE
        self._last_utterance = np.zeros(0, dtype=np.float32)
        self._session_decided = False
        self._session_task_started = False
        self._bg_tasks: set[asyncio.Task] = set()
        self._setup_listeners()
        self._setup_audio()
        self._setup_room_ready()
        self._track(self._publish_auth_status(AUTH_PENDING))
        self._track(self._publish_action_status(AUTH_PENDING))
        logger.info(
            "[AUTH-OBSERVER] attached delay=%ss owner=%s",
            self.delay_seconds,
            self._owner_email() or "unknown",
        )

    def _userdata(self) -> dict[str, Any]:
        userdata = getattr(self.session, "userdata", None)
        return userdata if isinstance(userdata, dict) else {}

    def _owner_email(self) -> str:
        return normalize_owner_email(str(self._userdata().get("end_user_id") or ""))

    def _current_status(self) -> str:
        status = str(self._userdata().get("auth_status") or AUTH_PENDING).strip().lower()
        if status in {AUTH_VERIFIED, AUTH_FAILED, AUTH_PENDING}:
            return status
        return AUTH_PENDING

    def _track(self, coro: Any) -> None:
        task = asyncio.create_task(coro, name="auth-observer-task")
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _setup_listeners(self) -> None:
        @self.session.on("conversation_item_added")
        def _on_conversation_item_added(event: Any) -> None:
            item = getattr(event, "item", event)
            if _item_role(item) != "user":
                return
            text = _item_text(item)
            if not text or is_injected_user_text(text):
                if text:
                    logger.info("[AUTH-OBSERVER] ignored injected user prompt: %s", text[:80])
                return
            self._userdata()["last_user_transcript"] = text
            self._track(self._on_user_speech(text))

    def _local_participant(self) -> Any | None:
        room = self.room
        if room is None:
            return None
        try:
            return room.local_participant
        except Exception:
            return None

    def _setup_room_ready(self) -> None:
        room = self.room
        if room is None or not hasattr(room, "on"):
            return

        @room.on("connected")
        def _on_connected(*_args: Any) -> None:
            self._track(self._publish_auth_status(self._current_status()))
            self._track(self._publish_action_status(AUTH_PENDING))

    def _setup_audio(self) -> None:
        room = self.room
        if room is None:
            return
        try:
            from livekit import rtc
        except Exception:
            logger.exception("[AUTH-OBSERVER] livekit rtc unavailable for mic capture")
            return

        def _maybe_consume(track: Any, participant: Any, publication: Any = None) -> None:
            kind = getattr(track, "kind", None)
            if kind not in {getattr(rtc.TrackKind, "KIND_AUDIO", "audio"), "audio", 1}:
                return
            source = getattr(publication, "source", None) or getattr(track, "source", None)
            microphone = getattr(rtc.TrackSource, "SOURCE_MICROPHONE", "microphone")
            if source not in {None, microphone, "microphone", 1}:
                return
            if participant is not None and getattr(participant, "identity", "") == getattr(
                getattr(room, "local_participant", None), "identity", ""
            ):
                return
            self._track(self._consume_audio(track))

        @room.on("track_subscribed")
        def _on_track_subscribed(track: Any, publication: Any, participant: Any) -> None:
            _maybe_consume(track, participant, publication)

        remote_participants = getattr(room, "remote_participants", {}) or {}
        if isinstance(remote_participants, dict):
            participants = list(remote_participants.values())
        else:
            participants = list(remote_participants)
        for participant in participants:
            publications = getattr(participant, "track_publications", {}) or {}
            pubs = publications.values() if isinstance(publications, dict) else publications
            for publication in pubs:
                track = getattr(publication, "track", None)
                if track is not None:
                    _maybe_consume(track, participant, publication)

    async def _consume_audio(self, track: Any) -> None:
        try:
            from livekit.rtc import AudioStream
        except Exception:
            logger.exception("[AUTH-OBSERVER] AudioStream unavailable")
            return
        try:
            stream = AudioStream.from_track(
                track=track,
                sample_rate=DEFAULT_SAMPLE_RATE,
                num_channels=1,
            )
            async for event in stream:
                frame = getattr(event, "frame", event)
                data = getattr(frame, "data", None)
                if data is None:
                    continue
                samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                rate = int(getattr(frame, "sample_rate", DEFAULT_SAMPLE_RATE) or DEFAULT_SAMPLE_RATE)
                await self.ingest_pcm(samples, rate)
        except Exception:
            logger.exception("[AUTH-OBSERVER] caller audio capture failed")

    async def ingest_pcm(self, samples: np.ndarray, sample_rate: int = DEFAULT_SAMPLE_RATE) -> None:
        audio = resample_mono(np.asarray(samples, dtype=np.float32).reshape(-1), int(sample_rate), self._pcm_rate)
        if audio.size == 0:
            return
        async with self._pcm_lock:
            self._pcm = np.concatenate([self._pcm, audio]) if self._pcm.size else audio
            max_n = int(PCM_BUFFER_SECONDS * self._pcm_rate)
            if self._pcm.size > max_n:
                self._pcm = self._pcm[-max_n:]

    def _latest_clip(self) -> tuple[np.ndarray, int]:
        if self._last_utterance.size:
            return np.array(self._last_utterance, copy=True), self._pcm_rate
        return np.zeros(0, dtype=np.float32), self._pcm_rate

    def _snapshot_utterance_locked(self) -> None:
        if self._pcm.size:
            self._last_utterance = np.array(self._pcm, copy=True)

    async def _on_user_speech(self, text: str) -> None:
        async with self._pcm_lock:
            self._snapshot_utterance_locked()
            clip_seconds = self._last_utterance.size / float(self._pcm_rate or DEFAULT_SAMPLE_RATE)
        logger.info(
            "[AUTH-OBSERVER] captured latest user utterance seconds=%.2f text=%s",
            clip_seconds,
            text[:80],
        )
        if self._session_decided:
            logger.info("[AUTH-OBSERVER] stored later speech for action check; session badge stays")
            return
        if self._session_task_started:
            return
        if clip_seconds < 1.2:
            logger.info("[AUTH-OBSERVER] skipped session cosine; waiting for real speech")
            return
        self._session_task_started = True
        logger.info("[AUTH-OBSERVER] queued session cosine after first transcribed speech")
        await self._evaluate_session()

    def _compare_latest(self) -> dict[str, Any]:
        samples, rate = self._latest_clip()
        return self.compare_fn(self._owner_email(), samples, rate)

    def _hint_for_status(self, status: str) -> str:
        if status == AUTH_VERIFIED:
            return AUTH_VERIFIED_HINT
        if status == AUTH_FAILED:
            return AUTH_FAILED_HINT
        return AUTH_UNVERIFIED_HINT

    async def _evaluate_session(self) -> None:
        try:
            if self._session_decided:
                return
            if self.delay_seconds:
                logger.info("[AUTH-OBSERVER] session cosine waiting %.1fs", self.delay_seconds)
                await asyncio.sleep(self.delay_seconds)
            result = self._compare_latest()
            next_status = _status_from_compare(result)
            logger.info(
                "[AUTH-OBSERVER] session cosine matched=%s score=%.3f reason=%s",
                result.get("matched"),
                float(result.get("score") or 0.0),
                result.get("reason") or "ok",
            )
            if next_status == AUTH_PENDING:
                self._session_task_started = False
                await self._publish_auth_status(self._current_status())
                return
            self._session_decided = True
            await self._inject(next_status, self._hint_for_status(next_status))
        except Exception:
            self._session_task_started = False
            logger.exception("[AUTH-OBSERVER] session cosine failed")

    async def authorize_action(
        self,
        *,
        action: str,
        transcript: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if transcript:
            self._userdata()["last_user_transcript"] = str(transcript)
        async with self._pcm_lock:
            if not self._last_utterance.size and self._pcm.size:
                self._snapshot_utterance_locked()
        if self.delay_seconds:
            logger.info("[AUTH-OBSERVER] action cosine waiting %.1fs action=%s", self.delay_seconds, action)
            await asyncio.sleep(self.delay_seconds)
        if not self._session_decided:
            await self._evaluate_session()

        session_status = self._current_status()
        action_result = self._compare_latest()
        action_status = _status_from_compare(action_result)
        if action_status == AUTH_PENDING:
            action_status = AUTH_FAILED
        self._userdata()["action_auth_status"] = action_status
        await self._publish_action_status(action_status, action_result)

        if session_status != AUTH_VERIFIED:
            result = {
                "authorized": False,
                "outcome": "blocked",
                "reason": "session_unauthenticated",
                "action": action,
                "session_status": session_status,
                "action_status": action_status,
                "score": float(action_result.get("score") or 0.0),
            }
        elif action_status != AUTH_VERIFIED:
            result = {
                "authorized": False,
                "outcome": "blocked",
                "reason": "action_check_failed",
                "action": action,
                "session_status": session_status,
                "action_status": action_status,
                "score": float(action_result.get("score") or 0.0),
            }
        else:
            result = {
                "authorized": True,
                "outcome": "completed",
                "reason": "",
                "action": action,
                "session_status": session_status,
                "action_status": action_status,
                "score": float(action_result.get("score") or 0.0),
            }

        if details:
            result.update(details)
        await self._publish_action_event(result)
        logger.info(
            "[AUTH-OBSERVER] action=%s outcome=%s reason=%s session=%s action_check=%s score=%.3f",
            action,
            result["outcome"],
            result["reason"] or "ok",
            session_status,
            action_status,
            float(result.get("score") or 0.0),
        )
        return result

    async def _publish_auth_status(self, status: str) -> None:
        await self._publish_json(
            AUTH_STATUS_TOPIC,
            {
                "type": "odion.auth.status",
                "status": status,
                "authenticated": status == AUTH_VERIFIED,
            },
            f"status={status}",
        )

    async def _publish_action_status(
        self,
        status: str,
        compare_result: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "type": "odion.auth.action_status",
            "status": status,
            "authenticated": status == AUTH_VERIFIED,
        }
        if compare_result:
            payload["score"] = float(compare_result.get("score") or 0.0)
            payload["reason"] = str(compare_result.get("reason") or "")
        await self._publish_json(AUTH_ACTION_STATUS_TOPIC, payload, f"action_status={status}")

    async def _publish_action_event(self, result: dict[str, Any]) -> None:
        await self._publish_json(
            AUTH_ACTION_TOPIC,
            {
                "type": "odion.auth.action",
                "action": result.get("action") or ACTION_AIRTIME_PURCHASE,
                "label": action_label(str(result.get("action") or "")),
                "outcome": result.get("outcome") or "blocked",
                "reason": result.get("reason") or "",
                "authorized": bool(result.get("authorized")),
                "amount_naira": result.get("amount_naira") or "",
                "phone_number": result.get("phone_number") or "",
                "account_number": result.get("account_number") or "",
                "bank_name": result.get("bank_name") or "",
            },
            f"action={result.get('action')} outcome={result.get('outcome')}",
        )

    async def _publish_json(self, topic: str, payload: dict[str, Any], log_label: str) -> None:
        participant = self._local_participant()
        publish = getattr(participant, "publish_data", None)
        if not callable(publish):
            logger.info("[AUTH-OBSERVER] no room publisher for %s", log_label)
            return
        try:
            await publish(json.dumps(payload), reliable=True, topic=topic)
            logger.info("[AUTH-OBSERVER] published UI %s", log_label)
        except Exception:
            logger.exception("[AUTH-OBSERVER] failed to publish UI %s", log_label)

    async def _inject(self, status: str, hint: str) -> None:
        userdata = self._userdata()
        userdata["auth_status"] = status
        await self._publish_auth_status(status)
        current_agent = getattr(self.session, "current_agent", None)
        if current_agent is None:
            logger.warning("[AUTH-OBSERVER] no active agent to inject %s", status)
            return

        # Qwen rejects mid-conversation system messages. Keep the latest auth
        # hint in the leading instructions instead of appending to chat_ctx.
        update_instructions = getattr(current_agent, "update_instructions", None)
        if callable(update_instructions):
            base = str(userdata.get("base_instructions") or "").strip()
            await update_instructions(f"{base}\n\n{hint}\n" if base else hint)
            logger.info("[AUTH-OBSERVER] injected %s via update_instructions", status)
            return

        logger.warning("[AUTH-OBSERVER] could not inject %s into the live agent", status)


def start_auth_observer(session: Any, room: Any | None = None) -> FakeVoiceAuthObserver | None:
    userdata = getattr(session, "userdata", None)
    if not isinstance(userdata, dict) or not userdata.get("auth_observer_enabled"):
        return None
    observer = FakeVoiceAuthObserver(session, room=room)
    userdata["auth_observer"] = observer
    return observer
