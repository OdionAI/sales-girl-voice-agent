from __future__ import annotations

import os
import logging
from typing import Any

import httpx

from .observability import observe, trace_tool, update_observation

OPS_SERVICE_BASE_URL = os.getenv("OPS_SERVICE_BASE_URL", "http://sales-girl-demo-crm-service:8095").rstrip("/")
OPS_SERVICE_TOKEN = os.getenv("OPS_SERVICE_TOKEN", "local-internal-service-token")
DEFAULT_TIMEOUT_SECONDS = float(os.getenv("OPS_SERVICE_TIMEOUT_SECONDS", "8"))
AGENT_CLIENT_ID = os.getenv("AGENT_CLIENT_ID", "sales-girl-internal")
AGENT_NAME = os.getenv("AGENT_NAME", "sales-girl-agent-en")
OPS_SHARED_OWNER_EMAIL = str(os.getenv("OPS_SHARED_OWNER_EMAIL") or "").strip().lower()
logger = logging.getLogger(__name__)


def _resolve_customer_identifier(
    customer_identifier: str | None,
    metadata: dict[str, Any] | None,
) -> str:
    explicit = str(customer_identifier or "").strip()
    if explicit:
        return explicit
    md = metadata or {}
    return str(md.get("end_user_id") or "").strip()


def _service_headers(metadata: dict[str, Any] | None) -> dict[str, str]:
    md = metadata or {}
    business_scope = OPS_SHARED_OWNER_EMAIL or str(md.get("business_id") or "").strip()
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
    if OPS_SHARED_OWNER_EMAIL:
        headers["X-Workspace-Owner-Email"] = OPS_SHARED_OWNER_EMAIL
    return headers


@observe(name="ops_api.request", as_type="span")
async def _request_json(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{OPS_SERVICE_BASE_URL}{path}"
    headers = _service_headers(metadata)
    if not str(headers.get("X-Business-ID") or "").strip():
        output = {"status": "failed", "message": "Missing business scope for ops request."}
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
            "OPS request %s %s business_scope=%s end_user=%s body=%s",
            method,
            path,
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


def _trace(tool_name: str, metadata: dict[str, Any] | None, user_id: str | None = None) -> None:
    md = metadata or {}
    trace_tool(
        tool_name,
        metadata=md,
        user_id=user_id or str(md.get("end_user_id") or ""),
        session_id=str(md.get("conversation_id") or md.get("session_id") or ""),
    )


@observe(name="tool.lookup_customer_account", as_type="tool")
async def lookup_customer_account(
    *,
    customer_identifier: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("lookup_customer_account", metadata, user_id=caller_id)
    resolved_customer_identifier = _resolve_customer_identifier(customer_identifier, metadata)
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
    resolved_customer_identifier = _resolve_customer_identifier(customer_identifier, metadata)
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
    resolved_customer_identifier = _resolve_customer_identifier(customer_identifier, metadata)
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
    resolved_customer_identifier = _resolve_customer_identifier(customer_identifier, metadata)
    return await _request_json(
        "POST",
        "/v1/tools/vending/history",
        json_body={"customer_identifier": resolved_customer_identifier},
        metadata=metadata,
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
    resolved_customer_identifier = _resolve_customer_identifier(customer_identifier, metadata)
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
        "/v1/tools/complaints/create",
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
    resolved_customer_identifier = _resolve_customer_identifier(customer_identifier, metadata)
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
    resolved_customer_identifier = _resolve_customer_identifier(customer_identifier, metadata)
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
    resolved_customer_identifier = _resolve_customer_identifier(customer_identifier, metadata)
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
