from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx


ANALYSIS_TIMEOUT_SECONDS = float(os.getenv("CONVERSATION_ANALYSIS_TIMEOUT_SECONDS", "15"))
ANALYSIS_MODEL = str(os.getenv("CONVERSATION_ANALYSIS_MODEL", "glm-5.2")).strip()
ANALYSIS_BASE_URL = str(
    os.getenv(
        "CONVERSATION_ANALYSIS_BASE_URL",
        "https://api-ap-southeast-1.modelarts-maas.com/openai/v1",
    )
).strip().rstrip("/")
ANALYSIS_API_KEY = str(
    os.getenv("CONVERSATION_ANALYSIS_API_KEY") or os.getenv("MAAS_API_KEY") or ""
).strip()


def is_enabled() -> bool:
    return str(os.getenv("CONVERSATION_ANALYSIS_ENABLED", "false")).strip().lower() in {"1", "true", "yes", "on"} and bool(ANALYSIS_API_KEY)


def _transcript_text(messages: list[dict[str, Any]]) -> str:
    lines = []
    for message in messages:
        role = str(message.get("role") or "").strip().lower()
        content = str(message.get("content") or "").strip()
        if not content or role in {"system", "tool"}:
            continue
        lines.append(f"{'Customer' if role == 'user' else 'Agent'}: {content}")
    return "\n".join(lines)


def _parse_json(text: str) -> dict[str, Any]:
    cleaned = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.S | re.I)
    if fenced:
        cleaned = fenced.group(1)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        cleaned = cleaned[start : end + 1]
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("analysis response was not an object")
    return value


async def analyze_messages(messages: list[dict[str, Any]], *, language: str = "en") -> dict[str, Any]:
    transcript = _transcript_text(messages)
    if not transcript:
        raise ValueError("conversation has no analyzable messages")
    prompt = f"""Analyze this customer-support conversation. Return JSON only with these keys:
summary (2-4 concise factual sentences), primary_intent (English snake_case), intent_confidence (0 to 1), sentiment (positive, neutral, frustrated, angry, or urgent), resolution_status (resolved, unresolved, escalated, or unknown).
Do not invent facts. Write summary in the conversation language ({language}).

Conversation:
{transcript[:30000]}"""
    url = f"{ANALYSIS_BASE_URL}/chat/completions"
    payload = {
        "model": ANALYSIS_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 1200,
        "chat_template_kwargs": {"thinking": False},
    }
    async with httpx.AsyncClient(timeout=ANALYSIS_TIMEOUT_SECONDS) as client:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {ANALYSIS_API_KEY}"},
            json=payload,
        )
        response.raise_for_status()
        body = response.json()
    choices = body.get("choices") or []
    message = ((choices[0] or {}).get("message") or {}) if choices else {}
    text = str(message.get("content") or "")
    result = _parse_json(text)
    confidence = float(result.get("intent_confidence"))
    if not 0 <= confidence <= 1:
        raise ValueError("intent confidence must be between 0 and 1")
    return {
        "analysis_status": "ready",
        "summary": str(result.get("summary") or "").strip() or None,
        "primary_intent": str(result.get("primary_intent") or "").strip().lower() or None,
        "intent_confidence": confidence,
        "sentiment": str(result.get("sentiment") or "").strip().lower() or None,
        "resolution_status": str(result.get("resolution_status") or "").strip().lower() or None,
    }
