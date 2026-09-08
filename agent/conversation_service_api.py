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
    headers: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    if not is_enabled(business_id):
        return {"status": "disabled"}
    url = f"{BASE_URL}{path}"
    request_headers = _headers(business_id)
    if headers:
        request_headers.update(
            {key: str(value) for key, value in headers.items() if str(value or "").strip()}
        )
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        response = await client.request(method, url, headers=request_headers, json=json, params=params)
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


async def fetch_context(
    conversation_id: str,
    limit: int = 30,
    session_id: str | None = None,
    business_id: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    if str(session_id or "").strip():
        params["session_id"] = str(session_id).strip()
    return await _request_json(
        "GET",
        f"/v1/conversations/{conversation_id}/context",
        params=params,
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


async def update_session_analysis(
    *, session_id: str, analysis_status: str, summary: str | None = None,
    primary_intent: str | None = None, intent_confidence: float | None = None,
    sentiment: str | None = None, resolution_status: str | None = None,
    business_id: str | None = None,
) -> dict[str, Any]:
    return await _request_json(
        "POST", f"/v1/conversations/sessions/{session_id}/analysis",
        json={"analysis_status": analysis_status, "summary": summary,
              "primary_intent": primary_intent, "intent_confidence": intent_confidence,
              "sentiment": sentiment, "resolution_status": resolution_status},
        business_id=business_id,
    )


async def create_caller_record(
    *,
    first_name: str,
    last_name: str,
    phone_number: str,
    email: str,
    theme: str,
    sub_theme: str,
    request_summary: str,
    treatment: str,
    treatment_comment: str,
    status: str,
    session_ref: str,
    agent_id: str | None = None,
    conversation_ref: str | None = None,
    end_user_ref: str | None = None,
    consular_registration_number: str | None = None,
    order_date: str | None = None,
    order_number: str | None = None,
    transferred_to_human: bool = False,
    business_id: str | None = None,
) -> dict[str, Any]:
    return await _request_json(
        "POST",
        "/v1/tools/caller-records",
        json={
            "first_name": first_name,
            "last_name": last_name,
            "phone_number": phone_number,
            "email": email,
            "theme": theme,
            "sub_theme": sub_theme,
            "request_summary": request_summary,
            "treatment": treatment,
            "treatment_comment": treatment_comment,
            "status": status,
            "consular_registration_number": consular_registration_number,
            "order_date": order_date,
            "order_number": order_number,
            "transferred_to_human": transferred_to_human,
        },
        headers={
            "X-Agent-Id": agent_id,
            "X-Conversation-Id": conversation_ref,
            "X-Session-Id": session_ref,
            "X-End-User-Id": end_user_ref,
        },
        business_id=business_id,
    )


async def finalize_caller_record(
    *,
    session_ref: str,
    theme: str,
    sub_theme: str,
    request_summary: str,
    treatment: str,
    treatment_comment: str,
    status: str,
    consular_registration_number: str | None = None,
    order_date: str | None = None,
    order_number: str | None = None,
    transferred_to_human: bool = False,
    business_id: str | None = None,
) -> dict[str, Any]:
    return await _request_json(
        "POST",
        "/v1/tools/caller-records/finalize",
        json={
            "session_ref": session_ref,
            "theme": theme,
            "sub_theme": sub_theme,
            "request_summary": request_summary,
            "treatment": treatment,
            "treatment_comment": treatment_comment,
            "status": status,
            "consular_registration_number": consular_registration_number,
            "order_date": order_date,
            "order_number": order_number,
            "transferred_to_human": transferred_to_human,
        },
        business_id=business_id,
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
