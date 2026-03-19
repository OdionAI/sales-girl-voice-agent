import json
import logging
import os
import re
import asyncio
import base64
import hashlib
from typing import Any
import uuid

from dotenv import load_dotenv
from livekit.agents import AgentServer, AgentSession, JobContext, cli, room_io
from livekit.plugins import deepgram, google

from agent.conversation_memory import (
    append_message,
    init_store,
    load_resume_context,
)
from agent.conversation_service_api import (
    append_message as append_message_remote,
    create_session_event as create_session_event_remote,
    end_session as end_session_remote,
    fetch_context as fetch_context_remote,
    is_enabled as conversation_service_enabled,
    resolve_conversation as resolve_conversation_remote,
    start_session as start_session_remote,
    update_session_recording as update_session_recording_remote,
    utcnow as conv_api_utcnow,
)
from agent.agent_config_api import get_active_config as get_agent_active_config
from agent.ops_api import (
    get_payment_summary as ops_get_payment_summary,
    get_tariff_profile as ops_get_tariff_profile,
    get_vending_history as ops_get_vending_history,
    lookup_customer_account as ops_lookup_customer_account,
)
from agent.odion_tts import OdionTTS
from agent.observability import flush_traces, trace_conversation_event
from agent.livekit_recording import (
    finalize_room_recording,
    is_recording_enabled,
    start_room_recording,
)
from agent.salon_en import SalonAgentEN
from agent.salon_fr import SalonAgentFR
from prompts.en import SYSTEM_PROMPT_EN
from prompts.fr import SYSTEM_PROMPT_FR


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Load environment variables from a .env file in the project root (if present)
load_dotenv()

# AgentServer allows only one rtc_session per process. To support both English and
# French, run two worker processes with EN/FR-prefixed names.
AGENT_NAME = os.environ.get("AGENT_NAME", "sales-girl-agent-en")


def _is_en_agent_name(name: str) -> bool:
    value = str(name or "").strip().lower()
    return (
        value == "sales-girl-agent-en"
        or value.startswith("sales-girl-agent-en-")
        or value == "odion-tts-staging-agent"
    )


def _is_fr_agent_name(name: str) -> bool:
    value = str(name or "").strip().lower()
    return value == "sales-girl-agent-fr" or value.startswith("sales-girl-agent-fr-")


if not (_is_en_agent_name(AGENT_NAME) or _is_fr_agent_name(AGENT_NAME)):
    raise SystemExit(
        "AGENT_NAME must be 'sales-girl-agent-en'/'sales-girl-agent-fr' or "
        "prefixed variants like 'sales-girl-agent-en-staging', or the "
        "experimental 'odion-tts-staging-agent'. Example: "
        "AGENT_NAME=sales-girl-agent-fr python main.py dev"
    )

server = AgentServer(
    num_idle_processes=1,
    initialize_process_timeout=60,
)
init_store()


def _short_text(value: Any, limit: int = 320) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit - 1]}…"


def _summarize_tool_output(value: Any) -> str:
    if value is None:
        return "No output returned."
    if isinstance(value, (dict, list)):
        try:
            return _short_text(json.dumps(value, ensure_ascii=True, default=str), 400)
        except Exception:
            return _short_text(str(value), 400)
    return _short_text(str(value), 400)


