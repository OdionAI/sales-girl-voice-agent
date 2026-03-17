from __future__ import annotations

import os
from typing import Any

import httpx
from .observability import observe, trace_tool, update_observation

APPOINTMENTS_API_BASE_URL = os.getenv("APPOINTMENTS_API_BASE_URL", "http://localhost:8080").rstrip("/")
DEFAULT_TIMEOUT_SECONDS = float(os.getenv("APPOINTMENTS_API_TIMEOUT_SECONDS", "8"))
AGENT_CLIENT_ID = os.getenv("AGENT_CLIENT_ID", "sales-girl-internal")
AGENT_NAME = os.getenv("AGENT_NAME", "sales-girl-agent-en")


def _clean_headers(metadata: dict[str, Any] | None) -> dict[str, str]:
    md = metadata or {}
    return {
        "X-Client-ID": str(md.get("client_id") or AGENT_CLIENT_ID),
        "X-Agent-ID": str(md.get("agent_id") or AGENT_NAME),
        "X-Conversation-ID": str(md.get("conversation_id") or ""),
        "X-Session-ID": str(md.get("session_id") or ""),
        "X-End-User-ID": str(md.get("end_user_id") or ""),
    }


@observe(name="appointments_api.request", as_type="span")
async def _request_json(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{APPOINTMENTS_API_BASE_URL}{path}"
    headers = _clean_headers(metadata)

    update_observation(
        input={
            "method": method,
            "path": path,
            "params": params,
            "json": json,
            "headers": headers,
        }
    )

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.request(
                method,
                url,
                params=params,
                json=json,
                headers=headers,
            )
    except httpx.TimeoutException:
        update_observation(
            output={"status": "failed", "message": "Backend request timed out. Please try again."}
        )
        return {
            "status": "failed",
            "message": "Backend request timed out. Please try again.",
        }
    except httpx.HTTPError:
        update_observation(
            output={"status": "failed", "message": "Backend is unavailable right now. Please try again."}
        )
        return {
            "status": "failed",
            "message": "Backend is unavailable right now. Please try again.",
        }

    try:
        payload = response.json()
    except ValueError:
        payload = None

    if response.status_code >= 400:
        detail = "Request failed."
        if isinstance(payload, dict) and payload.get("detail"):
            detail = str(payload["detail"])
        update_observation(
            output={
                "status": "failed",
                "message": detail,
                "http_status": response.status_code,
            }
        )
        return {
            "status": "failed",
            "message": detail,
            "http_status": response.status_code,
        }

    if isinstance(payload, dict):
        update_observation(output=payload)
        return payload

    update_observation(output={"status": "failed", "message": "Backend returned an invalid response."})
    return {
        "status": "failed",
        "message": "Backend returned an invalid response.",
    }


@observe(name="tool.list_available_slots", as_type="tool")
async def list_available_slots(
    date_iso: str,
    slot_minutes: int,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trace_tool(
        "list_available_slots",
        metadata=metadata,
        user_id=(metadata or {}).get("end_user_id"),
        session_id=(metadata or {}).get("conversation_id") or (metadata or {}).get("session_id"),
    )
    return await _request_json(
        "GET",
        "/appointments/available-slots",
        params={
            "date_iso": date_iso,
            "slot_minutes": slot_minutes,
        },
        metadata=metadata,
    )


@observe(name="tool.create_appointment", as_type="tool")
async def create_appointment(
    customer_name: str,
    service: str,
    date_iso: str,
    start_time: str,
    customer_phone: str,
    duration_minutes: int,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trace_tool(
        "create_appointment",
        metadata=metadata,
        user_id=(metadata or {}).get("end_user_id") or customer_phone,
        session_id=(metadata or {}).get("conversation_id") or (metadata or {}).get("session_id"),
    )
    return await _request_json(
        "POST",
        "/appointments",
        json={
            "customer_name": customer_name,
            "service": service,
            "date_iso": date_iso,
            "start_time": start_time,
            "duration_minutes": duration_minutes,
            "customer_phone": customer_phone,
        },
        metadata=metadata,
    )


@observe(name="tool.get_appointments", as_type="tool")
async def get_appointments(
    date_iso: str | None,
    customer_name: str | None,
    customer_phone: str | None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trace_tool(
        "get_appointments",
        metadata=metadata,
        user_id=(metadata or {}).get("end_user_id") or customer_phone,
        session_id=(metadata or {}).get("conversation_id") or (metadata or {}).get("session_id"),
    )
    params: dict[str, Any] = {}
    if date_iso:
        params["date_iso"] = date_iso
    if customer_name:
        params["customer_name"] = customer_name
    if customer_phone:
        params["customer_phone"] = customer_phone

    result = await _request_json("GET", "/appointments", params=params, metadata=metadata)
    if result.get("status") == "failed":
        return result

    if isinstance(result, list):
        return {
            "status": "success",
            "appointments": result,
        }

    if isinstance(result, dict) and isinstance(result.get("appointments"), list):
        return result

    return {
        "status": "failed",
        "message": "Invalid appointments response from backend.",
    }


@observe(name="tool.reschedule_appointment", as_type="tool")
async def reschedule_appointment(
    appointment_id: str,
    new_date_iso: str,
    new_start_time: str,
    duration_minutes: int,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trace_tool(
        "reschedule_appointment",
        metadata=metadata,
        user_id=(metadata or {}).get("end_user_id"),
        session_id=(metadata or {}).get("conversation_id") or (metadata or {}).get("session_id"),
    )
    return await _request_json(
        "PATCH",
        f"/appointments/{appointment_id}",
        json={
            "date_iso": new_date_iso,
            "start_time": new_start_time,
            "duration_minutes": duration_minutes,
        },
        metadata=metadata,
    )
