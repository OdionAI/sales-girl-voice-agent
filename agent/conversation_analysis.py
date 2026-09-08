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
CALLER_RECORD_THEMES = {
    "Document de voyage",
    "Documents d’identité",
    "Attestations et certificats",
    "Vie familiale et statut civil",
    "Documents juridiques",
    "Autres",
}
CALLER_RECORD_SUB_THEMES = {
    "Laissez-passer consulaire",
    "Passeport",
    "Passeport biométrique",
    "Certificat de coutume et de célibat",
    "Immatriculation consulaire",
    "RAVIP",
    "Procuration",
    "Autorisation parentale",
    "Autres",
}
CALLER_RECORD_TREATMENTS = {
    "Information",
    "Information et assistance",
    "Redirection",
    "Remontée",
    "Création de ticket",
    "Envoi d’email",
    "Autre",
}
CALLER_RECORD_STATUSES = {
    "Terminé",
    "Escaladé",
    "Ticket créé",
    "En attente",
    "Échec",
}


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


async def _complete_json(prompt: str, *, max_tokens: int) -> dict[str, Any]:
    url = f"{ANALYSIS_BASE_URL}/chat/completions"
    payload = {
        "model": ANALYSIS_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": max_tokens,
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
    return _parse_json(str(message.get("content") or ""))


def _required_text(result: dict[str, Any], field: str) -> str:
    value = str(result.get(field) or "").strip()
    if not value:
        raise ValueError(f"caller-record analysis field '{field}' is required")
    return value


def _optional_text(result: dict[str, Any], field: str) -> str | None:
    value = result.get(field)
    if value is None:
        return None
    normalized = str(value).strip()
    if normalized.lower() in {"", "absent", "null", "none", "non mentionné"}:
        return None
    return normalized


def _required_phone(result: dict[str, Any]) -> str:
    phone = re.sub(r"[\s().-]+", "", _required_text(result, "phone_number"))
    if phone.startswith("00"):
        phone = f"+{phone[2:]}"
    if not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
        raise ValueError("caller-record analysis returned an invalid phone number")
    return phone


def _required_email(result: dict[str, Any]) -> str:
    email = _required_text(result, "email").lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("caller-record analysis returned an invalid email")
    return email


async def analyze_messages(messages: list[dict[str, Any]], *, language: str = "en") -> dict[str, Any]:
    transcript = _transcript_text(messages)
    if not transcript:
        raise ValueError("conversation has no analyzable messages")
    prompt = f"""Analyze this customer-support conversation. Return JSON only with these keys:
summary (2-4 concise factual sentences), primary_intent (English snake_case), intent_confidence (0 to 1), sentiment (positive, neutral, frustrated, angry, or urgent), resolution_status (resolved, unresolved, escalated, or unknown).
Do not invent facts. Write summary in the conversation language ({language}).

Conversation:
{transcript[:30000]}"""
    result = await _complete_json(prompt, max_tokens=1200)
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


async def analyze_caller_record(
    messages: list[dict[str, Any]], *, language: str = "fr"
) -> dict[str, Any]:
    """Extract caller identity and classify operational sheet fields."""
    transcript = _transcript_text(messages)
    if not transcript:
        raise ValueError("conversation has no analyzable messages")
    prompt = f"""You are the post-call quality-assurance analyst for a Benin consular support line.
Do not answer the caller. Analyze the completed conversation and return JSON only with exactly these keys:
first_name, last_name, phone_number, email, theme, sub_theme, request_summary,
treatment, treatment_comment, status, consular_registration_number, order_date,
order_number, transferred_to_human.

Use one exact theme value:
{", ".join(sorted(CALLER_RECORD_THEMES))}

Use one exact sub_theme value:
{", ".join(sorted(CALLER_RECORD_SUB_THEMES))}

Use one exact treatment value:
{", ".join(sorted(CALLER_RECORD_TREATMENTS))}

Use one exact status value:
{", ".join(sorted(CALLER_RECORD_STATUSES))}

Rules:
- Extract first_name, last_name, phone_number, and email from the caller's confirmed details in the transcript.
- Reconstruct names from spelling when the caller spells them. Use normal readable casing, not all caps.
- Return phone_number in E.164 format with a leading + and digits only. Convert "00" international prefixes to "+" and remove spaces.
- Return email in lowercase. Reconstruct spoken or spelled "arobase/at" and "point/dot" addresses only when the transcript is clear.
- Write request_summary and treatment_comment in concise factual French.
- Infer classification, treatment, and status from the conversation; never ask the caller for them.
- transferred_to_human must be true only if the caller was actually transferred to a human during this call.
- Use JSON null for consular_registration_number, order_date, or order_number unless the caller explicitly provided it.
- Do not invent contact details, facts, or treat the caller's phone number as a consular/order number.
- Conversation language is {language}.

Conversation:
{transcript[:30000]}"""
    result = await _complete_json(prompt, max_tokens=1600)
    theme = _required_text(result, "theme")
    sub_theme = _required_text(result, "sub_theme")
    treatment = _required_text(result, "treatment")
    status = _required_text(result, "status")
    if theme not in CALLER_RECORD_THEMES:
        raise ValueError("caller-record analysis returned an unsupported theme")
    if sub_theme not in CALLER_RECORD_SUB_THEMES:
        raise ValueError("caller-record analysis returned an unsupported sub-theme")
    if treatment not in CALLER_RECORD_TREATMENTS:
        raise ValueError("caller-record analysis returned an unsupported treatment")
    if status not in CALLER_RECORD_STATUSES:
        raise ValueError("caller-record analysis returned an unsupported status")
    transferred_to_human = result.get("transferred_to_human")
    if not isinstance(transferred_to_human, bool):
        raise ValueError("caller-record transferred_to_human must be a boolean")
    return {
        "first_name": _required_text(result, "first_name"),
        "last_name": _required_text(result, "last_name"),
        "phone_number": _required_phone(result),
        "email": _required_email(result),
        "theme": theme,
        "sub_theme": sub_theme,
        "request_summary": _required_text(result, "request_summary"),
        "treatment": treatment,
        "treatment_comment": _required_text(result, "treatment_comment"),
        "status": status,
        "consular_registration_number": _optional_text(
            result, "consular_registration_number"
        ),
        "order_date": _optional_text(result, "order_date"),
        "order_number": _optional_text(result, "order_number"),
        "transferred_to_human": transferred_to_human,
    }
