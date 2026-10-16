from __future__ import annotations

import copy
import logging
import os
from typing import Any
from urllib.parse import urlsplit

import httpx

from livekit.agents import RunContext, function_tool
from livekit.agents.llm.async_toolset import AsyncRunContext

from .latency_trace import elapsed_ms, emit as emit_latency_trace, monotonic_ms
from .tool_schema_compat import strictify_schema_for_groq

from .salon_agent import _is_tool_enabled, _tool_metadata

logger = logging.getLogger(__name__)

HTTP_TOOL_TIMEOUT_SECONDS = float(
    os.getenv("AGENT_DYNAMIC_TOOL_TIMEOUT_SECONDS", "12")
)
HTTP_TOOL_ALLOWED_METHODS = {
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "HEAD",
}

_SAWW_MUTATING_TOOL_MARKERS = (
    "prepare",
    "transaction",
    "transact",
    "purchase",
    "airtime",
    "top_up",
    "topup",
    "transfer",
)
_SAWW_MUTATING_HTTP_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_SAWW_PROFILE_FIELD_ALIASES = {
    "customer_id": {"customerid", "customeridentifier", "custid"},
    "phone_number": {"phone", "phonenumber", "mobile", "mobilenumber"},
    "account_number": {"accountnumber", "accountno", "acctnumber", "acctno"},
}


def _saww_profile_overrides(
    active_agent_config: dict[str, Any] | None,
    tool: dict[str, Any],
    runtime_context: dict[str, Any] | None,
) -> dict[str, Any]:
    if _saww_progress_messages(active_agent_config, tool) is None:
        return {}
    context = runtime_context or {}
    profile = context.get("wema_context")
    if not isinstance(profile, dict):
        return {}

    configured_properties = _normalize_schema(tool.get("request_schema")).get(
        "properties", {}
    )
    if not isinstance(configured_properties, dict):
        return {}

    normalized_values = {
        "customer_id": str(profile.get("customer_id") or "").strip(),
        "phone_number": str(profile.get("phone_number") or "").strip(),
        "account_number": str(profile.get("account_number") or "").strip(),
    }
    overrides: dict[str, Any] = {}
    for property_name in configured_properties:
        normalized_property = "".join(
            character
            for character in str(property_name).casefold()
            if character.isalnum()
        )
        for canonical_name, aliases in _SAWW_PROFILE_FIELD_ALIASES.items():
            if normalized_property in aliases and normalized_values[canonical_name]:
                overrides[str(property_name)] = normalized_values[canonical_name]
                break
    return overrides


def _saww_progress_messages(
    active_agent_config: dict[str, Any] | None,
    tool: dict[str, Any],
) -> str | None:
    """Return progress copy only for SAWW's mutating prepare/transaction tools."""
    config = active_agent_config or {}
    if str(config.get("name") or "").strip().casefold() != "saww":
        return None

    method = str(tool.get("method") or "POST").strip().upper()
    if method not in _SAWW_MUTATING_HTTP_METHODS:
        return None

    tool_name = str(tool.get("name") or "").strip().casefold()
    if not any(marker in tool_name for marker in _SAWW_MUTATING_TOOL_MARKERS):
        return None

    if "prepare" in tool_name:
        return "One moment, I'm preparing that request now."
    return "One moment, I'm processing that transaction now. I'll confirm the result shortly."


def _emit_dynamic_tool_latency(
    metadata: dict[str, Any] | None,
    event: str,
    **fields: Any,
) -> None:
    emit_latency_trace(event, metadata=metadata or {}, **fields)


def _active_tool_records(active_agent_config: dict[str, Any] | None) -> list[dict[str, Any]]:
    cfg = active_agent_config or {}
    tools = cfg.get("tools")
    if not isinstance(tools, list):
        return []
    return [tool for tool in tools if isinstance(tool, dict)]


def _use_strict_tool_schemas() -> bool:
    return str(os.getenv("LLM_PROVIDER") or "google").strip().lower() == "groq"


