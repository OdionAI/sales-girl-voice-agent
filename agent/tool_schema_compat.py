from __future__ import annotations

import copy
import json
from typing import Any


PRICE_SNAPSHOT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "amount": {"type": "number", "description": "Quoted price amount."},
        "currency": {
            "type": "string",
            "description": "Three-letter currency code, for example NGN or USD.",
        },
    },
    "required": ["amount", "currency"],
}

ORDER_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "item_name": {"type": "string"},
        "quantity": {"type": "integer"},
        "price_snapshot": PRICE_SNAPSHOT_SCHEMA,
    },
    "required": ["item_name", "quantity"],
}

CREATE_BOOKING_RAW_SCHEMA: dict[str, Any] = {
    "name": "create_booking",
    "description": (
        "Create a hotel booking inside the platform for the current guest and business."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "room_type": {"type": "string"},
            "check_in_date": {"type": "string"},
            "check_out_date": {"type": "string"},
            "guest_count": {"type": "integer"},
            "guest_name": {"type": "string"},
            "special_requests": {"type": "string"},
            "price_snapshot": PRICE_SNAPSHOT_SCHEMA,
            "customer_identifier": {"type": "string"},
        },
        "required": ["room_type", "check_in_date", "check_out_date"],
    },
}

CREATE_ORDER_RAW_SCHEMA: dict[str, Any] = {
    "name": "create_order",
    "description": (
        "Create a restaurant or fashion order inside the platform for the current "
        "customer and business. If the customer orders multiple different items, use "
        "the items array. Each item should include item_name, quantity, and an optional "
        "price_snapshot with amount and currency."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "item_name": {"type": "string"},
            "quantity": {"type": "integer"},
            "items": {
                "type": "array",
                "items": ORDER_ITEM_SCHEMA,
            },
            "customer_name": {"type": "string"},
            "notes": {"type": "string"},
            "price_snapshot": PRICE_SNAPSHOT_SCHEMA,
            "customer_identifier": {"type": "string"},
        },
        "required": [],
    },
}


SEARCH_BUSINESS_KNOWLEDGE_RAW_SCHEMA: dict[str, Any] = {
    "name": "search_business_knowledge",
    "description": (
        "Search the saved business knowledge base for policies, services, "
        "amenities, FAQs, and other documented facts before saying you do not "
        "know an answer."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
            "top_k": {
                "anyOf": [
                    {"type": "integer"},
                    {"type": "string"},
                    {"type": "null"},
                ]
            },
        },
        "required": ["query"],
    },
}

TRANSFER_TO_AICC_RAW_SCHEMA: dict[str, Any] = {
    "name": "transfer_to_aicc",
    "description": (
        "Transfer the current live call to the configured Huawei AICC human-agent "
        "route when the caller asks for a human or the AI cannot safely resolve the request."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "reason_summary": {"type": "string"},
        },
        "required": [],
    },
}


def strictify_schema_for_groq(schema: Any) -> Any:
    if isinstance(schema, list):
        return [strictify_schema_for_groq(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    out = copy.deepcopy(schema)
    schema_type = str(out.get("type") or "").strip().lower()
    if schema_type == "object" or isinstance(out.get("properties"), dict):
        out["type"] = "object"
        out["additionalProperties"] = False
        properties = out.get("properties")
        if isinstance(properties, dict):
            out["properties"] = {
                key: strictify_schema_for_groq(value)
                for key, value in properties.items()
            }

    if schema_type == "array" and isinstance(out.get("items"), (dict, list)):
        out["items"] = strictify_schema_for_groq(out["items"])

    for composite_key in ("anyOf", "oneOf", "allOf"):
        composite = out.get(composite_key)
        if isinstance(composite, list):
            out[composite_key] = [strictify_schema_for_groq(item) for item in composite]

    return out


def _object_has_additional_properties_false(schema: Any) -> bool:
    if isinstance(schema, list):
        return all(_object_has_additional_properties_false(item) for item in schema)
    if not isinstance(schema, dict):
        return True

    schema_type = str(schema.get("type") or "").strip().lower()
    if schema_type == "object" or isinstance(schema.get("properties"), dict):
        if schema.get("additionalProperties") is not False:
            return False

    properties = schema.get("properties")
    if isinstance(properties, dict):
        for value in properties.values():
            if not _object_has_additional_properties_false(value):
                return False

    items = schema.get("items")
    if items is not None and not _object_has_additional_properties_false(items):
        return False

    for composite_key in ("anyOf", "oneOf", "allOf"):
        composite = schema.get(composite_key)
        if isinstance(composite, list):
            for item in composite:
                if not _object_has_additional_properties_false(item):
                    return False

    return True


def normalize_price_snapshot(value: Any) -> dict[str, Any] | str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        return parsed if isinstance(parsed, dict) else raw
    return None


def normalize_order_items(value: Any) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        if "price_snapshot" in entry:
            entry["price_snapshot"] = normalize_price_snapshot(entry.get("price_snapshot"))
        normalized.append(entry)
    return normalized or None


def normalize_top_k(value: Any, *, default: int = 4, maximum: int = 6) -> int:
    try:
        resolved = int(value if value is not None else default)
    except (TypeError, ValueError):
        resolved = default
    return max(1, min(resolved, maximum))
