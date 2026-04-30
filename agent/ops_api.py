from __future__ import annotations

import os
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

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
logger = logging.getLogger(__name__)


def _business_use_case(metadata: dict[str, Any] | None) -> str:
    md = metadata or {}
    return str(md.get("business_use_case") or "").strip().lower()


def _uses_internal_business_ops(metadata: dict[str, Any] | None) -> bool:
    return _business_use_case(metadata) in {"hotel", "restaurant", "fashion"}


def _ops_base_url(metadata: dict[str, Any] | None) -> str:
    use_case = _business_use_case(metadata)
    if use_case in {"hotel", "restaurant", "fashion", "custom", "generic"}:
        return HOTEL_OPS_SERVICE_BASE_URL
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


def _normalize_http_url(value: str | None) -> str:
    resolved = str(value or "").strip()
    if not resolved:
        return ""
    parsed = urlparse(resolved)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return parsed.geturl().rstrip("/")


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
) -> dict[str, Any]:
    base_url = _ops_base_url(metadata)
    if not str(base_url or "").strip():
        output = {"status": "failed", "message": "Hotel ops backend is not configured."}
        update_observation(output=output)
        return output
    url = f"{base_url}{path}"
    headers = _service_headers(metadata)
    if not str(headers.get("X-Business-ID") or "").strip():
        output = {
            "status": "failed",
            "message": "Missing business scope for ops request.",
        }
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
        update_observation(output=output)
        return output
    except httpx.HTTPError:
        output = {"status": "failed", "message": "Ops backend is unavailable."}
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
        update_observation(output=output)
        return output

    if isinstance(payload, dict):
        logger.info("OPS response %s %s -> %s", method, path, payload)
        payload["status"] = "success"
        update_observation(output=payload)
        return payload
    if isinstance(payload, list):
        output = {"status": "success", "items": payload}
        logger.info("OPS response %s %s -> %s", method, path, output)
        update_observation(output=output)
        return output

    output = {"status": "failed", "message": "Invalid response from ops backend."}
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
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("search_business_knowledge", metadata, user_id=caller_id)
    base_url = str(KNOWLEDGE_SERVICE_BASE_URL or "").strip()
    if not base_url:
        output = {
            "status": "failed",
            "message": "Business knowledge lookup is not configured.",
        }
        update_observation(output=output)
        return output

    headers = _knowledge_headers(metadata)
    if not str(headers.get("X-Business-ID") or "").strip():
        output = {
            "status": "failed",
            "message": "Missing business scope for knowledge lookup.",
        }
        update_observation(output=output)
        return output

    request_body = {
        "query": str(query or "").strip(),
        "top_k": int(max(1, min(int(top_k or 4), 6))),
        "agent_id": str((metadata or {}).get("agent_id") or "").strip() or None,
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
        update_observation(output=output)
        return output
    except httpx.HTTPError:
        output = {"status": "failed", "message": "Knowledge lookup is unavailable."}
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
        update_observation(output=output)
        return output

    matches = payload.get("matches") if isinstance(payload, dict) else None
    if not isinstance(matches, list) or not matches:
        output = {
            "status": "success",
            "matches": [],
            "message": "No matching business knowledge was found.",
        }
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
    if _uses_internal_business_ops(metadata):
        conversation_id = (
            str((metadata or {}).get("conversation_id") or "").strip() or None
        )
        agent_id = str((metadata or {}).get("agent_id") or "").strip() or None
        body: dict[str, Any] = {
            "customer_name": str((metadata or {}).get("end_user_name") or "").strip()
            or None,
            "customer_contact": str(
                (metadata or {}).get("caller_phone_e164") or ""
            ).strip()
            or None,
            "title": title,
            "description": description,
            "priority": priority,
            "status": "open",
            "conversation_id": conversation_id,
            "agent_id": agent_id,
        }
        return await _request_json(
            "POST", "/v1/tickets", json_body=body, metadata=metadata
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
