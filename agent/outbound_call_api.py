"""Internal API for binding one prepared AICC call to one LiveKit room."""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx


def _settings() -> tuple[str, str, float]:
    # The Lagos worker loads its platform environment when a job starts.
    return (
        (os.getenv("CONVERSATION_API_BASE_URL") or os.getenv("CONVERSATION_SERVICE_BASE_URL") or "").rstrip("/"),
        os.getenv("CONVERSATION_SERVICE_TOKEN", ""),
        float(os.getenv("CONVERSATION_API_TIMEOUT_SECONDS", "8")),
    )


class OutboundCallError(RuntimeError):
    pass


def is_enabled() -> bool:
    base_url, service_token, _ = _settings()
    return bool(base_url and service_token)


async def claim_call(*, call_ref: str, room_name: str) -> dict[str, Any]:
    base_url, service_token, timeout_seconds = _settings()
    if not base_url or not service_token:
        raise OutboundCallError("Outbound call context service is not configured.")
    url = f"{base_url}/v1/internal/outbound/sessions/{quote(call_ref, safe='')}/claim"
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(
            url,
            json={"room_name": room_name},
            headers={
                "Content-Type": "application/json",
                "X-Service-Token": service_token,
                "X-Service-Name": "sales-girl-voice-agent",
            },
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400 or not isinstance(payload, dict):
        raise OutboundCallError(
            str(payload.get("detail") or f"Call context request failed ({response.status_code}).")
        )
    return payload


async def claim_call_by_recipient(
    *, business_id: str, recipient_phone: str, room_name: str
) -> dict[str, Any]:
    """Bind the prepared call for a customer number to this room.

    AICC identifies the call by the customer's phone number, so the worker
    claims by number rather than an opaque reference.
    """
    base_url, service_token, timeout_seconds = _settings()
    if not base_url or not service_token:
        raise OutboundCallError("Outbound call context service is not configured.")
    url = f"{base_url}/v1/internal/outbound/sessions/claim-by-recipient"
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(
            url,
            json={
                "business_id": business_id,
                "recipient_phone": recipient_phone,
                "room_name": room_name,
            },
            headers={
                "Content-Type": "application/json",
                "X-Service-Token": service_token,
                "X-Service-Name": "sales-girl-voice-agent",
            },
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400 or not isinstance(payload, dict):
        raise OutboundCallError(
            str(payload.get("detail") or f"Call context request failed ({response.status_code}).")
        )
    return payload


async def finish_call(
    *,
    call_ref: str,
    status: str = "completed",
    failure_reason: str | None = None,
    summary: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    base_url, service_token, timeout_seconds = _settings()
    if not base_url or not service_token:
        return {"status": "disabled"}
    url = f"{base_url}/v1/internal/outbound/sessions/{quote(call_ref, safe='')}/finish"
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(
            url,
            json={
                "status": status,
                "failure_reason": failure_reason,
                "summary": summary,
                "conversation_id": conversation_id,
            },
            headers={
                "Content-Type": "application/json",
                "X-Service-Token": service_token,
                "X-Service-Name": "sales-girl-voice-agent",
            },
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400:
        return {"status": "failed", "http_status": response.status_code, "detail": payload.get("detail")}
    return payload if isinstance(payload, dict) else {"status": "failed", "detail": "invalid payload"}
