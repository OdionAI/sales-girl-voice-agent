from __future__ import annotations

import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from agent.dynamic_tools import build_dynamic_http_tools


class DynamicHttpToolsTests(unittest.IsolatedAsyncioTestCase):
    def _run_context(self, enabled_tool_names: list[str]) -> SimpleNamespace:
        return SimpleNamespace(
            session=SimpleNamespace(
                userdata={
                    "enabled_tool_names": enabled_tool_names,
                    "client_id": "sales-girl-internal",
                    "agent_config_id": "agent-123",
                    "business_id": "biz-123",
                    "conversation_id": "conv-123",
                    "session_id": "sess-123",
                    "end_user_id": "caller@example.com",
                }
            ),
            room=SimpleNamespace(name="room-123"),
        )

    async def test_dynamic_post_tool_executes_and_returns_json_payload(self) -> None:
        tools = build_dynamic_http_tools(
            {
                "tools": [
                    {
                        "name": "lookup_inventory",
                        "description": "Check live inventory and prices whenever a caller asks what items are available.",
                        "method": "POST",
                        "url": "https://vendor.example.com/inventory",
                        "request_schema": {
                            "type": "object",
                            "properties": {
                                "item_name": {"type": "string"},
                            },
                            "required": ["item_name"],
                        },
                    }
                ]
            }
        )
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].info.name, "lookup_inventory")

        with patch("agent.dynamic_tools.httpx.AsyncClient") as client_cls:
            client = AsyncMock()
            client.request.return_value = httpx.Response(
                200,
                json={
                    "status": "success",
                    "items": [{"item_name": "Canvas Tote", "price": 22000}],
                },
                headers={"content-type": "application/json"},
            )
            client_cls.return_value.__aenter__.return_value = client
            client_cls.return_value.__aexit__.return_value = False

            result = await tools[0](
                ctx=self._run_context(["lookup_inventory"]),
                raw_arguments={"item_name": "Canvas Tote"},
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["items"][0]["item_name"], "Canvas Tote")
        client.request.assert_awaited_once()
        kwargs = client.request.await_args.kwargs
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["url"], "https://vendor.example.com/inventory")
        self.assertEqual(kwargs["json"], {"item_name": "Canvas Tote"})
        self.assertEqual(kwargs["headers"]["X-Business-Id"], "biz-123")
        self.assertEqual(kwargs["headers"]["X-Agent-Id"], "agent-123")

    async def test_dynamic_tool_validates_required_fields(self) -> None:
        tools = build_dynamic_http_tools(
            {
                "tools": [
                    {
                        "name": "create_vendor_order",
                        "description": "Create a vendor order after the caller confirms the item and quantity.",
                        "method": "POST",
                        "url": "https://vendor.example.com/order",
                        "request_schema": {
                            "type": "object",
                            "properties": {
                                "item_name": {"type": "string"},
                                "quantity": {"type": "integer"},
                            },
                            "required": ["item_name", "quantity"],
                            "additionalProperties": False,
                        },
                    }
                ]
            }
        )

        result = await tools[0](
            ctx=self._run_context(["create_vendor_order"]),
            raw_arguments={"item_name": "Canvas Tote"},
        )

        self.assertEqual(result["status"], "failed")
        self.assertIn("Missing required fields", result["detail"])

    async def test_conversation_service_tool_uses_runtime_service_auth(self) -> None:
        tools = build_dynamic_http_tools(
            {
                "tools": [
                    {
                        "name": "record_caller_details",
                        "description": "Record confirmed caller details after the request has been handled.",
                        "method": "POST",
                        "url": "http://conversation-service:8091/v1/tools/caller-records",
                        "request_schema": {
                            "type": "object",
                            "properties": {"first_name": {"type": "string"}},
                            "required": ["first_name"],
                        },
                    }
                ]
            }
        )

        with (
            patch.dict(
                os.environ,
                {
                    "CONVERSATION_API_BASE_URL": "http://conversation-service:8091",
                    "CONVERSATION_SERVICE_TOKEN": "runtime-only-token",
                },
            ),
            patch("agent.dynamic_tools.httpx.AsyncClient") as client_cls,
        ):
            client = AsyncMock()
            client.request.return_value = httpx.Response(
                201,
                json={"status": "success", "saved": True},
                headers={"content-type": "application/json"},
            )
            client_cls.return_value.__aenter__.return_value = client
            client_cls.return_value.__aexit__.return_value = False

            result = await tools[0](
                ctx=self._run_context(["record_caller_details"]),
                raw_arguments={"first_name": "Aïcha"},
            )

        self.assertEqual(result["status"], "success")
        headers = client.request.await_args.kwargs["headers"]
        self.assertEqual(headers["X-Service-Token"], "runtime-only-token")
        self.assertEqual(headers["X-Service-Name"], "sales-girl-voice-agent")

    async def test_post_call_caller_marker_can_be_hidden_from_live_runtime(self) -> None:
        tools = build_dynamic_http_tools(
            {
                "tools": [
                    {
                        "name": "record_caller_details",
                        "description": "Internal post-call caller intake marker.",
                        "method": "POST",
                        "url": "http://conversation-service:8091/v1/tools/caller-contacts",
                    },
                    {
                        "name": "lookup_case",
                        "description": "Look up a case when the caller asks for one.",
                        "method": "POST",
                        "url": "https://vendor.example.com/cases",
                    },
                ]
            },
            excluded_tool_names={"record_caller_details"},
        )

        self.assertEqual([tool.info.name for tool in tools], ["lookup_case"])

    async def test_runtime_service_auth_is_not_sent_to_external_tools(self) -> None:
        tools = build_dynamic_http_tools(
            {
                "tools": [
                    {
                        "name": "external_lookup",
                        "description": "Look up information from an approved external vendor endpoint.",
                        "method": "POST",
                        "url": "https://vendor.example.com/lookup",
                    }
                ]
            }
        )

        with (
            patch.dict(
                os.environ,
                {
                    "CONVERSATION_API_BASE_URL": "http://conversation-service:8091",
                    "CONVERSATION_SERVICE_TOKEN": "must-not-leak",
                },
            ),
            patch("agent.dynamic_tools.httpx.AsyncClient") as client_cls,
        ):
            client = AsyncMock()
            client.request.return_value = httpx.Response(
                200,
                json={"status": "success"},
                headers={"content-type": "application/json"},
            )
            client_cls.return_value.__aenter__.return_value = client
            client_cls.return_value.__aexit__.return_value = False

            await tools[0](
                ctx=self._run_context(["external_lookup"]),
                raw_arguments={},
            )

        headers = client.request.await_args.kwargs["headers"]
        self.assertNotIn("X-Service-Token", headers)

    async def test_disabled_dynamic_tool_returns_failed_result(self) -> None:
        tools = build_dynamic_http_tools(
            {
                "tools": [
                    {
                        "name": "lookup_inventory",
                        "description": "Check live inventory and prices whenever a caller asks what items are available.",
                        "method": "GET",
                        "url": "https://vendor.example.com/inventory",
                    }
                ]
            }
        )

        result = await tools[0](
            ctx=self._run_context([]),
            raw_arguments={},
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["message"], "I can't use that tool from this agent right now."
        )
