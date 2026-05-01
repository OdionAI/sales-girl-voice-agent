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

# Load env immediately so API clients can read the correct base URLs
load_dotenv()

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
    get_account_overview as ops_get_account_overview,
    get_payment_summary as ops_get_payment_summary,
    get_recent_transactions as ops_get_recent_transactions,
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
from agent.salon_agent import SalonAgent
from prompts.en import SYSTEM_PROMPT_EN
from prompts.fr import SYSTEM_PROMPT_FR


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)


# AgentServer allows only one rtc_session per process. To support both English and
# French, run two worker processes with EN/FR-prefixed names.
AGENT_NAME = os.environ.get("AGENT_NAME", "sales-girl-agent-en")
AGENT_PORT = int(
    os.environ.get(
        "AGENT_PORT",
        "8082"
        if str(AGENT_NAME or "").strip().lower().startswith("sales-girl-agent-fr")
        else "8081",
    )
)
DEFAULT_BUSINESS_USE_CASE = (
    str(os.environ.get("DEFAULT_BUSINESS_USE_CASE", "generic") or "generic")
    .strip()
    .lower()
)
FIDELITY_BUSINESS_IDS = {
    item.strip()
    for item in str(os.environ.get("FIDELITY_BUSINESS_IDS") or "").split(",")
    if item.strip()
}
EKEDC_BUSINESS_IDS = {
    item.strip()
    for item in str(os.environ.get("EKEDC_BUSINESS_IDS") or "").split(",")
    if item.strip()
}
FIDELITY_STATIC_PROMPT_EN = (
    "You are Fidelity Bank's AI customer care assistant. Help callers with account inquiries, "
    "recent transaction questions, transaction status checks, card block and unblock requests, "
    "failed transaction reversals when the backend confirms eligibility, and ticket creation "
    "for issues that require human review."
)

RESTAURANT_STATIC_PROMPT_EN = (
    "You are a restaurant host and customer support assistant for this business. "
    "Help callers with menu questions, reservations, order-related questions, service policies, "
    "and support requests. Use live availability only when connected, never invent current menu "
    "availability or pricing, and create tickets when human follow-up is needed."
)

FASHION_STATIC_PROMPT_EN = (
    "You are a fashion sales and customer support assistant for this business. "
    "Help callers with product questions, sizes, styles, availability, delivery or return policies, "
    "and support requests. Use live product availability only when connected, never invent stock or pricing, "
    "and create tickets when human follow-up is needed."
)

GENERIC_STATIC_PROMPT_EN = (
    "You are the business's AI voice assistant. Represent the business clearly, calmly, and professionally. "
    "Use the saved business instructions and knowledge first, use tools only when relevant, never invent live data, "
    "and create a support ticket when human follow-up is needed."
)


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
    port=AGENT_PORT,
)
init_store()


