from __future__ import annotations

import os
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = os.getenv("AGENT_CONFIG_API_BASE_URL", "").rstrip("/")
SERVICE_TOKEN = os.getenv("AGENT_CONFIG_SERVICE_TOKEN", os.getenv("CONVERSATION_SERVICE_TOKEN", ""))
DEFAULT_TIMEOUT_SECONDS = float(os.getenv("AGENT_CONFIG_API_TIMEOUT_SECONDS", "8"))
SIP_ROUTE_TIMEOUT_SECONDS = float(os.getenv("SIP_ROUTE_LOOKUP_TIMEOUT_SECONDS", "2"))


def is_enabled() -> bool:
    return bool(BASE_URL and SERVICE_TOKEN)


def _headers(business_id: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Service-Token": SERVICE_TOKEN,
        "X-Service-Name": "sales-girl-voice-agent",
        "X-Business-ID": business_id,
    }


async def get_runtime_config(*, agent_id: str, business_id: str) -> dict[str, Any]:
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
        logger.error(
            "Agent config API request failed: url=%s status=%s detail=%s",
            url,
            response.status_code,
            payload.get("detail") if isinstance(payload, dict) else "request failed",
        )
        return {
            "status": "failed",
            "http_status": response.status_code,
            "detail": payload.get("detail") if isinstance(payload, dict) else "request failed",
        }
    if isinstance(payload, dict):
        payload["status"] = payload.get("status") or "success"
        return payload
    return {"status": "failed", "detail": "invalid payload"}


async def resolve_sip_route(*, number: str) -> dict[str, Any]:
    """Resolve a called number against the dashboard-managed Agent Config routes."""
    base_url = str(
        os.getenv("AGENT_CONFIG_SERVICE_BASE_URL")
        or os.getenv("AGENT_CONFIG_API_BASE_URL")
        or BASE_URL
    ).rstrip("/")
    service_token = str(
        os.getenv("AGENT_CONFIG_SERVICE_TOKEN")
        or os.getenv("CONVERSATION_SERVICE_TOKEN")
        or SERVICE_TOKEN
    ).strip()
    if not base_url or not service_token or not str(number or "").strip():
        return {"status": "disabled"}

    url = f"{base_url}/v1/agents/internal/sip-routes/resolve"
    try:
        async with httpx.AsyncClient(timeout=SIP_ROUTE_TIMEOUT_SECONDS) as client:
            response = await client.get(
                url,
                params={"number": str(number).strip()},
                headers={
                    "X-Service-Token": service_token,
                    "X-Service-Name": "sales-girl-voice-agent",
                },
            )
    except httpx.HTTPError as exc:
        logger.warning("Agent SIP route lookup unavailable; continuing with static routes: %s", exc)
        return {"status": "failed"}
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400:
        logger.error("Agent SIP route lookup failed: status=%s", response.status_code)
        return {"status": "failed", "http_status": response.status_code}
    return payload if isinstance(payload, dict) else {"status": "failed"}
