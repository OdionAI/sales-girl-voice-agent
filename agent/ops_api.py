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
logger = logging.getLogger(__name__)


def _service_headers(metadata: dict[str, Any] | None) -> dict[str, str]:
    md = metadata or {}
    business_id = str(md.get("business_id") or "").strip()
    return {
        "X-Service-Token": OPS_SERVICE_TOKEN,
        "X-Service-Name": AGENT_CLIENT_ID,
        "X-Business-ID": business_id,
        "X-Client-ID": str(md.get("client_id") or AGENT_CLIENT_ID),
        "X-Agent-ID": str(md.get("agent_id") or AGENT_NAME),
        "X-Conversation-ID": str(md.get("conversation_id") or ""),
        "X-Session-ID": str(md.get("session_id") or ""),
        "X-End-User-ID": str(md.get("end_user_id") or ""),
    }


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


@observe(name="tool.resolve_customer", as_type="tool")
async def resolve_customer(*, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("resolve_customer", metadata, user_id=caller_id)
    return await _request_json(
        "POST",
        "/v1/tools/resolve-customer",
        json_body={"create_if_missing": True},
        metadata=metadata,
    )


@observe(name="tool.search_passport_application", as_type="tool")
async def search_passport_application(
    *,
    application_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("applications_search", metadata, user_id=caller_id)
    body: dict[str, Any] = {}
    if application_id:
        body["application_id"] = application_id
    return await _request_json(
        "POST",
        "/v1/tools/applications/search",
        json_body=body,
        metadata=metadata,
    )


@observe(name="tool.search_certificate_request", as_type="tool")
async def search_certificate_request(
    *,
    certificate_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("certificates_search", metadata, user_id=caller_id)
    body: dict[str, Any] = {}
    if certificate_id:
        body["certificate_id"] = certificate_id
    return await _request_json(
        "POST",
        "/v1/tools/certificates/search",
        json_body=body,
        metadata=metadata,
    )


@observe(name="tool.dispatch_passport_now", as_type="tool")
async def dispatch_passport_now(
    *,
    application_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("dispatch_create", metadata, user_id=caller_id)
    body: dict[str, Any] = {"address_confirmed": True}
    if application_id:
        body["application_id"] = application_id
    return await _request_json(
        "POST",
        "/v1/tools/dispatch/create",
        json_body=body,
        metadata=metadata,
    )


@observe(name="tool.issue_certificate_now", as_type="tool")
async def issue_certificate_now(
    *,
    certificate_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("certificate_issue", metadata, user_id=caller_id)
    body: dict[str, Any] = {}
    if certificate_id:
        body["certificate_id"] = certificate_id
    return await _request_json(
        "POST",
        "/v1/tools/certificates/issue",
        json_body=body,
        metadata=metadata,
    )


@observe(name="tool.create_escalation_ticket", as_type="tool")
async def create_escalation_ticket(
    *,
    title: str,
    description: str,
    priority: str = "high",
    case_reference: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    caller_id = str((metadata or {}).get("end_user_id") or "")
    _trace("create_escalation_ticket", metadata, user_id=caller_id)
    body: dict[str, Any] = {
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
