from __future__ import annotations

import asyncio
import json
import os
import logging
from typing import Any
from urllib.parse import urlparse

import httpx
from livekit import api

from .latency_trace import elapsed_ms, emit as emit_latency_trace, monotonic_ms
from .observability import observe, trace_tool, update_observation

# Default aligns with platform port convention in AGENTS.md:
# demo CRM on 8096 (knowledge-service runs on 8095)
OPS_SERVICE_BASE_URL = os.getenv(
    "OPS_SERVICE_BASE_URL", "http://sales-girl-demo-crm-service:8096"
).rstrip("/")
HOTEL_OPS_SERVICE_BASE_URL = os.getenv("HOTEL_OPS_SERVICE_BASE_URL", "").rstrip("/")
FIDELITY_OPS_SERVICE_BASE_URL = os.getenv(
    "FIDELITY_OPS_SERVICE_BASE_URL",
    "http://sales-girl-fidelity-ops-service:8095",
).rstrip("/")
OPS_SERVICE_TOKEN = os.getenv("OPS_SERVICE_TOKEN", "local-internal-service-token")
DEFAULT_TIMEOUT_SECONDS = float(os.getenv("OPS_SERVICE_TIMEOUT_SECONDS", "8"))
KNOWLEDGE_SERVICE_BASE_URL = os.getenv("KNOWLEDGE_SERVICE_BASE_URL", "").rstrip("/")
KNOWLEDGE_SERVICE_TOKEN = os.getenv(
    "KNOWLEDGE_SERVICE_TOKEN",
    os.getenv("CONVERSATION_SERVICE_TOKEN", OPS_SERVICE_TOKEN),
)
KNOWLEDGE_SERVICE_TIMEOUT_SECONDS = float(
    os.getenv("KNOWLEDGE_SERVICE_TIMEOUT_SECONDS", "8")
)
AGENT_CLIENT_ID = os.getenv("AGENT_CLIENT_ID", "sales-girl-internal")
AGENT_NAME = os.getenv("AGENT_NAME", "sales-girl-agent-en")
OPS_SHARED_OWNER_EMAIL = str(os.getenv("OPS_SHARED_OWNER_EMAIL") or "").strip().lower()
AICC_OUTBOUND_TRUNK_NAME = str(
    os.getenv("AICC_OUTBOUND_TRUNK_NAME", "Huawei AICC Outbound Test") or ""
).strip()
AICC_OUTBOUND_TRUNK_ID = str(os.getenv("AICC_OUTBOUND_TRUNK_ID", "") or "").strip()
AICC_TEST_ACCESS_CODE = str(
    os.getenv("AICC_TEST_ACCESS_CODE", "02014114559") or ""
).strip()
AICC_TRANSFER_TARGET_NUMBER = str(
    os.getenv("AICC_TRANSFER_TARGET_NUMBER") or AICC_TEST_ACCESS_CODE
).strip()
AICC_TRANSFER_FROM_NUMBER = str(
    os.getenv("AICC_TRANSFER_FROM_NUMBER") or AICC_TEST_ACCESS_CODE
).strip()
AICC_TRANSFER_CALLER_ID_MODE = str(
    os.getenv("AICC_TRANSFER_CALLER_ID_MODE", "caller_then_configured") or ""
).strip().lower()
AICC_TRANSFER_NORMALIZE_NG_CALLER = (
    str(os.getenv("AICC_TRANSFER_NORMALIZE_NG_CALLER", "true") or "")
    .strip()
    .lower()
    not in {"0", "false", "no", "off"}
)
AICC_REMOVE_AGENT_AFTER_TRANSFER = (
    str(os.getenv("AICC_REMOVE_AGENT_AFTER_TRANSFER", "true") or "")
    .strip()
    .lower()
    not in {"0", "false", "no", "off"}
)
AICC_HANDOFF_DELAY_SECONDS = max(
    0.0,
    float(os.getenv("AICC_HANDOFF_DELAY_SECONDS", "2.5") or "0"),
)
logger = logging.getLogger(__name__)


def _emit_tool_latency(
    metadata: dict[str, Any] | None,
    event: str,
    **fields: Any,
) -> None:
    emit_latency_trace(event, metadata=metadata or {}, **fields)


def _phone_like_from_value(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or "@" in raw:
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) < 5:
        return ""
    return f"+{digits}" if raw.startswith("+") else digits


def _aicc_transfer_from_number(metadata: dict[str, Any] | None) -> str:
    md = metadata or {}
    for key in (
        "sip_caller_number",
        "caller_phone_e164",
        "caller_phone",
        "end_user_phone",
        "original_caller_number",
        "caller_number",
        "from_number",
        "end_user_id",
    ):
        candidate = _phone_like_from_value(md.get(key))
        if candidate:
            return candidate
    return AICC_TRANSFER_FROM_NUMBER.strip()


def _unique_non_empty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique


