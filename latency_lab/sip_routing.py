from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any


_CALLEE_KEYS = (
    "sip.h.x-callee-number",
    "sip.h.x-odion-callee-number",
    "sip.x_callee_number",
    "sip.calleenumber",
    "sip.callee_number",
)
_FALLBACK_KEYS = (
    "sip.phonenumber",
    "sip.phone_number",
    "sip.fromuser",
    "sip.from_user",
    "sip.from",
    "phonenumber",
    "fromnumber",
    "callernumber",
)


@dataclass(frozen=True)
class SipAgentRoute:
    party_number: str
    business_id: str
    agent_id: str
    configured_name: str


def normalize_phone(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or raw.lower() in {
        "anonymous",
        "restricted",
        "unavailable",
        "unknown",
        "private",
    }:
        return ""
    if "@" in raw:
        match = re.search(r"(?i)(?:sip:|tel:)?([^;>\s@]+)@", raw)
        raw = match.group(1) if match else ""
    digits = "".join(character for character in raw if character.isdigit())
    while digits.startswith("00") and len(digits) > 2:
        digits = digits[2:]
    if digits.startswith("0") and len(digits) in {10, 11}:
        digits = f"234{digits[1:]}"
    elif len(digits) == 10 and digits[:1] in {"7", "8", "9"}:
        digits = f"234{digits}"
    return f"+{digits}" if len(digits) >= 7 else ""


def load_number_agent_map(raw: str | None = None) -> dict[str, SipAgentRoute]:
    text = str(
        raw
        if raw is not None
        else os.getenv("POC_OUTBOUND_NUMBER_AGENT_MAP", "")
    ).strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    default_business_id = str(os.getenv("POC_OUTBOUND_BUSINESS_ID", "")).strip()
    routes: dict[str, SipAgentRoute] = {}
    for number, value in payload.items():
        party_number = normalize_phone(number)
        if not party_number:
            continue
        if isinstance(value, str):
            agent_id = value.strip()
            business_id = default_business_id
            configured_name = ""
        elif isinstance(value, dict):
            agent_id = str(
                value.get("config_agent_id")
                or value.get("configAgentId")
                or value.get("agent_id")
                or value.get("agentId")
                or ""
            ).strip()
            business_id = str(
                value.get("business_id")
                or value.get("businessId")
                or default_business_id
            ).strip()
            configured_name = str(
                value.get("configured_agent_name")
                or value.get("configuredAgentName")
                or value.get("name")
                or ""
            ).strip()
        else:
            continue
        if agent_id and business_id:
            routes[party_number] = SipAgentRoute(
                party_number=party_number,
                business_id=business_id,
                agent_id=agent_id,
                configured_name=configured_name,
            )
    return routes


def is_sip_participant(participant: Any) -> bool:
    identity = str(getattr(participant, "identity", "") or "").lower()
    attributes = getattr(participant, "attributes", None)
    return identity.startswith("sip_") or bool(
        isinstance(attributes, dict)
        and any(str(key).lower().startswith("sip.") for key in attributes)
    )


def extract_sip_party_number(participant: Any) -> str:
    attributes = getattr(participant, "attributes", None)
    lowered = {
        str(key).lower(): value
        for key, value in (attributes.items() if isinstance(attributes, dict) else [])
    }
    for key in (*_CALLEE_KEYS, *_FALLBACK_KEYS):
        candidate = normalize_phone(lowered.get(key))
        if candidate:
            return candidate
    identity = str(getattr(participant, "identity", "") or "")
    return normalize_phone(identity[4:]) if identity.lower().startswith("sip_") else ""


def resolve_sip_agent_route(
    participant: Any,
    *,
    number_map: dict[str, SipAgentRoute] | None = None,
) -> SipAgentRoute | None:
    if not is_sip_participant(participant):
        return None
    routes = number_map if number_map is not None else load_number_agent_map()
    party_number = extract_sip_party_number(participant)
    if not party_number:
        return None
    direct = routes.get(party_number)
    if direct:
        return direct
    digits = "".join(character for character in party_number if character.isdigit())
    for mapped_number, route in routes.items():
        mapped_digits = "".join(
            character for character in mapped_number if character.isdigit()
        )
        if digits == mapped_digits or (
            len(digits) >= 10
            and len(mapped_digits) >= 10
            and digits[-10:] == mapped_digits[-10:]
        ):
            return route
    return None
