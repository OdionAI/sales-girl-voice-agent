import json
import logging
import os
import re
import asyncio
import base64
import hashlib
import time
from typing import Any
import uuid
from urllib.parse import urlparse
from dotenv import load_dotenv

# Load env immediately so API clients can read the correct base URLs
load_dotenv()

from livekit.agents import (
    APIConnectOptions,
    EndpointingOptions,
    InterruptionOptions,
    NOT_GIVEN,
    AgentServer,
    AgentSession,
    JobContext,
    TurnHandlingOptions,
    cli,
    room_io,
)
from livekit.agents import llm, stt
from livekit.agents._exceptions import APIConnectionError, APIStatusError
from livekit.agents.llm import LLMStream
from livekit.agents.llm.tool_context import find_function_tools
from livekit.plugins import deepgram, google, groq, openai, silero

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
from agent.billing_hooks import (
    FAIL_CLOSED as BILLING_FAIL_CLOSED,
    HEARTBEAT_INTERVAL_SECONDS as BILLING_HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_MAX_FAILURES as BILLING_HEARTBEAT_MAX_FAILURES,
    authorize_call_start as authorize_billing_call_start,
    is_enabled as billing_hooks_enabled,
    report_call_usage as report_billing_call_usage,
    send_call_heartbeat as send_billing_call_heartbeat,
)
from agent.agent_config_api import get_runtime_config as get_agent_runtime_config
from agent.dynamic_tools import build_dynamic_http_tools
from agent.ops_api import (
    create_ticket as ops_create_ticket,
    get_account_overview as ops_get_account_overview,
    get_payment_summary as ops_get_payment_summary,
    get_recent_transactions as ops_get_recent_transactions,
    get_tariff_profile as ops_get_tariff_profile,
    get_vending_history as ops_get_vending_history,
    lookup_customer_account as ops_lookup_customer_account,
    search_business_knowledge as ops_search_business_knowledge,
)
from agent.odion_tts import OdionTTS
from agent.odion_stt import (
    DEFAULT_ODION_STT_BASE_URL,
    ODION_STT_REALTIME_ENDPOINTING_SILENCE_SECONDS,
    ODION_STT_REALTIME_MIN_SPEECH_SECONDS,
    ODION_STT_REALTIME_VAD_ACTIVATION_THRESHOLD,
    OdionSTT,
)
from agent.observability import flush_traces, trace_conversation_event
from agent.livekit_recording import (
    finalize_room_recording,
    is_recording_enabled,
    start_room_recording,
)
from agent.salon_agent import SalonAgent
from prompts.en import NIGERIAN_SPOKEN_STYLE_EN, SYSTEM_PROMPT_EN
from prompts.fr import SYSTEM_PROMPT_FR


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

VOICE_LAB_METRICS_TOPIC = "odion.voice_lab.metrics"


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
ALWAYS_ENABLED_RUNTIME_TOOLS = ("search_business_knowledge",)

SHARED_ODION_CATALOG_OWNER_BY_VOICE_ID = {
    "d270a5cec6914373b9deed1d1c3cbade": "mavinomichael@gmail.com",
    "46f5ac744a504023b93c6dd8ddd46ac6": "mavinomichael@gmail.com",
}


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
BUILTIN_RUNTIME_TOOL_NAMES = frozenset(
    str(getattr(tool, "info").name or "").strip()
    for tool in find_function_tools(SalonAgent)
    if getattr(getattr(tool, "info", None), "name", None)
)


class UsageMeter:
    def snapshot(self) -> dict[str, Any] | None:
        return None


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


def _append_recent_user_message(userdata: dict[str, Any], content: str) -> None:
    recent = userdata.setdefault("recent_user_messages", [])
    if not isinstance(recent, list):
        recent = []
        userdata["recent_user_messages"] = recent
    cleaned = str(content or "").strip()
    if not cleaned:
        return
    recent.append(cleaned)
    if len(recent) > 8:
        del recent[:-8]


def _tool_metadata_from_userdata(userdata: dict[str, Any]) -> dict[str, Any]:
    conversation_id = str(userdata.get("conversation_id") or "")
    session_id = str(userdata.get("session_id") or conversation_id)
    return {
        "client_id": str(userdata.get("client_id") or "sales-girl-internal"),
        "agent_id": str(
            userdata.get("agent_config_id")
            or userdata.get("agent_id")
            or AGENT_NAME
        ),
        "business_id": str(userdata.get("business_id") or ""),
        "business_use_case": str(userdata.get("business_use_case") or ""),
        "knowledge_base_ids": list(userdata.get("knowledge_base_ids") or []),
        "live_data_endpoint": str(userdata.get("live_data_endpoint") or ""),
        "conversation_id": conversation_id,
        "session_id": session_id,
        "end_user_id": str(userdata.get("end_user_id") or ""),
        "end_user_name": str(userdata.get("end_user_name") or ""),
        "caller_phone_e164": str(userdata.get("caller_phone_e164") or ""),
        "enabled_tool_names": list(userdata.get("enabled_tool_names") or []),
        "turn_index": int(userdata.get("turn_index", 0)),
        "last_user_transcript": str(userdata.get("last_user_transcript") or ""),
        "last_assistant_message": str(userdata.get("last_assistant_message") or ""),
        "timeline_event_index": int(userdata.get("timeline_event_index", 0)),
    }


def _assistant_claims_ticket_created(message: str) -> bool:
    normalized = str(message or "").strip().lower()
    if not normalized:
        return False
    patterns = (
        "i have created a ticket",
        "i created a ticket",
        "ticket has been created",
        "j'ai créé un ticket",
        "j’ai créé un ticket",
        "le ticket a été créé",
        "un ticket a été créé",
    )
    return any(pattern in normalized for pattern in patterns)


def _assistant_claims_ticket_failed(message: str) -> bool:
    normalized = str(message or "").strip().lower()
    if not normalized:
        return False
    patterns = (
        "ticket creation failed",
        "couldn't create the ticket",
        "could not create the ticket",
        "could not create a ticket",
        "unable to create the ticket",
        "unable to create a ticket",
        "there was an issue creating the ticket",
        "there is an issue creating the ticket",
        "issue creating the ticket",
        "problem creating the ticket",
        "sorry, there was an issue creating the ticket",
        "n'ai pas pu créer le ticket",
        "la création du ticket a échoué",
        "je suis désolé, la création du ticket a échoué",
        "je n'ai pas pu créer le ticket",
        "impossible de créer le ticket",
        "un problème est survenu lors de la création du ticket",
    )
    return any(pattern in normalized for pattern in patterns)


def _assistant_claims_ticket_updated(message: str) -> bool:
    normalized = str(message or "").strip().lower()
    if not normalized:
        return False
    patterns = (
        "i updated the ticket",
        "i have updated the ticket",
        "the ticket has been updated",
        "j'ai mis à jour le ticket",
        "j’ai mis à jour le ticket",
        "j'ai mis a jour le ticket",
        "j’ai mis a jour le ticket",
        "le ticket a été mis à jour",
        "le ticket a ete mis a jour",
    )
    return any(pattern in normalized for pattern in patterns)


def _should_skip_assistant_message_persist(userdata: dict[str, Any], content: str) -> bool:
    candidate = str(content or "").strip()
    if not candidate:
        return True

    lowered = candidate.lower()
    if len(candidate) <= 24 and (
        lowered in {"bonjour ! je", "bonjour! je", "bonjour ! j", "bonjour! j"}
        or re.match(r"^(bonjour|salut|hello|hi)\W+(je|j|i)\s*$", lowered)
    ):
        return True

    last_saved = str(userdata.get("last_persisted_assistant_content") or "").strip()
    if not last_saved:
        return False
    if candidate == last_saved:
        return True
    if len(candidate) < len(last_saved) and last_saved.startswith(candidate):
        return True
    return False


