from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx


BASE_URL = os.getenv("CONVERSATION_API_BASE_URL", "").rstrip("/")
SERVICE_TOKEN = os.getenv("CONVERSATION_SERVICE_TOKEN", "")
BUSINESS_ID = os.getenv("CONVERSATION_BUSINESS_ID", "")
DEFAULT_TIMEOUT_SECONDS = float(os.getenv("CONVERSATION_API_TIMEOUT_SECONDS", "8"))


def _normalize_business_id(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        return ""


def is_enabled(business_id: str | None = None) -> bool:
    return bool(BASE_URL and SERVICE_TOKEN and (_normalize_business_id(business_id) or _normalize_business_id(BUSINESS_ID)))


def _headers(business_id: str | None = None) -> dict[str, str]:
    normalized_business_id = _normalize_business_id(business_id) or _normalize_business_id(BUSINESS_ID)
    return {
        "Content-Type": "application/json",
        "X-Service-Token": SERVICE_TOKEN,
        "X-Service-Name": "sales-girl-voice-agent",
        "X-Business-ID": normalized_business_id,
    }


async def _request_json(
    method: str,
    path: str,
    *,
    json: dict | None = None,
    params: dict | None = None,
    business_id: str | None = None,
) -> dict[str, Any]:
    if not is_enabled(business_id):
        return {"status": "disabled"}
    url = f"{BASE_URL}{path}"
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        response = await client.request(method, url, headers=_headers(business_id), json=json, params=params)
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


async def resolve_conversation(
    *,
    agent_id: str,
    external_id: str,
    external_name: str | None = None,
    channel: str = "voice",
    business_id: str | None = None,
) -> dict[str, Any]:
    return await _request_json(
        "POST",
        "/v1/conversations/resolve",
        json={
            "agent_id": agent_id,
            "external_id": external_id,
            "external_name": (str(external_name).strip() or None) if external_name else None,
            "channel": channel,
        },
        business_id=business_id,
    )


async def fetch_context(conversation_id: str, limit: int = 30, business_id: str | None = None) -> dict[str, Any]:
    return await _request_json(
        "GET",
        f"/v1/conversations/{conversation_id}/context",
        params={"limit": limit},
        business_id=business_id,
    )


async def append_message(
    *,
    conversation_id: str,
    role: str,
    content: str,
    session_id: str | None = None,
    idempotency_key: str | None = None,
    metadata: dict | None = None,
    business_id: str | None = None,
) -> dict[str, Any]:
    return await _request_json(
        "POST",
        f"/v1/conversations/{conversation_id}/messages",
        json={
            "role": role,
            "content": content,
            "session_id": session_id,
            "idempotency_key": idempotency_key,
            "metadata": metadata,
        },
        business_id=business_id,
    )


async def start_session(
    *,
    conversation_id: str,
    client_session_id: str,
    channel: str = "voice",
    business_id: str | None = None,
) -> dict[str, Any]:
    return await _request_json(
        "POST",
        "/v1/conversations/sessions/start",
        json={
            "conversation_id": conversation_id,
            "channel": channel,
            "client_session_id": client_session_id,
        },
        business_id=business_id,
    )


async def end_session(*, session_id: str, duration_seconds: int, business_id: str | None = None) -> dict[str, Any]:
    return await _request_json(
        "POST",
        "/v1/conversations/sessions/end",
        json={"session_id": session_id, "duration_seconds": max(0, int(duration_seconds))},
        business_id=business_id,
    )


async def create_session_event(
    *,
    session_id: str,
    event_type: str,
    role: str | None = None,
    title: str | None = None,
    body: str | None = None,
    payload: dict | None = None,
    business_id: str | None = None,
) -> dict[str, Any]:
    return await _request_json(
        "POST",
        f"/v1/conversations/sessions/{session_id}/events",
        json={
            "event_type": event_type,
            "role": role,
            "title": title,
            "body": body,
            "payload": payload,
        },
        business_id=business_id,
    )


async def update_session_recording(
    *,
    session_id: str,
    recording_status: str | None = None,
    recording_url: str | None = None,
    recording_duration_seconds: int | None = None,
    business_id: str | None = None,
) -> dict[str, Any]:
    return await _request_json(
        "POST",
        f"/v1/conversations/sessions/{session_id}/recording",
        json={
            "recording_status": recording_status,
            "recording_url": recording_url,
            "recording_duration_seconds": recording_duration_seconds,
        },
        business_id=business_id,
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
