from __future__ import annotations

import os
import logging
from typing import Any

import httpx
import time

logger = logging.getLogger(__name__)

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


_INBOUND_TARGET_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
INBOUND_TARGET_CACHE_SECONDS = float(
    os.getenv("AGENT_CONFIG_INBOUND_TARGET_CACHE_SECONDS", "10")
)


async def get_inbound_target(*, business_id: str) -> dict[str, Any]:
    """Fetch the business's configured inbound target agent (dashboard switch)."""
    business_id = str(business_id or "").strip()
    if not is_enabled() or not business_id:
        return {"status": "disabled"}
    url = f"{BASE_URL}/v1/agents/inbound-target"
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=_headers(business_id))
    except Exception as exc:  # noqa: BLE001
        logger.error("Inbound target lookup failed: business_id=%s error=%s", business_id, exc)
        return {"status": "failed", "detail": type(exc).__name__}
    if response.status_code == 404:
        return {"status": "unset"}
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400:
        return {"status": "failed", "http_status": response.status_code}
    if isinstance(payload, dict):
        payload["status"] = "success"
        return payload
    return {"status": "failed", "detail": "invalid payload"}


async def resolve_inbound_agent(*, business_id: str) -> str | None:
    """Return the configured inbound target agent id for a business, or None.

    Cached briefly so a dashboard switch takes effect within the cache window
    without adding a lookup to every call. A transient failure returns None so
    the caller keeps the route's own default agent.
    """
    business_id = str(business_id or "").strip()
    if not is_enabled() or not business_id:
        return None
    now = time.monotonic()
    cached = _INBOUND_TARGET_CACHE.get(business_id)
    if cached and now - cached[0] < INBOUND_TARGET_CACHE_SECONDS:
        return cached[1].get("agent_id") or None
    payload = await get_inbound_target(business_id=business_id)
    if payload.get("status") in {"success", "unset"}:
        _INBOUND_TARGET_CACHE[business_id] = (now, payload)
    if payload.get("status") == "success":
        return payload.get("agent_id") or None
    return None
