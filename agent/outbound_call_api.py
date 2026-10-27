"""Internal API for binding one prepared AICC call to one LiveKit room."""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx


BASE_URL = (
    os.getenv("CONVERSATION_API_BASE_URL")
    or os.getenv("CONVERSATION_SERVICE_BASE_URL")
    or ""
).rstrip("/")
SERVICE_TOKEN = os.getenv("CONVERSATION_SERVICE_TOKEN", "")
TIMEOUT_SECONDS = float(os.getenv("CONVERSATION_API_TIMEOUT_SECONDS", "8"))


class OutboundCallError(RuntimeError):
    pass


def is_enabled() -> bool:
    return bool(BASE_URL and SERVICE_TOKEN)


async def claim_call(*, call_ref: str, room_name: str) -> dict[str, Any]:
    if not is_enabled():
        raise OutboundCallError("Outbound call context service is not configured.")
    url = f"{BASE_URL}/v1/internal/outbound/sessions/{quote(call_ref, safe='')}/claim"
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        response = await client.post(
            url,
            json={"room_name": room_name},
            headers={
                "Content-Type": "application/json",
                "X-Service-Token": SERVICE_TOKEN,
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
    if not is_enabled():
        return {"status": "disabled"}
    url = f"{BASE_URL}/v1/internal/outbound/sessions/{quote(call_ref, safe='')}/finish"
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
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
                "X-Service-Token": SERVICE_TOKEN,
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
