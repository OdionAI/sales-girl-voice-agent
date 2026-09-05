from __future__ import annotations

import copy
import logging
import os
import re
import time
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

import httpx

from livekit.agents import RunContext, function_tool

from .tool_schema_compat import strictify_schema_for_groq

from .salon_agent import _is_tool_enabled, _tool_metadata
from .auth_observer import ACTION_WEMA_EXECUTE_PREPARED, is_privileged_action
from .tool_wait_speech import GeneratedToolWaitSpeech, normalize_tool_wait_speech_mode

logger = logging.getLogger(__name__)

HTTP_TOOL_TIMEOUT_SECONDS = float(
    os.getenv("AGENT_DYNAMIC_TOOL_TIMEOUT_SECONDS", "12")
)
HTTP_TOOL_FILLER_DELAY_SECONDS = float(
    os.getenv("AGENT_DYNAMIC_TOOL_FILLER_DELAY_SECONDS", "0.75")
)
HTTP_TOOL_FILLER_INTERVAL_SECONDS = float(
    os.getenv("AGENT_DYNAMIC_TOOL_FILLER_INTERVAL_SECONDS", "6")
)
HTTP_TOOL_FILLER_MESSAGES = (
    "One moment while I check your request.",
    "Thanks for waiting. I'm still checking that for you.",
    "It's taking a little longer. I'm still here with you.",
)
HTTP_TOOL_ACKNOWLEDGEMENTS = {
    "wema_get_balance": "Let me check your available balance.",
    "wema_get_transactions": "Let me check your recent transactions.",
    "wema_list_data_plans": "Let me check the data plans for you.",
    "wema_list_transfer_banks": "Let me look up that bank for you.",
    "wema_prepare_data_purchase": "Let me check the details for your data purchase.",
    "wema_prepare_transfer": "Let me check the details for your transfer.",
    "wema_execute_prepared": "Let me check your confirmed transaction request.",
}
HTTP_TOOL_ALLOWED_METHODS = {
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "HEAD",
}
WEMA_TOOL_PREFIX = "wema_"
WEMA_CUSTOMER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,120}$")
WEMA_ACCOUNT_NUMBER_PATTERN = re.compile(r"^\d{10}$")
WEMA_PHONE_PATTERN = re.compile(r"^(?:0\d{10}|\+234\d{10})$")
AUTH_CONTROL_FIELDS = {
    "authenticated",
    "auth_status",
    "skip_auth",
    "voice_auth_authorized",
}


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
    if tool_name.startswith(WEMA_TOOL_PREFIX):
        customer_id = str(metadata.get("wema_customer_id") or "").strip()
        if WEMA_CUSTOMER_ID_PATTERN.fullmatch(customer_id):
            headers["X-Wema-Customer-Id"] = customer_id
    return {key: value for key, value in headers.items() if value}


def _wema_arguments_with_session_defaults(
    tool: dict[str, Any],
    metadata: dict[str, Any],
    raw_arguments: dict[str, Any],
) -> dict[str, Any]:
    arguments = dict(raw_arguments)
    tool_name = str(tool.get("name") or "").strip()
    if not tool_name.startswith(WEMA_TOOL_PREFIX):
        return arguments

    schema = _normalize_schema(tool.get("request_schema"))
    properties = schema.get("properties") or {}
    account_number = str(metadata.get("wema_account_number") or "").strip()
    phone_number = str(metadata.get("wema_phone_number") or "").strip()
    if (
        "source_account" in properties
        and arguments.get("source_account") in (None, "")
        and WEMA_ACCOUNT_NUMBER_PATTERN.fullmatch(account_number)
    ):
        arguments["source_account"] = account_number
    if (
        "phone_number" in properties
        and arguments.get("phone_number") in (None, "")
        and WEMA_PHONE_PATTERN.fullmatch(phone_number)
    ):
        arguments["phone_number"] = phone_number
    return arguments


def _model_facing_schema(tool_name: str, schema: dict[str, Any]) -> dict[str, Any]:
    if not tool_name.startswith(WEMA_TOOL_PREFIX):
        return schema
    model_schema = copy.deepcopy(schema)
    properties = model_schema.get("properties")
    if isinstance(properties, dict):
        for field in AUTH_CONTROL_FIELDS:
            properties.pop(field, None)
    required = model_schema.get("required") or []
    model_schema["required"] = [
        field
        for field in required
        if field
        not in {"source_account", "phone_number", *AUTH_CONTROL_FIELDS}
    ]
    return model_schema


def _notify_tool_activity(
    callback: Callable[[dict[str, Any]], Any] | None,
    payload: dict[str, Any],
) -> None:
    if callback is None:
        return
    try:
        callback(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not publish dynamic tool activity name=%s event=%s: %s",
            payload.get("tool_name"),
            payload.get("event"),
            exc,
        )


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