def _is_non_informative_ticket_reply(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return True
    trivial = {
        "ok",
        "okay",
        "no problem",
        "oui",
        "yes",
        "d'accord",
        "d’accord",
        "or",
        "dit",
    }
    return normalized in trivial or len(normalized) < 3


def _fallback_ticket_summary(userdata: dict[str, Any]) -> tuple[str, str]:
    recent = [
        str(item).strip()
        for item in list(userdata.get("recent_user_messages") or [])
        if not _is_non_informative_ticket_reply(str(item))
    ]
    combined = " ".join(recent[-4:]).strip()
    lowered = combined.lower()
    if "alat" in lowered:
        return (
            "ALAT App Issues",
            combined or "Customer reported issues while using the ALAT app.",
        )
    if "passeport" in lowered or "passport" in lowered:
        return (
            "Passport Support Request",
            combined or "Customer requested help related to a passport issue.",
        )
    title_source = recent[0] if recent else "Support Request"
    title = " ".join(title_source.replace('"', "").split()[:6]).strip() or "Support Request"
    if len(title) < 2:
        title = "Support Request"
    description = combined or str(userdata.get("last_user_transcript") or "").strip() or "Customer requested human follow-up."
    return title[:255], description


async def _reconcile_ticket_claim_if_needed(userdata: dict[str, Any], assistant_message: str) -> None:
    enabled = {
        str(name or "").strip()
        for name in list(userdata.get("enabled_tool_names") or [])
        if str(name or "").strip()
    }
    if "create_ticket" not in enabled:
        return

    current_turn = int(userdata.get("turn_index", 0))
    successful_turn = int(userdata.get("last_create_ticket_success_turn", -1))
    if successful_turn == current_turn:
        return

    assistant_message = str(assistant_message or "").strip()
    if not assistant_message:
        return
    if successful_turn >= 0 and _assistant_claims_ticket_updated(assistant_message):
        return

    should_reconcile = _assistant_claims_ticket_created(assistant_message)
    if not should_reconcile and _assistant_claims_ticket_failed(assistant_message):
        recent = " ".join(list(userdata.get("recent_user_messages") or [])[-4:]).lower()
        should_reconcile = any(
            phrase in recent
            for phrase in (
                "create a ticket",
                "create ticket",
                "créer un billet",
                "créer un ticket",
                "creer un ticket",
            )
        )
    if not should_reconcile:
        return

    title, description = _fallback_ticket_summary(userdata)
    logger.warning(
        "Reconciling missing ticket tool execution: business_id=%s conversation_id=%s turn=%s title=%s",
        userdata.get("business_id"),
        userdata.get("conversation_id"),
        current_turn,
        title,
    )
    result = await ops_create_ticket(
        title=title,
        description=description,
        issue_type="general",
        priority="high",
        requires_human=True,
        metadata=_tool_metadata_from_userdata(userdata),
    )
    if str(result.get("status") or "").lower() != "failed":
        userdata["last_create_ticket_success_turn"] = current_turn
        userdata["last_create_ticket_result"] = result
        _persist_session_event_async(
            userdata,
            event_type="tool_call",
            role="tool",
            title="create_ticket_fallback",
            body=_summarize_tool_output(result),
            payload={
                "tool_name": "create_ticket_fallback",
                "tool_result": result,
                "event_index": int(userdata.get("timeline_event_index", 0)),
                "turn_index": current_turn,
            },
        )
    else:
        logger.error(
            "Fallback ticket reconciliation failed: business_id=%s conversation_id=%s detail=%s",
            userdata.get("business_id"),
            userdata.get("conversation_id"),
            result.get("detail") or result.get("message"),
        )


async def _drain_background_tasks(userdata: dict[str, Any]) -> None:
    pending = list(userdata.get("background_tasks") or [])
    if not pending:
        return
    results = await asyncio.gather(*pending, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            logger.error("Background persistence task failed: %s", result)


def _billing_idempotency_key(*, session_id: str, duration_seconds: int) -> str:
    digest = hashlib.sha256(
        f"{session_id}:{max(0, int(duration_seconds))}".encode("utf-8")
    ).hexdigest()[:16]
    return f"hb-{max(0, int(duration_seconds))}-{digest}"


def _billing_usage_payload(userdata: dict[str, Any]) -> dict[str, Any] | None:
    meter = userdata.get("usage_meter")
    snapshot = getattr(meter, "snapshot", None)
    if not callable(snapshot):
        return None
    try:
        value = snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Billing usage meter snapshot failed: %s", exc)
        return None
    return value if isinstance(value, dict) and value else None


def _billing_session_id(userdata: dict[str, Any]) -> str:
    return str(userdata.get("session_tracker_id") or userdata.get("session_id") or "")


def _billing_bypass_reason(userdata: dict[str, Any], call_channel: str) -> str:
    # Platform-owned help/guide sessions are funded by the platform and never
    # charged to the viewing business wallet, so callers are never blocked by
    # low airtime when asking the SalesGirl guide for help.
    if str(userdata.get("session_kind") or "").strip().lower() == "help":
        return "platform_help_session"
    runtime_overrides = userdata.get("runtime_overrides")
    if (
        call_channel == "web"
        and isinstance(runtime_overrides, dict)
        and bool(runtime_overrides)
    ):
        return "voice_lab_runtime_overrides"
    entry_surface = str(userdata.get("entry_surface") or "").strip().lower()
    session_owner = str(userdata.get("session_owner") or "").strip().lower()
    if entry_surface == "aicc_inbound" or session_owner == "sip_lab":
        return "aicc_sip_lab_session"
    bypass_agent_ids = {
        item.strip()
        for item in str(os.getenv("BILLING_BYPASS_AGENT_CONFIG_IDS") or "").split(",")
        if item.strip()
    }
    agent_config_id = str(userdata.get("agent_config_id") or "").strip()
    if agent_config_id and agent_config_id in bypass_agent_ids:
        return "agent_billing_bypass"
    return ""


async def _authorize_billing_start_or_raise(
    *,
    userdata: dict[str, Any],
    business_id: str,
    call_channel: str,
) -> None:
    billing_bypass_reason = _billing_bypass_reason(userdata, call_channel)
    if billing_bypass_reason:
        userdata["billing_bypassed"] = True
        userdata["billing_bypass_reason"] = billing_bypass_reason
        logger.info(
            "Billing authorization bypassed: reason=%s business_id=%s conversation_id=%s",
            billing_bypass_reason,
            business_id,
            userdata.get("conversation_id"),
        )
        return

    if not billing_hooks_enabled(business_id):
        if BILLING_FAIL_CLOSED:
            raise RuntimeError("Billing hooks are not configured.")
        logger.info("Billing hooks disabled for this session.")
        return

    result = await authorize_billing_call_start(
        conversation_id=str(userdata.get("conversation_id") or ""),
        end_user_id=str(userdata.get("end_user_id") or ""),
        channel=call_channel,
        business_id=business_id,
    )
    status = str(result.get("status") or "").lower()
    if status in {"disabled", "failed"}:
        detail = str(result.get("detail") or status or "billing authorization failed")
        if BILLING_FAIL_CLOSED:
            raise RuntimeError(detail)
        logger.warning("Billing authorization degraded open: %s", detail)
        return
    if not bool(result.get("authorized")):
        raise RuntimeError("Insufficient wallet balance to start this call.")
    userdata["billing_authorized"] = True


async def _shutdown_session_for_billing(
    *,
    session: AgentSession,
    ctx: JobContext,
    userdata: dict[str, Any],
    reason: str,
    result: dict[str, Any] | None = None,
) -> None:
    if userdata.get("billing_shutdown_requested"):
        return
    userdata["billing_shutdown_requested"] = True
    logger.warning(
        "Ending session due to billing: reason=%s session_id=%s result=%s",
        reason,
        _billing_session_id(userdata),
        result or {},
    )
    _persist_session_event_async(
        userdata,
        event_type="billing_exhausted",
        role="system",
        title="Billing balance exhausted",
        body="The call ended because the wallet balance reached zero.",
        payload=result or {},
    )
    try:
        session.shutdown(drain=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Billing shutdown could not drain session: %s", exc)
    try:
        ctx.shutdown(reason)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Billing shutdown could not signal job shutdown: %s", exc)
    try:
        deleted = ctx.delete_room()
        if hasattr(deleted, "__await__"):
            await deleted
    except Exception as exc:  # noqa: BLE001
        logger.warning("Billing shutdown room delete fallback failed: %s", exc)


async def _billing_heartbeat_loop(
    *,
    session: AgentSession,
    ctx: JobContext,
    userdata: dict[str, Any],
    business_id: str,
    started_at: Any,
    call_channel: str,
) -> None:
    failures = 0
    while True:
        await asyncio.sleep(BILLING_HEARTBEAT_INTERVAL_SECONDS)
        duration = int(max(0, (conv_api_utcnow() - started_at).total_seconds()))
        if duration <= 0:
            continue
        billing_session_id = _billing_session_id(userdata)
        result = await send_billing_call_heartbeat(
            conversation_id=str(userdata.get("conversation_id") or ""),
            session_id=billing_session_id,
            end_user_id=str(userdata.get("end_user_id") or ""),
            duration_seconds=duration,
            idempotency_key=_billing_idempotency_key(
                session_id=billing_session_id,
                duration_seconds=duration,
            ),
            channel=call_channel,
            business_id=business_id,
        )
        status = str(result.get("status") or "").lower()
        if status in {"disabled", "failed"}:
            failures += 1
            logger.warning(
                "Billing heartbeat failed: failures=%s/%s session_id=%s detail=%s",
                failures,
                BILLING_HEARTBEAT_MAX_FAILURES,
                billing_session_id,
                result.get("detail"),
            )
            if BILLING_FAIL_CLOSED and failures >= BILLING_HEARTBEAT_MAX_FAILURES:
                await _shutdown_session_for_billing(
                    session=session,
                    ctx=ctx,
                    userdata=userdata,
                    reason="billing_heartbeat_failed",
                    result=result,
                )
                return
            continue

        failures = 0
        userdata["last_billing_heartbeat"] = result
        if bool(result.get("should_end_call")):
            await _shutdown_session_for_billing(
                session=session,
                ctx=ctx,
                userdata=userdata,
                reason="billing_exhausted",
                result=result,
            )
            return


def _start_billing_heartbeat(
    *,
    session: AgentSession,
    ctx: JobContext,
    userdata: dict[str, Any],
    business_id: str,
    started_at: Any,
    call_channel: str,
) -> None:
    if userdata.get("billing_bypassed"):
        return
    if not billing_hooks_enabled(business_id):
        return
    if userdata.get("billing_heartbeat_task"):
        return
    userdata["billing_heartbeat_task"] = asyncio.create_task(
        _billing_heartbeat_loop(
            session=session,
            ctx=ctx,
            userdata=userdata,
            business_id=business_id,
            started_at=started_at,
            call_channel=call_channel,
        )
    )


async def _stop_billing_heartbeat(userdata: dict[str, Any]) -> None:
    task = userdata.pop("billing_heartbeat_task", None)
    if not task or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("Billing heartbeat task ended with error: %s", exc)


async def _report_billing_final_usage(
    *,
    userdata: dict[str, Any],
    business_id: str,
    duration_seconds: int,
    call_channel: str,
) -> None:
    if userdata.get("billing_final_reported"):
        return
    userdata["billing_final_reported"] = True
    if userdata.get("billing_bypassed"):
        return
    if not billing_hooks_enabled(business_id):
        return
    result = await report_billing_call_usage(
        conversation_id=str(userdata.get("conversation_id") or ""),
        session_id=_billing_session_id(userdata),
        end_user_id=str(userdata.get("end_user_id") or ""),
        duration_seconds=max(0, int(duration_seconds)),
        channel=call_channel,
        business_id=business_id,
        usage=_billing_usage_payload(userdata),
    )
    if str(result.get("status") or "").lower() == "failed":
        logger.error(
            "Final billing report failed: session_id=%s detail=%s http_status=%s",
            _billing_session_id(userdata),
            result.get("detail"),
            result.get("http_status"),
        )
    else:
        userdata["last_billing_report"] = result


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

        await _stop_billing_heartbeat(userdata)
        await _report_billing_final_usage(
            userdata=userdata,
            business_id=business_id,
            duration_seconds=duration,
            call_channel=call_channel,
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


async def _start_session_recording_capture(
    *,
    ctx: JobContext,
    userdata: dict[str, Any],
    business_id: str,
    session_tracker_id: str,
    started_at: Any,
) -> None:
    if not session_tracker_id or not is_recording_enabled():
        return

    logger.info(
        "Attempting room recording start: session_id=%s room=%s",
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


REQUIRE_VERIFIED_PHONE = os.getenv("REQUIRE_VERIFIED_PHONE", "true").lower() == "true"
CONVERSATION_SERVICE_REQUIRED = (
    os.getenv("CONVERSATION_SERVICE_REQUIRED", "true").lower() == "true"
)
ENABLE_ODION_TTS_EN = os.getenv("ENABLE_ODION_TTS_EN", "true").lower() == "true"
ENABLE_ODION_TTS_FR = os.getenv("ENABLE_ODION_TTS_FR", "false").lower() == "true"
ODION_TTS_EXPERIMENT_OWNER_ID = str(
    os.getenv("ODION_TTS_EXPERIMENT_OWNER_ID") or ""
).strip()
ODION_TTS_EXPERIMENT_VOICE_ID = str(
    os.getenv("ODION_TTS_EXPERIMENT_VOICE_ID") or ""
).strip()
FORCE_ODION_TTS_EXPERIMENT_VOICE = (
    os.getenv("FORCE_ODION_TTS_EXPERIMENT_VOICE", "false").lower() == "true"
)
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
    0.3,
    min_value=0.1,
)
TURN_MAX_ENDPOINTING_DELAY = _float_env(
    "TURN_MAX_ENDPOINTING_DELAY",
    0.65,
    min_value=0.2,
)
if TURN_MAX_ENDPOINTING_DELAY < TURN_MIN_ENDPOINTING_DELAY:
    logger.warning(
        "TURN_MAX_ENDPOINTING_DELAY < TURN_MIN_ENDPOINTING_DELAY; aligning max to min."
    )
    TURN_MAX_ENDPOINTING_DELAY = TURN_MIN_ENDPOINTING_DELAY

TURN_MIN_INTERRUPTION_DURATION = _float_env(
    "TURN_MIN_INTERRUPTION_DURATION",
    0.1,
    min_value=0.1,
)
TURN_AEC_WARMUP_DURATION = _float_env(
    "TURN_AEC_WARMUP_DURATION",
    0.1,
    min_value=0.0,
)

GOOGLE_LLM_MODEL_DEFAULT = (
    str(os.getenv("GOOGLE_LLM_MODEL_DEFAULT") or "gemini-3-flash-preview").strip()
    or "gemini-3-flash-preview"
)
GOOGLE_LLM_MODEL_EN = (
    str(os.getenv("GOOGLE_LLM_MODEL_EN") or GOOGLE_LLM_MODEL_DEFAULT).strip()
    or GOOGLE_LLM_MODEL_DEFAULT
)
GOOGLE_LLM_MODEL_FR = (
    str(os.getenv("GOOGLE_LLM_MODEL_FR") or GOOGLE_LLM_MODEL_DEFAULT).strip()
    or GOOGLE_LLM_MODEL_DEFAULT
)
GOOGLE_LLM_BACKUP_MODEL_DEFAULT = (
    str(os.getenv("GOOGLE_LLM_BACKUP_MODEL_DEFAULT") or "gemini-3.1-flash-lite").strip()
    or "gemini-3.1-flash-lite"
)
GOOGLE_LLM_BACKUP_MODEL_EN = (
    str(os.getenv("GOOGLE_LLM_BACKUP_MODEL_EN") or GOOGLE_LLM_BACKUP_MODEL_DEFAULT).strip()
    or GOOGLE_LLM_BACKUP_MODEL_DEFAULT
)
GOOGLE_LLM_BACKUP_MODEL_FR = (
    str(os.getenv("GOOGLE_LLM_BACKUP_MODEL_FR") or GOOGLE_LLM_BACKUP_MODEL_DEFAULT).strip()
    or GOOGLE_LLM_BACKUP_MODEL_DEFAULT
)

LLM_PROVIDER = str(os.getenv("LLM_PROVIDER") or "google").strip().lower() or "google"
GROQ_LLM_MODEL_DEFAULT = (
    str(
        os.getenv("GROQ_LLM_MODEL_DEFAULT")
        or "meta-llama/llama-4-scout-17b-16e-instruct"
    ).strip()
    or "meta-llama/llama-4-scout-17b-16e-instruct"
)
GROQ_LLM_MODEL_EN = (
    str(os.getenv("GROQ_LLM_MODEL_EN") or GROQ_LLM_MODEL_DEFAULT).strip()
    or GROQ_LLM_MODEL_DEFAULT
)
GROQ_LLM_MODEL_FR = (
    str(os.getenv("GROQ_LLM_MODEL_FR") or GROQ_LLM_MODEL_DEFAULT).strip()
    or GROQ_LLM_MODEL_DEFAULT
)
GROQ_LLM_BACKUP_MODEL_DEFAULT = (
    str(os.getenv("GROQ_LLM_BACKUP_MODEL_DEFAULT") or "llama-3.3-70b-versatile").strip()
    or "llama-3.3-70b-versatile"
)
GROQ_LLM_BACKUP_MODEL_EN = (
    str(os.getenv("GROQ_LLM_BACKUP_MODEL_EN") or GROQ_LLM_BACKUP_MODEL_DEFAULT).strip()
    or GROQ_LLM_BACKUP_MODEL_DEFAULT
)
GROQ_LLM_BACKUP_MODEL_FR = (
    str(os.getenv("GROQ_LLM_BACKUP_MODEL_FR") or GROQ_LLM_BACKUP_MODEL_DEFAULT).strip()
    or GROQ_LLM_BACKUP_MODEL_DEFAULT
)
QWEN_LLM_MODEL_DEFAULT = (
    str(os.getenv("QWEN_LLM_MODEL_DEFAULT") or "qwen3.8_27b").strip()
    or "qwen3.8_27b"
)
QWEN_LLM_MODEL_EN = (
    str(os.getenv("QWEN_LLM_MODEL_EN") or QWEN_LLM_MODEL_DEFAULT).strip()
    or QWEN_LLM_MODEL_DEFAULT
)
QWEN_LLM_MODEL_FR = (
    str(os.getenv("QWEN_LLM_MODEL_FR") or QWEN_LLM_MODEL_DEFAULT).strip()
    or QWEN_LLM_MODEL_DEFAULT
)


def _is_llm_model_unavailable_error(error: Exception) -> bool:
    if not isinstance(error, APIStatusError):
        return False
    body = str(getattr(error, "body", "") or error).lower()
    return error.status_code == 404 and (
        "no longer available" in body
        or "model" in body and "not available" in body
        or "model not found" in body
        or "not_found" in body
    )


def _is_google_model_unavailable_error(error: Exception) -> bool:
    return _is_llm_model_unavailable_error(error)


def _groq_llm_kwargs_for_model(model: str) -> dict[str, Any]:
    lowered = str(model or "").strip().lower()
    if "qwen" in lowered:
        return {"reasoning_effort": "none"}
    return {}


class FallbackGoogleLLM(llm.LLM):
    def __init__(
        self,
        *,
        primary_model: str,
        backup_model: str = "",
    ) -> None:
        super().__init__()
        self._primary_model = str(primary_model or "").strip()
        self._backup_model = str(backup_model or "").strip()
        self._active_model = self._primary_model
        self._primary = google.LLM(model=self._primary_model)
        self._backup = (
            google.LLM(model=self._backup_model)
            if self._backup_model and self._backup_model != self._primary_model
            else None
        )

    @property
    def model(self) -> str:
        return self._active_model or self._primary_model or "unknown"

    @property
    def provider(self) -> str:
        return "google"

    def prewarm(self, *args: Any, **kwargs: Any) -> None:
        self._primary.prewarm(*args, **kwargs)
        if self._backup:
            self._backup.prewarm(*args, **kwargs)

    async def aclose(self) -> None:
        await self._primary.aclose()
        if self._backup:
            await self._backup.aclose()

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = APIConnectOptions(),
        parallel_tool_calls=NOT_GIVEN,
        tool_choice=NOT_GIVEN,
        response_format=NOT_GIVEN,
        extra_kwargs=NOT_GIVEN,
    ) -> LLMStream:
        return _FallbackGoogleLLMStream(
            self,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
            parallel_tool_calls=parallel_tool_calls,
            tool_choice=tool_choice,
            response_format=response_format,
            extra_kwargs=extra_kwargs,
        )


class _FallbackGoogleLLMStream(llm.LLMStream):
    def __init__(
        self,
        llm_v: FallbackGoogleLLM,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        conn_options,
        parallel_tool_calls,
        tool_choice,
        response_format,
        extra_kwargs,
    ) -> None:
        super().__init__(llm_v, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._routing_llm = llm_v
        self._parallel_tool_calls = parallel_tool_calls
        self._tool_choice = tool_choice
        self._response_format = response_format
        self._extra_kwargs = extra_kwargs

    async def _run(self) -> None:
        attempts: list[tuple[google.LLM, str]] = [
            (self._routing_llm._primary, self._routing_llm._primary_model)
        ]
        if self._routing_llm._backup:
            attempts.append((self._routing_llm._backup, self._routing_llm._backup_model))

        last_error: Exception | None = None
        for index, (provider_llm, model_name) in enumerate(attempts):
            self._routing_llm._active_model = model_name
            stream = provider_llm.chat(
                chat_ctx=self._chat_ctx,
                tools=self._tools,
                conn_options=self._conn_options,
                parallel_tool_calls=self._parallel_tool_calls,
                tool_choice=self._tool_choice,
                response_format=self._response_format,
                extra_kwargs=self._extra_kwargs,
            )
            try:
                async with stream:
                    async for chunk in stream:
                        self._event_ch.send_nowait(chunk)
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                has_backup = index + 1 < len(attempts)
                if has_backup and _is_google_model_unavailable_error(exc):
                    logger.warning(
                        "Primary Gemini model unavailable; switching to backup model. primary=%s backup=%s error=%s",
                        model_name,
                        attempts[index + 1][1],
                        exc,
                    )
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise APIConnectionError("No Gemini model could be selected for this request.")


class FallbackGroqLLM(llm.LLM):
    def __init__(
        self,
        *,
        primary_model: str,
        backup_model: str = "",
    ) -> None:
        super().__init__()
        self._primary_model = str(primary_model or "").strip()
        self._backup_model = str(backup_model or "").strip()
        self._active_model = self._primary_model
        self._primary = groq.LLM(
            model=self._primary_model,
            **_groq_llm_kwargs_for_model(self._primary_model),
        )
        self._backup = (
            groq.LLM(
                model=self._backup_model,
                **_groq_llm_kwargs_for_model(self._backup_model),
            )
            if self._backup_model and self._backup_model != self._primary_model
            else None
        )

    @property
    def model(self) -> str:
        return self._active_model or self._primary_model or "unknown"

    @property
    def provider(self) -> str:
        return "groq"

    def prewarm(self, *args: Any, **kwargs: Any) -> None:
        self._primary.prewarm(*args, **kwargs)
        if self._backup:
            self._backup.prewarm(*args, **kwargs)

    async def aclose(self) -> None:
        await self._primary.aclose()
        if self._backup:
            await self._backup.aclose()

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = APIConnectOptions(),
        parallel_tool_calls=NOT_GIVEN,
        tool_choice=NOT_GIVEN,
        response_format=NOT_GIVEN,
        extra_kwargs=NOT_GIVEN,
    ) -> LLMStream:
        return _FallbackGroqLLMStream(
            self,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
            parallel_tool_calls=parallel_tool_calls,
            tool_choice=tool_choice,
            response_format=response_format,
            extra_kwargs=extra_kwargs,
        )


class _FallbackGroqLLMStream(llm.LLMStream):
    def __init__(
        self,
        llm_v: FallbackGroqLLM,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        conn_options,
        parallel_tool_calls,
        tool_choice,
        response_format,
        extra_kwargs,
    ) -> None:
        super().__init__(llm_v, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._routing_llm = llm_v
        self._parallel_tool_calls = parallel_tool_calls
        self._tool_choice = tool_choice
        self._response_format = response_format
        self._extra_kwargs = extra_kwargs

    async def _run(self) -> None:
        attempts: list[tuple[groq.LLM, str]] = [
            (self._routing_llm._primary, self._routing_llm._primary_model)
        ]
        if self._routing_llm._backup:
            attempts.append((self._routing_llm._backup, self._routing_llm._backup_model))

        last_error: Exception | None = None
        for index, (provider_llm, model_name) in enumerate(attempts):
            self._routing_llm._active_model = model_name
            stream = provider_llm.chat(
                chat_ctx=self._chat_ctx,
                tools=self._tools,
                conn_options=self._conn_options,
                parallel_tool_calls=self._parallel_tool_calls,
                tool_choice=self._tool_choice,
                response_format=self._response_format,
                extra_kwargs=self._extra_kwargs,
            )
            try:
                async with stream:
                    async for chunk in stream:
                        self._event_ch.send_nowait(chunk)
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                has_backup = index + 1 < len(attempts)
                if has_backup and _is_llm_model_unavailable_error(exc):
                    logger.warning(
                        "Primary Groq model unavailable; switching to backup model. primary=%s backup=%s error=%s",
                        model_name,
                        attempts[index + 1][1],
                        exc,
                    )
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise APIConnectionError("No Groq model could be selected for this request.")


def _openai_compatible_base_url(value: str) -> str:
    base_url = str(value or "").strip().rstrip("/")
    suffix = "/chat/completions"
    if base_url.endswith(suffix):
        return base_url[: -len(suffix)]
    return base_url


def _runtime_override_truthy(value: Any, *, default: bool = False) -> bool:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _build_llm_for_language(
    *, language: str, userdata: dict[str, Any] | None = None
) -> llm.LLM:
    lang = str(language or "").strip().lower()
    overrides = _normalize_runtime_overrides((userdata or {}).get("runtime_overrides"))
    override_provider = str(overrides.get("llm_provider") or "").strip().lower()
    provider = override_provider or LLM_PROVIDER

    if provider in {"qwen", "qwen_openai", "openai", "openai_compatible", "custom"}:
        model = (
            str(overrides.get("llm_model") or "").strip()
            or (QWEN_LLM_MODEL_FR if lang == "fr" else QWEN_LLM_MODEL_EN)
        )
        endpoint = (
            str(overrides.get("llm_base_url") or "").strip()
            or str(os.getenv("QWEN_LLM_BASE_URL") or "").strip()
        )
        base_url = _openai_compatible_base_url(endpoint)
        if not base_url:
            raise ValueError("QWEN_LLM_BASE_URL is required for the Qwen LLM provider")
        api_key = (
            str(overrides.get("llm_api_key") or "").strip()
            or str(os.getenv("QWEN_LLM_API_KEY") or "").strip()
            or "EMPTY"
        )
        disable_thinking = _runtime_override_truthy(
            overrides.get("llm_disable_thinking"),
            default=_runtime_override_truthy(
                os.getenv("QWEN_LLM_DISABLE_THINKING"),
                default=True,
            ),
        )
        logger.info(
            "Using Qwen OpenAI-compatible LLM for %s session: model=%s base_url=%s thinking=%s runtime_override=%s",
            "French" if lang == "fr" else "English",
            model,
            base_url,
            "disabled" if disable_thinking else "enabled",
            bool(override_provider or overrides.get("llm_base_url")),
        )
        return openai.LLM(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=0,
            extra_body={
                "chat_template_kwargs": {
                    "enable_thinking": not disable_thinking,
                }
            },
        )

    if provider == "groq":
        primary_model = (
            str(overrides.get("llm_model") or "").strip()
            or (GROQ_LLM_MODEL_FR if lang == "fr" else GROQ_LLM_MODEL_EN)
        )
        backup_model = "" if override_provider else (
            GROQ_LLM_BACKUP_MODEL_FR if lang == "fr" else GROQ_LLM_BACKUP_MODEL_EN
        )
        logger.info(
            "Using Groq LLM for %s session: primary=%s backup=%s",
            "French" if lang == "fr" else "English",
            primary_model,
            backup_model,
        )
        return FallbackGroqLLM(
            primary_model=primary_model,
            backup_model=backup_model,
        )

    primary_model = (
        str(overrides.get("llm_model") or "").strip()
        or (GOOGLE_LLM_MODEL_FR if lang == "fr" else GOOGLE_LLM_MODEL_EN)
    )
    backup_model = "" if override_provider else (
        GOOGLE_LLM_BACKUP_MODEL_FR if lang == "fr" else GOOGLE_LLM_BACKUP_MODEL_EN
    )
    logger.info(
        "Using Google LLM for %s session: primary=%s backup=%s",
        "French" if lang == "fr" else "English",
        primary_model,
        backup_model,
    )
    return FallbackGoogleLLM(
        primary_model=primary_model,
        backup_model=backup_model,
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


def _job_metadata_from_ctx(ctx: JobContext) -> dict[str, Any]:
    job = getattr(ctx, "job", None)
    candidates = [
        getattr(job, "metadata", None),
        getattr(getattr(job, "agent_dispatch", None), "metadata", None),
        getattr(getattr(job, "dispatch", None), "metadata", None),
    ]
    for raw in candidates:
        metadata_raw = str(raw or "").strip()
        if not metadata_raw:
            continue
        try:
            payload = json.loads(metadata_raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


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


def _normalize_tts_mode(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode in {"default_voice", "cloned_voice", "auto"}:
        return mode
    return "auto"


def _normalize_tts_endpoint(value: str) -> str:
    endpoint = str(value or "").strip()
    if not endpoint:
        return ""
    try:
        parsed = urlparse(endpoint)
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return endpoint.rstrip("/")


_RUNTIME_OVERRIDE_KEYS = (
    "stt_provider",
    "stt_model",
    "stt_base_url",
    "stt_transport",
    "tts_provider",
    "tts_model",
    "tts_base_url",
    "tts_api_key",
    "tts_transport",
    "tts_mode",
    "tts_voice_id",
    "tts_owner_id",
    "tts_language_hint",
    "tts_seed",
    "tts_initial_codec_chunk_frames",
    "tts_stream_first_chunk_bytes",
    "tts_stream_chunk_bytes",
    "tts_http_chunk_bytes",
    "tts_initial_buffer_ms",
    "llm_provider",
    "llm_model",
    "llm_base_url",
    "llm_api_key",
    "llm_disable_thinking",
)


def _normalize_runtime_overrides(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, str] = {}
    for key in _RUNTIME_OVERRIDE_KEYS:
        value = str(raw.get(key) or "").strip()
        if value:
            normalized[key] = value
    return normalized


WEB_METADATA_WAIT_SECONDS = 3


def _has_remote_participants(ctx: JobContext) -> bool:
    room = getattr(ctx, "room", None)
    participants = getattr(room, "remote_participants", None)
    return bool(participants)


def _extract_tts_overrides_from_ctx(ctx: JobContext) -> dict[str, Any]:
    room = getattr(ctx, "room", None)
    participants = getattr(room, "remote_participants", None)
    if not participants:
        return {}

    values = participants.values() if hasattr(participants, "values") else participants
    for participant in values:
        metadata_raw = str(getattr(participant, "metadata", "") or "").strip()
        if not metadata_raw:
            continue
        try:
            payload = json.loads(metadata_raw)
        except json.JSONDecodeError:
            continue
        overrides: dict[str, Any] = {
            "tts_endpoint": _normalize_tts_endpoint(payload.get("tts_endpoint") or ""),
            "tts_mode": _normalize_tts_mode(payload.get("tts_mode") or ""),
            "tts_owner_id": str(payload.get("tts_owner_id") or "").strip(),
            "tts_voice_id": str(payload.get("tts_voice_id") or "").strip(),
            "tts_language_hint": str(payload.get("tts_language_hint") or "").strip(),
            "tts_seed": str(payload.get("tts_seed") or "").strip(),
        }
        runtime_overrides = _normalize_runtime_overrides(payload.get("runtime_overrides"))
        if runtime_overrides:
            overrides["runtime_overrides"] = runtime_overrides
        return overrides
    return {}


def _extract_session_extras_from_ctx(ctx: JobContext) -> dict[str, Any]:
    """Read optional session-scoped extras from participant metadata.

    Used by platform-owned shared agents (for example the Help Center guide)
    that live on one business but must be aware of the *viewing* account.
    ``guest_context`` is a pre-formatted, human-readable block injected into the
    agent instructions; ``session_kind`` marks platform sessions (for example
    ``help``) that should not bill the room's business wallet.
    """
    room = getattr(ctx, "room", None)
    participants = getattr(room, "remote_participants", None)
    if not participants:
        return {}

    values = participants.values() if hasattr(participants, "values") else participants
    for participant in values:
        metadata_raw = str(getattr(participant, "metadata", "") or "").strip()
        if not metadata_raw:
            continue
        try:
            payload = json.loads(metadata_raw)
        except json.JSONDecodeError:
            continue
        extras: dict[str, Any] = {}
        guest_context = str(payload.get("guest_context") or "").strip()
        if guest_context:
            extras["guest_context"] = guest_context[:4000]
        session_kind = str(payload.get("session_kind") or "").strip().lower()
        if session_kind:
            extras["session_kind"] = session_kind
        if extras:
            return extras
    return {}


# Extract participant identity and related room context from the LiveKit job.
def _participant_identity_from_ctx(
    ctx: JobContext,
) -> tuple[str, str, str, str, str, str, str, dict[str, str]]:
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
            {},
        )

    # First preference: encoded phone in room name (always available at session bootstrap)
    phone_from_room = _phone_from_room_name(room_name)
    if phone_from_room:
        return phone_from_room, "voice", fallback_business_id, "", "", "", "", {}

    # Fallback: read remote participant metadata / identity
    participants = getattr(room, "remote_participants", None)
    if not participants:
        return "", "voice", fallback_business_id, "", "", "", "", {}

    values = participants.values() if hasattr(participants, "values") else participants
    for participant in values:
        metadata_business_id = ""
        metadata_config_agent_id = ""
        metadata_configured_agent_name = ""
        metadata_end_user_name = ""
        metadata_tts_endpoint = ""
        metadata_runtime_overrides: dict[str, str] = {}
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
                metadata_runtime_overrides = _normalize_runtime_overrides(
                    payload.get("runtime_overrides")
                )
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
                            metadata_runtime_overrides,
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
                        metadata_tts_endpoint,
                        metadata_runtime_overrides,
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
                metadata_tts_endpoint,
                metadata_runtime_overrides,
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
                    metadata_tts_endpoint,
                    metadata_runtime_overrides,
                )

    return "", "voice", fallback_business_id, "", "", "", "", {}


async def _init_session_userdata(ctx: JobContext, language: str) -> dict[str, Any]:
    room_name = _room_name_from_ctx(ctx)
    job_metadata = _job_metadata_from_ctx(ctx)
    stable_session_id = _stable_id(room_name, prefix="sid", max_len=120)
    (
        end_user_id,
        identity_type,
        business_id,
        config_agent_id,
        configured_agent_name,
        end_user_name,
        tts_endpoint,
        identity_runtime_overrides,
    ) = _participant_identity_from_ctx(ctx)
    job_runtime_overrides = _normalize_runtime_overrides(
        job_metadata.get("runtime_overrides")
    )
    participant_overrides = _extract_tts_overrides_from_ctx(ctx)
    runtime_overrides = {
        **job_runtime_overrides,
        **identity_runtime_overrides,
        **participant_overrides.get("runtime_overrides", {}),
    }
    tts_endpoint = (
        participant_overrides.get("tts_endpoint")
        or _normalize_tts_endpoint(job_metadata.get("tts_endpoint") or "")
        or tts_endpoint
    )

    needs_identity = REQUIRE_VERIFIED_PHONE and not end_user_id
    needs_web_metadata = (
        identity_type == "web"
        and not runtime_overrides
        and not _has_remote_participants(ctx)
    )
    if needs_identity or needs_web_metadata:
        try:
            # In web flows, participant metadata/identity can arrive slightly after job start.
            wait_timeout = 12 if needs_identity else WEB_METADATA_WAIT_SECONDS
            await asyncio.wait_for(ctx.wait_for_participant(), timeout=wait_timeout)
            (
                end_user_id,
                identity_type,
                business_id,
                config_agent_id,
                configured_agent_name,
                end_user_name,
                tts_endpoint,
                identity_runtime_overrides,
            ) = _participant_identity_from_ctx(ctx)
            participant_overrides = _extract_tts_overrides_from_ctx(ctx)
            runtime_overrides = {
                **job_runtime_overrides,
                **identity_runtime_overrides,
                **participant_overrides.get("runtime_overrides", {}),
            }
            tts_endpoint = (
                participant_overrides.get("tts_endpoint")
                or _normalize_tts_endpoint(job_metadata.get("tts_endpoint") or "")
                or tts_endpoint
            )
            logger.info(
                "Retried participant identity after join: end_user_id=%s type=%s business_id=%s config_agent_id=%s configured_name=%s end_user_name=%s tts_endpoint=%s runtime_overrides=%s",
                end_user_id,
                identity_type,
                business_id,
                config_agent_id,
                configured_agent_name,
                end_user_name,
                tts_endpoint,
                runtime_overrides,
            )
        except RuntimeError as exc:
            # Some jobs can reach here before room connection is established.
            logger.warning("Could not wait for participant yet: %s", exc)
        except asyncio.TimeoutError:
            logger.warning(
                "Timed out waiting for participant before metadata extraction."
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
        "runtime_overrides": runtime_overrides,
        "entry_surface": str(job_metadata.get("entry_surface") or "").strip(),
        "session_owner": str(job_metadata.get("owner") or "").strip(),
        "route_number": str(job_metadata.get("route_number") or "").strip(),
        "tts_mode": runtime_overrides.get("tts_mode") or "auto",
        "tts_owner_id": runtime_overrides.get("tts_owner_id") or "",
        "tts_voice_id": runtime_overrides.get("tts_voice_id") or "",
        "tts_language_hint": runtime_overrides.get("tts_language_hint") or "",
        "tts_seed": runtime_overrides.get("tts_seed") or "",
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
        "guest_context": "",
        "session_kind": "",
        "usage_meter": UsageMeter(),
    } | participant_overrides | _extract_session_extras_from_ctx(ctx)


def _wire_session_timeline(
    session: AgentSession,
    userdata: dict[str, Any],
    *,
    room: Any | None = None,
) -> None:
    voice_lab_metrics_enabled = room is not None and bool(
        userdata.get("runtime_overrides")
        or str(userdata.get("entry_surface") or "").strip().lower() == "voice_lab"
    )

    def _publish_voice_lab_metric(event: str, **values: Any) -> None:
        if not voice_lab_metrics_enabled:
            return
        turn_id = str(
            values.pop("turn_id", "")
            or userdata.get("voice_lab_active_turn_id")
            or ""
        ).strip()
        turn_index = int(
            values.pop("turn_index", 0)
            or userdata.get("voice_lab_active_turn_index")
            or 0
        )
        if not turn_id or turn_index <= 0:
            return
        payload = {
            "type": "odion.voice_lab.metric",
            "event": event,
            "turn_id": turn_id,
            "turn_index": turn_index,
            "ts_ms": int(values.pop("ts_ms", 0) or time.time() * 1000),
            **values,
        }

        async def _publish() -> None:
            try:
                await room.local_participant.publish_data(
                    json.dumps(payload),
                    reliable=True,
                    topic=VOICE_LAB_METRICS_TOPIC,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not publish Voice Lab metric event=%s turn=%s: %s",
                    event,
                    turn_index,
                    exc,
                )

        _track_background_task(userdata, _publish())

    async def _update_live_agent_instructions(instructions: str) -> None:
        current_agent = getattr(session, "current_agent", None)
        if current_agent is None:
            logger.warning("Skipping dynamic knowledge refresh because no active agent is attached to the session.")
            return
        update_instructions = getattr(current_agent, "update_instructions", None)
        if not callable(update_instructions):
            logger.warning(
                "Skipping dynamic knowledge refresh because the active agent does not support update_instructions()."
            )
            return

        await update_instructions(instructions)

    async def _refresh_dynamic_knowledge_context(transcript: str) -> None:
        business_use_case = str(userdata.get("business_use_case") or "").strip().lower()
        if business_use_case not in {"generic", "custom", "other"}:
            return

        query = str(transcript or "").strip()
        base_instructions = str(userdata.get("base_instructions") or "").strip()
        if not query or not base_instructions:
            return

        try:
            result = await ops_search_business_knowledge(
                query=query,
                top_k=4,
                metadata=_ops_tool_metadata_from_userdata(userdata),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Dynamic knowledge prefetch failed for query=%s: %s", query, exc)
            return

        matches = result.get("matches") if isinstance(result, dict) else None
        if not isinstance(matches, list) or not matches:
            return

        snippets: list[str] = []
        for match in matches[:3]:
            if not isinstance(match, dict):
                continue
            text = " ".join(str(match.get("text") or "").split()).strip()
            if not text:
                continue
            source_name = str(match.get("source_name") or "Knowledge").strip()
            snippets.append(f"- {source_name}: {text[:900]}")

        if not snippets:
            return

        enriched_instructions = (
            f"{base_instructions}\n\n"
            "Dynamic knowledge context for the caller's latest request:\n"
            "- The following snippets were retrieved from the business knowledge base for this turn.\n"
            "- Use them first when answering the caller's current question.\n"
            "- If the snippets are incomplete, call search_business_knowledge again before saying you do not know.\n"
            f"{chr(10).join(snippets)}\n"
        )
        await _update_live_agent_instructions(enriched_instructions)
        userdata["last_dynamic_knowledge_query"] = query
        logger.info(
            "Dynamic knowledge context updated: turn=%s matches=%s query=%s",
            int(userdata.get("turn_index", 0)),
            len(snippets),
            _short_text(query, 120),
        )

    def _schedule_dynamic_knowledge_refresh(transcript: str) -> None:
        query = str(transcript or "").strip()
        if not query:
            return
        if query == str(userdata.get("last_dynamic_knowledge_query") or "").strip():
            return
        _track_background_task(userdata, _refresh_dynamic_knowledge_context(query))

    def _next_event_idx() -> int:
        userdata["timeline_event_index"] = (
            int(userdata.get("timeline_event_index", 0)) + 1
        )
        return int(userdata["timeline_event_index"])

    @session.on("metrics_collected")
    def _on_metrics_collected(ev: Any) -> None:
        metrics = getattr(ev, "metrics", ev)
        metric_type = str(getattr(metrics, "type", "") or "").strip().lower()
        turn_index = int(userdata.get("turn_index", 0) or 0)
        if metric_type == "stt_metrics":
            logger.info(
                "Voice latency: stage=stt turn=%s provider=%s model=%s duration_ms=%.1f audio_duration_s=%.2f streamed=%s",
                turn_index,
                str(getattr(metrics, "provider", "") or ""),
                str(getattr(metrics, "model", "") or ""),
                float(getattr(metrics, "duration", 0.0) or 0.0) * 1000,
                float(getattr(metrics, "audio_duration", 0.0) or 0.0),
                bool(getattr(metrics, "streamed", False)),
            )
            return
        if metric_type == "eou_metrics":
            endpointing_ms = (
                float(
                    getattr(metrics, "end_of_utterance_delay", 0.0) or 0.0
                )
                * 1000
            )
            transcription_delay_ms = (
                float(getattr(metrics, "transcription_delay", 0.0) or 0.0)
                * 1000
            )
            turn_callback_ms = (
                float(
                    getattr(metrics, "on_user_turn_completed_delay", 0.0)
                    or 0.0
                )
                * 1000
            )
            logger.info(
                "Voice latency: stage=turn_detection turn=%s endpointing_ms=%.1f transcription_delay_ms=%.1f turn_callback_ms=%.1f",
                turn_index,
                endpointing_ms,
                transcription_delay_ms,
                turn_callback_ms,
            )
            _publish_voice_lab_metric(
                "stt_timing",
                transcript_delay_ms=transcription_delay_ms,
                endpointing_ms=endpointing_ms,
            )
            return
        if metric_type == "llm_metrics":
            llm_ttft_ms = float(getattr(metrics, "ttft", 0.0) or 0.0) * 1000
            logger.info(
                "Voice latency: stage=llm turn=%s provider=%s model=%s ttft_ms=%.1f duration_ms=%.1f completion_tokens=%s cancelled=%s",
                turn_index,
                str(getattr(metrics, "provider", "") or ""),
                str(getattr(metrics, "model", "") or ""),
                float(getattr(metrics, "ttft", 0.0) or 0.0) * 1000,
                float(getattr(metrics, "duration", 0.0) or 0.0) * 1000,
                int(getattr(metrics, "completion_tokens", 0) or 0),
                bool(getattr(metrics, "cancelled", False)),
            )
            if llm_ttft_ms > 0 and not bool(getattr(metrics, "cancelled", False)):
                _publish_voice_lab_metric(
                    "llm_first_token",
                    provider=str(getattr(metrics, "provider", "") or ""),
                    model=str(getattr(metrics, "model", "") or ""),
                    llm_ttft_ms=llm_ttft_ms,
                )
            return
        if metric_type == "tts_metrics":
            tts_ttfb_ms = float(getattr(metrics, "ttfb", 0.0) or 0.0) * 1000
            tts_total_ms = float(getattr(metrics, "duration", 0.0) or 0.0) * 1000
            tts_audio_seconds = float(
                getattr(metrics, "audio_duration", 0.0) or 0.0
            )
            logger.info(
                "Voice latency: stage=tts turn=%s provider=%s model=%s ttfb_ms=%.1f duration_ms=%.1f audio_duration_s=%.2f cancelled=%s",
                turn_index,
                str(getattr(metrics, "provider", "") or ""),
                str(getattr(metrics, "model", "") or ""),
                float(getattr(metrics, "ttfb", 0.0) or 0.0) * 1000,
                float(getattr(metrics, "duration", 0.0) or 0.0) * 1000,
                float(getattr(metrics, "audio_duration", 0.0) or 0.0),
                bool(getattr(metrics, "cancelled", False)),
            )
            if tts_ttfb_ms > 0 and not bool(getattr(metrics, "cancelled", False)):
                duration_seconds = tts_total_ms / 1000
                _publish_voice_lab_metric(
                    "tts_done",
                    transport=str(
                        (userdata.get("runtime_overrides") or {}).get("tts_transport")
                        or "http"
                    ),
                    ttfa_ms=tts_ttfb_ms,
                    total_ms=tts_total_ms,
                    audio_seconds=tts_audio_seconds,
                    rtf=(
                        duration_seconds / tts_audio_seconds
                        if tts_audio_seconds > 0
                        else None
                    ),
                    audio_wall=(
                        tts_audio_seconds / duration_seconds
                        if duration_seconds > 0
                        else None
                    ),
                )

    @session.on("user_input_transcribed")
    def _on_user_input_transcribed(ev: Any) -> None:
        transcript = str(getattr(ev, "transcript", "") or "").strip()
        if not transcript or not bool(getattr(ev, "is_final", False)):
            return

        userdata["turn_index"] = int(userdata.get("turn_index", 0)) + 1
        userdata["last_user_transcript"] = transcript
        turn_index = int(userdata["turn_index"])
        created_at = float(getattr(ev, "created_at", 0.0) or time.time())
        turn_id = f"turn-{turn_index}-{int(created_at * 1000)}"
        userdata["voice_lab_active_turn_id"] = turn_id
        userdata["voice_lab_active_turn_index"] = turn_index
        _publish_voice_lab_metric(
            "stt_final",
            turn_id=turn_id,
            turn_index=turn_index,
            ts_ms=int(created_at * 1000),
            transcript_preview=transcript[:160],
            transcript_chars=len(transcript),
        )
        _schedule_dynamic_knowledge_refresh(transcript)
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
            _publish_voice_lab_metric(
                "llm_first_text",
                assistant_preview=content[:160],
                assistant_chars=len(content),
            )
        elif role.lower() == "user":
            if content != userdata.get("last_user_transcript"):
                userdata["turn_index"] = int(userdata.get("turn_index", 0)) + 1
            userdata["last_user_transcript"] = content
            _append_recent_user_message(userdata, content)
            _schedule_dynamic_knowledge_refresh(content)

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
        if role_l == "assistant" and _should_skip_assistant_message_persist(userdata, content):
            return

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
                    elif role_l == "assistant":
                        userdata["last_persisted_assistant_content"] = content

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
                if role_l == "assistant":
                    userdata["last_persisted_assistant_content"] = content

        if role_l == "assistant":

            async def _reconcile_claim() -> None:
                await _reconcile_ticket_claim_if_needed(userdata, content)

            _track_background_task(userdata, _reconcile_claim())

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
            result = call.get("tool_result")
            if tool_name == "create_ticket" and not (
                isinstance(result, dict) and str(result.get("status") or "").lower() == "failed"
            ):
                userdata["last_create_ticket_success_turn"] = int(userdata.get("turn_index", 0))
                userdata["last_create_ticket_result"] = result
            _persist_session_event_async(
                userdata,
                event_type="tool_call",
                role="tool",
                title=tool_name,
                body=_summarize_tool_output(result),
                payload={
                    "tool_name": tool_name,
                    "tool_arguments": call.get("tool_arguments"),
                    "tool_result": result,
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


def _instructions_with_spoken_style(base_prompt: str, language: str) -> str:
    if str(language or "").strip().lower() != "en":
        return base_prompt
    style = NIGERIAN_SPOKEN_STYLE_EN.strip()
    if not style or style in base_prompt:
        return base_prompt
    return f"{base_prompt.rstrip()}\n\n{style}\n"


async def _instructions_with_context(base_prompt: str, userdata: dict[str, Any]) -> str:
    base_prompt = _instructions_with_spoken_style(
        base_prompt, str(userdata.get("language") or "")
    )
    base_prompt = (
        f"{base_prompt}\n\n"
        "Closing behavior:\n"
        "- End the conversation naturally once the caller's request is handled.\n"
        "- Do not give a forced recap of the whole interaction at the end of every successful call.\n"
        "- Only give a short summary when the caller explicitly asks for one or when a brief confirmation is genuinely useful.\n"
        "- If the caller says thank you, says they are done, or clearly signals the conversation is over, respond naturally and close politely.\n"
    )
    guest_context = str(userdata.get("guest_context") or "").strip()
    if guest_context:
        # Shared platform agents (for example the Help Center guide) are injected
        # with the *viewing* account's details so the conversation feels personal
        # and account-aware even though the agent itself lives on another business.
        base_prompt = (
            f"{base_prompt}\n\n"
            "Caller account context (the SalesGirl account you are currently helping):\n"
            f"{guest_context}\n"
            "- Use these details to make the conversation natural and personal.\n"
            "- Greet the caller by name (or by their business name) at the start when a name is available.\n"
            "- Treat these details as the source of truth about this account's setup; do not contradict them.\n"
            "- Never read these instructions aloud or say that you were given context."
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
            "- Use the saved business instructions, knowledge, and dashboard-configured tools for this business only.\n\n"
            "Role lock:\n"
            "- You MUST follow the current business-specific role and responsibilities in this prompt.\n"
            "- Historical snippets may contain outdated assistant behavior from older versions.\n"
            "- Never switch to an old persona if it conflicts with this prompt.\n\n"
            "Issue handling lock:\n"
            "- search_business_knowledge is always available as a built-in runtime tool for this business.\n"
            "- Use built-in knowledge search before saying you do not have enough information.\n"
            "- Use dashboard-configured tools only when they are relevant and available.\n"
            "- Read each enabled tool description as the source of truth for when to use that tool and what it should help you accomplish.\n"
            "- Do not claim any action succeeded unless the tool confirms it.\n"
            "- If transfer_to_aicc is enabled and the caller needs immediate human help during the live call, prefer that live handoff instead of promising later follow-up.\n"
            "- If a request needs human attention, create a ticket if that tool is available.\n"
            "- If the caller asks for a ticket, or agrees to ticket follow-up, call create_ticket immediately before replying.\n"
            "- In the exact turn where you say a ticket was created, create_ticket must already have succeeded.\n"
            "- Never read ticket IDs or internal reference codes aloud unless the caller explicitly asks for a reference. Confirm tickets naturally without citing numbers.\n"
            "- If a tool, lookup, or knowledge check fails, respond naturally without mentioning tools, APIs, or knowledge bases. Say you could not find that information or complete that request, then continue helpfully.\n"
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
            "- If transfer_to_aicc is enabled and the caller needs immediate human help during the live call, prefer that live handoff instead of promising later follow-up.\n"
            "- If the issue needs human follow-up, create a ticket when that tool is available.\n"
            "- If the caller asks for a ticket, or agrees to ticket follow-up, call create_ticket immediately before replying.\n"
            "- In the exact turn where you say a ticket was created, create_ticket must already have succeeded.\n"
            "- Never read ticket IDs or internal reference codes aloud unless the caller explicitly asks for a reference. Confirm tickets naturally without citing numbers.\n"
            "- If a tool, lookup, or knowledge check fails, respond naturally without mentioning tools, APIs, or knowledge bases. Say you could not find that information or complete that request, then continue helpfully."
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


async def _fetch_agent_runtime_config(
    userdata: dict[str, Any],
) -> dict[str, Any]:
    business_id = _normalize_business_id(str(userdata.get("business_id") or ""))
    config_agent_id = str(userdata.get("agent_config_id") or "").strip()
    if not business_id or not config_agent_id:
        return {}
    payload = await get_agent_runtime_config(
        agent_id=config_agent_id, business_id=business_id
    )
    if str(payload.get("status") or "") == "failed":
        logger.error(
            "Agent runtime config fetch failed: business_id=%s agent_id=%s detail=%s http_status=%s",
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
    configured_kb_ids = cfg.get("knowledge_base_ids")
    userdata["knowledge_base_ids"] = [
        str(kb_id).strip()
        for kb_id in (configured_kb_ids if isinstance(configured_kb_ids, list) else [])
        if str(kb_id).strip()
    ]
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
    enabled_tool_names = [
        str(tool.get("name") or "").strip()
        for tool in active_tools
        if str(tool.get("name") or "").strip()
    ]
    for tool_name in ALWAYS_ENABLED_RUNTIME_TOOLS:
        if tool_name not in enabled_tool_names:
            enabled_tool_names.append(tool_name)
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


def _strip_configured_tool_access_block(text: str) -> str:
    start_marker = "Configured tool access (system-generated):"
    end_marker = "End configured tool access."
    raw = str(text or "")
    start_index = raw.find(start_marker)
    if start_index == -1:
        return raw.strip()
    end_index = raw.find(end_marker, start_index)
    if end_index == -1:
        sanitized = raw[:start_index]
    else:
        sanitized = f"{raw[:start_index]}{raw[end_index + len(end_marker):]}"
    return sanitized.strip()


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
        "- Treat each tool description as the contract for what the tool does, when to use it, and which request fields matter.",
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

    if "transfer_to_aicc" in enabled_names:
        desc = _tool_description(by_name["transfer_to_aicc"])
        lines.append(
            f"- transfer_to_aicc is enabled. Use it when the caller asks for a human, or when the request cannot be safely resolved during this live call after you have used business knowledge first. Before using it, tell the caller naturally that you are connecting them to a colleague and ask them to hold briefly. Do not mention tools, SIP, routing, or internal systems. {desc}".strip()
        )
    else:
        lines.append(
            "- transfer_to_aicc is not enabled. Do not say you are transferring the live call to a human agent."
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
            "transfer_to_aicc",
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
        for tool in (tools if isinstance(tools, list) else [])
        if isinstance(tool, dict)
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
    if tool_names and tool_names <= {"create_ticket", "send_email"}:
        return "generic"

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
    configured_instructions = _strip_configured_tool_access_block(
        str(cfg.get("instructions") or "").strip()
    )
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
            "- Never read ticket IDs or internal reference codes aloud unless the guest explicitly asks for a reference.\n"
            "- Never say a booking was created unless create_booking returned success.\n"
            "- If a tool call fails, respond naturally without mentioning tools, APIs, or knowledge bases. Say you could not find that information or complete that request, then continue helpfully.\n"
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
            "- Never read ticket IDs or internal reference codes aloud unless the customer explicitly asks for a reference.\n"
            "- Never say an order was created unless create_order returned success.\n"
            "- If a tool call fails, respond naturally without mentioning tools, APIs, or knowledge bases. Say you could not find that information or complete that request, then continue helpfully.\n"
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
            "- Never read ticket IDs or internal reference codes aloud unless the customer explicitly asks for a reference.\n"
            "- Never say an order was created unless create_order returned success.\n"
            "- If a tool call fails, respond naturally without mentioning tools, APIs, or knowledge bases. Say you could not find that information or complete that request, then continue helpfully.\n"
        )

    if business_use_case == "generic":
        return (
            f"{configured_instructions.rstrip()}\n\n"
            f"{runtime_tool_guidance}\n\n"
            "Built-in tool rule:\n"
            "- search_business_knowledge is a built-in runtime tool for every agent, even when it is not part of the dashboard-configured tool list.\n"
            "- Use business knowledge search before saying you do not have enough information.\n"
            "- Treat dashboard-configured tools as additional tools, not as the full list of built-in runtime capabilities.\n"
            "- If transfer_to_aicc is enabled and the caller needs a live human handoff, tell them naturally that you are connecting them, then use that tool.\n"
        )

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
        "knowledge_base_ids": list(userdata.get("knowledge_base_ids") or []),
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

    if business_use_case in {
        "hotel",
        "restaurant",
        "fashion",
        "generic",
        "custom",
        "other",
    }:
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


async def _instructions_with_initial_knowledge_context(
    instructions: str, userdata: dict[str, Any]
) -> str:
    business_use_case = str(userdata.get("business_use_case") or "").strip().lower()
    if business_use_case not in {"generic", "custom", "other"}:
        return instructions

    query = (
        "Summarize this business's services, products, qualification questions, "
        "FAQs, and the main facts the voice assistant should know."
    )
    try:
        result = await ops_search_business_knowledge(
            query=query,
            top_k=6,
            metadata=_ops_tool_metadata_from_userdata(userdata),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Initial knowledge preload failed: %s", exc)
        return instructions

    matches = result.get("matches") if isinstance(result, dict) else None
    if not isinstance(matches, list) or not matches:
        return instructions

    snippets: list[str] = []
    for match in matches[:4]:
        if not isinstance(match, dict):
            continue
        text = " ".join(str(match.get("text") or "").split()).strip()
        if not text:
            continue
        source_name = str(match.get("source_name") or "Knowledge").strip()
        snippets.append(f"- {source_name}: {text[:900]}")

    if not snippets:
        return instructions

    logger.info(
        "Initial knowledge context loaded: business_id=%s matches=%s",
        str(userdata.get("business_id") or ""),
        len(snippets),
    )
    return (
        f"{instructions}\n\n"
        "Core business knowledge loaded at session start:\n"
        "- Use these facts whenever they answer the caller's question.\n"
        "- If the caller asks something more specific, use search_business_knowledge for a deeper lookup before saying you do not know.\n"
        f"{chr(10).join(snippets)}\n"
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
    stt_engine: Any | None = None,
    tts_engine: Any | None = None,
) -> AgentSession:
    if stt_engine is None:
        stt_engine = _build_stt_engine_for_language(language=language, userdata=userdata)
    session_llm = _build_llm_for_language(language=language, userdata=userdata)
    odion_stt = (
        stt_engine.wrapped_stt
        if isinstance(stt_engine, stt.StreamAdapter)
        else stt_engine
    )
    session_vad = (
        odion_stt.endpointing_vad if isinstance(odion_stt, OdionSTT) else None
    )
    turn_handling = TurnHandlingOptions(
        endpointing=EndpointingOptions(
            min_delay=TURN_MIN_ENDPOINTING_DELAY,
            max_delay=TURN_MAX_ENDPOINTING_DELAY,
        ),
        interruption=InterruptionOptions(
            enabled=True,
            mode="vad",
            min_duration=TURN_MIN_INTERRUPTION_DURATION,
            min_words=0,
            resume_false_interruption=False,
            false_interruption_timeout=None,
        ),
    )
    session_options: dict[str, Any] = {
        "stt": stt_engine,
        "llm": session_llm,
        "userdata": userdata,
        "turn_handling": turn_handling,
        "aec_warmup_duration": TURN_AEC_WARMUP_DURATION,
    }
    if session_vad is not None:
        session_options["vad"] = session_vad
    if language == "fr":
        return AgentSession(
            tts=tts_engine or deepgram.TTS(model="aura-2-agathe-fr"),
            **session_options,
        )

    return AgentSession(
        tts=tts_engine,
        **session_options,
    )


def _trigger_first_turn(
    session: AgentSession, *, language: str, business_use_case: str
) -> None:
    try:
        session.generate_reply(
            user_input=_kickoff_prompt_for_language(language, business_use_case),
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


def _resolve_saved_odion_owner_id(
    *,
    tts_voice_id: str,
    explicit_owner_id: str,
    business_id: str,
) -> str:
    normalized_explicit_owner_id = str(explicit_owner_id or "").strip()
    if normalized_explicit_owner_id:
        return normalized_explicit_owner_id

    normalized_voice_id = str(tts_voice_id or "").strip()
    if normalized_voice_id:
        mapped_owner = SHARED_ODION_CATALOG_OWNER_BY_VOICE_ID.get(normalized_voice_id)
        if mapped_owner:
            return mapped_owner

    return str(business_id or "").strip()


def _normalized_language_code(value: str) -> str:
    lowered = str(value or "").strip().lower()
    if lowered in {"fr", "french", "français", "francais"}:
        return "fr"
    return "en"


def _deepgram_tts_model_for_language(language: str) -> str:
    return "aura-2-agathe-fr" if str(language or "").strip().lower() == "fr" else "aura-asteria-en"


def _strict_language_aware_deepgram_model(model: str, language: str) -> str:
    selected_model = str(model or "").strip()
    if not selected_model:
        return _deepgram_tts_model_for_language(language)
    lowered_model = selected_model.lower()
    normalized_lang = str(language or "").strip().lower()
    if normalized_lang == "fr" and lowered_model.endswith("-en"):
        return _deepgram_tts_model_for_language("fr")
    if normalized_lang != "fr" and lowered_model.endswith("-fr"):
        return _deepgram_tts_model_for_language("en")
    return selected_model


def _deepgram_stt_language_for_language(language: str) -> str:
    return "fr" if str(language or "").strip().lower() == "fr" else "en"


def _runtime_model_language_hint(model: str) -> str:
    lowered = str(model or "").strip().lower()
    if any(token in lowered for token in ("pidgin", "pijin", "naija", "pcm")):
        return "Pidgin"
    return ""


def _runtime_overrides_from_userdata(userdata: dict[str, Any]) -> dict[str, str]:
    return _normalize_runtime_overrides(userdata.get("runtime_overrides"))


def _default_stt_provider() -> str:
    return (
        str(
            os.getenv("VOICE_AGENT_STT_PROVIDER")
            or os.getenv("DEFAULT_STT_PROVIDER")
            or "deepgram"
        ).strip().lower()
        or "deepgram"
    )


def _default_stt_model() -> str:
    return str(os.getenv("VOICE_AGENT_STT_MODEL") or "nova-3").strip() or "nova-3"


def _default_odion_stt_base_url() -> str:
    return (
        str(os.getenv("ODION_STT_BASE_URL") or DEFAULT_ODION_STT_BASE_URL).strip()
        or DEFAULT_ODION_STT_BASE_URL
    )


def _default_odion_stt_transport() -> str:
    return str(os.getenv("ODION_STT_TRANSPORT") or "").strip().lower()


def _build_stt_engine_for_language(*, language: str, userdata: dict[str, Any]) -> Any:
    lang = str(language or "").strip().lower()
    overrides = _runtime_overrides_from_userdata(userdata)
    provider = (
        str(overrides.get("stt_provider") or _default_stt_provider()).strip().lower()
    )
    model = str(overrides.get("stt_model") or _default_stt_model()).strip() or _default_stt_model()
    base_url = str(overrides.get("stt_base_url") or "").strip()
    transport = str(
        overrides.get("stt_transport")
        or ("" if base_url else _default_odion_stt_transport())
    ).strip().lower()

    if provider == "odion_stt":
        resolved_base_url = base_url or _default_odion_stt_base_url()
        logger.info(
            "Using Odion STT runtime selection: base_url=%s model=%s language=%s transport=%s override=%s",
            resolved_base_url,
            model,
            lang,
            transport or "auto",
            bool(overrides.get("stt_provider") or overrides.get("stt_base_url")),
        )
        endpointing_vad = silero.VAD.load(
            min_speech_duration=ODION_STT_REALTIME_MIN_SPEECH_SECONDS,
            min_silence_duration=ODION_STT_REALTIME_ENDPOINTING_SILENCE_SECONDS,
            activation_threshold=ODION_STT_REALTIME_VAD_ACTIVATION_THRESHOLD,
        )
        odion_stt = OdionSTT(
            language=lang,
            model=model,
            base_url=resolved_base_url,
            transport=transport,
            endpointing_vad=endpointing_vad,
        )
        if odion_stt.capabilities.streaming:
            return odion_stt
        return stt.StreamAdapter(
            stt=odion_stt,
            vad=endpointing_vad,
        )
    stt_kwargs: dict[str, Any] = {
        "language": _deepgram_stt_language_for_language(lang),
        "model": model,
    }
    if provider == "custom" and base_url:
        stt_kwargs["base_url"] = base_url
        logger.info(
            "Using custom Deepgram-compatible STT override: base_url=%s model=%s language=%s",
            base_url,
            model,
            lang,
        )
    elif model != "nova-3":
        logger.info(
            "Using Deepgram STT override: model=%s language=%s",
            model,
            lang,
        )

    return deepgram.STT(**stt_kwargs)


def _build_tts_engine_for_language(
    *,
    language: str,
    active_agent_config: dict[str, Any],
    userdata: dict[str, Any],
    business_id: str,
) -> Any:
    lang = str(language or "").strip().lower()
    is_fr = lang == "fr"
    saved_provider = str(active_agent_config.get("tts_provider") or "").strip().lower()
    runtime_overrides = _runtime_overrides_from_userdata(userdata)
    default_tts_provider = str(
        os.getenv("VOICE_AGENT_TTS_PROVIDER")
        or os.getenv("DEFAULT_TTS_PROVIDER")
        or ""
    ).strip().lower()
    override_provider = str(
        runtime_overrides.get("tts_provider") or default_tts_provider
    ).strip().lower()
    override_model = (
        str(runtime_overrides.get("tts_model") or "").strip()
        or _deepgram_tts_model_for_language(lang)
    )
    override_base_url = str(runtime_overrides.get("tts_base_url") or "").strip()
    override_api_key = str(runtime_overrides.get("tts_api_key") or "").strip()
    fallback_tts: Any = (
        deepgram.TTS(model=_deepgram_tts_model_for_language(lang))
    )
    odion_enabled = ENABLE_ODION_TTS_FR if is_fr else ENABLE_ODION_TTS_EN
    fallback_label = "French" if is_fr else "English"

    tts_model_override = str(runtime_overrides.get("tts_model") or "").strip()
    tts_endpoint_override = _normalize_tts_endpoint(
        userdata.get("tts_endpoint") or ""
    ) or _normalize_tts_endpoint(override_base_url)
    runtime_odion_tts_requested = bool(tts_endpoint_override) or override_provider in {
        "odion_tts",
        "odion",
    }

    if override_provider == "deepgram" or (
        override_provider == "custom" and not runtime_odion_tts_requested
    ):
        resolved_override_model = _strict_language_aware_deepgram_model(
            override_model, lang
        )
        if resolved_override_model != override_model:
            logger.info(
                "Adjusted Deepgram override model for language: requested=%s resolved=%s language=%s",
                override_model,
                resolved_override_model,
                lang,
            )
        tts_kwargs: dict[str, Any] = {"model": resolved_override_model}
        if override_provider == "custom" and override_base_url:
            tts_kwargs["base_url"] = override_base_url
            logger.info(
                "Using custom Deepgram-compatible TTS override: base_url=%s model=%s language=%s",
                override_base_url,
                override_model,
                lang,
            )
        else:
            logger.info(
                "Using Deepgram TTS override: model=%s language=%s",
                override_model,
                lang,
            )
        return deepgram.TTS(**tts_kwargs)

    if saved_provider == "deepgram" and not runtime_odion_tts_requested:
        saved_model = _strict_language_aware_deepgram_model(
            str(active_agent_config.get("tts_voice_id") or "").strip()
            or _deepgram_tts_model_for_language(lang),
            lang,
        )
        logger.info(
            "Using saved Deepgram TTS provider: model=%s language=%s agent_config_id=%s",
            saved_model,
            lang,
            userdata.get("agent_config_id"),
        )
        return deepgram.TTS(model=saved_model)

    use_experiment_clone = (
        not runtime_odion_tts_requested
        and FORCE_ODION_TTS_EXPERIMENT_VOICE
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
        else _resolve_saved_odion_owner_id(
            tts_voice_id=tts_voice_id,
            explicit_owner_id=str(active_agent_config.get("tts_owner_id") or "").strip(),
            business_id=business_id,
        )
    )
    tts_language_hint = (
        ODION_TTS_EXPERIMENT_LANGUAGE_HINT
        if use_experiment_clone
        else str(
            active_agent_config.get("tts_language_hint")
            or ("French" if is_fr else "English")
        ).strip()
        or ("French" if is_fr else "English")
    )
    tts_mode_override = _normalize_tts_mode(
        userdata.get("tts_mode") or os.getenv("VOICE_AGENT_TTS_MODE") or ""
    )
    tts_owner_id_override = str(userdata.get("tts_owner_id") or "").strip()
    tts_voice_id_override = str(userdata.get("tts_voice_id") or "").strip()
    tts_language_hint_override = str(userdata.get("tts_language_hint") or "").strip()
    tts_seed_raw = str(userdata.get("tts_seed") or "").strip()
    tts_seed_override = (
        int(tts_seed_raw) if tts_seed_raw.isdigit() and int(tts_seed_raw) >= 0 else None
    )
    if tts_owner_id_override:
        tts_owner_id = tts_owner_id_override
    if tts_voice_id_override:
        tts_voice_id = tts_voice_id_override
    if tts_language_hint_override:
        tts_language_hint = tts_language_hint_override
    elif runtime_odion_tts_requested:
        tts_language_hint = (
            _runtime_model_language_hint(tts_model_override) or tts_language_hint
        )
    use_configured_clone = use_experiment_clone or _should_use_odion_tts_for_language(
        active_agent_config, lang
    )
    if tts_mode_override == "cloned_voice":
        use_configured_clone = True
    elif tts_mode_override == "default_voice":
        use_configured_clone = False
    use_odion_default = not use_configured_clone

    if runtime_odion_tts_requested:
        use_configured_clone = (
            tts_mode_override == "cloned_voice"
            and (bool(tts_voice_id) or bool(os.getenv("ASCEND_TTS_CACHED_VOICE")))
        )
        use_odion_default = not use_configured_clone

    if not odion_enabled and not runtime_odion_tts_requested:
        logger.info(
            "ENABLE_ODION_TTS_%s=false; using Deepgram TTS for %s session.",
            "FR" if is_fr else "EN",
            fallback_label,
        )
        return fallback_tts
    if not odion_enabled and runtime_odion_tts_requested:
        logger.info(
            "Using runtime Odion TTS override for %s session even though ENABLE_ODION_TTS_%s=false.",
            fallback_label,
            "FR" if is_fr else "EN",
        )

    try:
        if use_configured_clone:
            tts_engine = OdionTTS(
                owner_id=tts_owner_id,
                voice_id=tts_voice_id,
                language=tts_language_hint,
                model=tts_model_override,
                seed=tts_seed_override
                if tts_seed_override is not None
                else ODION_TTS_CLONE_SEED,
                mode="cloned_voice",
                base_url=tts_endpoint_override or None,
                api_key=override_api_key or None,
            )
            logger.info(
                "Using Odion cloned TTS for %s session: agent_config_id=%s voice_id=%s owner_id=%s model=%s seed=%s",
                fallback_label,
                userdata.get("agent_config_id"),
                tts_voice_id,
                tts_owner_id,
                tts_model_override,
                ODION_TTS_CLONE_SEED,
            )
            return tts_engine
        if use_odion_default:
            tts_engine = OdionTTS(
                owner_id=tts_owner_id or business_id,
                voice_id=None,
                language=tts_language_hint,
                model=tts_model_override,
                seed=tts_seed_override,
                mode="default_voice",
                base_url=tts_endpoint_override or None,
                api_key=override_api_key or None,
            )
            cached_voice = str(os.getenv("ASCEND_TTS_CACHED_VOICE") or "").strip()
            if cached_voice and str(os.getenv("ODION_TTS_BACKEND") or "").strip().lower() == "ascend":
                logger.info(
                    "Using Odion Ascend cached-voice TTS for %s session: agent_config_id=%s owner_id=%s cached_voice=%s model=%s language_hint=%s",
                    fallback_label,
                    userdata.get("agent_config_id"),
                    tts_owner_id or business_id,
                    cached_voice,
                    tts_model_override or "Qwen3-TTS",
                    tts_language_hint,
                )
            else:
                logger.info(
                    "Using Odion default TTS for %s session: agent_config_id=%s owner_id=%s model=%s language_hint=%s",
                    fallback_label,
                    userdata.get("agent_config_id"),
                    tts_owner_id or business_id,
                    tts_model_override,
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
        if runtime_odion_tts_requested:
            logger.error(
                "Failed to initialize runtime Odion TTS override for %s session; refusing to fall back to Deepgram: %s",
                fallback_label,
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
        active_agent_config = await _fetch_agent_runtime_config(userdata)
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
        instructions = await _instructions_with_initial_knowledge_context(
            instructions, userdata
        )
        userdata["base_instructions"] = instructions
        started_at = conv_api_utcnow()
        business_id = str(userdata.get("business_id") or "")
        call_channel = (
            "web"
            if str(userdata.get("identity_type") or "").lower() == "web"
            else "voice"
        )
        await _authorize_billing_start_or_raise(
            userdata=userdata,
            business_id=business_id,
            call_channel=call_channel,
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
        stt_engine = _build_stt_engine_for_language(language="en", userdata=userdata)

        session = _build_session_for_language(
            language="en",
            instructions=instructions,
            userdata=userdata,
            stt_engine=stt_engine,
            tts_engine=tts_engine,
        )
        dynamic_tools = build_dynamic_http_tools(
            active_agent_config,
            excluded_tool_names=set(BUILTIN_RUNTIME_TOOL_NAMES),
        )
        if dynamic_tools:
            logger.info(
                "Registered dynamic runtime tools: %s",
                [
                    str(getattr(getattr(tool, "info", None), "name", "")).strip()
                    for tool in dynamic_tools
                ],
            )
        _wire_session_timeline(session, session.userdata, room=ctx.room)
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
            await session.start(
                agent=SalonAgent(instructions=instructions, tools=dynamic_tools),
                room=ctx.room,
                room_options=room_io.RoomOptions(delete_room_on_close=True),
            )
            _start_billing_heartbeat(
                session=session,
                ctx=ctx,
                userdata=userdata,
                business_id=business_id,
                started_at=started_at,
                call_channel=call_channel,
            )
            _trigger_first_turn(
                session, language="en", business_use_case=business_use_case
            )
            if is_recording_enabled():
                async def _start_recording_after_join_en() -> None:
                    try:
                        await _start_session_recording_capture(
                            ctx=ctx,
                            userdata=userdata,
                            business_id=business_id,
                            session_tracker_id=str(userdata.get("session_tracker_id") or ""),
                            started_at=started_at,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.exception(
                            "Recording startup failed after English session join: business_id=%s session_id=%s error=%s",
                            business_id,
                            str(userdata.get("session_id") or ""),
                            exc,
                        )

                _track_background_task(userdata, _start_recording_after_join_en())
            else:
                logger.info(
                    "Recording not enabled for this session: language=en business_id=%s",
                    business_id,
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
        active_agent_config = await _fetch_agent_runtime_config(userdata)
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
        instructions = await _instructions_with_initial_knowledge_context(
            instructions, userdata
        )
        userdata["base_instructions"] = instructions
        started_at = conv_api_utcnow()
        business_id = str(userdata.get("business_id") or "")
        call_channel = (
            "web"
            if str(userdata.get("identity_type") or "").lower() == "web"
            else "voice"
        )
        await _authorize_billing_start_or_raise(
            userdata=userdata,
            business_id=business_id,
            call_channel=call_channel,
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
        stt_engine = _build_stt_engine_for_language(language="fr", userdata=userdata)
        session = _build_session_for_language(
            language="fr",
            instructions=instructions,
            userdata=userdata,
            stt_engine=stt_engine,
            tts_engine=tts_engine,
        )
        dynamic_tools = build_dynamic_http_tools(
            active_agent_config,
            excluded_tool_names=set(BUILTIN_RUNTIME_TOOL_NAMES),
        )
        if dynamic_tools:
            logger.info(
                "Registered dynamic runtime tools: %s",
                [
                    str(getattr(getattr(tool, "info", None), "name", "")).strip()
                    for tool in dynamic_tools
                ],
            )
        _wire_session_timeline(session, session.userdata, room=ctx.room)
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
            await session.start(
                agent=SalonAgent(instructions=instructions, tools=dynamic_tools),
                room=ctx.room,
                room_options=room_io.RoomOptions(delete_room_on_close=True),
            )
            _start_billing_heartbeat(
                session=session,
                ctx=ctx,
                userdata=userdata,
                business_id=business_id,
                started_at=started_at,
                call_channel=call_channel,
            )
            _trigger_first_turn(
                session, language="fr", business_use_case=business_use_case
            )
            if is_recording_enabled():
                async def _start_recording_after_join_fr() -> None:
                    try:
                        await _start_session_recording_capture(
                            ctx=ctx,
                            userdata=userdata,
                            business_id=business_id,
                            session_tracker_id=str(userdata.get("session_tracker_id") or ""),
                            started_at=started_at,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.exception(
                            "Recording startup failed after French session join: business_id=%s session_id=%s error=%s",
                            business_id,
                            str(userdata.get("session_id") or ""),
                            exc,
                        )

                _track_background_task(userdata, _start_recording_after_join_fr())
            else:
                logger.info(
                    "Recording not enabled for this session: language=fr business_id=%s",
                    business_id,
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