def _short_text(value: Any, limit: int = 320) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


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
            logger.exception(
                "Failed to drain background tasks during session cleanup: %s", exc
            )

        recording_status = None
        recording_url = None
        recording_duration_seconds = None
        recording_detail = None

        if (
            conversation_service_enabled(business_id)
            and session_tracker_id
            and is_recording_enabled()
        ):
            try:
                logger.info(
                    "Finalizing room recording: session_id=%s egress_id=%s expected_url=%s",
                    session_tracker_id,
                    str(userdata.get("recording_egress_id") or ""),
                    str(userdata.get("recording_expected_url") or ""),
                )
                recording_finalized = await finalize_room_recording(
                    egress_id=str(userdata.get("recording_egress_id") or "").strip()
                    or None,
                    expected_url=str(
                        userdata.get("recording_expected_url") or ""
                    ).strip()
                    or None,
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
                logger.exception(
                    "Failed during recording finalization: session_id=%s error=%s",
                    session_tracker_id,
                    exc,
                )
                recording_status = recording_status or "failed"
                recording_detail = recording_detail or str(exc)

            _persist_session_event_async(
                userdata,
                event_type="recording_ready"
                if recording_status == "available"
                else "recording_status",
                role="system",
                title="Recording available"
                if recording_status == "available"
                else "Recording status updated",
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
            logger.exception(
                "Failed to flush final background tasks during session cleanup: %s", exc
            )

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
                logger.exception(
                    "Failed to persist session end: session_id=%s error=%s",
                    session_tracker_id,
                    exc,
                )


REQUIRE_VERIFIED_PHONE = os.getenv("REQUIRE_VERIFIED_PHONE", "true").lower() == "true"
CONVERSATION_SERVICE_REQUIRED = (
    os.getenv("CONVERSATION_SERVICE_REQUIRED", "true").lower() == "true"
)
ENABLE_ODION_TTS_EN = os.getenv("ENABLE_ODION_TTS_EN", "true").lower() == "true"
ENABLE_ODION_TTS_FR = os.getenv("ENABLE_ODION_TTS_FR", "false").lower() == "true"
ODION_TTS_EXPERIMENT_OWNER_ID = str(
    os.getenv("ODION_TTS_EXPERIMENT_OWNER_ID") or "mavinomichael@gmail.com"
).strip()
ODION_TTS_EXPERIMENT_VOICE_ID = str(
    os.getenv("ODION_TTS_EXPERIMENT_VOICE_ID") or "d270a5cec6914373b9deed1d1c3cbade"
).strip()
ODION_TTS_EXPERIMENT_LANGUAGE_HINT = (
    str(os.getenv("ODION_TTS_EXPERIMENT_LANGUAGE_HINT") or "English").strip()
    or "English"
)
try:
    _odion_seed_raw = str(os.getenv("ODION_TTS_EXPERIMENT_SEED") or "0").strip()
    ODION_TTS_EXPERIMENT_SEED = int(_odion_seed_raw) if _odion_seed_raw else None
    if ODION_TTS_EXPERIMENT_SEED is not None and ODION_TTS_EXPERIMENT_SEED < 0:
        ODION_TTS_EXPERIMENT_SEED = None
except ValueError:
    ODION_TTS_EXPERIMENT_SEED = None
ODION_TTS_CLONE_SEED = (
    ODION_TTS_EXPERIMENT_SEED if ODION_TTS_EXPERIMENT_SEED is not None else 0
)
STRICT_ODION_CLONE_CONSISTENCY = (
    os.getenv("STRICT_ODION_CLONE_CONSISTENCY", "true").lower() == "true"
)

def _float_env(name: str, default: float, *, min_value: float) -> float:
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %.3f", name, raw, default)
        return default
    if value < min_value:
        logger.warning(
            "%s=%s is below minimum %.3f; using minimum.",
            name,
            value,
            min_value,
        )
        return min_value
    return value

TURN_MIN_ENDPOINTING_DELAY = _float_env(
    "TURN_MIN_ENDPOINTING_DELAY",
    0.45,
    min_value=0.1,
)
TURN_MAX_ENDPOINTING_DELAY = _float_env(
    "TURN_MAX_ENDPOINTING_DELAY",
    1.2,
    min_value=0.2,
)
if TURN_MAX_ENDPOINTING_DELAY < TURN_MIN_ENDPOINTING_DELAY:
    logger.warning(
        "TURN_MAX_ENDPOINTING_DELAY < TURN_MIN_ENDPOINTING_DELAY; aligning max to min."
    )
    TURN_MAX_ENDPOINTING_DELAY = TURN_MIN_ENDPOINTING_DELAY

TURN_MIN_INTERRUPTION_DURATION = _float_env(
    "TURN_MIN_INTERRUPTION_DURATION",
    0.7,
    min_value=0.1,
)


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
        return (
            base64.urlsafe_b64decode(raw + "=" * ((4 - len(raw) % 4) % 4))
            .decode("utf-8")
            .strip()
        )
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
        job_room_name = str(
            getattr(getattr(getattr(ctx, "job", None), "room", None), "name", "") or ""
        ).strip()
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
        decoded = base64.urlsafe_b64decode(
            encoded + "=" * ((4 - len(encoded) % 4) % 4)
        ).decode("utf-8")
    except Exception:
        return ""
    return _normalize_end_user_id(decoded)


# Extract participant identity and related room context from the LiveKit job.
def _participant_identity_from_ctx(
    ctx: JobContext,
) -> tuple[str, str, str, str, str, str, str]:
    room = getattr(ctx, "room", None)
    room_name = _room_name_from_ctx(ctx)
    fallback_business_id = _normalize_business_id(
        os.getenv("CONVERSATION_BUSINESS_ID", "")
    )

    # First preference for web: encoded identity/context in room name (available at bootstrap).
    (
        email_from_room,
        room_business_id,
        room_config_agent_id,
        room_configured_name,
        room_end_user_name,
    ) = _web_room_context_from_name(room_name)
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
            "",
        )

    # First preference: encoded phone in room name (always available at session bootstrap)
    phone_from_room = _phone_from_room_name(room_name)
    if phone_from_room:
        return phone_from_room, "voice", fallback_business_id, "", "", "", ""

    # Fallback: read remote participant metadata / identity
    participants = getattr(room, "remote_participants", None)
    if not participants:
        return "", "voice", fallback_business_id, "", "", "", ""

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
                metadata_config_agent_id = str(
                    payload.get("config_agent_id") or ""
                ).strip()
                metadata_configured_agent_name = str(
                    payload.get("configured_agent_name") or ""
                ).strip()
                metadata_end_user_name = str(payload.get("end_user_name") or "").strip()
                metadata_tts_endpoint = str(payload.get("tts_endpoint") or "").strip()
                email_candidate = str(payload.get("end_user_email") or "").strip()
                if email_candidate:
                    normalized_email = _normalize_end_user_id(email_candidate)
                    if normalized_email:
                        return (
                            normalized_email,
                            "web",
                            _normalize_business_id(metadata_business_id)
                            or fallback_business_id,
                            metadata_config_agent_id,
                            metadata_configured_agent_name,
                            metadata_end_user_name,
                            metadata_tts_endpoint,
                        )
                candidate = str(
                    payload.get("end_user_phone") or payload.get("end_user_id") or ""
                )
                normalized = _normalize_end_user_id(candidate)
                if normalized:
                    channel = (
                        str(payload.get("identity_type") or "voice").strip().lower()
                    )
                    return (
                        normalized,
                        ("web" if channel == "web" else "voice"),
                        _normalize_business_id(metadata_business_id)
                        or fallback_business_id,
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

    return "", "voice", fallback_business_id, "", "", "", ""


async def _init_session_userdata(ctx: JobContext, language: str) -> dict[str, Any]:
    room_name = _room_name_from_ctx(ctx)
    stable_session_id = _stable_id(room_name, prefix="sid", max_len=120)
    (
        end_user_id,
        identity_type,
        business_id,
        config_agent_id,
        configured_agent_name,
        end_user_name,
        tts_endpoint,
    ) = _participant_identity_from_ctx(ctx)
    if REQUIRE_VERIFIED_PHONE and not end_user_id:
        try:
            # In web flows, participant metadata/identity can arrive slightly after job start.
            await asyncio.wait_for(ctx.wait_for_participant(), timeout=12)
            (
                end_user_id,
                identity_type,
                business_id,
                config_agent_id,
                configured_agent_name,
                end_user_name,
                tts_endpoint,
            ) = _participant_identity_from_ctx(ctx)
            logger.info(
                "Retried participant identity after join: end_user_id=%s type=%s business_id=%s config_agent_id=%s configured_name=%s end_user_name=%s tts_endpoint=%s",
                end_user_id,
                identity_type,
                business_id,
                config_agent_id,
                configured_agent_name,
                end_user_name,
                tts_endpoint,
            )
        except RuntimeError as exc:
            # Some jobs can reach here before room connection is established.
            logger.warning("Could not wait for participant yet: %s", exc)
        except asyncio.TimeoutError:
            logger.warning(
                "Timed out waiting for participant before identity extraction."
            )
    if REQUIRE_VERIFIED_PHONE and not end_user_id:
        raise RuntimeError(
            "Verified end-user identifier is required to start a session."
        )
    effective_config_agent_id = str(config_agent_id or AGENT_NAME)
    conversation_id = (
        f"{effective_config_agent_id}:{end_user_id}" if end_user_id else room_name
    )

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
        "tts_endpoint": tts_endpoint,
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
        userdata["timeline_event_index"] = (
            int(userdata.get("timeline_event_index", 0)) + 1
        )
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
                        metadata={
                            "agent_id": userdata.get("agent_id"),
                            "language": userdata.get("language"),
                        },
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


def _instructions_with_resume_context(
    base_prompt: str, userdata: dict[str, Any]
) -> str:
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
    base_prompt = (
        f"{base_prompt}\n\n"
        "Closing behavior:\n"
        "- End the conversation naturally once the caller's request is handled.\n"
        "- Do not give a forced recap of the whole interaction at the end of every successful call.\n"
        "- Only give a short summary when the caller explicitly asks for one or when a brief confirmation is genuinely useful.\n"
        "- If the caller says thank you, says they are done, or clearly signals the conversation is over, respond naturally and close politely.\n"
    )
    end_user_id = str(userdata.get("end_user_id") or "")
    if not end_user_id:
        return base_prompt
    business_use_case = (
        str(userdata.get("business_use_case") or "ekedc").strip().lower()
    )
    enabled_tool_names = {
        str(name or "").strip()
        for name in (userdata.get("enabled_tool_names") or [])
        if str(name or "").strip()
    }
    configured_agent_name = str(userdata.get("configured_agent_name") or "").strip()
    if configured_agent_name:
        logger.info(
            "Applying configured agent name to prompt: %s", configured_agent_name
        )
        base_prompt = (
            f"{base_prompt}\n\n"
            f"Agent profile detail: your name is '{configured_agent_name}'.\n"
            f"- If a customer asks your name, respond that your name is '{configured_agent_name}'.\n"
            "- Do not say you don't have a name."
        )
    if business_use_case == "fidelity":
        base_prompt = (
            f"{base_prompt}\n\n"
            "Domain lock:\n"
            "- You are Fidelity Bank's customer care assistant.\n"
            "- Never present yourself as an electricity, salon, passport, certificate, or appointment assistant.\n"
            "- Use banking framing in your replies: account, card, transaction, balance, reversal, and ticket.\n\n"
            "Role lock:\n"
            "- You MUST follow the current banking role and responsibilities in this prompt.\n"
            "- Historical snippets may contain outdated assistant behavior from older versions.\n"
            "- Never switch back to an old non-banking persona if it conflicts with this prompt.\n\n"
            "Issue handling lock:\n"
            "- Use the available banking tools for account overview, recent transactions, transaction checks, card actions, reversals, and ticket creation.\n"
            "- Do not claim an action was completed unless the tool confirms it.\n"
            "- For fraud, suspicious activity, compliance restrictions, or other sensitive cases, create a ticket instead of promising a direct resolution."
        )
    elif business_use_case == "hotel":
        room_lookup_line = (
            "- Room availability lookup is enabled for this agent. Use it when guests ask about available rooms or prices."
            if "fetch_room_availability" in enabled_tool_names
            else "- Room availability lookup is not enabled for this agent. If asked, say you can't confirm that right now and offer human follow-up."
        )
        booking_line = (
            "- Booking creation is enabled for this agent, but only use it after room availability and pricing have been checked successfully."
            if "create_booking" in enabled_tool_names
            else "- Booking creation is not enabled for this agent. Do not say a room booking was created."
        )
        ticket_line = (
            "- Ticket creation is enabled for this agent for complaints or manual follow-up."
            if "create_ticket" in enabled_tool_names
            else "- Ticket creation is not enabled for this agent. Do not say a ticket was created."
        )
        base_prompt = (
            f"{base_prompt}\n\n"
            "Domain lock:\n"
            "- You are a hotel guest support and booking assistant for this business.\n"
            "- Never present yourself as an electricity or banking assistant.\n"
            "- Use hotel knowledge and the enabled tools for this specific agent.\n\n"
            "Role lock:\n"
            "- You MUST follow the current hotel role and responsibilities in this prompt.\n"
            "- Historical snippets may contain outdated assistant behavior from older versions.\n"
            "- Never switch back to an old non-hotel persona if it conflicts with this prompt.\n\n"
            "Issue handling lock:\n"
            f"{room_lookup_line}\n"
            f"{booking_line}\n"
            f"{ticket_line}\n"
            "- Do not claim a booking or ticket was completed unless the tool confirms it."
        )
    elif business_use_case == "restaurant":
        menu_lookup_line = (
            "- Menu lookup is enabled for this agent. Use it when customers ask what is available or how much items cost."
            if "fetch_menu_availability" in enabled_tool_names
            else "- Menu lookup is not enabled for this agent. If asked, say you can't confirm that right now and offer human follow-up."
        )
        order_line = (
            "- Order creation is enabled for this agent, but only use it after menu details and prices have been checked successfully."
            if "create_order" in enabled_tool_names
            else "- Order creation is not enabled for this agent. Do not say an order was created."
        )
        ticket_line = (
            "- Ticket creation is enabled for this agent for complaints or manual follow-up."
            if "create_ticket" in enabled_tool_names
            else "- Ticket creation is not enabled for this agent. Do not say a ticket was created."
        )
        base_prompt = (
            f"{base_prompt}\n\n"
            "Domain lock:\n"
            "- You are a restaurant host and customer support assistant for this business.\n"
            "- Never present yourself as a hotel, electricity, or banking assistant.\n"
            "- Use restaurant knowledge and the enabled tools for this specific agent.\n\n"
            "Role lock:\n"
            "- You MUST follow the current restaurant role and responsibilities in this prompt.\n"
            "- Historical snippets may contain outdated assistant behavior from older versions.\n"
            "- Never switch back to an old non-restaurant persona if it conflicts with this prompt.\n\n"
            "Issue handling lock:\n"
            f"{menu_lookup_line}\n"
            f"{order_line}\n"
            f"{ticket_line}\n"
            "- Do not claim an order or ticket was completed unless the tool confirms it.\n"
        )
    elif business_use_case == "fashion":
        product_lookup_line = (
            "- Product lookup is enabled for this agent. Use it when customers ask what is available or how much items cost."
            if "fetch_product_availability" in enabled_tool_names
            else "- Product lookup is not enabled for this agent. If asked, say you can't confirm that right now and offer human follow-up."
        )
        order_line = (
            "- Order creation is enabled for this agent, but only use it after product details and prices have been checked successfully."
            if "create_order" in enabled_tool_names
            else "- Order creation is not enabled for this agent. Do not say an order was created."
        )
        ticket_line = (
            "- Ticket creation is enabled for this agent for complaints or manual follow-up."
            if "create_ticket" in enabled_tool_names
            else "- Ticket creation is not enabled for this agent. Do not say a ticket was created."
        )
        base_prompt = (
            f"{base_prompt}\n\n"
            "Domain lock:\n"
            "- You are a fashion sales and customer support assistant for this business.\n"
            "- Never present yourself as a hotel, electricity, or banking assistant.\n"
            "- Use fashion product knowledge and the enabled tools for this specific agent.\n\n"
            "Role lock:\n"
            "- You MUST follow the current fashion retail role and responsibilities in this prompt.\n"
            "- Historical snippets may contain outdated assistant behavior from older versions.\n"
            "- Never switch back to an old non-fashion persona if it conflicts with this prompt.\n\n"
            "Issue handling lock:\n"
            f"{product_lookup_line}\n"
            f"{order_line}\n"
            f"{ticket_line}\n"
            "- Do not claim an order or ticket was completed unless the tool confirms it.\n"
        )
    elif business_use_case == "generic":
        base_prompt = (
            f"{base_prompt}\n\n"
            "Domain lock:\n"
            "- You are the business's AI voice assistant for this specific company.\n"
            "- Never present yourself as an electricity, banking, hotel, restaurant, or fashion assistant unless the business instructions explicitly say so.\n"
            "- Use the saved business instructions, knowledge, and configured tools for this business only.\n\n"
            "Role lock:\n"
            "- You MUST follow the current business-specific role and responsibilities in this prompt.\n"
            "- Historical snippets may contain outdated assistant behavior from older versions.\n"
            "- Never switch to an old persona if it conflicts with this prompt.\n\n"
            "Issue handling lock:\n"
            "- Use configured tools only when they are relevant and available.\n"
            "- Do not claim any action succeeded unless the tool confirms it.\n"
            "- If a request needs human attention, create a ticket if that tool is available.\n"
            "- If the caller asks for a ticket, or agrees to ticket follow-up, call create_ticket immediately before replying.\n"
            "- In the exact turn where you say a ticket was created, create_ticket must already have succeeded.\n"
        )
    else:
        # Prevent old assistant personas in historical context from overriding current role.
        base_prompt = (
            f"{base_prompt}\n\n"
            "Domain lock:\n"
            "- You are the current business's AI customer support assistant.\n"
            "- Never present yourself as an electricity, banking, beauty, appointment-booking, hotel, restaurant, or fashion assistant unless the current business instructions explicitly require that.\n"
            "- Do not reuse stale domain framing from unrelated older assistants.\n\n"
            "Role lock:\n"
            "- You MUST follow the current role and responsibilities in this prompt.\n"
            "- Historical snippets may contain outdated assistant behavior from older versions.\n"
            "- Never switch back to an old business persona if it conflicts with this prompt.\n\n"
            "Issue handling lock:\n"
            "- Use only the configured tools for the current business.\n"
            "- Do not claim an action was completed unless the tool confirms it.\n"
            "- If the issue needs human follow-up, create a ticket when that tool is available.\n"
            "- If the caller asks for a ticket, or agrees to ticket follow-up, call create_ticket immediately before replying.\n"
            "- In the exact turn where you say a ticket was created, create_ticket must already have succeeded."
        )
    channel = (
        "web" if str(userdata.get("identity_type") or "").lower() == "web" else "voice"
    )
    business_id = _normalize_business_id(str(userdata.get("business_id") or ""))
    config_agent_id = str(
        userdata.get("agent_config_id") or userdata.get("agent_id") or AGENT_NAME
    )

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
            context_payload = await fetch_context_remote(
                conv_id, limit=30, business_id=business_id
            )
            msgs = (
                context_payload.get("messages")
                if isinstance(context_payload, dict)
                else None
            )
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


async def _fetch_active_agent_runtime_config(
    userdata: dict[str, Any],
) -> dict[str, Any]:
    business_id = _normalize_business_id(str(userdata.get("business_id") or ""))
    config_agent_id = str(userdata.get("agent_config_id") or "").strip()
    if not business_id or not config_agent_id:
        return {}
    payload = await get_agent_active_config(
        agent_id=config_agent_id, business_id=business_id
    )
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


def _active_tool_url(active_agent_config: dict[str, Any] | None, tool_name: str) -> str:
    cfg = active_agent_config or {}
    tools = cfg.get("tools")
    if not isinstance(tools, list):
        return ""
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if str(tool.get("name") or "").strip() != tool_name:
            continue
        return str(tool.get("url") or "").strip()
    return ""


def _hydrate_userdata_from_active_agent_config(
    userdata: dict[str, Any],
    active_agent_config: dict[str, Any] | None,
    business_use_case: str,
) -> None:
    cfg = active_agent_config or {}
    tools = cfg.get("tools")
    active_tools = (
        [
            tool
            for tool in tools
            if isinstance(tool, dict) and str(tool.get("name") or "").strip()
        ]
        if isinstance(tools, list)
        else []
    )
    userdata["active_tools"] = active_tools
    enabled_tool_names = [str(tool.get("name") or "").strip() for tool in active_tools]
    userdata["enabled_tool_names"] = enabled_tool_names

    expected_tool_by_use_case = {
        "hotel": "fetch_room_availability",
        "restaurant": "fetch_menu_availability",
        "fashion": "fetch_product_availability",
    }
    tool_name = expected_tool_by_use_case.get(
        str(business_use_case or "").strip().lower()
    )
    if not tool_name:
        userdata["live_data_endpoint"] = ""
        return
    userdata["live_data_endpoint"] = _active_tool_url(active_agent_config, tool_name)
    logger.info(
        "Runtime tool context: use_case=%s enabled_tools=%s live_data_endpoint=%s",
        business_use_case,
        ",".join(enabled_tool_names) if enabled_tool_names else "-",
        str(userdata.get("live_data_endpoint") or ""),
    )


def _strip_live_connectivity_lines(text: str) -> str:
    blocked_fragments = (
        "live room data is not connected",
        "live menu data is not connected",
        "live product data is not connected",
        "live operational data is not connected",
        "live room availability and pricing are not connected yet",
        "live menu availability and pricing are not connected yet",
        "live product availability and pricing are not connected yet",
        "since live room availability is not connected",
        "since live menu availability is not connected",
        "since live product availability is not connected",
        "do not invent current availability",
        "do not invent current menu availability",
        "do not invent current product availability",
        "offer to create a ticket for follow-up instead",
        "current room lookup status:",
        "current menu lookup status:",
        "current product lookup status:",
        "hotel tool guardrails:",
        "restaurant tool guardrails:",
        "fashion tool guardrails:",
        "if room lookup is available",
        "if menu lookup is available",
        "if product lookup is available",
    )
    kept_lines: list[str] = []
    for line in str(text or "").splitlines():
        normalized = " ".join(line.lower().split())
        if any(fragment in normalized for fragment in blocked_fragments):
            continue
        kept_lines.append(line)
    return "\n".join(kept_lines).strip()


def _active_tool_records(
    active_agent_config: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    cfg = active_agent_config or {}
    tools = cfg.get("tools")
    if not isinstance(tools, list):
        return []
    return [
        tool
        for tool in tools
        if isinstance(tool, dict) and str(tool.get("name") or "").strip()
    ]


def _tool_description(tool: dict[str, Any]) -> str:
    return " ".join(str(tool.get("description") or "").split()).strip()


def _runtime_tool_guidance(
    active_agent_config: dict[str, Any] | None, business_use_case: str
) -> str:
    tools = _active_tool_records(active_agent_config)
    enabled_names = {str(tool.get("name") or "").strip() for tool in tools}
    by_name = {str(tool.get("name") or "").strip(): tool for tool in tools}
    lines = [
        "Enabled tools for this agent right now:",
        "- Only the tools described here are available in this conversation.",
        "- Use an enabled tool whenever it is the right way to answer or complete the request.",
        "- If a tool is not listed as enabled here, do not act as if you can use it.",
    ]
    lines.append(
        "- search_business_knowledge is enabled. Use it to look up saved business facts, policies, amenities, FAQs, and other documented information before saying you cannot answer."
    )

    if "create_ticket" in enabled_names:
        desc = _tool_description(by_name["create_ticket"])
        lines.append(
            f"- create_ticket is enabled. Use it for complaints, unresolved requests, or any human follow-up that should be handed to the team. {desc}".strip()
        )
    else:
        lines.append(
            "- create_ticket is not enabled. Do not say a support ticket was created."
        )

    if business_use_case == "hotel":
        if "create_booking" in enabled_names:
            desc = _tool_description(by_name["create_booking"])
            lines.append(
                f"- create_booking is enabled. Use it only after room availability and pricing have been checked successfully and the guest has confirmed the booking details. {desc}".strip()
            )
        else:
            lines.append(
                "- create_booking is not enabled. Do not say a room booking was created."
            )
        if "fetch_room_availability" in enabled_names:
            desc = _tool_description(by_name["fetch_room_availability"])
            lines.append(
                f"- fetch_room_availability is enabled. Use it to check currently available rooms and prices whenever a guest asks about room availability, room options, or prices, including broad questions. {desc}".strip()
            )
        else:
            lines.append(
                "- fetch_room_availability is not enabled. If a guest asks about current room availability or prices, say you cannot confirm that right now and offer ticket follow-up."
            )

    if business_use_case == "restaurant":
        if "create_order" in enabled_names:
            desc = _tool_description(by_name["create_order"])
            lines.append(
                f"- create_order is enabled. Use it only after current menu details and prices have been checked successfully and the customer confirms the order. {desc}".strip()
            )
        else:
            lines.append(
                "- create_order is not enabled. Do not say an order was created."
            )
        if "fetch_menu_availability" in enabled_names:
            desc = _tool_description(by_name["fetch_menu_availability"])
            lines.append(
                f"- fetch_menu_availability is enabled. Use it to check current menu items and prices whenever a customer asks what is available or how much items cost, including broad questions. {desc}".strip()
            )
        else:
            lines.append(
                "- fetch_menu_availability is not enabled. If a customer asks about the current menu or prices, say you cannot confirm that right now and offer ticket follow-up."
            )

    if business_use_case == "fashion":
        if "create_order" in enabled_names:
            desc = _tool_description(by_name["create_order"])
            lines.append(
                f"- create_order is enabled. Use it only after current product details and prices have been checked successfully and the customer confirms the order. {desc}".strip()
            )
        else:
            lines.append(
                "- create_order is not enabled. Do not say an order was created."
            )
        if "fetch_product_availability" in enabled_names:
            desc = _tool_description(by_name["fetch_product_availability"])
            lines.append(
                f"- fetch_product_availability is enabled. Use it to check current product availability and prices whenever a customer asks what is available or how much items cost, including broad questions. {desc}".strip()
            )
        else:
            lines.append(
                "- fetch_product_availability is not enabled. If a customer asks about current product availability or prices, say you cannot confirm that right now and offer ticket follow-up."
            )

    generic_tool_names = sorted(
        name
        for name in enabled_names
        if name
        not in {
            "create_ticket",
            "create_booking",
            "create_order",
            "fetch_room_availability",
            "fetch_menu_availability",
            "fetch_product_availability",
        }
    )
    for name in generic_tool_names:
        desc = _tool_description(by_name[name])
        if desc:
            lines.append(
                f"- {name} is enabled. {desc} Use it only when the caller's request clearly needs it."
            )
        else:
            lines.append(
                f"- {name} is enabled. Use it only when the caller's request clearly needs it."
            )
    return "\n".join(lines)


def _detect_business_use_case(
    *,
    active_agent_config: dict[str, Any] | None,
    userdata: dict[str, Any],
) -> str:
    business_id = _normalize_business_id(str(userdata.get("business_id") or ""))
    if business_id and business_id in FIDELITY_BUSINESS_IDS:
        return "fidelity"
    if business_id and business_id in EKEDC_BUSINESS_IDS:
        return "ekedc"

    cfg = active_agent_config or {}
    tools = cfg.get("tools")
    tool_names = {
        str(tool.get("name") or "").strip().lower()
        for tool in tools
        if isinstance(tools, list) and isinstance(tool, dict)
    }
    fidelity_tool_names = {
        "account_overview",
        "recent_transactions",
        "transaction_status",
        "block_card",
        "unblock_card",
        "reverse_failed_transaction",
    }
    if tool_names & fidelity_tool_names:
        return "fidelity"

    ekedc_tool_names = {
        "resolve_customer",
        "customer_account_lookup",
        "tariff_profile",
        "payments_summary",
        "vending_history",
        "update_customer_record",
        "create_payment_plan",
        "create_complaint",
        "create_outage_report",
        "create_meter_request",
        "create_escalation_ticket",
        "check_case_status",
        "refresh_meter_token_state",
    }
    if tool_names & ekedc_tool_names:
        return "ekedc"
    if "fetch_room_availability" in tool_names or "create_booking" in tool_names:
        return "hotel"
    if "fetch_menu_availability" in tool_names:
        return "restaurant"
    if "fetch_product_availability" in tool_names:
        return "fashion"

    text = " ".join(
        [
            str(cfg.get("name") or ""),
            str(cfg.get("description") or ""),
            str(cfg.get("instructions") or ""),
            str(userdata.get("configured_agent_name") or ""),
        ]
    ).lower()
    if any(
        token in text
        for token in (
            "ekedc",
            "ekedc demo",
            "electricity customer support",
            "electricity support",
            "tariff band",
            "meter request",
            "token vending",
            "power outage",
            "low voltage",
        )
    ):
        return "ekedc"
    if any(
        token in text
        for token in (
            "fidelity",
            "fidelity bank",
            "block card",
            "recent transactions",
            "failed transaction",
            "account balance",
        )
    ):
        return "fidelity"
    if any(
        token in text
        for token in (
            "restaurant",
            "menu",
            "order tool",
            "create_order",
            "dining",
            "host stand",
            "reservation request",
            "pickup",
            "delivery",
        )
    ):
        return "restaurant"
    if any(
        token in text
        for token in (
            "fashion",
            "size",
            "sizes",
            "style",
            "styles",
            "product availability",
            "catalog",
            "boutique",
            "apparel",
        )
    ):
        return "fashion"
    if any(
        token in text
        for token in (
            "hotel",
            "guest support",
            "room availability",
            "check-in",
            "check out",
            "accommodation",
            "concierge",
            "room reservation",
        )
    ):
        return "hotel"
    if any(
        token in text
        for token in (
            "ekedc",
            "electricity",
            "tariff",
            "meter",
            "outage",
            "token vending",
        )
    ):
        return "ekedc"

    return (
        DEFAULT_BUSINESS_USE_CASE
        if DEFAULT_BUSINESS_USE_CASE
        in {"ekedc", "fidelity", "hotel", "restaurant", "fashion", "generic"}
        else "generic"
    )


def _effective_base_prompt(
    *,
    static_prompt: str,
    active_agent_config: dict[str, Any] | None,
    business_use_case: str,
    language: str,
) -> str:
    cfg = active_agent_config or {}
    configured_instructions = str(cfg.get("instructions") or "").strip()
    runtime_tool_guidance = _runtime_tool_guidance(cfg, business_use_case)
    live_tool_by_use_case = {
        "hotel": "fetch_room_availability",
        "restaurant": "fetch_menu_availability",
        "fashion": "fetch_product_availability",
    }
    live_endpoint_url = _active_tool_url(
        cfg, live_tool_by_use_case.get(business_use_case, "")
    )
    live_data_connected = bool(str(live_endpoint_url or "").strip())
    if not configured_instructions:
        if business_use_case == "hotel":
            if str(language or "").strip().lower() == "fr":
                return (
                    "Vous êtes l'assistant IA de support et de réservation de l'hôtel pour ce business.\n"
                    "Répondez de façon claire, calme et professionnelle.\n"
                    "Utilisez les connaissances de l'hôtel, la disponibilité en direct si elle est connectée, "
                    "et créez des réservations ou des tickets uniquement lorsque les outils confirment l'action."
                )
            return (
                "You are the hotel's AI guest support and booking assistant for this business.\n"
                "Respond clearly, calmly, and professionally.\n"
                "Use hotel knowledge, live availability only if connected, and create bookings or tickets only when the tools confirm the action."
            )
        if business_use_case == "restaurant":
            return RESTAURANT_STATIC_PROMPT_EN
        if business_use_case == "fashion":
            return FASHION_STATIC_PROMPT_EN
        if business_use_case == "fidelity":
            return FIDELITY_STATIC_PROMPT_EN
        return GENERIC_STATIC_PROMPT_EN

    normalized = " ".join(configured_instructions.lower().split())
    default_like = {
        "you are a helpful ai voice assistant for this business. be concise, friendly, and accurate.",
        "you are a helpful ai voice assistant for this business.",
    }
    if normalized in default_like:
        logger.info(
            "Ignoring default-like dashboard instructions; keeping static domain prompt."
        )
        if business_use_case == "fidelity":
            return FIDELITY_STATIC_PROMPT_EN
        if business_use_case == "hotel":
            if str(language or "").strip().lower() == "fr":
                return (
                    "Vous êtes l'assistant IA de support et de réservation de l'hôtel pour ce business.\n"
                    "Répondez de façon claire, calme et professionnelle.\n"
                    "Utilisez les connaissances de l'hôtel, la disponibilité en direct si elle est connectée, "
                    "et créez des réservations ou des tickets uniquement lorsque les outils confirment l'action."
                )
            return (
                "You are the hotel's AI guest support and booking assistant for this business.\n"
                "Respond clearly, calmly, and professionally.\n"
                "Use hotel knowledge, live availability only if connected, and create bookings or tickets only when the tools confirm the action."
            )
        return static_prompt

    if business_use_case == "fidelity":
        return configured_instructions

    if business_use_case == "hotel":
        sanitized_instructions = (
            _strip_live_connectivity_lines(configured_instructions)
            if live_data_connected
            else configured_instructions
        )
        return (
            f"{sanitized_instructions.rstrip()}\n\n"
            f"{runtime_tool_guidance}\n\n"
            "Tool truthfulness rules:\n"
            "- If the guest asks you to create a ticket, or agrees to ticket follow-up, call create_ticket immediately before replying if that tool is enabled.\n"
            "- In the exact turn where you say a ticket was created, create_ticket must already have succeeded.\n"
            "- Infer ticket titles and descriptions yourself from the conversation; do not ask the guest to write them for you.\n"
            "- Only ask a follow-up question before creating a ticket if a concrete missing fact is essential.\n"
            "- Never say a ticket was created unless create_ticket returned success.\n"
            "- Never say a booking was created unless create_booking returned success.\n"
            "- If a tool call fails, explain that clearly and offer the next best fallback.\n"
        )

    if business_use_case == "restaurant":
        sanitized_instructions = (
            _strip_live_connectivity_lines(configured_instructions)
            if live_data_connected
            else configured_instructions
        )
        return (
            f"{sanitized_instructions.rstrip()}\n\n"
            f"{runtime_tool_guidance}\n\n"
            "Tool truthfulness rules:\n"
            "- If the customer asks you to create a ticket, or agrees to ticket follow-up, call create_ticket immediately before replying if that tool is enabled.\n"
            "- In the exact turn where you say a ticket was created, create_ticket must already have succeeded.\n"
            "- Infer ticket titles and descriptions yourself from the conversation; do not ask the customer to write them for you.\n"
            "- Never say a ticket was created unless create_ticket returned success.\n"
            "- Never say an order was created unless create_order returned success.\n"
            "- If a tool call fails, explain that clearly and offer the next best fallback.\n"
        )

    if business_use_case == "fashion":
        sanitized_instructions = (
            _strip_live_connectivity_lines(configured_instructions)
            if live_data_connected
            else configured_instructions
        )
        return (
            f"{sanitized_instructions.rstrip()}\n\n"
            f"{runtime_tool_guidance}\n\n"
            "Tool truthfulness rules:\n"
            "- If the customer asks you to create a ticket, or agrees to ticket follow-up, call create_ticket immediately before replying if that tool is enabled.\n"
            "- In the exact turn where you say a ticket was created, create_ticket must already have succeeded.\n"
            "- Infer ticket titles and descriptions yourself from the conversation; do not ask the customer to write them for you.\n"
            "- Never say a ticket was created unless create_ticket returned success.\n"
            "- Never say an order was created unless create_order returned success.\n"
            "- If a tool call fails, explain that clearly and offer the next best fallback.\n"
        )

    if business_use_case == "generic":
        return f"{configured_instructions.rstrip()}\n\n{runtime_tool_guidance}"

    incompatible_tokens = (
        "salon",
        "appointment",
        "hair",
        "beauty",
        "booking",
        "barber",
        "spa",
        "receptionist",
    )
    if any(token in normalized for token in incompatible_tokens):
        logger.warning(
            "Ignoring incompatible dashboard instructions containing stale non-EKEDC persona terms."
        )
        return static_prompt

    return (
        f"{static_prompt}\n\n"
        "Business-specific overlay instructions (follow these in addition to your domain role):\n"
        f"{configured_instructions}"
    )


def _ops_tool_metadata_from_userdata(userdata: dict[str, Any]) -> dict[str, Any]:
    return {
        "client_id": os.getenv("AGENT_CLIENT_ID", "sales-girl-internal"),
        "agent_id": str(
            userdata.get("agent_config_id") or userdata.get("agent_id") or AGENT_NAME
        ),
        "business_id": str(userdata.get("business_id") or ""),
        "business_use_case": str(userdata.get("business_use_case") or ""),
        "conversation_id": str(userdata.get("conversation_id") or ""),
        "session_id": str(userdata.get("session_id") or ""),
        "end_user_id": str(userdata.get("end_user_id") or ""),
    }


async def _build_preloaded_ops_context(userdata: dict[str, Any]) -> str:
    md = _ops_tool_metadata_from_userdata(userdata)
    caller_id = str(md.get("end_user_id") or "").strip()
    if not caller_id:
        return ""
    business_use_case = str(userdata.get("business_use_case") or "").strip().lower()

    if business_use_case == "hotel":
        return ""

    if business_use_case == "fidelity":
        overview: dict[str, Any] = {}
        transactions_payload: dict[str, Any] = {}
        try:
            resolved_overview = await ops_get_account_overview(
                customer_identifier=caller_id,
                metadata=md,
            )
            if isinstance(resolved_overview, dict):
                overview = resolved_overview
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Ops preload Fidelity account overview failed for %s: %s",
                caller_id,
                exc,
            )

        try:
            resolved_transactions = await ops_get_recent_transactions(
                customer_identifier=caller_id,
                limit=5,
                metadata=md,
            )
            if isinstance(resolved_transactions, dict):
                transactions_payload = resolved_transactions
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Ops preload Fidelity transactions failed for %s: %s", caller_id, exc
            )

        account = overview.get("account") if isinstance(overview, dict) else {}
        if not isinstance(account, dict):
            account = {}
        cards = overview.get("cards") if isinstance(overview, dict) else []
        transactions = (
            transactions_payload.get("transactions")
            if isinstance(transactions_payload, dict)
            else None
        )
        if not isinstance(transactions, list) or not transactions:
            transactions = (
                overview.get("recent_transactions")
                if isinstance(overview, dict)
                else []
            )

        card_lines = []
        for item in cards[:2] if isinstance(cards, list) else []:
            if isinstance(item, dict):
                card_lines.append(
                    f"- {item.get('card_type') or 'Card'} ending {item.get('last4') or '----'} status={item.get('card_status') or item.get('status')}"
                )
        transaction_lines = []
        for item in transactions[:3] if isinstance(transactions, list) else []:
            if isinstance(item, dict):
                transaction_lines.append(
                    f"- {item.get('transaction_date') or item.get('date')}: {item.get('narration') or item.get('title') or 'Transaction'} amount={item.get('amount_naira') or item.get('amount')} status={item.get('transaction_status') or item.get('status')}"
                )

        customer_name = str(
            overview.get("customer_name") or overview.get("name") or ""
        ).strip()
        logger.info(
            "Preloaded Fidelity caller context: email=%s cards=%s transactions=%s",
            caller_id,
            len(cards) if isinstance(cards, list) else 0,
            len(transactions) if isinstance(transactions, list) else 0,
        )
        return (
            "Verified caller profile and banking context (fetched before this conversation starts):\n"
            "- This caller has already been identified from the authenticated session context.\n"
            "- Use the caller profile below confidently for account and transaction questions.\n"
            "- If caller name is present below, do not say you do not know the caller.\n"
            "- Do not read this whole block aloud at the start of the call. Use it only when relevant.\n"
            f"- Caller email: {caller_id}\n"
            f"- Caller name: {customer_name or account.get('account_name') or '-'}\n"
            f"- Account number: {account.get('account_number') or '-'}\n"
            f"- Account name: {account.get('account_name') or '-'}\n"
            f"- Account type: {account.get('account_type') or '-'}\n"
            f"- Available balance: {account.get('available_balance_naira') or account.get('available_balance') or '-'}\n"
            f"- Current balance: {account.get('balance_naira') or account.get('balance') or '-'}\n"
            f"- Cards found: {len(cards) if isinstance(cards, list) else 0}\n"
            f"{chr(10).join(card_lines) if card_lines else '- none'}\n"
            f"- Recent transactions found: {len(transactions) if isinstance(transactions, list) else 0}\n"
            f"{chr(10).join(transaction_lines) if transaction_lines else '- none'}\n"
            "- Use this preloaded context first. Do not ask for the customer's email as your first move.\n"
        )

    customer: dict[str, Any] = {}
    tariff: dict[str, Any] = {}
    payments: dict[str, Any] = {}
    vending: dict[str, Any] = {}

    try:
        resolved_customer = await ops_lookup_customer_account(
            customer_identifier=caller_id,
            metadata=md,
        )
        if isinstance(resolved_customer, dict):
            customer = resolved_customer
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ops preload customer lookup failed for %s: %s", caller_id, exc)

    try:
        resolved_tariff = await ops_get_tariff_profile(
            customer_identifier=caller_id,
            metadata=md,
        )
        if isinstance(resolved_tariff, dict):
            tariff = resolved_tariff
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ops preload tariff lookup failed for %s: %s", caller_id, exc)

    try:
        resolved_payments = await ops_get_payment_summary(
            customer_identifier=caller_id,
            metadata=md,
        )
        if isinstance(resolved_payments, dict):
            payments = resolved_payments
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ops preload payment lookup failed for %s: %s", caller_id, exc)

    try:
        resolved_vending = await ops_get_vending_history(
            customer_identifier=caller_id,
            metadata=md,
        )
        if isinstance(resolved_vending, dict):
            vending = resolved_vending
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ops preload vending lookup failed for %s: %s", caller_id, exc)

    customer_name = (
        str(customer.get("name") or "").strip() if isinstance(customer, dict) else ""
    )
    customer_email = (
        str(customer.get("email") or "").strip() if isinstance(customer, dict) else ""
    )
    customer_phone = (
        str(customer.get("phone") or "").strip() if isinstance(customer, dict) else ""
    )
    account_number = str(
        customer.get("account_number") or tariff.get("account_number") or ""
    ).strip()
    tariff_band = str(
        tariff.get("tariff_band") or customer.get("tariff_band") or ""
    ).strip()
    meter_type = str(
        tariff.get("meter_type") or customer.get("meter_type") or ""
    ).strip()
    business_unit = str(
        tariff.get("business_unit") or customer.get("business_unit") or ""
    ).strip()
    service_address = str(
        tariff.get("service_address") or customer.get("service_address") or ""
    ).strip()
    feeder_name = str(
        tariff.get("feeder_name") or customer.get("feeder_name") or ""
    ).strip()
    payment_items = payments.get("payments") if isinstance(payments, dict) else []
    vend_items = vending.get("vend_history") if isinstance(vending, dict) else []
    payment_lines = []
    for item in payment_items[:3] if isinstance(payment_items, list) else []:
        if isinstance(item, dict):
            payment_lines.append(
                f"- {item.get('date')}: amount={item.get('amount')} status={item.get('status')}"
            )
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


def _instructions_with_preloaded_ops_context(
    base_prompt: str, preloaded_context: str
) -> str:
    if not preloaded_context:
        return base_prompt
    return f"{base_prompt}\n\n{preloaded_context}\n"


def _kickoff_prompt_for_language(language: str, business_use_case: str) -> str:
    lang = str(language or "").strip().lower()
    if lang == "fr":
        return (
            "Commencez la conversation maintenant. Saluez l'appelant en français. Présentez-vous brièvement par votre nom et proposez votre aide de manière naturelle, en fonction de votre rôle spécifique. "
            "Ne demandez pas d'abord l'email ou d'autres informations d'identification. "
            "N'énumérez pas immédiatement tout le profil de l'appelant ; saluez d'abord puis attendez sa demande."
        )
    return (
        "Start the conversation now. Greet the caller first in English. Introduce yourself briefly by name and offer assistance naturally based on your specific role and instructions. "
        "Do not ask for email or other identifiers as your first move. Do not dump the caller profile immediately; greet first and wait for the caller's request."
    )


def _build_session_for_language(
    *,
    language: str,
    instructions: str,
    userdata: dict[str, Any],
    tts_engine: Any | None = None,
) -> AgentSession:
    if language == "fr":
        return AgentSession(
            stt=deepgram.STT(language="fr"),
            tts=tts_engine or deepgram.TTS(model="aura-2-agathe-fr"),
            llm=google.LLM(model="gemini-3.1-pro"),
            userdata=userdata,
            min_endpointing_delay=TURN_MIN_ENDPOINTING_DELAY,
            max_endpointing_delay=TURN_MAX_ENDPOINTING_DELAY,
            min_interruption_duration=TURN_MIN_INTERRUPTION_DURATION,
        )

    return AgentSession(
        stt=deepgram.STT(language="en"),
        tts=tts_engine,
        llm=google.LLM(model="gemini-3.1-pro"),
        userdata=userdata,
        min_endpointing_delay=TURN_MIN_ENDPOINTING_DELAY,
        max_endpointing_delay=TURN_MAX_ENDPOINTING_DELAY,
        min_interruption_duration=TURN_MIN_INTERRUPTION_DURATION,
    )


def _trigger_first_turn(
    session: AgentSession, *, language: str, business_use_case: str
) -> None:
    try:
        session.generate_reply(
            instructions=_kickoff_prompt_for_language(language, business_use_case),
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


def _normalized_language_code(value: str) -> str:
    lowered = str(value or "").strip().lower()
    if lowered in {"fr", "french", "français", "francais"}:
        return "fr"
    return "en"


def _build_tts_engine_for_language(
    *,
    language: str,
    active_agent_config: dict[str, Any],
    userdata: dict[str, Any],
    business_id: str,
) -> Any:
    lang = str(language or "").strip().lower()
    is_fr = lang == "fr"
    fallback_tts: Any = (
        deepgram.TTS(model="aura-2-agathe-fr")
        if is_fr
        else deepgram.TTS(model="aura-asteria-en")
    )
    odion_enabled = ENABLE_ODION_TTS_FR if is_fr else ENABLE_ODION_TTS_EN
    fallback_label = "French" if is_fr else "English"

    use_experiment_clone = bool(ODION_TTS_EXPERIMENT_OWNER_ID) and bool(
        ODION_TTS_EXPERIMENT_VOICE_ID
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
        ("French" if is_fr else "English")
        if use_experiment_clone
        else str(
            active_agent_config.get("tts_language_hint")
            or ("French" if is_fr else "English")
        ).strip()
        or ("French" if is_fr else "English")
    )
    use_configured_clone = use_experiment_clone or _should_use_odion_tts_for_language(
        active_agent_config, lang
    )
    use_odion_default = not use_configured_clone

    if not odion_enabled:
        logger.info(
            "ENABLE_ODION_TTS_%s=false; using Deepgram TTS for %s session.",
            "FR" if is_fr else "EN",
            fallback_label,
        )
        return fallback_tts

    try:
        if use_configured_clone:
            tts_engine = OdionTTS(
                owner_id=tts_owner_id,
                voice_id=tts_voice_id,
                language=tts_language_hint,
                seed=ODION_TTS_CLONE_SEED,
                mode="cloned_voice",
            )
            logger.info(
                "Using Odion cloned TTS for %s session: agent_config_id=%s voice_id=%s owner_id=%s seed=%s",
                fallback_label,
                userdata.get("agent_config_id"),
                tts_voice_id,
                tts_owner_id,
                ODION_TTS_CLONE_SEED,
            )
            return tts_engine
        if use_odion_default:
            tts_engine = OdionTTS(
                owner_id=tts_owner_id or business_id,
                voice_id=None,
                language=tts_language_hint,
                seed=None,
                mode="default_voice",
            )
            logger.info(
                "Using Odion default TTS for %s session: agent_config_id=%s owner_id=%s language_hint=%s",
                fallback_label,
                userdata.get("agent_config_id"),
                tts_owner_id or business_id,
                tts_language_hint,
            )
            return tts_engine
    except Exception as exc:  # noqa: BLE001
        if use_configured_clone and STRICT_ODION_CLONE_CONSISTENCY:
            logger.error(
                "Failed to initialize Odion cloned TTS with strict consistency enabled: language=%s agent_config_id=%s voice_id=%s owner_id=%s seed=%s error=%s",
                lang,
                userdata.get("agent_config_id"),
                tts_voice_id,
                tts_owner_id,
                ODION_TTS_CLONE_SEED,
                exc,
            )
            raise
        logger.error(
            "Failed to initialize Odion TTS for %s session, falling back to Deepgram: %s",
            fallback_label,
            exc,
        )
    return fallback_tts


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
        business_use_case = _detect_business_use_case(
            active_agent_config=active_agent_config,
            userdata=userdata,
        )
        userdata["business_use_case"] = business_use_case
        _hydrate_userdata_from_active_agent_config(
            userdata, active_agent_config, business_use_case
        )
        config_name = str(active_agent_config.get("name") or "").strip()
        if config_name:
            userdata["configured_agent_name"] = config_name
        base_prompt = _effective_base_prompt(
            static_prompt=SYSTEM_PROMPT_EN,
            active_agent_config=active_agent_config,
            business_use_case=business_use_case,
            language="en",
        )
        prompt_preview = " ".join(str(base_prompt).split())[:220]
        logger.info(
            "Prompt source: %s preview=%s",
            "active-config"
            if str(active_agent_config.get("instructions") or "").strip()
            else "static-default",
            prompt_preview,
        )
        if "salon" in str(base_prompt).lower():
            logger.warning(
                "Active prompt contains 'salon' text for this session. Forcing safe fallback prompt."
            )
            base_prompt = (
                FIDELITY_STATIC_PROMPT_EN
                if business_use_case == "fidelity"
                else SYSTEM_PROMPT_EN
            )
        preloaded_context = await _build_preloaded_ops_context(userdata)
        instructions = _instructions_with_preloaded_ops_context(
            base_prompt, preloaded_context
        )
        instructions = await _instructions_with_context(instructions, userdata)
        started_at = conv_api_utcnow()
        business_id = str(userdata.get("business_id") or "")
        call_channel = (
            "web"
            if str(userdata.get("identity_type") or "").lower() == "web"
            else "voice"
        )

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

        tts_engine = _build_tts_engine_for_language(
            language="en",
            active_agent_config=active_agent_config,
            userdata=userdata,
            business_id=business_id,
        )

        session = _build_session_for_language(
            language="en",
            instructions=instructions,
            userdata=userdata,
            tts_engine=tts_engine,
        )
        _wire_session_timeline(session, session.userdata)
        try:
            if conversation_service_enabled(business_id) and userdata.get(
                "conversation_id"
            ):
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
                        session_id=session_tracker_id
                        or str(userdata.get("session_id") or ""),
                        started_at=started_at,
                    )
                    userdata["recording_egress_id"] = recording_started.egress_id
                    userdata["recording_expected_url"] = recording_started.expected_url
                    userdata["recording_filepath"] = recording_started.filepath
                    initial_recording_status = (
                        "recording" if recording_started.egress_id else "failed"
                    )
                    await update_session_recording_remote(
                        session_id=session_tracker_id,
                        recording_status=initial_recording_status,
                        recording_url=recording_started.expected_url
                        if initial_recording_status == "recording"
                        else None,
                        business_id=business_id,
                    )
                    _persist_session_event_async(
                        userdata,
                        event_type="recording_started"
                        if recording_started.egress_id
                        else "recording_failed",
                        role="system",
                        title="Recording started"
                        if recording_started.egress_id
                        else "Recording failed",
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
                    logger.info(
                        "Recording not enabled for this session: language=en business_id=%s",
                        business_id,
                    )
            await session.start(
                agent=SalonAgent(instructions=instructions),
                room=ctx.room,
                room_options=room_io.RoomOptions(delete_room_on_close=True),
            )
            _trigger_first_turn(
                session, language="en", business_use_case=business_use_case
            )
            shutdown_reason = await _wait_for_job_shutdown(ctx)
            logger.info(
                "Session shutdown received (en): reason=%s",
                shutdown_reason or "unknown",
            )
        finally:
            await asyncio.shield(
                _finalize_session_cleanup(
                    userdata=userdata,
                    business_id=business_id,
                    session_tracker_id=str(userdata.get("session_tracker_id") or ""),
                    started_at=started_at,
                    call_channel=call_channel,
                    language="en",
                    shutdown_reason=shutdown_reason
                    if "shutdown_reason" in locals()
                    else None,
                )
            )
    else:
        userdata = await _init_session_userdata(ctx, language="fr")
        active_agent_config = await _fetch_active_agent_runtime_config(userdata)
        business_use_case = _detect_business_use_case(
            active_agent_config=active_agent_config,
            userdata=userdata,
        )
        userdata["business_use_case"] = business_use_case
        _hydrate_userdata_from_active_agent_config(
            userdata, active_agent_config, business_use_case
        )
        config_name = str(active_agent_config.get("name") or "").strip()
        if config_name:
            userdata["configured_agent_name"] = config_name
        base_prompt = _effective_base_prompt(
            static_prompt=SYSTEM_PROMPT_FR,
            active_agent_config=active_agent_config,
            business_use_case=business_use_case,
            language="fr",
        )
        prompt_preview = " ".join(str(base_prompt).split())[:220]
        logger.info(
            "Prompt source: %s preview=%s",
            "active-config"
            if str(active_agent_config.get("instructions") or "").strip()
            else "static-default",
            prompt_preview,
        )
        if "salon" in str(base_prompt).lower():
            logger.warning(
                "Active prompt contains 'salon' text for this session. Forcing French static consular prompt."
            )
            base_prompt = SYSTEM_PROMPT_FR
        preloaded_context = await _build_preloaded_ops_context(userdata)
        instructions = _instructions_with_preloaded_ops_context(
            base_prompt, preloaded_context
        )
        instructions = await _instructions_with_context(instructions, userdata)
        started_at = conv_api_utcnow()
        business_id = str(userdata.get("business_id") or "")
        call_channel = (
            "web"
            if str(userdata.get("identity_type") or "").lower() == "web"
            else "voice"
        )

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
        tts_engine = _build_tts_engine_for_language(
            language="fr",
            active_agent_config=active_agent_config,
            userdata=userdata,
            business_id=business_id,
        )
        session = _build_session_for_language(
            language="fr",
            instructions=instructions,
            userdata=userdata,
            tts_engine=tts_engine,
        )
        _wire_session_timeline(session, session.userdata)
        try:
            if conversation_service_enabled(business_id) and userdata.get(
                "conversation_id"
            ):
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
                        session_id=session_tracker_id
                        or str(userdata.get("session_id") or ""),
                        started_at=started_at,
                    )
                    userdata["recording_egress_id"] = recording_started.egress_id
                    userdata["recording_expected_url"] = recording_started.expected_url
                    userdata["recording_filepath"] = recording_started.filepath
                    initial_recording_status = (
                        "recording" if recording_started.egress_id else "failed"
                    )
                    await update_session_recording_remote(
                        session_id=session_tracker_id,
                        recording_status=initial_recording_status,
                        recording_url=recording_started.expected_url
                        if initial_recording_status == "recording"
                        else None,
                        business_id=business_id,
                    )
                    _persist_session_event_async(
                        userdata,
                        event_type="recording_started"
                        if recording_started.egress_id
                        else "recording_failed",
                        role="system",
                        title="Recording started"
                        if recording_started.egress_id
                        else "Recording failed",
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
                    logger.info(
                        "Recording not enabled for this session: language=fr business_id=%s",
                        business_id,
                    )
            await session.start(
                agent=SalonAgent(instructions=instructions),
                room=ctx.room,
                room_options=room_io.RoomOptions(delete_room_on_close=True),
            )
            _trigger_first_turn(
                session, language="fr", business_use_case=business_use_case
            )
            shutdown_reason = await _wait_for_job_shutdown(ctx)
            logger.info(
                "Session shutdown received (fr): reason=%s",
                shutdown_reason or "unknown",
            )
        finally:
            await asyncio.shield(
                _finalize_session_cleanup(
                    userdata=userdata,
                    business_id=business_id,
                    session_tracker_id=str(userdata.get("session_tracker_id") or ""),
                    started_at=started_at,
                    call_channel=call_channel,
                    language="fr",
                    shutdown_reason=shutdown_reason
                    if "shutdown_reason" in locals()
                    else None,
                )
            )


if __name__ == "__main__":
    # LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET are read from the environment/.env
    try:
        _validate_runtime_requirements()
        cli.run_app(server)
    finally:
        flush_traces()
