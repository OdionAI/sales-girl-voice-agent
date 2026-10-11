"""SIP call scope from operator-pinned routing and a server-classified SIP participant."""
import json
import os

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

def participant_identity(route, participant, room_name, sip_kind):
    attrs = dict(participant.attributes)
    if participant.kind != sip_kind or attrs.get("sip.trunkID") != route["expected_trunk_id"]:
        raise ValueError("Caller is not a SIP participant on the configured trunk.")
    return {"business_id": route["business_id"], "config_agent_id": route["config_agent_id"],
            "configured_agent_name": route.get("configured_agent_name", ""),
            "end_user_id": attrs.get("sip.phoneNumber") or participant.identity,
            "participant_identity": participant.identity, "room_name": room_name,
            "entry_surface": "aicc_inbound", "language": "en",
            "caller_phone": attrs.get("sip.phoneNumber", ""),
            "sip_trunk_id": attrs["sip.trunkID"], "sip_caller_verified": True,
            "voice_auth_required": route.get("voice_auth_required", True)}