def _normalize_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(schema, dict):
        default_schema = {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "General lookup text or the specific thing the caller wants checked.",
                },
                "item_name": {
                    "type": "string",
                    "description": "Item name to look up or order.",
                },
                "product_name": {
                    "type": "string",
                    "description": "Product name to look up or order.",
                },
                "service_name": {
                    "type": "string",
                    "description": "Service or offering name when the endpoint is service-based.",
                },
                "quantity": {
                    "type": "integer",
                    "description": "How many units the caller wants.",
                },
                "customer_name": {
                    "type": "string",
                    "description": "Customer or guest name tied to the request.",
                },
                "customer_identifier": {
                    "type": "string",
                    "description": "Customer email, phone, or business-specific identifier if relevant.",
                },
                "check_in_date": {
                    "type": "string",
                    "description": "Check-in or start date when the request is date-based.",
                },
                "check_out_date": {
                    "type": "string",
                    "description": "Check-out or end date when the request is date-based.",
                },
                "guest_count": {
                    "type": "integer",
                    "description": "Guest count when the request is for bookings or reservations.",
                },
                "notes": {
                    "type": "string",
                    "description": "Extra details that should be sent with the request.",
                },
            },
            "additionalProperties": True,
        }
        if _use_strict_tool_schemas():
            return strictify_schema_for_groq(default_schema)
        return default_schema

    normalized = copy.deepcopy(schema)
    if str(normalized.get("type") or "").strip().lower() != "object":
        normalized["type"] = "object"
    if not isinstance(normalized.get("properties"), dict):
        normalized["properties"] = {}
    if "additionalProperties" not in normalized:
        normalized["additionalProperties"] = True
    required = normalized.get("required")
    if not isinstance(required, list):
        normalized["required"] = []
    else:
        normalized["required"] = [
            str(item).strip() for item in required if str(item).strip()
        ]
    if _use_strict_tool_schemas():
        return strictify_schema_for_groq(normalized)
    return normalized


def _value_matches_type(value: Any, expected_type: Any, item_schema: dict[str, Any] | None = None) -> bool:
    expected = expected_type
    if isinstance(expected, list):
        return any(_value_matches_type(value, item, item_schema=item_schema) for item in expected)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(
            value, float
        )
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        if not isinstance(value, list):
            return False
        if not isinstance(item_schema, dict):
            return True
        item_type = item_schema.get("type")
        return all(_value_matches_type(item, item_type, item_schema=item_schema.get("items")) for item in value)
    if expected == "null":
        return value is None
    return True


def _validate_arguments(schema: dict[str, Any], raw_arguments: dict[str, Any]) -> str | None:
    if not isinstance(raw_arguments, dict):
        return "Tool arguments must be sent as a JSON object."

    required = schema.get("required") or []
    missing = [
        field
        for field in required
        if field not in raw_arguments or raw_arguments.get(field) in ("", None)
    ]
    if missing:
        return f"Missing required fields: {', '.join(sorted(missing))}."

    properties = schema.get("properties") or {}
    allow_additional = schema.get("additionalProperties", True)

    for key, value in raw_arguments.items():
        prop_schema = properties.get(key)
        if prop_schema is None:
            if allow_additional is False:
                return f"Unexpected field: {key}."
            continue

        if not isinstance(prop_schema, dict):
            continue
        expected_type = prop_schema.get("type")
        if expected_type and not _value_matches_type(
            value, expected_type, item_schema=prop_schema.get("items")
        ):
            return f"Field '{key}' has the wrong type."
        enum_values = prop_schema.get("enum")
        if isinstance(enum_values, list) and enum_values and value not in enum_values:
            return f"Field '{key}' must be one of: {', '.join(map(str, enum_values))}."

    return None


def _metadata_headers(metadata: dict[str, Any], tool_name: str) -> dict[str, str]:
    headers = {
        "X-Tool-Name": tool_name,
        "X-Client-Id": str(metadata.get("client_id") or ""),
        "X-Agent-Id": str(metadata.get("agent_id") or ""),
        "X-Business-Id": str(metadata.get("business_id") or ""),
        "X-Conversation-Id": str(metadata.get("conversation_id") or ""),
        "X-Session-Id": str(metadata.get("session_id") or ""),
        "X-End-User-Id": str(metadata.get("end_user_id") or ""),
    }
    return {key: value for key, value in headers.items() if value}


def _custom_headers(tool: dict[str, Any], method: str) -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/json"}
    if method not in {"GET", "DELETE", "HEAD"}:
        headers["Content-Type"] = "application/json"
    raw_headers = tool.get("headers")
    if isinstance(raw_headers, dict):
        for key, value in raw_headers.items():
            header_name = str(key or "").strip()
            if not header_name:
                continue
            headers[header_name] = str(value)
    return headers


def _normalized_origin(raw_url: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(str(raw_url or "").strip())
        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or "").lower()
        if scheme not in {"http", "https"} or not hostname:
            return None
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    return scheme, hostname, port


def _conversation_service_auth_headers(url: str) -> dict[str, str]:
    """Inject runtime auth only for the configured conversation-service origin."""
    configured_origin = _normalized_origin(
        os.getenv("CONVERSATION_API_BASE_URL", "")
    )
    request_origin = _normalized_origin(url)
    service_token = str(os.getenv("CONVERSATION_SERVICE_TOKEN") or "").strip()
    if not service_token or not configured_origin or request_origin != configured_origin:
        return {}
    return {
        "X-Service-Token": service_token,
        "X-Service-Name": "sales-girl-voice-agent",
    }


