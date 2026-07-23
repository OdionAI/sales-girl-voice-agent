from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from typing import Any


logger = logging.getLogger("salesgirl.voice_latency")
TRACE_LOG_PREFIX = "VOICE_LATENCY_TRACE"


def _truthy(value: str | None, *, default: bool = True) -> bool:
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "enabled"}


def _csv_set(value: str | None) -> set[str]:
    return {item.strip() for item in str(value or "").split(",") if item.strip()}


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


def elapsed_ms(start_ms: int | float | None) -> int | None:
    if start_ms is None:
        return None
    try:
        return max(0, int(monotonic_ms() - int(start_ms)))
    except (TypeError, ValueError):
        return None


def new_trace_id(prefix: str = "trace") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def hash_ref(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def is_enabled(metadata: dict[str, Any] | None = None) -> bool:
    if not _truthy(os.getenv("VOICE_LATENCY_TRACE_ENABLED"), default=True):
        return False

    filters = _csv_set(os.getenv("VOICE_LATENCY_TRACE_AGENT_IDS")) | _csv_set(
        os.getenv("VOICE_LATENCY_TRACE_BUSINESS_IDS")
    )
    if not filters:
        return True

    md = metadata or {}
    candidates = {
        str(md.get("agent_id") or "").strip(),
        str(md.get("agent_config_id") or "").strip(),
        str(md.get("runtime_agent") or "").strip(),
        str(md.get("business_id") or "").strip(),
    }
    return bool(filters & {candidate for candidate in candidates if candidate})


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _base_record(metadata: dict[str, Any] | None) -> dict[str, Any]:
    md = metadata or {}
    record: dict[str, Any] = {
        "trace_version": 1,
        "timestamp_ms": int(time.time() * 1000),
        "runtime_agent": str(md.get("runtime_agent") or md.get("agent_id") or "").strip(),
        "agent_id": str(md.get("agent_id") or md.get("agent_config_id") or "").strip(),
        "agent_config_id": str(md.get("agent_config_id") or "").strip(),
        "business_id": str(md.get("business_id") or "").strip(),
        "language": str(md.get("language") or "").strip(),
        "channel": str(md.get("channel") or md.get("identity_type") or "").strip(),
        "session_id": str(md.get("session_id") or "").strip(),
        "session_tracker_id": str(md.get("session_tracker_id") or "").strip(),
        "turn_id": str(md.get("latency_turn_id") or "").strip(),
    }
    turn_index = _safe_int(md.get("turn_index"))
    if turn_index is not None:
        record["turn_index"] = turn_index

    conversation_hash = hash_ref(md.get("conversation_id"))
    if conversation_hash:
        record["conversation_hash"] = conversation_hash
    end_user_hash = hash_ref(md.get("end_user_id"))
    if end_user_hash:
        record["end_user_hash"] = end_user_hash
    room_hash = hash_ref(md.get("room_name"))
    if room_hash:
        record["room_hash"] = room_hash

    return {key: value for key, value in record.items() if value not in ("", None)}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def emit(event: str, *, metadata: dict[str, Any] | None = None, **fields: Any) -> None:
    if not is_enabled(metadata):
        return
    record = _base_record(metadata)
    record["event"] = str(event or "").strip() or "unknown"
    for key, value in fields.items():
        if value is not None:
            record[key] = _json_safe(value)
    logger.info(
        "%s %s",
        TRACE_LOG_PREFIX,
        json.dumps(record, ensure_ascii=True, sort_keys=True, default=str),
    )
