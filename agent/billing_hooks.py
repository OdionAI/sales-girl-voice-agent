from __future__ import annotations

import os
from typing import Any

import httpx


BASE_URL = os.getenv("BILLING_HOOK_BASE_URL", "").rstrip("/")
SERVICE_TOKEN = os.getenv("BILLING_HOOK_SERVICE_TOKEN", "")
BUSINESS_ID = os.getenv("CONVERSATION_BUSINESS_ID", "")
TIMEOUT_SECONDS = float(os.getenv("BILLING_HOOK_TIMEOUT_SECONDS", "5"))
FAIL_CLOSED = os.getenv("BILLING_FAIL_CLOSED", "false").lower() == "true"


def is_enabled(business_id: str | None = None) -> bool:
    return bool(BASE_URL and SERVICE_TOKEN and (business_id or BUSINESS_ID))


def _headers(business_id: str | None = None) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Service-Token": SERVICE_TOKEN,
        "X-Service-Name": "sales-girl-voice-agent",
        "X-Business-ID": str(business_id or BUSINESS_ID),
    }


async def _post(path: str, payload: dict[str, Any], *, business_id: str | None = None) -> dict[str, Any]:
    if not is_enabled(business_id):
        return {"status": "disabled"}
    url = f"{BASE_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=payload, headers=_headers(business_id))
    except httpx.HTTPError as exc:
        return {
            "status": "failed",
            "detail": str(exc) or exc.__class__.__name__,
            "error_type": exc.__class__.__name__,
        }
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 400:
        return {
            "status": "failed",
            "http_status": resp.status_code,
            "detail": data.get("detail") if isinstance(data, dict) else "billing hook failed",
        }
    if isinstance(data, dict):
        data["status"] = str(data.get("status") or "success")
        return data
    return {"status": "success"}


async def authorize_call_start(
    *,
    conversation_id: str,
    end_user_id: str,
    channel: str,
    business_id: str | None = None,
) -> dict[str, Any]:
    return await _post(
        "/v1/internal/credits/authorize-call",
        {
            "conversation_id": conversation_id,
            "end_user_id": end_user_id,
            "channel": channel,
        },
        business_id=business_id,
    )


async def report_call_usage(
    *,
    conversation_id: str,
    session_id: str,
    end_user_id: str,
    duration_seconds: int,
    channel: str,
    business_id: str | None = None,
) -> dict[str, Any]:
    return await _post(
        "/v1/internal/credits/report-usage",
        {
            "conversation_id": conversation_id,
            "session_id": session_id,
            "end_user_id": end_user_id,
            "duration_seconds": max(0, int(duration_seconds)),
            "channel": channel,
        },
        business_id=business_id,
    )
