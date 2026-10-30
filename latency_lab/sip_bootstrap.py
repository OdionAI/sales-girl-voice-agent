"""SIP call scope from operator-pinned routing and a server-classified SIP participant.

``RVC_AICC_SIP_ROUTE`` may carry per-number routes in addition to its top-level
default agent:

* ``did_routes``     — keyed by the **called** number (the DID / short code).
* ``caller_routes``  — keyed by the **caller** number (the ``from`` number).

Resolution order: ``did_routes`` -> ``caller_routes`` -> the top-level default.

The dialed number arrives under different attribute names depending on the
carrier/trunk (a mapped header such as ``X-Callee-Number``, or LiveKit's own
trunk-number attribute). To be robust we first read the well-known keys, then —
only for numbers the operator has explicitly configured — scan every attribute
value plus the participant identity and room name. Scanning can only match a
configured DID, so it cannot misroute a number the operator did not map.
"""
import json
import os
import re
import sys

_NAMED_UNKNOWN = {"anonymous", "restricted", "unavailable", "unknown", "private"}

# LiveKit exposes the callee (called) number under one of these attributes.
_CALLEE_KEYS = (
    "sip.to",
    "sip.h.to",
    "sip.h.x-to",
    "sip.trunkphonenumber",
    "sip.trunk_phone_number",
    "sip.h.x-callee-number",
    "sip.h.x-odion-callee-number",
    "sip.x_callee_number",
    "sip.calleenumber",
    "sip.callee_number",
    "sip.called",
)
# Caller (from) number attributes.
_CALLER_KEYS = (
    "sip.phonenumber",
    "sip.phone_number",
    "sip.fromuser",
    "sip.from_user",
    "sip.from",
)


def normalize_number(value):
    """Return the significant digits of a phone value (drops sip:/host/user)."""
    raw = str(value or "").strip()
    if not raw or raw.lower() in _NAMED_UNKNOWN:
        return ""
    if "@" in raw:
        match = re.search(r"(?i)(?:sip:|tel:)?([^;>\s@]+)@", raw)
        raw = match.group(1) if match else ""
    digits = "".join(character for character in raw if character.isdigit())
    while digits.startswith("00") and len(digits) > 2:
        digits = digits[2:]
    return digits


def _lookup(number_map, value):
    """Find the route for ``value`` in ``number_map`` (exact, then last-10 digits)."""
    target = normalize_number(value)
    if not target or not isinstance(number_map, dict):
        return None
    for key, route in number_map.items():
        candidate = normalize_number(key)
        if not candidate or not isinstance(route, dict):
            continue
        if target == candidate:
            return route
        if len(target) >= 10 and len(candidate) >= 10 and target[-10:] == candidate[-10:]:
            return route
    return None


def _lookup_any(number_map, values):
    """Return the first route whose key matches any of ``values``."""
    for value in values:
        route = _lookup(number_map, value)
        if route:
            return route
    return None


def resolve_route(metadata, room_name):
    payload = json.loads(metadata or "{}")
    if payload.get("entry_surface") != "aicc_inbound":
        return None
    route = json.loads(os.getenv("RVC_AICC_SIP_ROUTE", "{}"))
    trunk = payload.get("sip_trunk_id")
    if (not isinstance(route, dict) or trunk not in route.get("trunk_ids", [])
            or not route.get("room_prefix") or not room_name.startswith(route["room_prefix"])
            or not route.get("business_id") or not route.get("config_agent_id")):
        raise ValueError("Unconfigured SIP dispatch scope.")
    return {**route, "expected_trunk_id": trunk}


def participant_identity(route, participant, room_name, sip_kind, *, called_override="", dynamic_did_route=None):
    attrs = dict(participant.attributes)
    lowered = {str(key).lower(): value for key, value in attrs.items()}
    if participant.kind != sip_kind or lowered.get("sip.trunkid") != route["expected_trunk_id"]:
        raise ValueError("Caller is not a SIP participant on the configured trunk.")

    called = called_override or next((lowered.get(key) for key in _CALLEE_KEYS if lowered.get(key)), "")
    caller = next((lowered.get(key) for key in _CALLER_KEYS if lowered.get(key)), "")
    if not caller:
        caller = attrs.get("sip.phoneNumber") or participant.identity

    did_map = route.get("did_routes") or {}
    caller_map = route.get("caller_routes") or {}
    # The caller/From number must never be mistaken for a configured To route.
    other_callee_values = [
        str(value) for key, value in lowered.items()
        if key not in _CALLER_KEYS and key != "sip.trunkid"
    ]

    # Temporary diagnostic: which attributes did this call actually carry?
    try:
        print(f"[sip-routing] attrs={json.dumps(attrs, default=str)}", file=sys.stderr, flush=True)
    except Exception:
        pass

    caller_route = (
        _lookup(caller_map, caller)
        or _lookup_any(caller_map, [participant.identity, room_name])
    )
    did_route = (
        _lookup(did_map, called)
        or (None if called else _lookup_any(did_map, other_callee_values))
    )
    # A caller-specific override is opt-in per route. This lets a verified
    # caller reach their configured agent when a carrier rewrites a dialed DID
    # to a shared short code, while keeping DID-first routing for everyone else.
    chosen = (
        caller_route
        if caller_route and caller_route.get("override_did_routes") is True
        else dynamic_did_route or did_route or caller_route or {}
    )
    return {
        "business_id": chosen.get("business_id") or route["business_id"],
        "config_agent_id": chosen.get("config_agent_id") or route["config_agent_id"],
        "configured_agent_name": (
            chosen.get("configured_agent_name") or route.get("configured_agent_name", "")
        ),
        "end_user_id": attrs.get("sip.phoneNumber") or participant.identity,
        "participant_identity": participant.identity,
        "room_name": room_name,
        "entry_surface": "aicc_inbound",
        "language": "en",
        "caller_phone": attrs.get("sip.phoneNumber", ""),
        "caller_number": caller,
        "called_phone": called,
        "outbound_required": chosen.get("outbound_required") is True,
        "sip_attribute_keys": sorted(str(key) for key in attrs.keys()),
        "sip_trunk_id": attrs["sip.trunkID"],
        "sip_caller_verified": True,
        "voice_auth_required": chosen.get(
            "voice_auth_required", route.get("voice_auth_required", True)
        ),
    }
