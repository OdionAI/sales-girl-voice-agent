from __future__ import annotations

import os
from typing import Any

import httpx


BASE_URL = os.getenv("AGENT_CONFIG_API_BASE_URL", "").rstrip("/")
SERVICE_TOKEN = os.getenv("AGENT_CONFIG_SERVICE_TOKEN", os.getenv("CONVERSATION_SERVICE_TOKEN", ""))
DEFAULT_TIMEOUT_SECONDS = float(os.getenv("AGENT_CONFIG_API_TIMEOUT_SECONDS", "8"))


def is_enabled() -> bool:
    return bool(BASE_URL and SERVICE_TOKEN)


def _headers(business_id: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Service-Token": SERVICE_TOKEN,
        "X-Service-Name": "sales-girl-voice-agent",
        "X-Business-ID": business_id,
    }


async def get_active_config(*, agent_id: str, business_id: str) -> dict[str, Any]:
    if not is_enabled() or not str(agent_id or "").strip() or not str(business_id or "").strip():
        return {"status": "disabled"}
    # Use runtime-config so public-agent URL sessions can load non-active agents too.
    # The dashboard widget can still choose whichever agent id it dispatches.
    url = f"{BASE_URL}/v1/agents/internal/agents/{agent_id}/runtime-config"
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        response = await client.get(url, headers=_headers(str(business_id)))
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400:
        return {
            "status": "failed",
            "http_status": response.status_code,
            "detail": payload.get("detail") if isinstance(payload, dict) else "request failed",
        }
    if isinstance(payload, dict):
        payload["status"] = payload.get("status") or "success"
        return payload
    return {"status": "failed", "detail": "invalid payload"}
