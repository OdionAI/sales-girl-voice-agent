from __future__ import annotations

import os
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from livekit.agents.llm.async_toolset import AsyncToolset

from agent.dynamic_tools import _saww_progress_messages, build_dynamic_http_tools


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

    def test_saww_progress_is_limited_to_mutating_prepare_and_transaction_tools(self) -> None:
        saww = {"name": "SAWW"}
        self.assertIsNotNone(
            _saww_progress_messages(
                saww,
                {"name": "prepare_airtime", "method": "POST"},
            )
        )
        self.assertIsNotNone(
            _saww_progress_messages(
                saww,
                {"name": "airtime_transaction", "method": "POST"},
            )
        )
        self.assertIsNone(
            _saww_progress_messages(
                {"name": "SAW"},
                {"name": "prepare_airtime", "method": "POST"},
            )
        )
        self.assertIsNone(
            _saww_progress_messages(
                saww,
                {"name": "transaction_history", "method": "GET"},
            )
        )
        self.assertIsNone(
            _saww_progress_messages(
                saww,
                {"name": "lookup_customer", "method": "POST"},
            )
        )

    async def test_saww_prepare_tool_announces_async_progress(self) -> None:
        tools = build_dynamic_http_tools(
            {
                "name": "SAWW",
                "tools": [
                    {
                        "name": "prepare_airtime",
                        "description": "Prepare an airtime purchase.",
                        "method": "POST",
                        "url": "https://vendor.example.com/prepare-airtime",
                    }
                ],
            }
        )
        ctx = self._run_context(["prepare_airtime"])
        ctx.update = AsyncMock()
        with patch(
            "agent.dynamic_tools.invoke_dynamic_http_tool",
            new=AsyncMock(return_value={"status": "success", "prepared": True}),
        ) as invoke:
            result = await tools[0](ctx=ctx, raw_arguments={})

        self.assertEqual(result["status"], "success")
        ctx.update.assert_awaited_once_with("One moment, I'm preparing that request now.")
        self.assertTrue(tools[0]._saww_async_progress)
        async_toolset = AsyncToolset(
            id="test-saww-progress", tools=tools, on_duplicate_call="reject"
        )
        self.assertEqual(async_toolset._on_duplicate_call, "reject")
        self.assertGreaterEqual(len(async_toolset._tools), 1)
        invoke.assert_awaited_once()

    async def test_same_prepare_tool_on_saw_remains_non_progress_tool(self) -> None:
        tools = build_dynamic_http_tools(
            {
                "name": "SAW",
                "tools": [
                    {
                        "name": "prepare_airtime",
                        "description": "Prepare an airtime purchase.",
                        "method": "POST",
                        "url": "https://vendor.example.com/prepare-airtime",
                    }
                ],
            }
        )
        ctx = self._run_context(["prepare_airtime"])
        with patch(
            "agent.dynamic_tools.invoke_dynamic_http_tool",
            new=AsyncMock(return_value={"status": "success"}),
        ) as invoke:
            result = await tools[0](ctx=ctx, raw_arguments={})

        self.assertEqual(result["status"], "success")
        invoke.assert_awaited_once()

    async def test_saww_profile_fields_are_injected_and_hidden_from_tool_schema(self) -> None:
        profile = {
            "customer_id": "demo-customer-01",
            "phone_number": "08012345678",
            "account_number": "1234567890",
        }
        tools = build_dynamic_http_tools(
            {
                "name": "SAWW",
                "tools": [
                    {
                        "name": "prepare_airtime",
                        "description": "Prepare an airtime transaction.",
                        "method": "POST",
                        "url": "https://vendor.example.com/prepare-airtime",
                        "request_schema": {
                            "type": "object",
                            "properties": {
                                "customerId": {"type": "string"},
                                "phoneNumber": {"type": "string"},
                                "accountNo": {"type": "string"},
                                "amount": {"type": "integer"},
                            },
                            "required": [
                                "customerId",
                                "phoneNumber",
                                "accountNo",
                                "amount",
                            ],
                        },
                    }
                ],
            },
            runtime_context={"wema_context": profile},
        )
        ctx = self._run_context(["prepare_airtime"])
        ctx.update = AsyncMock()
        with patch(
            "agent.dynamic_tools.invoke_dynamic_http_tool",
            new=AsyncMock(return_value={"status": "success"}),
        ) as invoke:
            await tools[0](ctx=ctx, raw_arguments={"amount": 500})

        forwarded = invoke.await_args.kwargs["raw_arguments"]
        self.assertEqual(forwarded["customerId"], profile["customer_id"])
        self.assertEqual(forwarded["phoneNumber"], profile["phone_number"])
        self.assertEqual(forwarded["accountNo"], profile["account_number"])
        self.assertEqual(forwarded["amount"], 500)
        parameters = tools[0].info.raw_schema["parameters"]
        self.assertEqual(set(parameters["properties"]), {"amount"})
        self.assertEqual(parameters["required"], ["amount"])

    async def test_saww_serializes_different_mutation_tools_in_one_call(self) -> None:
        tools = build_dynamic_http_tools(
            {
                "name": "SAWW",
                "tools": [
                    {
                        "name": "prepare_airtime",
                        "description": "Prepare an airtime request.",
                        "method": "POST",
                        "url": "https://vendor.example.com/prepare",
                    },
                    {
                        "name": "airtime_transaction",
                        "description": "Submit an airtime transaction.",
                        "method": "POST",
                        "url": "https://vendor.example.com/transaction",
                    },
                ],
            }
        )
        ctx = self._run_context(["prepare_airtime", "airtime_transaction"])
        ctx.update = AsyncMock()
        started = asyncio.Event()
        finish = asyncio.Event()

        async def invoke(**_kwargs):
            started.set()
            await finish.wait()
            return {"status": "success"}

        self.assertTrue(tools[0]._saww_async_progress)
        self.assertTrue(tools[1]._saww_async_progress)
        call = AsyncMock(side_effect=invoke)
        with patch("agent.dynamic_tools.invoke_dynamic_http_tool", new=call):
            first = asyncio.create_task(
                tools[0](ctx=ctx, raw_arguments={})
            )
            await started.wait()
            second = await tools[1](ctx=ctx, raw_arguments={})
            finish.set()
            await first

        self.assertEqual(second["status"], "failed")
        self.assertIn("Another request is still being processed", second["message"])
        call.assert_called_once()

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