def _session_userdata(ctx: RunContext) -> dict[str, Any]:
    session = getattr(ctx, "session", None)
    userdata = getattr(session, "userdata", None)
    return userdata if isinstance(userdata, dict) else {}


def _tool_wait_filler(
    ctx: RunContext, tool_name: str, step: int,
    generated: GeneratedToolWaitSpeech | None = None,
):
    userdata = _session_userdata(ctx)
    now = time.monotonic()
    last_spoken = userdata.get("_dynamic_tool_last_filler_at", float("-inf"))
    # Parallel tools share one voice; only one may fill the same quiet pause.
    if now - last_spoken < HTTP_TOOL_FILLER_INTERVAL_SECONDS:
        return None
    userdata["_dynamic_tool_last_filler_at"] = now
    text = (
        HTTP_TOOL_ACKNOWLEDGEMENTS.get(tool_name, HTTP_TOOL_FILLER_MESSAGES[0])
        if step == 0 else HTTP_TOOL_FILLER_MESSAGES[step]
    )
    return generated.say(step, text) if generated is not None else text


def _voice_auth_blocked_payload(tool_name: str, decision: dict[str, Any] | None = None) -> dict[str, Any]:
    auth_decision = decision if isinstance(decision, dict) else {}
    request_label = (
        "transaction"
        if tool_name == ACTION_WEMA_EXECUTE_PREPARED
        else "banking request"
    )
    return {
        "status": "failed",
        "tool_name": tool_name,
        "auth_required": True,
        "auth_status": str(auth_decision.get("session_status") or "pending"),
        "action_status": str(auth_decision.get("action_status") or "pending"),
        "reason": "voice_not_recognized",
        "message": (
            f"The {request_label} was blocked because the caller's voice was not recognized. "
            "Tell the caller clearly that you could not recognize their voice, so you "
            f"cannot complete this {request_label}."
        ),
    }