def _response_payload(tool_name: str, response: httpx.Response) -> Any:
    content_type = str(response.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            return response.json()
        except ValueError:
            return response.text
    return response.text


def _success_payload(tool_name: str, response: httpx.Response, payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        enriched = dict(payload)
        enriched.setdefault("status", "success")
        enriched.setdefault("tool_name", tool_name)
        enriched.setdefault("http_status", response.status_code)
        return enriched
    return {
        "status": "success",
        "tool_name": tool_name,
        "http_status": response.status_code,
        "data": payload,
    }


def _failure_payload(tool_name: str, message: str, *, http_status: int | None = None, detail: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "failed",
        "tool_name": tool_name,
        "message": message,
    }
    if http_status is not None:
        payload["http_status"] = http_status
    if detail not in (None, ""):
        payload["detail"] = detail
    return payload


async def invoke_dynamic_http_tool(
    *,
    tool: dict[str, Any],
    raw_arguments: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    started_ms = monotonic_ms()
    tool_name = str(tool.get("name") or "").strip() or "dynamic_tool"
    method = str(tool.get("method") or "POST").strip().upper() or "POST"
    url = str(tool.get("url") or "").strip()
    schema = _normalize_schema(tool.get("request_schema"))
    validation_error = _validate_arguments(schema, raw_arguments)
    if validation_error:
        _emit_dynamic_tool_latency(
            metadata,
            "dynamic_tool_completed",
            tool_name=tool_name,
            method=method,
            status="failed",
            duration_ms=elapsed_ms(started_ms),
            failure_reason="validation_error",
        )
        return _failure_payload(
            tool_name,
            "I couldn't complete that request with the information provided.",
            detail=validation_error,
        )

    if method not in HTTP_TOOL_ALLOWED_METHODS:
        _emit_dynamic_tool_latency(
            metadata,
            "dynamic_tool_completed",
            tool_name=tool_name,
            method=method,
            status="failed",
            duration_ms=elapsed_ms(started_ms),
            failure_reason="unsupported_method",
        )
        return _failure_payload(
            tool_name,
            "I couldn't complete that request right now.",
            detail=f"Unsupported HTTP method: {method}",
        )
    if not url.startswith(("http://", "https://")):
        _emit_dynamic_tool_latency(
            metadata,
            "dynamic_tool_completed",
            tool_name=tool_name,
            method=method,
            status="failed",
            duration_ms=elapsed_ms(started_ms),
            failure_reason="invalid_url",
        )
        return _failure_payload(
            tool_name,
            "I couldn't complete that request right now.",
            detail="Tool endpoint must use http or https.",
        )

    headers = _custom_headers(tool, method)
    # Internal service credentials stay in the runtime environment. They are
    # never persisted in an agent tool definition returned by agent-config.
    headers.update(_conversation_service_auth_headers(url))
    headers.update(_metadata_headers(metadata, tool_name))
    request_kwargs: dict[str, Any] = {
        "method": method,
        "url": url,
        "headers": headers,
    }
    if method in {"GET", "DELETE", "HEAD"}:
        if raw_arguments:
            request_kwargs["params"] = raw_arguments
    else:
        request_kwargs["json"] = raw_arguments

    try:
        async with httpx.AsyncClient(
            timeout=HTTP_TOOL_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            response = await client.request(**request_kwargs)
    except httpx.TimeoutException:
        logger.warning("[TOOL] dynamic_http_tool timeout name=%s url=%s", tool_name, url)
        _emit_dynamic_tool_latency(
            metadata,
            "dynamic_tool_completed",
            tool_name=tool_name,
            method=method,
            status="timeout",
            duration_ms=elapsed_ms(started_ms),
        )
        return _failure_payload(
            tool_name,
            "I couldn't complete that request right now.",
            detail="Tool request timed out.",
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "[TOOL] dynamic_http_tool http_error name=%s url=%s detail=%s",
            tool_name,
            url,
            exc,
        )
        _emit_dynamic_tool_latency(
            metadata,
            "dynamic_tool_completed",
            tool_name=tool_name,
            method=method,
            status="http_error",
            duration_ms=elapsed_ms(started_ms),
            error_type=type(exc).__name__,
        )
        return _failure_payload(
            tool_name,
            "I couldn't complete that request right now.",
            detail=str(exc),
        )

    payload = _response_payload(tool_name, response)
    if response.status_code >= 400:
        logger.warning(
            "[TOOL] dynamic_http_tool failed name=%s status=%s url=%s",
            tool_name,
            response.status_code,
            url,
        )
        detail = payload if isinstance(payload, dict) else str(payload).strip()
        _emit_dynamic_tool_latency(
            metadata,
            "dynamic_tool_completed",
            tool_name=tool_name,
            method=method,
            status="failed",
            http_status=response.status_code,
            duration_ms=elapsed_ms(started_ms),
        )
        return _failure_payload(
            tool_name,
            "I couldn't complete that request right now.",
            http_status=response.status_code,
            detail=detail,
        )

    logger.info(
        "[TOOL] dynamic_http_tool name=%s method=%s status=%s",
        tool_name,
        method,
        response.status_code,
    )
    _emit_dynamic_tool_latency(
        metadata,
        "dynamic_tool_completed",
        tool_name=tool_name,
        method=method,
        status="success",
        http_status=response.status_code,
        duration_ms=elapsed_ms(started_ms),
    )
    return _success_payload(tool_name, response, payload)


def build_dynamic_http_tools(
    active_agent_config: dict[str, Any] | None,
    *,
    excluded_tool_names: set[str] | None = None,
    runtime_context: dict[str, Any] | None = None,
) -> list[Any]:
    excluded = {str(name or "").strip() for name in (excluded_tool_names or set())}
    dynamic_tools: list[Any] = []

    for tool in _active_tool_records(active_agent_config):
        tool_name = str(tool.get("name") or "").strip()
        description = " ".join(str(tool.get("description") or "").split()).strip()
        url = str(tool.get("url") or "").strip()
        if (
            not tool_name
            or tool_name in excluded
            or not description
            or not url.startswith(("http://", "https://"))
        ):
            continue

        progress = _saww_progress_messages(active_agent_config, tool)
        profile_overrides = _saww_profile_overrides(
            active_agent_config, tool, runtime_context
        )
        parameters = _normalize_schema(tool.get("request_schema"))
        if profile_overrides:
            properties = parameters.get("properties")
            if isinstance(properties, dict):
                parameters["properties"] = {
                    name: definition
                    for name, definition in properties.items()
                    if name not in profile_overrides
                }
            parameters["required"] = [
                name
                for name in parameters.get("required", [])
                if name not in profile_overrides
            ]

        raw_schema = {
            "name": tool_name,
            "description": (
                f"{description} When you call this tool, send the relevant request fields as top-level JSON keys."
            ),
            "parameters": parameters,
        }

        async def _call_dynamic_http_tool(
            ctx: RunContext | AsyncRunContext,
            raw_arguments: dict[str, Any],
            _tool: dict[str, Any] = tool,
            _active_agent_config: dict[str, Any] = active_agent_config or {},
            _runtime_context: dict[str, Any] = runtime_context or {},
            _profile_overrides: dict[str, Any] = profile_overrides,
            _progress: str | None = progress,
        ) -> dict[str, Any]:
            current_name = str(_tool.get("name") or "").strip()
            if not _is_tool_enabled(ctx, current_name):
                return _failure_payload(
                    current_name,
                    "I can't use that tool from this agent right now.",
                )
            arguments = (
                raw_arguments if isinstance(raw_arguments, dict) else {}
            )
            if _profile_overrides:
                arguments = {**arguments, **_profile_overrides}

            if _progress is None:
                return await invoke_dynamic_http_tool(
                    tool=_tool,
                    raw_arguments=arguments,
                    metadata=_tool_metadata(ctx),
                )

            session_userdata = getattr(ctx, "userdata", None)
            if not isinstance(session_userdata, dict):
                session_userdata = getattr(
                    getattr(ctx, "session", None), "userdata", None
                )
            if isinstance(session_userdata, dict) and session_userdata.get(
                "_saww_mutating_tool_in_flight"
            ):
                return _failure_payload(
                    current_name,
                    "Another request is still being processed. Wait for its result before starting another transaction.",
                )

            if isinstance(session_userdata, dict):
                session_userdata["_saww_mutating_tool_in_flight"] = True
            # The first update switches this function tool to LiveKit's async
            # progress mode: the agent can acknowledge immediately while the
            # HTTP request continues, then speak again with the real result.
            try:
                await ctx.update(_progress)
                return await invoke_dynamic_http_tool(
                    tool=_tool,
                    raw_arguments=arguments,
                    metadata=_tool_metadata(ctx),
                )
            finally:
                if isinstance(session_userdata, dict):
                    session_userdata.pop("_saww_mutating_tool_in_flight", None)

        if progress is not None:
            # AsyncToolset recognizes this annotation and runs the tool in
            # the background when its first ctx.update() is called.
            _call_dynamic_http_tool.__annotations__["ctx"] = AsyncRunContext
        dynamic_tool = function_tool(raw_schema=raw_schema)(_call_dynamic_http_tool)
        dynamic_tool._saww_async_progress = progress is not None
        dynamic_tools.append(dynamic_tool)

    return dynamic_tools