def _persist_session_event_async(
    userdata: dict[str, Any],
    *,
    event_type: str,
    role: str | None = None,
    title: str | None = None,
    body: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    business_id = str(userdata.get("business_id") or "")
    session_tracker_id = str(userdata.get("session_tracker_id") or "").strip()
    if not (conversation_service_enabled(business_id) and session_tracker_id):
        return

    async def _persist() -> None:
        persisted = await create_session_event_remote(
            session_id=session_tracker_id,
            event_type=event_type,
            role=role,
            title=title,
            body=body,
            payload=payload,
            business_id=business_id,
        )
        if str(persisted.get("status") or "") == "failed":
            logger.error(
                "Session event persist failed: session_id=%s event_type=%s detail=%s http_status=%s",
                session_tracker_id,
                event_type,
                persisted.get("detail"),
                persisted.get("http_status"),
            )

    _track_background_task(userdata, _persist())


def _track_background_task(userdata: dict[str, Any], coro: Any) -> None:
    task = asyncio.create_task(coro)
    tasks = userdata.setdefault("background_tasks", set())
    tasks.add(task)

    def _cleanup(done_task: asyncio.Task[Any]) -> None:
        try:
            tasks.discard(done_task)
        except Exception:
            pass

    task.add_done_callback(_cleanup)


async def _drain_background_tasks(userdata: dict[str, Any]) -> None:
    pending = list(userdata.get("background_tasks") or [])
    if not pending:
        return
    results = await asyncio.gather(*pending, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            logger.error("Background persistence task failed: %s", result)


async def _finalize_session_cleanup(
    *,
    userdata: dict[str, Any],
    business_id: str,
    session_tracker_id: str,
    started_at: Any,
    call_channel: str,
    language: str,
    shutdown_reason: str | None,
) -> None:
    cleanup_lock = userdata.setdefault("session_cleanup_lock", asyncio.Lock())
    async with cleanup_lock:
        if userdata.get("session_cleanup_completed"):
            return
        userdata["session_cleanup_completed"] = True

        ended_at = conv_api_utcnow()
        duration = int(max(0, (ended_at - started_at).total_seconds()))
        logger.info(
            "Finalizing session cleanup: language=%s session_id=%s duration=%ss shutdown_reason=%s recording_enabled=%s",
            language,
            session_tracker_id or userdata.get("session_id") or "",
            duration,
            shutdown_reason or "",
            is_recording_enabled(),
        )

        try:
            await _drain_background_tasks(userdata)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to drain background tasks during session cleanup: %s", exc)

        recording_status = None
        recording_url = None
        recording_duration_seconds = None
        recording_detail = None

        if conversation_service_enabled(business_id) and session_tracker_id and is_recording_enabled():
            try:
                logger.info(
                    "Finalizing room recording: session_id=%s egress_id=%s expected_url=%s",
                    session_tracker_id,
                    str(userdata.get("recording_egress_id") or ""),
                    str(userdata.get("recording_expected_url") or ""),
                )
                recording_finalized = await finalize_room_recording(
                    egress_id=str(userdata.get("recording_egress_id") or "").strip() or None,
                    expected_url=str(userdata.get("recording_expected_url") or "").strip() or None,
                    duration_seconds=duration,
                )
                recording_status = recording_finalized.status
                recording_url = recording_finalized.recording_url
                recording_duration_seconds = recording_finalized.duration_seconds
                recording_detail = recording_finalized.detail

                logger.info(
                    "Recording finalize result: session_id=%s status=%s url=%s detail=%s",
                    session_tracker_id,
                    recording_status,
                    recording_url,
                    recording_detail,
                )
                persisted = await update_session_recording_remote(
                    session_id=session_tracker_id,
                    recording_status=recording_status,
                    recording_url=recording_url,
                    recording_duration_seconds=recording_duration_seconds,
                    business_id=business_id,
                )
                if str(persisted.get("status") or "") != "success":
                    logger.error(
                        "Recording metadata persist failed: session_id=%s detail=%s http_status=%s",
                        session_tracker_id,
                        persisted.get("detail"),
                        persisted.get("http_status"),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed during recording finalization: session_id=%s error=%s", session_tracker_id, exc)
                recording_status = recording_status or "failed"
                recording_detail = recording_detail or str(exc)

            _persist_session_event_async(
                userdata,
                event_type="recording_ready" if recording_status == "available" else "recording_status",
                role="system",
                title="Recording available" if recording_status == "available" else "Recording status updated",
                body=(
                    f"Audio recording saved to {recording_url}."
                    if recording_status == "available"
                    else f"Recording status is {recording_status or 'unknown'}."
                ),
                payload={
                    "recording_status": recording_status,
                    "recording_url": recording_url,
                    "duration_seconds": recording_duration_seconds,
                    "detail": recording_detail,
                },
            )

        _persist_session_event_async(
            userdata,
            event_type="session_ended",
            role="system",
            title="Session ended",
            body=f"{'English' if language == 'en' else 'French'} session ended after {duration} seconds.",
            payload={
                "language": language,
                "channel": call_channel,
                "duration_seconds": duration,
                "shutdown_reason": shutdown_reason,
            },
        )

        try:
            await _drain_background_tasks(userdata)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to flush final background tasks during session cleanup: %s", exc)

        if conversation_service_enabled(business_id) and session_tracker_id:
            try:
                ended = await end_session_remote(
                    session_id=session_tracker_id,
                    duration_seconds=duration,
                    business_id=business_id,
                )
                if str(ended.get("status") or "") != "success":
                    logger.error(
                        "End session persist failed: session_id=%s detail=%s http_status=%s",
                        session_tracker_id,
                        ended.get("detail"),
                        ended.get("http_status"),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to persist session end: session_id=%s error=%s", session_tracker_id, exc)

REQUIRE_VERIFIED_PHONE = os.getenv("REQUIRE_VERIFIED_PHONE", "true").lower() == "true"
CONVERSATION_SERVICE_REQUIRED = os.getenv("CONVERSATION_SERVICE_REQUIRED", "true").lower() == "true"
ENABLE_ODION_TTS_EN = os.getenv("ENABLE_ODION_TTS_EN", "false").lower() == "true"
ODION_TTS_EXPERIMENT_OWNER_ID = str(os.getenv("ODION_TTS_EXPERIMENT_OWNER_ID") or "").strip()
ODION_TTS_EXPERIMENT_VOICE_ID = str(os.getenv("ODION_TTS_EXPERIMENT_VOICE_ID") or "").strip()
ODION_TTS_EXPERIMENT_LANGUAGE_HINT = str(os.getenv("ODION_TTS_EXPERIMENT_LANGUAGE_HINT") or "English").strip() or "English"


def _normalize_business_id(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        return ""


def _text_from_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                txt = item.strip()
                if txt:
                    parts.append(txt)
            elif hasattr(item, "text"):
                txt = str(getattr(item, "text", "")).strip()
                if txt:
                    parts.append(txt)
            elif isinstance(item, dict):
                txt = str(item.get("text") or "").strip()
                if txt:
                    parts.append(txt)
        return " ".join(parts).strip()
    if hasattr(content, "text"):
        return str(getattr(content, "text", "")).strip()
    return str(content).strip()


def _phone_from_room_name(room_name: str) -> str:
    # Expected room format: voice_assistant_room_u<digits>_<rand>
    m = re.match(r"^voice_assistant_room_u(\d{8,15})_\d+$", room_name or "")
    if not m:
        return ""
    digits = "".join(ch for ch in str(m.group(1) or "") if ch.isdigit())
    return f"+{digits}" if digits else ""


def _email_from_room_name(room_name: str) -> str:
    # Expected room format for web email identity:
    # voice_assistant_room_eid<base64url_email>_<rand>
    m = re.match(r"^voice_assistant_room_eid([A-Za-z0-9_-]+)_\d+$", room_name or "")
    if not m:
        return ""
    token = str(m.group(1) or "").strip()
    if not token:
        return ""
    return _normalize_end_user_id(_decode_room_token(token))


def _decode_room_token(token: str) -> str:
    raw = str(token or "").strip()
    if not raw:
        return ""
    # Preferred modern format: h<hex-utf8>
    if raw.startswith("h"):
        hex_payload = raw[1:]
        if hex_payload and re.fullmatch(r"[0-9a-fA-F]+", hex_payload):
            try:
                return bytes.fromhex(hex_payload).decode("utf-8").strip()
            except Exception:
                return ""
        return ""
    try:
        return base64.urlsafe_b64decode(raw + "=" * ((4 - len(raw) % 4) % 4)).decode("utf-8").strip()
    except Exception:
        return ""


def _web_room_context_from_name(room_name: str) -> tuple[str, str, str, str, str]:
    # Supports:
    # voice_assistant_room_eid<emailToken>_bid<bizToken>_aid<agentToken>_nid<nameToken>_<rand>
    # Optional segments: bid/aid/nid. Parse by segments to avoid greedy regex issues.
    raw = str(room_name or "").strip()
    prefix = "voice_assistant_room_"
    if not raw.startswith(prefix):
        return "", "", "", "", ""
    body = raw[len(prefix) :]
    # Collision-safe parser: tokens can contain underscores, so don't split by "_".
    # Layout:
    # eid<token>[ _bid<token> ][ _aid<token> ][ _nid<token> ][ _uid<token> ]_<rand>
    m = re.match(
        r"^eid(?P<eid>.+?)(?:_bid(?P<bid>.+?))?(?:_aid(?P<aid>.+?))?(?:_nid(?P<nid>.+?))?(?:_uid(?P<uid>.+?))?_(?P<rand>\d+)$",
        body,
    )
    if not m:
        return "", "", "", "", ""
    email_token = str(m.group("eid") or "")
    business_token = str(m.group("bid") or "")
    agent_token = str(m.group("aid") or "")
    name_token = str(m.group("nid") or "")
    user_name_token = str(m.group("uid") or "")

    email = _normalize_end_user_id(_decode_room_token(email_token))
    business_id = str(_decode_room_token(business_token) or "").strip()
    config_agent_id = str(_decode_room_token(agent_token) or "").strip()
    configured_name = str(_decode_room_token(name_token) or "").strip()
    end_user_name = str(_decode_room_token(user_name_token) or "").strip()
    return email, business_id, config_agent_id, configured_name, end_user_name


def _normalize_end_user_id(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "@" in raw:
        return raw.lower()
    digits = "".join(ch for ch in raw if ch.isdigit())
    return f"+{digits}" if digits else ""


def _room_name_from_ctx(ctx: JobContext) -> str:
    # During entrypoint bootstrap, ctx.room may not be connected yet.
    # ctx.job.room.name is available from assignment metadata.
    try:
        job_room_name = str(getattr(getattr(getattr(ctx, "job", None), "room", None), "name", "") or "").strip()
        if job_room_name:
            return job_room_name
    except Exception:
        pass
    return str(getattr(getattr(ctx, "room", None), "name", "") or "").strip()


def _stable_id(value: str, *, prefix: str, max_len: int) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) <= max_len:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


def _decode_identity_email(identity: str) -> str:
    prefix = "voice_assistant_user_email_"
    if not str(identity or "").startswith(prefix):
        return ""
    encoded = str(identity)[len(prefix) :]
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=" * ((4 - len(encoded) % 4) % 4)).decode("utf-8")
    except Exception:
        return ""
    return _normalize_end_user_id(decoded)


def _participant_identity_from_ctx(ctx: JobContext) -> tuple[str, str, str, str, str, str]:
    room = getattr(ctx, "room", None)
    room_name = _room_name_from_ctx(ctx)
    fallback_business_id = _normalize_business_id(os.getenv("CONVERSATION_BUSINESS_ID", ""))

    # First preference for web: encoded identity/context in room name (available at bootstrap).
    email_from_room, room_business_id, room_config_agent_id, room_configured_name, room_end_user_name = _web_room_context_from_name(room_name)
    if not email_from_room:
        email_from_room = _email_from_room_name(room_name)
    if email_from_room:
        return (
            email_from_room,
            "web",
            _normalize_business_id(room_business_id) or fallback_business_id,
            room_config_agent_id,
            room_configured_name,
            room_end_user_name,
        )

    # First preference: encoded phone in room name (always available at session bootstrap)
    phone_from_room = _phone_from_room_name(room_name)
    if phone_from_room:
        return phone_from_room, "voice", fallback_business_id, "", "", ""

    # Fallback: read remote participant metadata / identity
    participants = getattr(room, "remote_participants", None)
    if not participants:
        return "", "voice", fallback_business_id, "", "", ""

    values = participants.values() if hasattr(participants, "values") else participants
    for participant in values:
        metadata_business_id = ""
        metadata_config_agent_id = ""
        metadata_configured_agent_name = ""
        metadata_end_user_name = ""
        metadata_raw = str(getattr(participant, "metadata", "") or "").strip()
        if metadata_raw:
            try:
                payload = json.loads(metadata_raw)
                metadata_business_id = str(payload.get("business_id") or "").strip()
                metadata_config_agent_id = str(payload.get("config_agent_id") or "").strip()
                metadata_configured_agent_name = str(payload.get("configured_agent_name") or "").strip()
                metadata_end_user_name = str(payload.get("end_user_name") or "").strip()
                email_candidate = str(payload.get("end_user_email") or "").strip()
                if email_candidate:
                    normalized_email = _normalize_end_user_id(email_candidate)
                    if normalized_email:
                        return (
                            normalized_email,
                            "web",
                            _normalize_business_id(metadata_business_id) or fallback_business_id,
                            metadata_config_agent_id,
                            metadata_configured_agent_name,
                            metadata_end_user_name,
                        )
                candidate = str(payload.get("end_user_phone") or payload.get("end_user_id") or "")
                normalized = _normalize_end_user_id(candidate)
                if normalized:
                    channel = str(payload.get("identity_type") or "voice").strip().lower()
                    return (
                        normalized,
                        ("web" if channel == "web" else "voice"),
                        _normalize_business_id(metadata_business_id) or fallback_business_id,
                        metadata_config_agent_id,
                        metadata_configured_agent_name,
                        metadata_end_user_name,
                    )
            except json.JSONDecodeError:
                pass

        identity = str(getattr(participant, "identity", "") or "")
        email_from_identity = _decode_identity_email(identity)
        if email_from_identity:
            return (
                email_from_identity,
                "web",
                fallback_business_id,
                metadata_config_agent_id,
                metadata_configured_agent_name,
                metadata_end_user_name,
            )
        if "voice_assistant_user_" in identity:
            phone_from_identity = identity.split("voice_assistant_user_", 1)[1]
            normalized = _normalize_end_user_id(phone_from_identity)
            if normalized:
                return (
                    normalized,
                    "voice",
                    fallback_business_id,
                    metadata_config_agent_id,
                    metadata_configured_agent_name,
                    metadata_end_user_name,
                )

    return "", "voice", fallback_business_id, "", "", ""


async def _init_session_userdata(ctx: JobContext, language: str) -> dict[str, Any]:
    room_name = _room_name_from_ctx(ctx)
    stable_session_id = _stable_id(room_name, prefix="sid", max_len=120)
    end_user_id, identity_type, business_id, config_agent_id, configured_agent_name, end_user_name = _participant_identity_from_ctx(ctx)
    if REQUIRE_VERIFIED_PHONE and not end_user_id:
        try:
            # In web flows, participant metadata/identity can arrive slightly after job start.
            await asyncio.wait_for(ctx.wait_for_participant(), timeout=12)
            end_user_id, identity_type, business_id, config_agent_id, configured_agent_name, end_user_name = _participant_identity_from_ctx(ctx)
            logger.info(
                "Retried participant identity after join: end_user_id=%s type=%s business_id=%s config_agent_id=%s configured_name=%s end_user_name=%s",
                end_user_id,
                identity_type,
                business_id,
                config_agent_id,
                configured_agent_name,
                end_user_name,
            )
        except RuntimeError as exc:
            # Some jobs can reach here before room connection is established.
            logger.warning("Could not wait for participant yet: %s", exc)
        except asyncio.TimeoutError:
            logger.warning("Timed out waiting for participant before identity extraction.")
    if REQUIRE_VERIFIED_PHONE and not end_user_id:
        raise RuntimeError("Verified end-user identifier is required to start a session.")
    effective_config_agent_id = str(config_agent_id or AGENT_NAME)
    conversation_id = f"{effective_config_agent_id}:{end_user_id}" if end_user_id else room_name

    logger.info(
        "Session init: runtime_agent=%s config_agent=%s configured_name=%s business_id=%s room=%s end_user_id=%s type=%s conversation_id=%s",
        AGENT_NAME,
        effective_config_agent_id,
        configured_agent_name,
        business_id,
        room_name,
        end_user_id,
        identity_type,
        conversation_id,
    )

    return {
        "client_id": os.getenv("AGENT_CLIENT_ID", "sales-girl-internal"),
        "agent_id": AGENT_NAME,
        "agent_config_id": effective_config_agent_id,
        "configured_agent_name": configured_agent_name,
        "end_user_name": end_user_name,
        "business_id": business_id,
        "conversation_id": conversation_id,
        "session_id": stable_session_id,
        "room_name": room_name,
        "language": language,
        "end_user_id": end_user_id,
        "identity_type": identity_type,
        "turn_index": 0,
        "timeline_event_index": 0,
        "last_user_transcript": "",
        "last_assistant_message": "",
    }


def _wire_session_timeline(session: AgentSession, userdata: dict[str, Any]) -> None:
    def _next_event_idx() -> int:
        userdata["timeline_event_index"] = int(userdata.get("timeline_event_index", 0)) + 1
        return int(userdata["timeline_event_index"])

    @session.on("user_input_transcribed")
    def _on_user_input_transcribed(ev: Any) -> None:
        transcript = str(getattr(ev, "transcript", "") or "").strip()
        if not transcript or not bool(getattr(ev, "is_final", False)):
            return

        userdata["turn_index"] = int(userdata.get("turn_index", 0)) + 1
        userdata["last_user_transcript"] = transcript
        event_idx = _next_event_idx()
        trace_conversation_event(
            "user_input_transcribed",
            payload={
                "event_index": event_idx,
                "turn_index": int(userdata["turn_index"]),
                "transcript": transcript,
                "is_final": True,
                "language": getattr(ev, "language", None),
                "speaker_id": getattr(ev, "speaker_id", None),
            },
            metadata={
                "agent_id": userdata.get("agent_id"),
                "client_id": userdata.get("client_id"),
                "conversation_id": userdata.get("conversation_id"),
                "language": userdata.get("language"),
            },
            user_id=str(userdata.get("end_user_id") or ""),
            session_id=str(userdata.get("session_id") or ""),
        )

    @session.on("conversation_item_added")
    def _on_conversation_item_added(ev: Any) -> None:
        item = getattr(ev, "item", None)
        role = str(getattr(item, "role", "") or "")
        content = _text_from_content(getattr(item, "content", None))
        if not content:
            return

        if role.lower() == "assistant":
            userdata["last_assistant_message"] = content
        elif role.lower() == "user":
            if content != userdata.get("last_user_transcript"):
                userdata["turn_index"] = int(userdata.get("turn_index", 0)) + 1
            userdata["last_user_transcript"] = content

        event_idx = _next_event_idx()
        trace_conversation_event(
            "conversation_item_added",
            payload={
                "event_index": event_idx,
                "turn_index": int(userdata.get("turn_index", 0)),
                "role": role,
                "content": content,
            },
            metadata={
                "agent_id": userdata.get("agent_id"),
                "client_id": userdata.get("client_id"),
                "conversation_id": userdata.get("conversation_id"),
                "language": userdata.get("language"),
            },
            user_id=str(userdata.get("end_user_id") or ""),
            session_id=str(userdata.get("session_id") or ""),
        )

        role_l = role.lower()
        if role_l in {"user", "assistant"}:
            business_id = str(userdata.get("business_id") or "")
            if conversation_service_enabled(business_id):
                async def _persist_remote() -> None:
                    idempotency = _stable_id(
                        f"{userdata.get('session_id')}-{event_idx}-{role_l}",
                        prefix="msg",
                        max_len=96,
                    )
                    persisted = await append_message_remote(
                        conversation_id=str(userdata.get("conversation_id") or ""),
                        role=role_l,
                        content=content,
                        session_id=str(userdata.get("session_id") or ""),
                        idempotency_key=idempotency,
                        metadata={"agent_id": userdata.get("agent_id"), "language": userdata.get("language")},
                        business_id=business_id,
                    )
                    if str(persisted.get("status") or "") == "failed":
                        logger.error(
                            "Conversation message persist failed: conversation_id=%s role=%s detail=%s http_status=%s",
                            userdata.get("conversation_id"),
                            role_l,
                            persisted.get("detail"),
                            persisted.get("http_status"),
                        )

                _track_background_task(userdata, _persist_remote())
            else:
                append_message(
                    conversation_id=str(userdata.get("conversation_id") or ""),
                    agent_id=str(userdata.get("agent_id") or AGENT_NAME),
                    phone=str(userdata.get("end_user_id") or ""),
                    role=role_l,
                    content=content,
                    session_id=str(userdata.get("session_id") or ""),
                )

    @session.on("function_tools_executed")
    def _on_function_tools_executed(ev: Any) -> None:
        calls: list[dict[str, Any]] = []
        if hasattr(ev, "zipped"):
            for function_call, function_call_output in ev.zipped():
                calls.append(
                    {
                        "tool_name": str(getattr(function_call, "name", "")),
                        "tool_arguments": getattr(function_call, "arguments", None),
                        "tool_result": getattr(function_call_output, "output", None),
                    }
                )
        if not calls:
            return

        event_idx = _next_event_idx()
        trace_conversation_event(
            "function_tools_executed",
            payload={
                "event_index": event_idx,
                "turn_index": int(userdata.get("turn_index", 0)),
                "last_user_transcript": userdata.get("last_user_transcript"),
                "tool_calls": calls,
            },
            metadata={
                "agent_id": userdata.get("agent_id"),
                "client_id": userdata.get("client_id"),
                "conversation_id": userdata.get("conversation_id"),
                "language": userdata.get("language"),
            },
            user_id=str(userdata.get("end_user_id") or ""),
            session_id=str(userdata.get("session_id") or ""),
        )
        for call in calls:
            tool_name = str(call.get("tool_name") or "").strip() or "unknown_tool"
            _persist_session_event_async(
                userdata,
                event_type="tool_call",
                role="tool",
                title=tool_name,
                body=_summarize_tool_output(call.get("tool_result")),
                payload={
                    "tool_name": tool_name,
                    "tool_arguments": call.get("tool_arguments"),
                    "tool_result": call.get("tool_result"),
                    "last_user_transcript": userdata.get("last_user_transcript"),
                    "event_index": event_idx,
                    "turn_index": int(userdata.get("turn_index", 0)),
                },
            )


def _instructions_with_resume_context(base_prompt: str, userdata: dict[str, Any]) -> str:
    phone = str(userdata.get("end_user_id") or "")
    agent_id = str(userdata.get("agent_id") or AGENT_NAME)
    if not phone:
        return base_prompt

    ctx = load_resume_context(agent_id=agent_id, phone=phone)
    if not ctx.has_history:
        return base_prompt

    logger.info(
        "Loaded resume context: agent=%s phone=%s total_messages=%s",
        agent_id,
        phone,
        ctx.total_messages,
    )

    return (
        f"{base_prompt}\n\n"
        "Persistent conversation memory for this returning customer:\n"
        f"- Customer phone: {phone}\n"
        f"- Historical message count: {ctx.total_messages}\n"
        "- Continue naturally from prior context when relevant.\n"
        "- If the customer asks whether you remember previous talks, answer yes and summarize briefly based on the memory below.\n\n"
        "Most recent saved conversation snippets:\n"
        f"{ctx.context_text}\n"
    )


async def _instructions_with_context(base_prompt: str, userdata: dict[str, Any]) -> str:
    end_user_id = str(userdata.get("end_user_id") or "")
    if not end_user_id:
        return base_prompt
    configured_agent_name = str(userdata.get("configured_agent_name") or "").strip()
    if configured_agent_name:
        logger.info("Applying configured agent name to prompt: %s", configured_agent_name)
        base_prompt = (
            f"{base_prompt}\n\n"
            f"Agent profile detail: your name is '{configured_agent_name}'.\n"
            f"- If a customer asks your name, respond that your name is '{configured_agent_name}'.\n"
            "- Do not say you don't have a name."
        )
    # Prevent old assistant personas in historical context from overriding current role.
    base_prompt = (
        f"{base_prompt}\n\n"
        "Domain lock:\n"
        "- You are an EKEDC electricity customer support assistant.\n"
        "- Never present yourself as a beauty, hair, appointment-booking, or consular assistant.\n"
        "- Do not use beauty, appointment, passport, or certificate framing in your replies.\n\n"
        "Role lock:\n"
        "- You MUST follow the current role and responsibilities in this prompt.\n"
        "- Historical snippets may contain outdated assistant behavior from older versions.\n"
        "- Never switch back to an old business persona if it conflicts with this prompt.\n\n"
        "Issue handling lock:\n"
        "- If the caller needs a complaint, outage report, meter request, or escalation, use the available EKEDC tools.\n"
        "- Do not claim an action was completed unless the tool confirms it.\n"
        "- If the issue is outage, faulty transformer, meter installation, disconnection, DT mapping, or billing reconciliation, escalate it."
    )
    channel = "web" if str(userdata.get("identity_type") or "").lower() == "web" else "voice"
    business_id = _normalize_business_id(str(userdata.get("business_id") or ""))
    config_agent_id = str(userdata.get("agent_config_id") or userdata.get("agent_id") or AGENT_NAME)

    if conversation_service_enabled(business_id):
        resolved = await resolve_conversation_remote(
            agent_id=config_agent_id,
            external_id=end_user_id,
            external_name=str(userdata.get("end_user_name") or ""),
            channel=channel,
            business_id=business_id,
        )
        if str(resolved.get("status") or "") == "failed":
            logger.error(
                "Conversation resolve failed: business_id=%s agent_id=%s end_user_id=%s detail=%s http_status=%s",
                business_id,
                config_agent_id,
                end_user_id,
                resolved.get("detail"),
                resolved.get("http_status"),
            )
        conv_id = str(resolved.get("conversation_id") or "")
        if conv_id:
            userdata["conversation_id"] = conv_id
            context_payload = await fetch_context_remote(conv_id, limit=30, business_id=business_id)
            msgs = context_payload.get("messages") if isinstance(context_payload, dict) else None
            if isinstance(msgs, list) and msgs:
                lines: list[str] = []
                for m in msgs[-30:]:
                    role = str(m.get("role") or "").lower()
                    content = str(m.get("content") or "").strip()
                    if not content:
                        continue
                    # Keep user side history to avoid replaying outdated assistant persona.
                    if role != "user":
                        continue
                    who = "Customer"
                    lines.append(f"{who}: {content}")
                memory_text = "\\n".join(lines).strip()
                if memory_text:
                    return (
                        f"{base_prompt}\n\n"
                        "Persistent conversation memory for this returning customer:\n"
                        f"- Customer id: {end_user_id}\n"
                        f"- Historical message count: {len(msgs)}\n"
                        "- Continue naturally from prior context when relevant.\n"
                        "- If the customer asks whether you remember previous talks, answer yes and summarize briefly based on the memory below.\n\n"
                        "Most recent saved conversation snippets:\n"
                        f"{memory_text}\n"
                    )
        if CONVERSATION_SERVICE_REQUIRED:
            logger.error(
                "Conversation strict mode fallback: proceeding without remote context. business_id=%s agent_id=%s end_user_id=%s",
                business_id,
                config_agent_id,
                end_user_id,
            )
        return base_prompt

    # Fallback: local sqlite memory
    if CONVERSATION_SERVICE_REQUIRED:
        raise RuntimeError("Conversation service is required but not configured.")
    return _instructions_with_resume_context(base_prompt, userdata)


def _validate_runtime_requirements() -> None:
    if CONVERSATION_SERVICE_REQUIRED and (
        not os.getenv("CONVERSATION_API_BASE_URL", "").strip()
        or not os.getenv("CONVERSATION_SERVICE_TOKEN", "").strip()
    ):
        raise RuntimeError(
            "CONVERSATION_SERVICE_REQUIRED=true but conversation service is not configured. "
            "Set CONVERSATION_API_BASE_URL and CONVERSATION_SERVICE_TOKEN."
        )


async def _fetch_active_agent_runtime_config(userdata: dict[str, Any]) -> dict[str, Any]:
    business_id = _normalize_business_id(str(userdata.get("business_id") or ""))
    config_agent_id = str(userdata.get("agent_config_id") or "").strip()
    if not business_id or not config_agent_id:
        return {}
    payload = await get_agent_active_config(agent_id=config_agent_id, business_id=business_id)
    if str(payload.get("status") or "") == "failed":
        logger.error(
            "Agent config fetch failed: business_id=%s agent_id=%s detail=%s http_status=%s",
            business_id,
            config_agent_id,
            payload.get("detail"),
            payload.get("http_status"),
        )
        return {}
    logger.info(
        "Agent config loaded: agent_id=%s business_id=%s name=%s instructions_len=%s",
        config_agent_id,
        business_id,
        str(payload.get("name") or ""),
        len(str(payload.get("instructions") or "")),
    )
    return payload if isinstance(payload, dict) else {}


def _effective_base_prompt(
    *,
    static_prompt: str,
    active_agent_config: dict[str, Any] | None,
) -> str:
    cfg = active_agent_config or {}
    configured_instructions = str(cfg.get("instructions") or "").strip()
    if not configured_instructions:
        return static_prompt

    normalized = " ".join(configured_instructions.lower().split())
    default_like = {
        "you are a helpful ai voice assistant for this business. be concise, friendly, and accurate.",
        "you are a helpful ai voice assistant for this business.",
    }
    if normalized in default_like:
        logger.info("Ignoring default-like dashboard instructions; keeping static domain prompt.")
        return static_prompt

    incompatible_tokens = ("salon", "appointment", "hair", "beauty", "booking", "barber", "spa", "receptionist")
    if any(token in normalized for token in incompatible_tokens):
        logger.warning("Ignoring incompatible dashboard instructions containing stale non-EKEDC persona terms.")
        return static_prompt

    return (
        f"{static_prompt}\n\n"
        "Business-specific overlay instructions (follow these in addition to your domain role):\n"
        f"{configured_instructions}"
    )


def _ops_tool_metadata_from_userdata(userdata: dict[str, Any]) -> dict[str, Any]:
    return {
        "client_id": os.getenv("AGENT_CLIENT_ID", "sales-girl-internal"),
        "agent_id": os.getenv("AGENT_NAME", AGENT_NAME),
        "business_id": str(userdata.get("business_id") or ""),
        "conversation_id": str(userdata.get("conversation_id") or ""),
        "session_id": str(userdata.get("session_id") or ""),
        "end_user_id": str(userdata.get("end_user_id") or ""),
    }


async def _build_preloaded_ops_context(userdata: dict[str, Any]) -> str:
    md = _ops_tool_metadata_from_userdata(userdata)
    caller_id = str(md.get("end_user_id") or "").strip()
    if not caller_id:
        return ""

    customer: dict[str, Any] = {}
    tariff: dict[str, Any] = {}
    payments: dict[str, Any] = {}
    vending: dict[str, Any] = {}

    try:
        resolved_customer = await ops_lookup_customer_account(metadata=md)
        if isinstance(resolved_customer, dict):
            customer = resolved_customer
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ops preload customer lookup failed for %s: %s", caller_id, exc)

    try:
        resolved_tariff = await ops_get_tariff_profile(metadata=md)
        if isinstance(resolved_tariff, dict):
            tariff = resolved_tariff
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ops preload tariff lookup failed for %s: %s", caller_id, exc)

    try:
        resolved_payments = await ops_get_payment_summary(metadata=md)
        if isinstance(resolved_payments, dict):
            payments = resolved_payments
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ops preload payment lookup failed for %s: %s", caller_id, exc)

    try:
        resolved_vending = await ops_get_vending_history(metadata=md)
        if isinstance(resolved_vending, dict):
            vending = resolved_vending
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ops preload vending lookup failed for %s: %s", caller_id, exc)

    customer_name = str(customer.get("name") or "").strip() if isinstance(customer, dict) else ""
    customer_email = str(customer.get("email") or "").strip() if isinstance(customer, dict) else ""
    customer_phone = str(customer.get("phone") or "").strip() if isinstance(customer, dict) else ""
    account_number = str(customer.get("account_number") or tariff.get("account_number") or "").strip()
    tariff_band = str(tariff.get("tariff_band") or customer.get("tariff_band") or "").strip()
    meter_type = str(tariff.get("meter_type") or customer.get("meter_type") or "").strip()
    business_unit = str(tariff.get("business_unit") or customer.get("business_unit") or "").strip()
    service_address = str(tariff.get("service_address") or customer.get("service_address") or "").strip()
    feeder_name = str(tariff.get("feeder_name") or customer.get("feeder_name") or "").strip()
    payment_items = payments.get("payments") if isinstance(payments, dict) else []
    vend_items = vending.get("vend_history") if isinstance(vending, dict) else []
    payment_lines = []
    for item in payment_items[:3] if isinstance(payment_items, list) else []:
        if isinstance(item, dict):
            payment_lines.append(f"- {item.get('date')}: amount={item.get('amount')} status={item.get('status')}")
    vend_lines = []
    for item in vend_items[:3] if isinstance(vend_items, list) else []:
        if isinstance(item, dict):
            vend_lines.append(
                f"- {item.get('date')}: amount={item.get('amount')} token_status={item.get('token_status')} load_status={item.get('load_status')}"
            )

    logger.info(
        "Preloaded caller context: email=%s tariff_band=%s payments=%s vending=%s",
        caller_id,
        tariff_band,
        len(payment_items) if isinstance(payment_items, list) else 0,
        len(vend_items) if isinstance(vend_items, list) else 0,
    )
    return (
        "Verified caller profile and case context (fetched before this conversation starts):\n"
        "- This caller has already been identified from the authenticated session context.\n"
        "- Use the caller profile below confidently when the caller asks about their account or recent activity.\n"
        "- If 'Caller name' is present below, never say you do not know the caller's name.\n"
        "- Do not read this whole block aloud at the start of the call. Use it only when relevant.\n"
        f"- Caller email: {customer_email or caller_id}\n"
        f"- Caller name: {customer_name or '-'}\n"
        f"- Caller phone: {customer_phone or '-'}\n"
        f"- Account number: {account_number or '-'}\n"
        f"- Tariff band: {tariff_band or '-'}\n"
        f"- Meter type: {meter_type or '-'}\n"
        f"- Business unit: {business_unit or '-'}\n"
        f"- Service address: {service_address or '-'}\n"
        f"- Feeder name: {feeder_name or '-'}\n"
        f"- Recent payments found: {len(payment_items) if isinstance(payment_items, list) else 0}\n"
        f"{chr(10).join(payment_lines) if payment_lines else '- none'}\n"
        f"- Recent vending records found: {len(vend_items) if isinstance(vend_items, list) else 0}\n"
        f"{chr(10).join(vend_lines) if vend_lines else '- none'}\n"
        "- Use this preloaded context first. Do not ask for the customer's email or account number as your first move.\n"
    )


def _instructions_with_preloaded_ops_context(base_prompt: str, preloaded_context: str) -> str:
    if not preloaded_context:
        return base_prompt
    return f"{base_prompt}\n\n{preloaded_context}\n"


def _kickoff_prompt_for_language(language: str) -> str:
    lang = str(language or "").strip().lower()
    if lang == "fr":
        return (
        "Commencez la conversation maintenant. Saluez d'abord l'appelant en français, présentez-vous "
        "brièvement et proposez votre aide concernant son compte EKEDC en utilisant le contexte déjà disponible. "
        "Ne demandez pas d'abord l'email ou le numéro de compte. "
        "N'énumérez pas immédiatement tout le profil de l'appelant ; saluez d'abord puis attendez sa demande."
    )
    return (
        "Start the conversation now. Greet the caller first, introduce yourself briefly, and proactively "
        "offer help with their EKEDC account requests using the context you already have. Do not ask for email "
        "or account number as your first move. Do not dump the caller profile immediately; greet first and "
        "wait for the caller's request."
    )


def _trigger_first_turn(session: AgentSession, *, language: str) -> None:
    try:
        session.generate_reply(
            instructions=_kickoff_prompt_for_language(language),
            input_modality="text",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to trigger first assistant turn (%s): %s", language, exc)


def _should_use_odion_tts_for_language(config: dict[str, Any], language: str) -> bool:
    provider = str(config.get("tts_provider") or "").strip().lower()
    voice_id = str(config.get("tts_voice_id") or "").strip()
    scope = str(config.get("tts_language_scope") or "").strip().lower()
    if provider != "odion_tts" or not voice_id:
        return False
    if not scope or scope == "all":
        return True
    language = str(language or "").strip().lower()
    return scope == language


async def _wait_for_job_shutdown(ctx: JobContext) -> str:
    loop = asyncio.get_running_loop()
    done: asyncio.Future[str] = loop.create_future()

    async def _on_shutdown(reason: str = "") -> None:
        if not done.done():
            done.set_result(str(reason or ""))

    ctx.add_shutdown_callback(_on_shutdown)
    return await done


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext):
    """
    Single entrypoint: start English or French agent based on AGENT_NAME.
    """
    if _is_en_agent_name(AGENT_NAME):
        userdata = await _init_session_userdata(ctx, language="en")
        active_agent_config = await _fetch_active_agent_runtime_config(userdata)
        config_name = str(active_agent_config.get("name") or "").strip()
        if config_name:
            userdata["configured_agent_name"] = config_name
        base_prompt = _effective_base_prompt(static_prompt=SYSTEM_PROMPT_EN, active_agent_config=active_agent_config)
        prompt_preview = " ".join(str(base_prompt).split())[:220]
        logger.info(
            "Prompt source: %s preview=%s",
            "active-config" if str(active_agent_config.get("instructions") or "").strip() else "static-default",
            prompt_preview,
        )
        if "salon" in str(base_prompt).lower():
            logger.warning("Active prompt contains 'salon' text for this session. Forcing English static consular prompt.")
            base_prompt = SYSTEM_PROMPT_EN
        preloaded_context = await _build_preloaded_ops_context(userdata)
        instructions = _instructions_with_preloaded_ops_context(base_prompt, preloaded_context)
        instructions = await _instructions_with_context(instructions, userdata)
        started_at = conv_api_utcnow()
        business_id = str(userdata.get("business_id") or "")
        call_channel = "web" if str(userdata.get("identity_type") or "").lower() == "web" else "voice"

        async def _cleanup_en(reason: str = "") -> None:
            await asyncio.shield(
                _finalize_session_cleanup(
                    userdata=userdata,
                    business_id=business_id,
                    session_tracker_id=str(userdata.get("session_tracker_id") or ""),
                    started_at=started_at,
                    call_channel=call_channel,
                    language="en",
                    shutdown_reason=reason or None,
                )
            )

        ctx.add_shutdown_callback(_cleanup_en)

        tts_engine: Any = deepgram.TTS(model="aura-asteria-en")
        use_experiment_clone = (
            AGENT_NAME == "odion-tts-staging-agent"
            and bool(ODION_TTS_EXPERIMENT_OWNER_ID)
            and bool(ODION_TTS_EXPERIMENT_VOICE_ID)
        )
        tts_voice_id = (
            ODION_TTS_EXPERIMENT_VOICE_ID
            if use_experiment_clone
            else str(active_agent_config.get("tts_voice_id") or "").strip()
        )
        tts_owner_id = (
            ODION_TTS_EXPERIMENT_OWNER_ID
            if use_experiment_clone
            else str(active_agent_config.get("tts_owner_id") or "").strip() or business_id
        )
        tts_language_hint = (
            ODION_TTS_EXPERIMENT_LANGUAGE_HINT
            if use_experiment_clone
            else str(active_agent_config.get("tts_language_hint") or "Auto").strip() or "Auto"
        )
        use_configured_clone = use_experiment_clone or _should_use_odion_tts_for_language(active_agent_config, "en")
        use_odion_default = not use_configured_clone
        if ENABLE_ODION_TTS_EN:
            try:
                if use_configured_clone:
                    tts_engine = OdionTTS(
                        owner_id=tts_owner_id,
                        voice_id=tts_voice_id,
                        language=tts_language_hint,
                    )
                    logger.info(
                        "Using Odion cloned TTS for English session: agent_config_id=%s voice_id=%s owner_id=%s",
                        userdata.get("agent_config_id"),
                        tts_voice_id,
                        tts_owner_id,
                    )
                elif use_odion_default:
                    # Default English voice path from Odion TTS when business has not cloned.
                    tts_engine = OdionTTS(
                        owner_id=tts_owner_id or business_id,
                        voice_id=None,
                        language=tts_language_hint,
                    )
                    logger.info(
                        "Using Odion default TTS for English session: agent_config_id=%s owner_id=%s",
                        userdata.get("agent_config_id"),
                        tts_owner_id or business_id,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to initialize Odion TTS, falling back to Deepgram: %s", exc)
        else:
            logger.info("ENABLE_ODION_TTS_EN=false; using Deepgram TTS for English session.")

        session = AgentSession(
            stt=deepgram.STT(language="en"),
            tts=tts_engine,
            llm=google.LLM(model="gemini-2.0-flash"),
            userdata=userdata,
        )
        _wire_session_timeline(session, session.userdata)
        try:
            if conversation_service_enabled(business_id) and userdata.get("conversation_id"):
                started = await start_session_remote(
                    conversation_id=str(userdata.get("conversation_id")),
                    client_session_id=str(userdata.get("session_id") or ""),
                    channel=call_channel,
                    business_id=business_id,
                )
                session_tracker_id = str(started.get("id") or "")
                userdata["session_tracker_id"] = session_tracker_id
                _persist_session_event_async(
                    userdata,
                    event_type="session_started",
                    role="system",
                    title="Session started",
                    body=f"English {call_channel} session started for {userdata.get('end_user_id') or 'unknown caller'}.",
                    payload={
                        "language": "en",
                        "channel": call_channel,
                        "client_session_id": str(userdata.get("session_id") or ""),
                        "runtime_agent": AGENT_NAME,
                        "configured_agent_name": userdata.get("configured_name"),
                    },
                )
                if is_recording_enabled():
                    logger.info(
                        "Attempting room recording start: language=en session_id=%s room=%s",
                        session_tracker_id or str(userdata.get("session_id") or ""),
                        str(ctx.room.name or ""),
                    )
                    recording_started = await start_room_recording(
                        room_name=str(ctx.room.name or ""),
                        business_id=business_id,
                        session_id=session_tracker_id or str(userdata.get("session_id") or ""),
                        started_at=started_at,
                    )
                    userdata["recording_egress_id"] = recording_started.egress_id
                    userdata["recording_expected_url"] = recording_started.expected_url
                    userdata["recording_filepath"] = recording_started.filepath
                    initial_recording_status = "recording" if recording_started.egress_id else "failed"
                    await update_session_recording_remote(
                        session_id=session_tracker_id,
                        recording_status=initial_recording_status,
                        recording_url=recording_started.expected_url if initial_recording_status == "recording" else None,
                        business_id=business_id,
                    )
                    _persist_session_event_async(
                        userdata,
                        event_type="recording_started" if recording_started.egress_id else "recording_failed",
                        role="system",
                        title="Recording started" if recording_started.egress_id else "Recording failed",
                        body=(
                            f"Audio recording started for room {ctx.room.name}."
                            if recording_started.egress_id
                            else f"Audio recording could not start: {recording_started.detail or 'unknown error'}."
                        ),
                        payload={
                            "recording_status": initial_recording_status,
                            "egress_id": recording_started.egress_id,
                            "recording_url": recording_started.expected_url,
                            "filepath": recording_started.filepath,
                            "detail": recording_started.detail,
                        },
                    )
                else:
                    logger.info("Recording not enabled for this session: language=en business_id=%s", business_id)
            await session.start(
                agent=SalonAgentEN(instructions=instructions),
                room=ctx.room,
                room_options=room_io.RoomOptions(delete_room_on_close=True),
            )
            _trigger_first_turn(session, language="en")
            shutdown_reason = await _wait_for_job_shutdown(ctx)
            logger.info("Session shutdown received (en): reason=%s", shutdown_reason or "unknown")
        finally:
            await asyncio.shield(
                _finalize_session_cleanup(
                    userdata=userdata,
                    business_id=business_id,
                    session_tracker_id=str(userdata.get("session_tracker_id") or ""),
                    started_at=started_at,
                    call_channel=call_channel,
                    language="en",
                    shutdown_reason=shutdown_reason if "shutdown_reason" in locals() else None,
                )
            )
    else:
        userdata = await _init_session_userdata(ctx, language="fr")
        active_agent_config = await _fetch_active_agent_runtime_config(userdata)
        config_name = str(active_agent_config.get("name") or "").strip()
        if config_name:
            userdata["configured_agent_name"] = config_name
        base_prompt = _effective_base_prompt(static_prompt=SYSTEM_PROMPT_FR, active_agent_config=active_agent_config)
        prompt_preview = " ".join(str(base_prompt).split())[:220]
        logger.info(
            "Prompt source: %s preview=%s",
            "active-config" if str(active_agent_config.get("instructions") or "").strip() else "static-default",
            prompt_preview,
        )
        if "salon" in str(base_prompt).lower():
            logger.warning("Active prompt contains 'salon' text for this session. Forcing French static consular prompt.")
            base_prompt = SYSTEM_PROMPT_FR
        preloaded_context = await _build_preloaded_ops_context(userdata)
        instructions = _instructions_with_preloaded_ops_context(base_prompt, preloaded_context)
        instructions = await _instructions_with_context(instructions, userdata)
        started_at = conv_api_utcnow()
        business_id = str(userdata.get("business_id") or "")
        call_channel = "web" if str(userdata.get("identity_type") or "").lower() == "web" else "voice"

        async def _cleanup_fr(reason: str = "") -> None:
            await asyncio.shield(
                _finalize_session_cleanup(
                    userdata=userdata,
                    business_id=business_id,
                    session_tracker_id=str(userdata.get("session_tracker_id") or ""),
                    started_at=started_at,
                    call_channel=call_channel,
                    language="fr",
                    shutdown_reason=reason or None,
                )
            )

        ctx.add_shutdown_callback(_cleanup_fr)
        session = AgentSession(
            stt=deepgram.STT(language="fr"),
            tts=deepgram.TTS(model="aura-2-agathe-fr"),
            llm=google.LLM(model="gemini-2.0-flash"),
            userdata=userdata,
        )
        _wire_session_timeline(session, session.userdata)
        try:
            if conversation_service_enabled(business_id) and userdata.get("conversation_id"):
                started = await start_session_remote(
                    conversation_id=str(userdata.get("conversation_id")),
                    client_session_id=str(userdata.get("session_id") or ""),
                    channel=call_channel,
                    business_id=business_id,
                )
                session_tracker_id = str(started.get("id") or "")
                userdata["session_tracker_id"] = session_tracker_id
                _persist_session_event_async(
                    userdata,
                    event_type="session_started",
                    role="system",
                    title="Session started",
                    body=f"French {call_channel} session started for {userdata.get('end_user_id') or 'unknown caller'}.",
                    payload={
                        "language": "fr",
                        "channel": call_channel,
                        "client_session_id": str(userdata.get("session_id") or ""),
                        "runtime_agent": AGENT_NAME,
                        "configured_agent_name": userdata.get("configured_name"),
                    },
                )
                if is_recording_enabled():
                    logger.info(
                        "Attempting room recording start: language=fr session_id=%s room=%s",
                        session_tracker_id or str(userdata.get("session_id") or ""),
                        str(ctx.room.name or ""),
                    )
                    recording_started = await start_room_recording(
                        room_name=str(ctx.room.name or ""),
                        business_id=business_id,
                        session_id=session_tracker_id or str(userdata.get("session_id") or ""),
                        started_at=started_at,
                    )
                    userdata["recording_egress_id"] = recording_started.egress_id
                    userdata["recording_expected_url"] = recording_started.expected_url
                    userdata["recording_filepath"] = recording_started.filepath
                    initial_recording_status = "recording" if recording_started.egress_id else "failed"
                    await update_session_recording_remote(
                        session_id=session_tracker_id,
                        recording_status=initial_recording_status,
                        recording_url=recording_started.expected_url if initial_recording_status == "recording" else None,
                        business_id=business_id,
                    )
                    _persist_session_event_async(
                        userdata,
                        event_type="recording_started" if recording_started.egress_id else "recording_failed",
                        role="system",
                        title="Recording started" if recording_started.egress_id else "Recording failed",
                        body=(
                            f"Audio recording started for room {ctx.room.name}."
                            if recording_started.egress_id
                            else f"Audio recording could not start: {recording_started.detail or 'unknown error'}."
                        ),
                        payload={
                            "recording_status": initial_recording_status,
                            "egress_id": recording_started.egress_id,
                            "recording_url": recording_started.expected_url,
                            "filepath": recording_started.filepath,
                            "detail": recording_started.detail,
                        },
                    )
                else:
                    logger.info("Recording not enabled for this session: language=fr business_id=%s", business_id)
            await session.start(
                agent=SalonAgentFR(instructions=instructions),
                room=ctx.room,
                room_options=room_io.RoomOptions(delete_room_on_close=True),
            )
            _trigger_first_turn(session, language="fr")
            shutdown_reason = await _wait_for_job_shutdown(ctx)
            logger.info("Session shutdown received (fr): reason=%s", shutdown_reason or "unknown")
        finally:
            await asyncio.shield(
                _finalize_session_cleanup(
                    userdata=userdata,
                    business_id=business_id,
                    session_tracker_id=str(userdata.get("session_tracker_id") or ""),
                    started_at=started_at,
                    call_channel=call_channel,
                    language="fr",
                    shutdown_reason=shutdown_reason if "shutdown_reason" in locals() else None,
                )
            )


if __name__ == "__main__":
    # LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET are read from the environment/.env
    try:
        _validate_runtime_requirements()
        cli.run_app(server)
    finally:
        flush_traces()