async def _authorize_privileged_dynamic_tool(
    ctx: RunContext,
    *,
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[bool, Any | None, dict[str, Any] | None]:
    if not is_privileged_action(tool_name):
        return True, None, None

    userdata = _session_userdata(ctx)
    observer = userdata.get("auth_observer")
    authorize = getattr(observer, "authorize_action", None)
    if not callable(authorize):
        logger.warning("[TOOL] %s blocked because voice auth observer is unavailable", tool_name)
        return False, None, _voice_auth_blocked_payload(tool_name)

    decision = await authorize(
        action=tool_name,
        transcript=str(userdata.get("last_user_transcript") or ""),
        details=arguments,
    )
    voice_auth_authorized = decision.get("authorized") is True
    if voice_auth_authorized is not True:
        logger.info(
            "[TOOL] %s blocked reason=%s",
            tool_name,
            decision.get("reason") or "voice_not_recognized",
        )
        return False, observer, _voice_auth_blocked_payload(tool_name, decision)
    return True, observer, None


async def _invoke_authorized_dynamic_http_tool(
    *,
    ctx: RunContext,
    tool: dict[str, Any],
    arguments: dict[str, Any],
    metadata: dict[str, Any],
    observer: Any,
    voice_auth_authorized: bool,
) -> dict[str, Any]:
    tool_name = str(tool.get("name") or "").strip()
    if voice_auth_authorized is not True:
        return _voice_auth_blocked_payload(tool_name)

    result = await invoke_dynamic_http_tool(
        tool=tool,
        raw_arguments=arguments,
        metadata=metadata,
    )
    publish_outcome = getattr(observer, "publish_action_outcome", None)
    if tool_name == ACTION_WEMA_EXECUTE_PREPARED and callable(publish_outcome):
        try:
            await publish_outcome(
                action=tool_name,
                tool_result=result,
                details=arguments,
            )
        except Exception:  # noqa: BLE001
            logger.exception("[TOOL] could not publish final auth action outcome name=%s", tool_name)
    return result


async def invoke_dynamic_http_tool(
    *,
    tool: dict[str, Any],
    raw_arguments: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    tool_name = str(tool.get("name") or "").strip() or "dynamic_tool"
    method = str(tool.get("method") or "POST").strip().upper() or "POST"
    url = str(tool.get("url") or "").strip()
    schema = _normalize_schema(tool.get("request_schema"))
    validation_error = _validate_arguments(schema, raw_arguments)
    if validation_error:
        return _failure_payload(
            tool_name,
            "I couldn't complete that request with the information provided.",
            detail=validation_error,
        )

    if method not in HTTP_TOOL_ALLOWED_METHODS:
        return _failure_payload(
            tool_name,
            "I couldn't complete that request right now.",
            detail=f"Unsupported HTTP method: {method}",
        )
    if not url.startswith(("http://", "https://")):
        return _failure_payload(
            tool_name,
            "I couldn't complete that request right now.",
            detail="Tool endpoint must use http or https.",
        )

    headers = _custom_headers(tool, method)
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
    return _success_payload(tool_name, response, payload)


def build_dynamic_http_tools(
    active_agent_config: dict[str, Any] | None,
    *,
    excluded_tool_names: set[str] | None = None,
    on_tool_activity: Callable[[dict[str, Any]], Any] | None = None,
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

        tool_schema = _normalize_schema(tool.get("request_schema"))
        description_suffix = (
            " The caller's selected account and own phone number are supplied by "
            "the session when omitted. Ask only when the caller wants a different one."
            if tool_name.startswith(WEMA_TOOL_PREFIX)
            else ""
        )
        raw_schema = {
            "name": tool_name,
            "description": (
                f"{description}{description_suffix} When you call this tool, send the relevant request fields as top-level JSON keys."
            ),
            "parameters": _model_facing_schema(tool_name, tool_schema),
        }

        async def _call_dynamic_http_tool(
            ctx: RunContext, raw_arguments: dict[str, Any], _tool: dict[str, Any] = tool
        ) -> dict[str, Any]:
            current_name = str(_tool.get("name") or "").strip()
            metadata = _tool_metadata(ctx)
            supplied_arguments = (
                raw_arguments if isinstance(raw_arguments, dict) else {}
            )
            arguments = _wema_arguments_with_session_defaults(
                _tool,
                metadata,
                supplied_arguments,
            )
            call_id = f"tool-{uuid.uuid4().hex}"
            started_at_ms = int(time.time() * 1000)
            _notify_tool_activity(
                on_tool_activity,
                {
                    "event": "started",
                    "call_id": call_id,
                    "tool_name": current_name,
                    "arguments": arguments,
                    "started_at_ms": started_at_ms,
                    "ts_ms": started_at_ms,
                },
            )
            if not _is_tool_enabled(ctx, current_name):
                result = _failure_payload(
                    current_name,
                    "I can't use that tool from this agent right now.",
                )
            elif is_privileged_action(current_name) and any(
                field in supplied_arguments for field in AUTH_CONTROL_FIELDS
            ):
                logger.warning(
                    "[TOOL] %s rejected model-supplied voice auth field",
                    current_name,
                )
                result = _voice_auth_blocked_payload(current_name)
            else:
                mode = normalize_tool_wait_speech_mode(
                    _session_userdata(ctx).get("tool_wait_speech_mode")
                )
                generated = (
                    GeneratedToolWaitSpeech(ctx, current_name)
                    if mode == "llm_generated" else None
                )
                logger.info("[TOOL] waiting speech mode=%s name=%s", mode, current_name)
                # The filler scope covers both voice checks and the HTTP wait.
                # LiveKit only speaks during idle pauses and closes the scheduler on exit.
                async with generated or nullcontext(), ctx.with_filler(
                    lambda step: _tool_wait_filler(ctx, current_name, step, generated),
                    delay=HTTP_TOOL_FILLER_DELAY_SECONDS,
                    interval=HTTP_TOOL_FILLER_INTERVAL_SECONDS,
                    max_steps=len(HTTP_TOOL_FILLER_MESSAGES),
                ):
                    if is_privileged_action(current_name):
                        authorized, observer, blocked = await _authorize_privileged_dynamic_tool(
                            ctx,
                            tool_name=current_name,
                            arguments=arguments,
                        )
                        if blocked is not None:
                            result = blocked
                        else:
                            result = await _invoke_authorized_dynamic_http_tool(
                                ctx=ctx,
                                tool=_tool,
                                arguments=arguments,
                                metadata=metadata,
                                observer=observer,
                                voice_auth_authorized=authorized,
                            )
                    else:
                        result = await invoke_dynamic_http_tool(
                            tool=_tool,
                            raw_arguments=arguments,
                            metadata=metadata,
                        )
            completed_at_ms = int(time.time() * 1000)
            _notify_tool_activity(
                on_tool_activity,
                {
                    "event": "completed",
                    "call_id": call_id,
                    "tool_name": current_name,
                    "arguments": arguments,
                    "result": result,
                    "status": (
                        str(result.get("status") or "success")
                        if isinstance(result, dict)
                        else "success"
                    ),
                    "started_at_ms": started_at_ms,
                    "ts_ms": completed_at_ms,
                },
            )
            return result

        dynamic_tools.append(function_tool(raw_schema=raw_schema)(_call_dynamic_http_tool))

    return dynamic_tools