def _ng_caller_id_variants(number: str) -> list[str]:
    """Return caller-ID formats Huawei/AICC may accept for Nigerian mobiles."""
    if not AICC_TRANSFER_NORMALIZE_NG_CALLER:
        return []

    raw = str(number or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    variants: list[str] = []
    if len(digits) == 10 and digits[0] in {"7", "8", "9"}:
        variants.extend([f"0{digits}", f"+234{digits}", f"234{digits}"])
    elif len(digits) == 11 and digits.startswith("0"):
        variants.extend([f"+234{digits[1:]}", f"234{digits[1:]}"])
    elif len(digits) == 13 and digits.startswith("234"):
        variants.extend([f"+{digits}", f"0{digits[3:]}"])
    return _unique_non_empty(variants)


def _aicc_transfer_from_number_candidates(metadata: dict[str, Any] | None) -> list[str]:
    configured = _phone_like_from_value(AICC_TRANSFER_FROM_NUMBER)
    caller = _aicc_transfer_from_number(metadata)
    if caller == configured:
        caller = ""

    if AICC_TRANSFER_CALLER_ID_MODE in {"configured", "fixed", "access_code"}:
        return _unique_non_empty([configured])

    if AICC_TRANSFER_CALLER_ID_MODE in {"caller", "original"}:
        return _unique_non_empty([caller, *_ng_caller_id_variants(caller)])

    return _unique_non_empty(
        [caller, *_ng_caller_id_variants(caller), configured]
    )


def _read_conversation_api_base_url() -> str:
    """Resolve conversation-service base URL at call time (not import time).

    Workers or alternate entrypoints may import this module before ``load_dotenv`` runs.
    Values written as JSON-quoted strings in ``.env`` are normalized.
    """
    raw = (os.getenv("CONVERSATION_API_BASE_URL") or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1].strip()
    return _normalize_http_url(raw)


def _read_conversation_service_token() -> str:
    return (
        os.getenv("CONVERSATION_SERVICE_TOKEN")
        or os.getenv("OPS_SERVICE_TOKEN")
        or "local-internal-service-token"
    ).strip()


def _internal_ticket_http_base_url(metadata: dict[str, Any] | None) -> str:
    """Base URL for internal (dashboard-shaped) POST /v1/tickets.

    Generic/custom agents historically used ``OPS_SERVICE_BASE_URL`` (demo CRM), which
    expects ``customer_id`` and rejects conversation-style payloads. Prefer
    ``HOTEL_OPS_SERVICE_BASE_URL`` when set (prod/staging usually point this at
    conversation-service) before falling back to ``_ops_base_url``.
    """
    hotel = _normalize_http_url(os.getenv("HOTEL_OPS_SERVICE_BASE_URL", "") or "")
    if hotel:
        return hotel
    return _ops_base_url(metadata)


def _business_use_case(metadata: dict[str, Any] | None) -> str:
    md = metadata or {}
    return str(md.get("business_use_case") or "").strip().lower()


def _uses_internal_business_ops(metadata: dict[str, Any] | None) -> bool:
    return _business_use_case(metadata) in {
        "hotel",
        "restaurant",
        "fashion",
        "custom",
        "generic",
        "other",
    }


def _ops_base_url(metadata: dict[str, Any] | None) -> str:
    use_case = _business_use_case(metadata)
    if use_case in {"hotel", "restaurant", "fashion"}:
        return HOTEL_OPS_SERVICE_BASE_URL
    if use_case in {"custom", "generic", "other"}:
        return OPS_SERVICE_BASE_URL
    if use_case == "fidelity" and FIDELITY_OPS_SERVICE_BASE_URL:
        return FIDELITY_OPS_SERVICE_BASE_URL
    return OPS_SERVICE_BASE_URL


def _resolve_customer_identifier(
    customer_identifier: str | None,
    metadata: dict[str, Any] | None,
) -> str:
    explicit = str(customer_identifier or "").strip()
    if explicit:
        return explicit
    md = metadata or {}
    return str(md.get("end_user_id") or "").strip()


# Web clients often send a generic display label while the verified caller id lives
# in metadata.end_user_id (email or E.164). Tickets must still show that id.
_PLACEHOLDER_CUSTOMER_NAMES = frozenset(
    {
        "guest",
        "guests",
        "anonymous",
        "caller",
        "user",
        "visitor",
        "me",
        "customer",
    }
)


def _is_placeholder_customer_name(name: str | None) -> bool:
    n = str(name or "").strip().lower()
    return not n or n in _PLACEHOLDER_CUSTOMER_NAMES


def _ticket_customer_name_and_contact(
    *,
    customer_identifier: str | None,
    metadata: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    md = metadata or {}
    raw_name = str(md.get("end_user_name") or "").strip() or None
    phone = str(md.get("caller_phone_e164") or "").strip() or None
    resolved = _resolve_customer_identifier(customer_identifier, md).strip() or None

    contact = phone or None
    if not contact and resolved:
        contact = resolved

    name = raw_name
    if _is_placeholder_customer_name(name):
        if resolved and "@" in resolved:
            # Prefer the verified email for dashboard "Guest" column on web calls.
            name = resolved
        elif resolved:
            name = resolved
        elif contact and "@" in contact:
            name = contact

    if not name and contact and "@" in contact:
        name = contact

    return name, contact


def _livekit_api() -> api.LiveKitAPI:
    return api.LiveKitAPI()


async def _resolve_aicc_outbound_trunk() -> api.SIPOutboundTrunkInfo | None:
    async with _livekit_api() as lkapi:
        trunks = await lkapi.sip.list_sip_outbound_trunk(
            api.ListSIPOutboundTrunkRequest()
        )

    items = list(getattr(trunks, "items", []) or [])
    if AICC_OUTBOUND_TRUNK_ID:
        for item in items:
            if (
                str(getattr(item, "sip_trunk_id", "") or "").strip()
                == AICC_OUTBOUND_TRUNK_ID
            ):
                return item
    if AICC_OUTBOUND_TRUNK_NAME:
        for item in items:
            if str(getattr(item, "name", "") or "").strip() == AICC_OUTBOUND_TRUNK_NAME:
                return item
    return items[0] if len(items) == 1 else None


def _is_voice_agent_participant(identity: str) -> bool:
    normalized = str(identity or "").strip()
    return normalized.startswith("agent-")


async def _remove_voice_agent_participants(room_name: str) -> list[str]:
    removed: list[str] = []
    target_room = str(room_name or "").strip()
    if not target_room:
        return removed

    async with _livekit_api() as lkapi:
        participants = await lkapi.room.list_participants(
            api.ListParticipantsRequest(room=target_room)
        )
        for participant in list(getattr(participants, "participants", []) or []):
            identity = str(getattr(participant, "identity", "") or "").strip()
            if not _is_voice_agent_participant(identity):
                continue
            await lkapi.room.remove_participant(
                api.RoomParticipantIdentity(room=target_room, identity=identity)
            )
            removed.append(identity)
    return removed


def _normalize_http_url(value: str | None) -> str:
    resolved = str(value or "").strip()
    if not resolved:
        return ""
    parsed = urlparse(resolved)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return parsed.geturl().rstrip("/")


def _conversation_ticket_headers(metadata: dict[str, Any] | None) -> dict[str, str]:
    """Headers for conversation-service ticket APIs (always business-scoped)."""
    md = metadata or {}
    return {
        "Content-Type": "application/json",
        "X-Service-Token": _read_conversation_service_token(),
        "X-Service-Name": AGENT_CLIENT_ID,
        "X-Business-ID": str(md.get("business_id") or "").strip(),
        "X-Client-ID": str(md.get("client_id") or AGENT_CLIENT_ID),
        "X-Agent-ID": str(md.get("agent_id") or AGENT_NAME),
        "X-Conversation-ID": str(md.get("conversation_id") or ""),
        "X-Session-ID": str(md.get("session_id") or ""),
        "X-End-User-ID": str(md.get("end_user_id") or ""),
    }


def _service_headers(metadata: dict[str, Any] | None) -> dict[str, str]:
    md = metadata or {}
    use_internal_business_ops = _uses_internal_business_ops(md)
    business_scope = (
        str(md.get("business_id") or "").strip()
        if use_internal_business_ops
        else (OPS_SHARED_OWNER_EMAIL or str(md.get("business_id") or "").strip())
    )
    headers = {
        "X-Service-Token": OPS_SERVICE_TOKEN,
        "X-Service-Name": AGENT_CLIENT_ID,
        "X-Business-ID": business_scope,
        "X-Client-ID": str(md.get("client_id") or AGENT_CLIENT_ID),
        "X-Agent-ID": str(md.get("agent_id") or AGENT_NAME),
        "X-Conversation-ID": str(md.get("conversation_id") or ""),
        "X-Session-ID": str(md.get("session_id") or ""),
        "X-End-User-ID": str(md.get("end_user_id") or ""),
    }
    if OPS_SHARED_OWNER_EMAIL and not use_internal_business_ops:
        headers["X-Workspace-Owner-Email"] = OPS_SHARED_OWNER_EMAIL
    return headers


def _knowledge_headers(metadata: dict[str, Any] | None) -> dict[str, str]:
    md = metadata or {}
    return {
        "Content-Type": "application/json",
        "X-Service-Token": KNOWLEDGE_SERVICE_TOKEN,
        "X-Service-Name": AGENT_CLIENT_ID,
        "X-Business-ID": str(md.get("business_id") or "").strip(),
        "X-Agent-ID": str(md.get("agent_id") or AGENT_NAME),
    }


@observe(name="ops_api.request", as_type="span")
async def _request_json(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    started_ms = monotonic_ms()
    resolved_override = str(base_url or "").strip()
    base_url = resolved_override or _ops_base_url(metadata)
    if not str(base_url or "").strip():
        output = {"status": "failed", "message": "Hotel ops backend is not configured."}
        _emit_tool_latency(
            metadata,
            "tool_http_completed",
            service="ops",
            method=method,
            path=path,
            status="failed",
            duration_ms=elapsed_ms(started_ms),
            failure_reason="missing_base_url",
        )
        update_observation(output=output)
        return output
    url = f"{base_url}{path}"
    headers = _service_headers(metadata)
    if not str(headers.get("X-Business-ID") or "").strip():
        output = {
            "status": "failed",
            "message": "Missing business scope for ops request.",
        }
        _emit_tool_latency(
            metadata,
            "tool_http_completed",
            service="ops",
            method=method,
            path=path,
            status="failed",
            duration_ms=elapsed_ms(started_ms),
            failure_reason="missing_business_scope",
        )
        update_observation(output=output)
        return output
    update_observation(
        input={
            "method": method,
            "path": path,
            "json": json_body,
            "headers": headers,
        }
    )
    try:
        logger.info(
            "OPS request %s %s base_url=%s business_scope=%s end_user=%s body=%s",
            method,
            path,
            base_url,
            headers.get("X-Business-ID"),
            headers.get("X-End-User-ID"),
            json_body,
        )
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.request(
                method=method,
                url=url,
                json=json_body,
                headers=headers,
            )
    except httpx.TimeoutException:
        output = {"status": "failed", "message": "Ops backend request timed out."}
        _emit_tool_latency(
            metadata,
            "tool_http_completed",
            service="ops",
            method=method,
            path=path,
            status="timeout",
            duration_ms=elapsed_ms(started_ms),
        )
        update_observation(output=output)
        return output
    except httpx.HTTPError:
        output = {"status": "failed", "message": "Ops backend is unavailable."}
        _emit_tool_latency(
            metadata,
            "tool_http_completed",
            service="ops",
            method=method,
            path=path,
            status="http_error",
            duration_ms=elapsed_ms(started_ms),
        )
        update_observation(output=output)
        return output

    payload: dict[str, Any] | list[Any] | None
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if response.status_code >= 400:
        detail = "Request failed."
        if isinstance(payload, dict):
            detail = str(payload.get("detail") or detail)
        output = {
            "status": "failed",
            "message": detail,
            "http_status": response.status_code,
        }
        _emit_tool_latency(
            metadata,
            "tool_http_completed",
            service="ops",
            method=method,
            path=path,
            status="failed",
            http_status=response.status_code,
            duration_ms=elapsed_ms(started_ms),
        )
        update_observation(output=output)
        return output

    if isinstance(payload, dict):
        logger.info("OPS response %s %s -> %s", method, path, payload)
        payload["status"] = "success"
        _emit_tool_latency(
            metadata,
            "tool_http_completed",
            service="ops",
            method=method,
            path=path,
            status="success",
            http_status=response.status_code,
            duration_ms=elapsed_ms(started_ms),
        )
        update_observation(output=payload)
        return payload
    if isinstance(payload, list):
        output = {"status": "success", "items": payload}
        logger.info("OPS response %s %s -> %s", method, path, output)
        _emit_tool_latency(
            metadata,
            "tool_http_completed",
            service="ops",
            method=method,
            path=path,
            status="success",
            http_status=response.status_code,
            duration_ms=elapsed_ms(started_ms),
            items_count=len(payload),
        )
        update_observation(output=output)
        return output

    output = {"status": "failed", "message": "Invalid response from ops backend."}
    _emit_tool_latency(
        metadata,
        "tool_http_completed",
        service="ops",
        method=method,
        path=path,
        status="failed",
        http_status=response.status_code,
        duration_ms=elapsed_ms(started_ms),
        failure_reason="invalid_payload",
    )
    update_observation(output=output)
    return output


@observe(name="ops_api.conversation_request", as_type="span")
async def _request_conversation_service_json(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started_ms = monotonic_ms()
    base_url = _read_conversation_api_base_url()
    if not base_url:
        output = {
            "status": "failed",
            "message": "Conversation service is not configured for tickets.",
        }
        _emit_tool_latency(
            metadata,
            "tool_http_completed",
            service="conversation",
            method=method,
            path=path,
            status="failed",
            duration_ms=elapsed_ms(started_ms),
            failure_reason="missing_base_url",
        )
        update_observation(output=output)
        return output
    url = f"{base_url}{path}"
    headers = _conversation_ticket_headers(metadata)
    if not str(headers.get("X-Business-ID") or "").strip():
        output = {
            "status": "failed",
            "message": "Missing business scope for ticket request.",
        }
        _emit_tool_latency(
            metadata,
            "tool_http_completed",
            service="conversation",
            method=method,
            path=path,
            status="failed",
            duration_ms=elapsed_ms(started_ms),
            failure_reason="missing_business_scope",
        )
        update_observation(output=output)
        return output
    update_observation(
        input={
            "method": method,
            "path": path,
            "json": json_body,
            "headers": {k: v for k, v in headers.items() if k != "X-Service-Token"},
        }
    )
    try:
        logger.info(
            "Conversation ticket request %s %s base_url=%s business_scope=%s end_user=%s body=%s",
            method,
            path,
            base_url,
            headers.get("X-Business-ID"),
            headers.get("X-End-User-ID"),
            json_body,
        )
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.request(
                method=method,
                url=url,
                json=json_body,
                headers=headers,
            )
    except httpx.TimeoutException:
        output = {
            "status": "failed",
            "message": "Conversation service ticket request timed out.",
        }
        _emit_tool_latency(
            metadata,
            "tool_http_completed",
            service="conversation",
            method=method,
            path=path,
            status="timeout",
            duration_ms=elapsed_ms(started_ms),
        )
        update_observation(output=output)
        return output
    except httpx.HTTPError:
        output = {
            "status": "failed",
            "message": "Conversation service is unavailable.",
        }
        _emit_tool_latency(
            metadata,
            "tool_http_completed",
            service="conversation",
            method=method,
            path=path,
            status="http_error",
            duration_ms=elapsed_ms(started_ms),
        )
        update_observation(output=output)
        return output

    payload: dict[str, Any] | list[Any] | None
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if response.status_code >= 400:
        detail = "Request failed."
        if isinstance(payload, dict):
            detail = str(payload.get("detail") or detail)
        output = {
            "status": "failed",
            "message": detail,
            "http_status": response.status_code,
        }
        _emit_tool_latency(
            metadata,
            "tool_http_completed",
            service="conversation",
            method=method,
            path=path,
            status="failed",
            http_status=response.status_code,
            duration_ms=elapsed_ms(started_ms),
        )
        update_observation(output=output)
        return output

    if isinstance(payload, dict):
        logger.info("Conversation ticket response %s %s -> %s", method, path, payload)
        payload["status"] = "success"
        _emit_tool_latency(
            metadata,
            "tool_http_completed",
            service="conversation",
            method=method,
            path=path,
            status="success",
            http_status=response.status_code,
            duration_ms=elapsed_ms(started_ms),
        )
        update_observation(output=payload)
        return payload

    output = {"status": "failed", "message": "Invalid response from conversation service."}
    _emit_tool_latency(
        metadata,
        "tool_http_completed",
        service="conversation",
        method=method,
        path=path,
        status="failed",
        http_status=response.status_code,
        duration_ms=elapsed_ms(started_ms),
        failure_reason="invalid_payload",
    )
    update_observation(output=output)
    return output


def _trace(
    tool_name: str, metadata: dict[str, Any] | None, user_id: str | None = None
) -> None:
    md = metadata or {}
    trace_tool(
        tool_name,
        metadata=md,
        user_id=user_id or str(md.get("end_user_id") or ""),
        session_id=str(md.get("conversation_id") or md.get("session_id") or ""),
    )


@observe(name="tool.search_business_knowledge", as_type="tool")
async def search_business_knowledge(
    *,
    query: str,
    top_k: int = 4,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started_ms = monotonic_ms()
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("search_business_knowledge", metadata, user_id=caller_id)
    base_url = str(KNOWLEDGE_SERVICE_BASE_URL or "").strip()
    if not base_url:
        output = {
            "status": "failed",
            "message": "Business knowledge lookup is not configured.",
        }
        _emit_tool_latency(
            metadata,
            "knowledge_lookup_completed",
            status="failed",
            duration_ms=elapsed_ms(started_ms),
            failure_reason="missing_base_url",
        )
        update_observation(output=output)
        return output

    headers = _knowledge_headers(metadata)
    if not str(headers.get("X-Business-ID") or "").strip():
        output = {
            "status": "failed",
            "message": "Missing business scope for knowledge lookup.",
        }
        _emit_tool_latency(
            metadata,
            "knowledge_lookup_completed",
            status="failed",
            duration_ms=elapsed_ms(started_ms),
            failure_reason="missing_business_scope",
        )
        update_observation(output=output)
        return output

    knowledge_base_ids = [
        str(kb_id).strip()
        for kb_id in ((metadata or {}).get("knowledge_base_ids") or [])
        if str(kb_id).strip()
    ]
    request_body = {
        "query": str(query or "").strip(),
        "top_k": int(max(1, min(int(top_k or 4), 6))),
        "knowledge_base_ids": knowledge_base_ids,
    }
    update_observation(
        input={
            "method": "POST",
            "path": "/v1/knowledge/search",
            "json": request_body,
            "headers": headers,
        }
    )
    try:
        async with httpx.AsyncClient(
            timeout=KNOWLEDGE_SERVICE_TIMEOUT_SECONDS
        ) as client:
            response = await client.post(
                f"{base_url}/v1/knowledge/search",
                json=request_body,
                headers=headers,
            )
    except httpx.TimeoutException:
        output = {"status": "failed", "message": "Knowledge lookup timed out."}
        _emit_tool_latency(
            metadata,
            "knowledge_lookup_completed",
            status="timeout",
            duration_ms=elapsed_ms(started_ms),
            requested_top_k=request_body["top_k"],
            knowledge_base_count=len(knowledge_base_ids),
        )
        update_observation(output=output)
        return output
    except httpx.HTTPError:
        output = {"status": "failed", "message": "Knowledge lookup is unavailable."}
        _emit_tool_latency(
            metadata,
            "knowledge_lookup_completed",
            status="http_error",
            duration_ms=elapsed_ms(started_ms),
            requested_top_k=request_body["top_k"],
            knowledge_base_count=len(knowledge_base_ids),
        )
        update_observation(output=output)
        return output

    try:
        payload = response.json()
    except ValueError:
        payload = {}

    if response.status_code >= 400:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        output = {
            "status": "failed",
            "message": str(detail or "Knowledge lookup failed."),
        }
        _emit_tool_latency(
            metadata,
            "knowledge_lookup_completed",
            status="failed",
            http_status=response.status_code,
            duration_ms=elapsed_ms(started_ms),
            requested_top_k=request_body["top_k"],
            knowledge_base_count=len(knowledge_base_ids),
        )
        update_observation(output=output)
        return output

    matches = payload.get("matches") if isinstance(payload, dict) else None
    if not isinstance(matches, list) or not matches:
        output = {
            "status": "success",
            "matches": [],
            "message": "No matching business knowledge was found.",
        }
        _emit_tool_latency(
            metadata,
            "knowledge_lookup_completed",
            status="success",
            http_status=response.status_code,
            duration_ms=elapsed_ms(started_ms),
            matches_count=0,
            requested_top_k=request_body["top_k"],
            knowledge_base_count=len(knowledge_base_ids),
        )
        update_observation(output=output)
        return output

    normalized_matches: list[dict[str, Any]] = []
    for match in matches[: request_body["top_k"]]:
        if not isinstance(match, dict):
            continue
        normalized_matches.append(
            {
                "source_name": str(match.get("source_name") or "Knowledge"),
                "source_type": str(match.get("source_type") or "text"),
                "score": float(match.get("score") or 0.0),
                "text": str(match.get("text") or "").strip()[:1500],
            }
        )

    output = {"status": "success", "matches": normalized_matches}
    _emit_tool_latency(
        metadata,
        "knowledge_lookup_completed",
        status="success",
        http_status=response.status_code,
        duration_ms=elapsed_ms(started_ms),
        matches_count=len(normalized_matches),
        requested_top_k=request_body["top_k"],
        knowledge_base_count=len(knowledge_base_ids),
    )
    update_observation(output=output)
    return output


@observe(name="tool.lookup_customer_account", as_type="tool")
async def lookup_customer_account(
    *,
    customer_identifier: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("lookup_customer_account", metadata, user_id=caller_id)
    resolved_customer_identifier = _resolve_customer_identifier(
        customer_identifier, metadata
    )
    return await _request_json(
        "POST",
        "/v1/tools/customer-account/lookup",
        json_body={"customer_identifier": resolved_customer_identifier},
        metadata=metadata,
    )


@observe(name="tool.get_tariff_profile", as_type="tool")
async def get_tariff_profile(
    *,
    customer_identifier: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("get_tariff_profile", metadata, user_id=caller_id)
    resolved_customer_identifier = _resolve_customer_identifier(
        customer_identifier, metadata
    )
    return await _request_json(
        "POST",
        "/v1/tools/tariff-profile",
        json_body={"customer_identifier": resolved_customer_identifier},
        metadata=metadata,
    )


@observe(name="tool.get_payment_summary", as_type="tool")
async def get_payment_summary(
    *,
    customer_identifier: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("get_payment_summary", metadata, user_id=caller_id)
    resolved_customer_identifier = _resolve_customer_identifier(
        customer_identifier, metadata
    )
    return await _request_json(
        "POST",
        "/v1/tools/payments/summary",
        json_body={"customer_identifier": resolved_customer_identifier},
        metadata=metadata,
    )


@observe(name="tool.get_vending_history", as_type="tool")
async def get_vending_history(
    *,
    customer_identifier: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("get_vending_history", metadata, user_id=caller_id)
    resolved_customer_identifier = _resolve_customer_identifier(
        customer_identifier, metadata
    )
    return await _request_json(
        "POST",
        "/v1/tools/vending/history",
        json_body={"customer_identifier": resolved_customer_identifier},
        metadata=metadata,
    )


@observe(name="tool.get_account_overview", as_type="tool")
async def get_account_overview(
    *,
    customer_identifier: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("get_account_overview", metadata, user_id=caller_id)
    resolved_customer_identifier = _resolve_customer_identifier(
        customer_identifier, metadata
    )
    return await _request_json(
        "POST",
        "/v1/tools/account/overview",
        json_body={"customer_identifier": resolved_customer_identifier},
        metadata=metadata,
    )


@observe(name="tool.get_recent_transactions", as_type="tool")
async def get_recent_transactions(
    *,
    customer_identifier: str | None = None,
    limit: int = 5,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("get_recent_transactions", metadata, user_id=caller_id)
    resolved_customer_identifier = _resolve_customer_identifier(
        customer_identifier, metadata
    )
    return await _request_json(
        "POST",
        "/v1/tools/transactions/recent",
        json_body={
            "customer_identifier": resolved_customer_identifier,
            "limit": limit,
        },
        metadata=metadata,
    )


@observe(name="tool.check_transaction_status", as_type="tool")
async def check_transaction_status(
    *,
    customer_identifier: str | None = None,
    transaction_reference: str | None = None,
    amount_naira: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("check_transaction_status", metadata, user_id=caller_id)
    resolved_customer_identifier = _resolve_customer_identifier(
        customer_identifier, metadata
    )
    return await _request_json(
        "POST",
        "/v1/tools/transactions/status",
        json_body={
            "customer_identifier": resolved_customer_identifier,
            "transaction_reference": transaction_reference,
            "amount_naira": amount_naira,
        },
        metadata=metadata,
    )


@observe(name="tool.block_card", as_type="tool")
async def block_card(
    *,
    customer_identifier: str | None = None,
    last4: str | None = None,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("block_card", metadata, user_id=caller_id)
    resolved_customer_identifier = _resolve_customer_identifier(
        customer_identifier, metadata
    )
    return await _request_json(
        "POST",
        "/v1/tools/cards/block",
        json_body={
            "customer_identifier": resolved_customer_identifier,
            "last4": last4,
            "reason": reason,
        },
        metadata=metadata,
    )


@observe(name="tool.unblock_card", as_type="tool")
async def unblock_card(
    *,
    customer_identifier: str | None = None,
    last4: str | None = None,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("unblock_card", metadata, user_id=caller_id)
    resolved_customer_identifier = _resolve_customer_identifier(
        customer_identifier, metadata
    )
    return await _request_json(
        "POST",
        "/v1/tools/cards/unblock",
        json_body={
            "customer_identifier": resolved_customer_identifier,
            "last4": last4,
            "reason": reason,
        },
        metadata=metadata,
    )


@observe(name="tool.reverse_failed_transaction", as_type="tool")
async def reverse_failed_transaction(
    *,
    customer_identifier: str | None = None,
    transaction_reference: str,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("reverse_failed_transaction", metadata, user_id=caller_id)
    resolved_customer_identifier = _resolve_customer_identifier(
        customer_identifier, metadata
    )
    return await _request_json(
        "POST",
        "/v1/tools/transactions/reverse",
        json_body={
            "customer_identifier": resolved_customer_identifier,
            "transaction_reference": transaction_reference,
            "reason": reason,
        },
        metadata=metadata,
    )


@observe(name="tool.create_ticket", as_type="tool")
async def create_ticket(
    *,
    customer_identifier: str | None = None,
    title: str,
    description: str,
    issue_type: str = "general",
    priority: str = "high",
    requires_human: bool = True,
    case_reference: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("create_ticket", metadata, user_id=caller_id)
    conversation_id = (
        str((metadata or {}).get("conversation_id") or "").strip() or None
    )
    agent_id = str((metadata or {}).get("agent_id") or "").strip() or None
    customer_name, customer_contact = _ticket_customer_name_and_contact(
        customer_identifier=customer_identifier,
        metadata=metadata,
    )

    dashboard_body: dict[str, Any] = {
        "customer_name": customer_name,
        "customer_contact": customer_contact,
        "title": title,
        "description": description,
        "priority": priority,
        "status": "open",
    }
    if conversation_id:
        dashboard_body["conversation_id"] = conversation_id
    if agent_id:
        dashboard_body["agent_id"] = agent_id

    if _read_conversation_api_base_url():
        return await _request_conversation_service_json(
            "POST",
            "/v1/tickets",
            json_body=dashboard_body,
            metadata=metadata,
        )

    if _uses_internal_business_ops(metadata):
        ticket_base = _internal_ticket_http_base_url(metadata)
        return await _request_json(
            "POST",
            "/v1/tickets",
            json_body=dashboard_body,
            metadata=metadata,
            base_url=ticket_base,
        )

    resolved_customer_identifier = _resolve_customer_identifier(
        customer_identifier, metadata
    )
    body: dict[str, Any] = {
        "customer_identifier": resolved_customer_identifier,
        "title": title,
        "description": description,
        "issue_type": issue_type,
        "priority": priority,
        "requires_human": requires_human,
        "conversation_id": str((metadata or {}).get("conversation_id") or ""),
    }
    if case_reference:
        body["case_reference"] = case_reference
    return await _request_json(
        "POST", "/v1/tools/tickets/create", json_body=body, metadata=metadata
    )


@observe(name="tool.transfer_to_aicc", as_type="tool")
async def transfer_to_aicc(
    *,
    reason_summary: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("transfer_to_aicc", metadata, user_id=caller_id)

    room_name = str((metadata or {}).get("room_name") or "").strip()
    if not room_name:
        return {
            "status": "failed",
            "message": "This call is missing room context, so I cannot transfer it yet.",
        }

    target_number = AICC_TRANSFER_TARGET_NUMBER.strip()
    from_number_candidates = _aicc_transfer_from_number_candidates(metadata)
    if not from_number_candidates:
        from_number_candidates = [target_number]
    if not target_number:
        return {
            "status": "failed",
            "message": "The AICC transfer destination is not configured yet.",
        }

    outbound_trunk = await _resolve_aicc_outbound_trunk()
    if not outbound_trunk or not str(getattr(outbound_trunk, "sip_trunk_id", "") or "").strip():
        return {
            "status": "failed",
            "message": "The AICC outbound SIP trunk is not configured yet.",
        }

    session_ref = str((metadata or {}).get("session_id") or "").strip() or "session"
    turn_ref = str((metadata or {}).get("turn_index") or "").strip() or "0"
    original_caller_number = str(
        (metadata or {}).get("sip_caller_number")
        or (metadata or {}).get("caller_phone_e164")
        or ""
    ).strip()
    if AICC_HANDOFF_DELAY_SECONDS > 0:
        logger.info(
            "[TOOL] transfer_to_aicc waiting before bridge: room=%s delay_seconds=%.2f",
            room_name,
            AICC_HANDOFF_DELAY_SECONDS,
        )
        await asyncio.sleep(AICC_HANDOFF_DELAY_SECONDS)

    participant = None
    successful_from_number = ""
    attempt_errors: list[dict[str, str]] = []
    try:
        async with _livekit_api() as lkapi:
            for attempt_index, from_number in enumerate(from_number_candidates, start=1):
                participant_identity = (
                    f"aicc_bridge_{session_ref}_{turn_ref}_{attempt_index}"
                )
                participant_metadata = {
                    "direction": "outbound",
                    "target_number": target_number,
                    "from_number": from_number,
                    "original_caller_number": original_caller_number,
                    "caller_id_attempt": attempt_index,
                    "caller_id_candidates": from_number_candidates,
                    "owner": "voice_agent",
                }
                if reason_summary:
                    participant_metadata["reason_summary"] = str(reason_summary).strip()[:240]

                request = api.CreateSIPParticipantRequest(
                    sip_trunk_id=str(
                        getattr(outbound_trunk, "sip_trunk_id", "") or ""
                    ).strip(),
                    sip_call_to=target_number,
                    sip_number=from_number,
                    room_name=room_name,
                    participant_identity=participant_identity,
                    participant_name="AICC bridge",
                    display_name="AICC bridge",
                    participant_metadata=json.dumps(participant_metadata),
                    participant_attributes={
                        "call_role": "aicc_bridge",
                        "route_number": target_number,
                        "from_number": from_number,
                        "original_caller_number": original_caller_number,
                    },
                    headers={
                        "X-Odion-Entry-Surface": "voice-agent",
                        "X-Odion-Room-Name": room_name,
                        "X-Odion-Caller-Number": original_caller_number or from_number,
                        "X-Odion-Caller-ID-Attempt": str(attempt_index),
                    },
                    play_dialtone=True,
                    wait_until_answered=True,
                    hide_phone_number=False,
                )

                try:
                    participant = await lkapi.sip.create_sip_participant(request)
                    successful_from_number = from_number
                    if attempt_index > 1:
                        logger.info(
                            "[TOOL] transfer_to_aicc succeeded after retry: room=%s target=%s from=%s attempts=%s",
                            room_name,
                            target_number,
                            from_number,
                            from_number_candidates[:attempt_index],
                        )
                    break
                except Exception as exc:
                    error_message = str(exc).strip()
                    attempt_errors.append(
                        {"from_number": from_number, "error": error_message[:500]}
                    )
                    logger.warning(
                        "[TOOL] transfer_to_aicc attempt failed: room=%s target=%s from=%s attempt=%s/%s trunk=%s error=%s",
                        room_name,
                        target_number,
                        from_number,
                        attempt_index,
                        len(from_number_candidates),
                        str(getattr(outbound_trunk, "name", "") or "").strip(),
                        error_message,
                    )

            if participant is None:
                raise RuntimeError(
                    "; ".join(
                        f"{item['from_number']}: {item['error']}"
                        for item in attempt_errors
                    )
                    or "AICC transfer failed before a SIP participant was created"
                )
    except Exception:
        return {
            "status": "failed",
            "message": (
                "I could not connect a colleague right now. "
                "Please stay on the line while I continue helping."
            ),
            "room_name": room_name,
            "target_number": target_number,
            "from_number": from_number_candidates[0],
            "attempted_from_numbers": from_number_candidates,
            "outbound_trunk_name": str(
                getattr(outbound_trunk, "name", "") or ""
            ).strip(),
            "outbound_trunk_id": str(
                getattr(outbound_trunk, "sip_trunk_id", "") or ""
            ).strip(),
            "errors": attempt_errors,
            "error": (attempt_errors[-1]["error"] if attempt_errors else "")[:500],
        }

    removed_agent_participants: list[str] = []
    remove_agent_error = ""
    if AICC_REMOVE_AGENT_AFTER_TRANSFER:
        try:
            removed_agent_participants = await _remove_voice_agent_participants(
                room_name
            )
        except Exception as exc:
            remove_agent_error = str(exc)
            logger.warning(
                "[TOOL] transfer_to_aicc bridge started but agent removal failed: room=%s error=%s",
                room_name,
                remove_agent_error,
            )

    result = {
        "status": "success",
        "message": "AICC transfer has started.",
        "room_name": room_name,
        "target_number": target_number,
        "from_number": successful_from_number,
        "attempted_from_numbers": from_number_candidates,
        "participant_identity": str(
            getattr(participant, "participant_identity", "") or ""
        ).strip(),
        "participant_id": str(getattr(participant, "participant_id", "") or "").strip(),
        "sip_call_id": str(getattr(participant, "sip_call_id", "") or "").strip(),
        "outbound_trunk_name": str(getattr(outbound_trunk, "name", "") or "").strip(),
        "outbound_trunk_id": str(
            getattr(outbound_trunk, "sip_trunk_id", "") or ""
        ).strip(),
        "agent_removed": bool(removed_agent_participants),
        "removed_agent_participants": removed_agent_participants,
    }
    if remove_agent_error:
        result["agent_remove_error"] = remove_agent_error
    logger.info(
        "[TOOL] transfer_to_aicc room=%s target=%s from=%s trunk=%s agent_removed=%s",
        room_name,
        target_number,
        successful_from_number,
        result["outbound_trunk_name"],
        bool(removed_agent_participants),
    )
    return result


@observe(name="tool.create_booking", as_type="tool")
async def create_booking(
    *,
    customer_identifier: str | None = None,
    guest_name: str | None = None,
    room_type: str,
    check_in_date: str,
    check_out_date: str,
    guest_count: int = 1,
    special_requests: str | None = None,
    price_snapshot: dict[str, Any] | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("create_booking", metadata, user_id=caller_id)
    if _business_use_case(metadata) == "hotel":
        conversation_id = (
            str((metadata or {}).get("conversation_id") or "").strip() or None
        )
        agent_id = str((metadata or {}).get("agent_id") or "").strip() or None
        body: dict[str, Any] = {
            "customer_name": guest_name
            or str((metadata or {}).get("end_user_name") or "").strip()
            or None,
            "customer_contact": str(
                (metadata or {}).get("caller_phone_e164") or ""
            ).strip()
            or None,
            "room_type": room_type,
            "stay_start": check_in_date,
            "stay_end": check_out_date,
            "guest_count": guest_count,
            "price_snapshot": price_snapshot
            if isinstance(price_snapshot, dict)
            else None,
            "status": "pending",
            "notes": special_requests,
            "conversation_id": conversation_id,
            "agent_id": agent_id,
        }
        return await _request_json(
            "POST", "/v1/bookings", json_body=body, metadata=metadata
        )

    resolved_customer_identifier = _resolve_customer_identifier(
        customer_identifier, metadata
    )
    body: dict[str, Any] = {
        "customer_identifier": resolved_customer_identifier,
        "guest_name": guest_name,
        "room_type": room_type,
        "check_in_date": check_in_date,
        "check_out_date": check_out_date,
        "guest_count": guest_count,
        "special_requests": special_requests,
        "price_snapshot": price_snapshot,
        "conversation_id": str((metadata or {}).get("conversation_id") or ""),
    }
    return await _request_json(
        "POST", "/v1/tools/bookings/create", json_body=body, metadata=metadata
    )


@observe(name="tool.create_order", as_type="tool")
async def create_order(
    *,
    customer_identifier: str | None = None,
    item_name: str = "",
    quantity: int = 1,
    items: list[dict[str, Any]] | None = None,
    customer_name: str | None = None,
    notes: str | None = None,
    price_snapshot: dict[str, Any] | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("create_order", metadata, user_id=caller_id)

    order_items = items if items else [{"item_name": item_name, "quantity": quantity}]

    if _business_use_case(metadata) in {"restaurant", "fashion"}:
        conversation_id = (
            str((metadata or {}).get("conversation_id") or "").strip() or None
        )
        agent_id = str((metadata or {}).get("agent_id") or "").strip() or None

        results = []
        for order_item in order_items:
            current_item_name = str(order_item.get("item_name") or item_name).strip()
            current_quantity = int(order_item.get("quantity") or quantity or 1)
            current_price = order_item.get("price_snapshot") or price_snapshot

            if not current_item_name:
                continue

            body: dict[str, Any] = {
                "customer_name": customer_name
                or str((metadata or {}).get("end_user_name") or "").strip()
                or None,
                "customer_contact": str(
                    (metadata or {}).get("caller_phone_e164") or ""
                ).strip()
                or str((metadata or {}).get("end_user_id") or "").strip()
                or None,
                "item_name": current_item_name,
                "quantity": current_quantity,
                "price_snapshot": current_price
                if isinstance(current_price, dict)
                else None,
                "status": "pending",
                "notes": notes,
                "conversation_id": conversation_id,
                "agent_id": agent_id,
            }
            res = await _request_json(
                "POST", "/v1/orders", json_body=body, metadata=metadata
            )
            results.append(res)

        if not results:
            return {"status": "failed", "message": "No valid items to order."}

        # Check if any failed
        failed = [r for r in results if r.get("status") == "failed"]
        if failed:
            return failed[0]  # return the first error

        return results[0]  # Return the first success result to satisfy the schema

    resolved_customer_identifier = _resolve_customer_identifier(
        customer_identifier, metadata
    )

    results = []
    for order_item in order_items:
        current_item_name = str(order_item.get("item_name") or item_name).strip()
        current_quantity = int(order_item.get("quantity") or quantity or 1)
        current_price = order_item.get("price_snapshot") or price_snapshot

        if not current_item_name:
            continue

        body = {
            "customer_identifier": resolved_customer_identifier,
            "item_name": current_item_name,
            "quantity": current_quantity,
            "customer_name": customer_name,
            "notes": notes,
            "price_snapshot": current_price,
            "conversation_id": str((metadata or {}).get("conversation_id") or ""),
        }
        res = await _request_json(
            "POST", "/v1/tools/orders/create", json_body=body, metadata=metadata
        )
        results.append(res)

    if not results:
        return {"status": "failed", "message": "No valid items to order."}

    # Check if any failed
    failed = [r for r in results if r.get("status") == "failed"]
    if failed:
        return failed[0]  # return the first error

    return results[0]  # Return the first success result


@observe(name="tool.fetch_room_availability", as_type="tool")
async def fetch_room_availability(
    *,
    endpoint_url: str | None = None,
    room_type: str | None = None,
    check_in_date: str | None = None,
    check_out_date: str | None = None,
    guest_count: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("fetch_room_availability", metadata, user_id=caller_id)
    resolved_endpoint = _normalize_http_url(
        endpoint_url or (metadata or {}).get("live_data_endpoint")
    )
    if not resolved_endpoint:
        output = {
            "status": "failed",
            "message": "Current room availability cannot be checked right now.",
        }
        update_observation(output=output)
        return output

    headers = {
        "Content-Type": "application/json",
        "X-Business-ID": str((metadata or {}).get("business_id") or ""),
        "X-Conversation-ID": str((metadata or {}).get("conversation_id") or ""),
        "X-Session-ID": str((metadata or {}).get("session_id") or ""),
        "X-End-User-ID": str((metadata or {}).get("end_user_id") or ""),
        "X-Client-ID": str((metadata or {}).get("client_id") or AGENT_CLIENT_ID),
        "X-Agent-ID": str((metadata or {}).get("agent_id") or AGENT_NAME),
    }
    body: dict[str, Any] = {
        "room_type": room_type,
        "check_in_date": check_in_date,
        "check_out_date": check_out_date,
        "guest_count": guest_count,
    }
    update_observation(
        input={
            "method": "POST",
            "path": resolved_endpoint,
            "json": body,
            "headers": headers,
        }
    )
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.post(resolved_endpoint, json=body, headers=headers)
    except httpx.TimeoutException:
        output = {
            "status": "failed",
            "message": "I couldn't check the current room availability in time.",
        }
        update_observation(output=output)
        return output
    except httpx.HTTPError:
        output = {
            "status": "failed",
            "message": "I can't check the current room availability right now.",
        }
        update_observation(output=output)
        return output

    try:
        payload = response.json()
    except ValueError:
        payload = None

    if response.status_code >= 400:
        detail = "Request failed."
        if isinstance(payload, dict):
            detail = str(payload.get("detail") or payload.get("message") or detail)
        output = {
            "status": "failed",
            "message": detail,
            "http_status": response.status_code,
        }
        update_observation(output=output)
        return output

    if isinstance(payload, dict):
        payload.setdefault("status", "success")
        update_observation(output=payload)
        return payload
    if isinstance(payload, list):
        output = {"status": "success", "items": payload}
        update_observation(output=output)
        return output

    output = {
        "status": "failed",
        "message": "Invalid response from room availability service.",
    }
    update_observation(output=output)
    return output


async def _fetch_live_catalog(
    *,
    endpoint_url: str | None = None,
    body: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    unavailable_message: str,
    timeout_message: str,
    service_unavailable_message: str,
    invalid_response_message: str,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    resolved_endpoint = _normalize_http_url(
        endpoint_url or (metadata or {}).get("live_data_endpoint")
    )
    if not resolved_endpoint:
        output = {"status": "failed", "message": unavailable_message}
        update_observation(output=output)
        return output

    headers = {
        "Content-Type": "application/json",
        "X-Business-ID": str((metadata or {}).get("business_id") or ""),
        "X-Conversation-ID": str((metadata or {}).get("conversation_id") or ""),
        "X-Session-ID": str((metadata or {}).get("session_id") or ""),
        "X-End-User-ID": str((metadata or {}).get("end_user_id") or ""),
        "X-Client-ID": str((metadata or {}).get("client_id") or AGENT_CLIENT_ID),
        "X-Agent-ID": str((metadata or {}).get("agent_id") or AGENT_NAME),
    }
    update_observation(
        input={
            "method": "POST",
            "path": resolved_endpoint,
            "json": body or {},
            "headers": headers,
            "caller_id": caller_id,
        }
    )
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.post(
                resolved_endpoint, json=body or {}, headers=headers
            )
    except httpx.TimeoutException:
        output = {"status": "failed", "message": timeout_message}
        update_observation(output=output)
        return output
    except httpx.HTTPError:
        output = {"status": "failed", "message": service_unavailable_message}
        update_observation(output=output)
        return output

    try:
        payload = response.json()
    except ValueError:
        payload = None

    if response.status_code >= 400:
        detail = "Request failed."
        if isinstance(payload, dict):
            detail = str(payload.get("detail") or payload.get("message") or detail)
        output = {
            "status": "failed",
            "message": detail,
            "http_status": response.status_code,
        }
        update_observation(output=output)
        return output

    if isinstance(payload, dict):
        payload.setdefault("status", "success")
        update_observation(output=payload)
        return payload
    if isinstance(payload, list):
        output = {"status": "success", "items": payload}
        update_observation(output=output)
        return output

    output = {"status": "failed", "message": invalid_response_message}
    update_observation(output=output)
    return output


@observe(name="tool.fetch_menu_availability", as_type="tool")
async def fetch_menu_availability(
    *,
    endpoint_url: str | None = None,
    item_name: str | None = None,
    party_size: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("fetch_menu_availability", metadata, user_id=caller_id)
    return await _fetch_live_catalog(
        endpoint_url=endpoint_url,
        body={"item_name": item_name, "party_size": party_size},
        metadata=metadata,
        unavailable_message="The current menu and prices cannot be checked right now.",
        timeout_message="I couldn't check the current menu in time.",
        service_unavailable_message="I can't check the current menu right now.",
        invalid_response_message="I couldn't read the current menu details properly.",
    )


@observe(name="tool.fetch_product_availability", as_type="tool")
async def fetch_product_availability(
    *,
    endpoint_url: str | None = None,
    product_name: str | None = None,
    size: str | None = None,
    color: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("fetch_product_availability", metadata, user_id=caller_id)
    return await _fetch_live_catalog(
        endpoint_url=endpoint_url,
        body={"product_name": product_name, "size": size, "color": color},
        metadata=metadata,
        unavailable_message="Current product availability and prices cannot be checked right now.",
        timeout_message="I couldn't check the current product availability in time.",
        service_unavailable_message="I can't check current product availability right now.",
        invalid_response_message="I couldn't read the current product details properly.",
    )


@observe(name="tool.create_complaint_ticket", as_type="tool")
async def create_complaint_ticket(
    *,
    customer_identifier: str | None = None,
    title: str,
    description: str,
    priority: str = "high",
    case_reference: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("create_complaint_ticket", metadata, user_id=caller_id)
    resolved_customer_identifier = _resolve_customer_identifier(
        customer_identifier, metadata
    )
    body: dict[str, Any] = {
        "customer_identifier": resolved_customer_identifier,
        "title": title,
        "description": description,
        "priority": priority,
        "conversation_id": str((metadata or {}).get("conversation_id") or ""),
    }
    if case_reference:
        body["case_reference"] = case_reference
    return await _request_json(
        "POST",
        "/v1/tools/tickets/create",
        json_body=body,
        metadata=metadata,
    )


@observe(name="tool.report_outage", as_type="tool")
async def report_outage(
    *,
    customer_identifier: str | None = None,
    summary: str,
    priority: str = "high",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("report_outage", metadata, user_id=caller_id)
    resolved_customer_identifier = _resolve_customer_identifier(
        customer_identifier, metadata
    )
    body: dict[str, Any] = {
        "customer_identifier": resolved_customer_identifier,
        "summary": summary,
        "priority": priority,
        "conversation_id": str((metadata or {}).get("conversation_id") or ""),
    }
    return await _request_json(
        "POST",
        "/v1/tools/outages/report",
        json_body=body,
        metadata=metadata,
    )


@observe(name="tool.create_meter_request", as_type="tool")
async def create_meter_request(
    *,
    customer_identifier: str | None = None,
    summary: str,
    priority: str = "normal",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("create_meter_request", metadata, user_id=caller_id)
    resolved_customer_identifier = _resolve_customer_identifier(
        customer_identifier, metadata
    )
    body: dict[str, Any] = {
        "customer_identifier": resolved_customer_identifier,
        "summary": summary,
        "priority": priority,
        "conversation_id": str((metadata or {}).get("conversation_id") or ""),
    }
    return await _request_json(
        "POST",
        "/v1/tools/meter-requests/create",
        json_body=body,
        metadata=metadata,
    )


@observe(name="tool.apply_billing_adjustment", as_type="tool")
async def apply_billing_adjustment(
    *,
    customer_identifier: str | None = None,
    amount: float,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("apply_billing_adjustment", metadata, user_id=caller_id)
    resolved_customer_identifier = _resolve_customer_identifier(
        customer_identifier, metadata
    )
    return await _request_json(
        "POST",
        "/v1/tools/billing/apply-adjustment",
        json_body={
            "customer_identifier": resolved_customer_identifier,
            "amount": amount,
            "reason": reason,
            "conversation_id": str((metadata or {}).get("conversation_id") or ""),
        },
        metadata=metadata,
    )


@observe(name="tool.refresh_meter_token_state", as_type="tool")
async def refresh_meter_token_state(
    *,
    customer_identifier: str | None = None,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("refresh_meter_token_state", metadata, user_id=caller_id)
    resolved_customer_identifier = _resolve_customer_identifier(
        customer_identifier, metadata
    )
    return await _request_json(
        "POST",
        "/v1/tools/metering/refresh-token-state",
        json_body={
            "customer_identifier": resolved_customer_identifier,
            "reason": reason,
            "conversation_id": str((metadata or {}).get("conversation_id") or ""),
        },
        metadata=metadata,
    )


@observe(name="tool.update_customer_record", as_type="tool")
async def update_customer_record(
    *,
    customer_identifier: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    service_address: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("update_customer_record", metadata, user_id=caller_id)
    resolved_customer_identifier = _resolve_customer_identifier(
        customer_identifier, metadata
    )
    return await _request_json(
        "POST",
        "/v1/tools/customers/update-record",
        json_body={
            "customer_identifier": resolved_customer_identifier,
            "email": email,
            "phone": phone,
            "service_address": service_address,
            "conversation_id": str((metadata or {}).get("conversation_id") or ""),
        },
        metadata=metadata,
    )


@observe(name="tool.create_payment_plan", as_type="tool")
async def create_payment_plan(
    *,
    customer_identifier: str | None = None,
    plan_name: str,
    installment_count: int,
    monthly_amount: float,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("create_payment_plan", metadata, user_id=caller_id)
    resolved_customer_identifier = _resolve_customer_identifier(
        customer_identifier, metadata
    )
    return await _request_json(
        "POST",
        "/v1/tools/payments/create-plan",
        json_body={
            "customer_identifier": resolved_customer_identifier,
            "plan_name": plan_name,
            "installment_count": installment_count,
            "monthly_amount": monthly_amount,
            "reason": reason,
            "conversation_id": str((metadata or {}).get("conversation_id") or ""),
        },
        metadata=metadata,
    )


@observe(name="tool.escalate_issue", as_type="tool")
async def escalate_issue(
    *,
    customer_identifier: str | None = None,
    title: str,
    description: str,
    priority: str = "high",
    case_reference: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("escalate_issue", metadata, user_id=caller_id)
    resolved_customer_identifier = _resolve_customer_identifier(
        customer_identifier, metadata
    )
    body: dict[str, Any] = {
        "customer_identifier": resolved_customer_identifier,
        "title": title,
        "description": description,
        "priority": priority,
        "conversation_id": str((metadata or {}).get("conversation_id") or ""),
    }
    if case_reference:
        body["case_reference"] = case_reference
    return await _request_json(
        "POST",
        "/v1/tools/create-escalation-ticket",
        json_body=body,
        metadata=metadata,
    )


@observe(name="tool.send_email", as_type="tool")
async def send_email(
    *,
    to_email: str,
    subject: str,
    body_text: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("send_email", metadata, user_id=caller_id)

    body: dict[str, Any] = {
        "to_email": to_email,
        "subject": subject,
        "body": body_text,
    }

    if _uses_internal_business_ops(metadata):
        result = await _request_json(
            "POST", "/v1/tools/send-email", json_body=body, metadata=metadata
        )
    else:
        result = await _request_json(
            "POST", "/v1/tools/send-email", json_body=body, metadata=metadata
        )

    if result.get("status") == "failed":
        return result

    if bool(result.get("mocked")) or not bool(result.get("sent")):
        output = {
            "status": "failed",
            "message": str(
                result.get("message") or "Email delivery is not configured right now."
            ),
            "to": result.get("to") or to_email,
            "subject": result.get("subject") or subject,
            "mocked": bool(result.get("mocked")),
        }
        update_observation(output=output)
        return output

    return result
