"""Voice-worker client for MoMo inbound AI call capacity leases."""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx


class CallCapacityError(RuntimeError):
    pass


def _settings() -> tuple[str, str, float]:
    return (
        (os.getenv("CONVERSATION_SERVICE_BASE_URL") or os.getenv("CONVERSATION_API_BASE_URL") or "").rstrip("/"),
        os.getenv("CONVERSATION_SERVICE_TOKEN", ""),
        float(os.getenv("CONVERSATION_API_TIMEOUT_SECONDS", "8")),
    )


def is_enabled_for_business(business_id: str) -> bool:
    configured = str(os.getenv("MOMO_CALL_CAPACITY_BUSINESS_ID") or "").strip()
    return bool(configured and str(business_id or "").strip() == configured)


def caller_number_from_sip_headers(headers: dict[str, str], business_id: str) -> str:
    if not is_enabled_for_business(business_id):
        return ""
    value = str(headers.get("from") or "").strip()
    if value.lower().startswith("sip:") or value.lower().startswith("sips:"):
        value = value.split(":", 1)[1].split("@", 1)[0]
    number = "".join(ch for ch in value if ch.isdigit())
    if len(number) < 7:
        raise CallCapacityError("MoMo AI call is missing a usable SIP From number.")
    return number


async def _post(reservation_id: str, action: str, room_name: str) -> dict[str, Any]:
    base_url, token, timeout = _settings()
    if not base_url or not token:
        raise CallCapacityError("Call capacity service is not configured.")
    url = (
        f"{base_url}/v1/internal/aicc/capacity/reservations/"
        f"{quote(reservation_id, safe='')}/{action}"
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url,
                json={"room_name": room_name},
                headers={
                    "Content-Type": "application/json",
                    "X-Service-Token": token,
                    "X-Service-Name": "sales-girl-voice-agent",
                },
            )
    except httpx.HTTPError as exc:
        raise CallCapacityError("Call capacity service is unavailable.") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400 or not isinstance(payload, dict):
        raise CallCapacityError(
            str(payload.get("detail") or f"Call capacity request failed ({response.status_code}).")
        )
    return payload


async def claim_call(*, reservation_id: str, room_name: str) -> dict[str, Any]:
    return await _post(reservation_id, "claim", room_name)


async def claim_call_by_caller(*, caller_number: str, room_name: str) -> dict[str, Any]:
    base_url, token, timeout = _settings()
    if not base_url or not token:
        raise CallCapacityError("Call capacity service is not configured.")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{base_url}/v1/internal/aicc/capacity/reservations/claim-by-caller",
                json={"caller_number": caller_number, "room_name": room_name},
                headers={"Content-Type": "application/json", "X-Service-Token": token,
                         "X-Service-Name": "sales-girl-voice-agent"},
            )
    except httpx.HTTPError as exc:
        raise CallCapacityError("Call capacity service is unavailable.") from exc
    payload = response.json() if response.status_code < 500 else {}
    if response.status_code >= 400 or not isinstance(payload, dict):
        raise CallCapacityError(str(payload.get("detail") or f"Call capacity request failed ({response.status_code})."))
    return payload


async def heartbeat_call(*, reservation_id: str, room_name: str) -> dict[str, Any]:
    return await _post(reservation_id, "heartbeat", room_name)


async def release_call(*, reservation_id: str, room_name: str) -> dict[str, Any]:
    return await _post(reservation_id, "release", room_name)
