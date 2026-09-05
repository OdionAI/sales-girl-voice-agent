from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from agent.auth_observer import ACTION_WEMA_EXECUTE_PREPARED
from agent.dynamic_tools import build_dynamic_http_tools


class DynamicHttpToolsTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _verified_observer() -> SimpleNamespace:
        return SimpleNamespace(
            authorize_action=AsyncMock(
                return_value={
                    "authorized": True,
                    "session_status": "verified",
                    "action_status": "verified",
                }
            ),
            publish_action_outcome=AsyncMock(),
        )

    @staticmethod
    def _execute_prepared_config(
        *, include_auth_fields: bool = False
    ) -> dict[str, object]:
        properties: dict[str, object] = {
            "operation_id": {"type": "string"},
        }
        required = ["operation_id"]
        if include_auth_fields:
            properties.update(
                {
                    "authenticated": {"type": "boolean"},
                    "voice_auth_authorized": {"type": "boolean"},
                }
            )
            required.extend(["authenticated", "voice_auth_authorized"])
        return {
            "tools": [
                {
                    "name": ACTION_WEMA_EXECUTE_PREPARED,
                    "description": "Execute the caller's latest prepared Wema transaction.",
                    "method": "POST",
                    "url": "https://wema.example.com/execute",
                    "request_schema": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False,
                    },
                }
            ]
        }

    def _run_context(
        self, enabled_tool_names: list[str], **session_values: object
    ) -> SimpleNamespace:
        userdata = {
            "enabled_tool_names": enabled_tool_names,
            "client_id": "sales-girl-internal",
            "agent_config_id": "agent-123",
            "business_id": "biz-123",
            "conversation_id": "conv-123",
            "session_id": "sess-123",
            "end_user_id": "caller@example.com",
        }
        userdata.update(session_values)
        return SimpleNamespace(
            session=SimpleNamespace(userdata=userdata),
            room=SimpleNamespace(name="room-123"),
            with_filler=Mock(side_effect=lambda *args, **kwargs: nullcontext()),
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
                ctx=self._run_context(
                    ["lookup_inventory"],
                    wema_customer_id="R008448055",
                    wema_account_number="0125679408",
                    wema_phone_number="08161540638",
                ),
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
        self.assertNotIn("X-Wema-Customer-Id", kwargs["headers"])

    async def test_wema_tool_uses_session_defaults_and_reports_effective_request(
        self,
    ) -> None:
        activity: list[dict] = []
        tools = build_dynamic_http_tools(
            {
                "tools": [
                    {
                        "name": "wema_prepare_data_purchase",
                        "description": "Prepare a data purchase for the caller.",
                        "method": "POST",
                        "url": "https://wema.example.com/data",
                        "request_schema": {
                            "type": "object",
                            "properties": {
                                "network": {"type": "string"},
                                "package_id": {"type": "integer"},
                                "phone_number": {"type": "string"},
                                "source_account": {"type": "string"},
                            },
                            "required": ["network", "package_id", "phone_number"],
                            "additionalProperties": False,
                        },
                    }
                ]
            },
            on_tool_activity=activity.append,
        )
        context = self._run_context(
            ["wema_prepare_data_purchase"],
            auth_observer=self._verified_observer(),
            wema_customer_id="R008448055",
            wema_account_number="0125679408",
            wema_phone_number="08161540638",
        )
        arguments = {
            "network": "MTN",
            "package_id": 4,
        }
        effective_arguments = {
            **arguments,
            "source_account": "0125679408",
            "phone_number": "08161540638",
        }

        with patch("agent.dynamic_tools.httpx.AsyncClient") as client_cls:
            client = AsyncMock()
            client.request.return_value = httpx.Response(
                200,
                json={"status": "prepared", "data": {"operation_id": "op-1"}},
                headers={"content-type": "application/json"},
            )
            client_cls.return_value.__aenter__.return_value = client
            client_cls.return_value.__aexit__.return_value = False

            result = await tools[0](
                ctx=context,
                raw_arguments=arguments,
            )

        self.assertEqual(result["status"], "prepared")
        request = client.request.await_args.kwargs
        self.assertEqual(request["json"], effective_arguments)
        self.assertEqual(request["headers"]["X-Wema-Customer-Id"], "R008448055")
        self.assertEqual(len(activity), 2)
        started, completed = activity
        self.assertEqual(started["event"], "started")
        self.assertEqual(completed["event"], "completed")
        self.assertEqual(started["call_id"], completed["call_id"])
        self.assertEqual(started["arguments"], effective_arguments)
        self.assertEqual(completed["arguments"], effective_arguments)
        self.assertEqual(completed["result"]["status"], "prepared")

    async def test_wema_balance_uses_selected_account_and_customer_header(self) -> None:
        tools = build_dynamic_http_tools(
            {
                "tools": [
                    {
                        "name": "wema_get_balance",
                        "description": "Check the caller's Wema balance.",
                        "method": "POST",
                        "url": "https://wema.example.com/balance",
                        "request_schema": {
                            "type": "object",
                            "properties": {
                                "source_account": {"type": ["string", "null"]},
                            },
                            "required": [],
                            "additionalProperties": False,
                        },
                    }
                ]
            }
        )
        context = self._run_context(
            ["wema_get_balance"],
            auth_observer=self._verified_observer(),
            wema_customer_id="R008448055",
            wema_account_number="0125679408",
        )

        with patch("agent.dynamic_tools.httpx.AsyncClient") as client_cls:
            client = AsyncMock()
            client.request.return_value = httpx.Response(
                200,
                json={"status": "ok", "data": {"balance": "12500.00"}},
                headers={"content-type": "application/json"},
            )
            client_cls.return_value.__aenter__.return_value = client
            client_cls.return_value.__aexit__.return_value = False

            result = await tools[0](ctx=context, raw_arguments={})

        self.assertEqual(result["status"], "ok")
        request = client.request.await_args.kwargs
        self.assertEqual(request["json"], {"source_account": "0125679408"})
        self.assertEqual(request["headers"]["X-Wema-Customer-Id"], "R008448055")

    async def test_wema_explicit_account_and_phone_override_session_defaults(self) -> None:
        tools = build_dynamic_http_tools(
            {
                "tools": [
                    {
                        "name": "wema_prepare_data_purchase",
                        "description": "Prepare a data purchase.",
                        "method": "POST",
                        "url": "https://wema.example.com/data",
                        "request_schema": {
                            "type": "object",
                            "properties": {
                                "network": {"type": "string"},
                                "package_id": {"type": "integer"},
                                "phone_number": {"type": "string"},
                                "source_account": {"type": ["string", "null"]},
                            },
                            "required": ["network", "package_id", "phone_number"],
                            "additionalProperties": False,
                        },
                    }
                ]
            }
        )
        context = self._run_context(
            ["wema_prepare_data_purchase"],
            auth_observer=self._verified_observer(),
            wema_customer_id="R008448055",
            wema_account_number="0125679408",
            wema_phone_number="08161540638",
        )
        arguments = {
            "network": "MTN",
            "package_id": 4,
            "phone_number": "08030000000",
            "source_account": "0281250121",
        }

        with patch("agent.dynamic_tools.httpx.AsyncClient") as client_cls:
            client = AsyncMock()
            client.request.return_value = httpx.Response(
                200,
                json={"status": "prepared"},
                headers={"content-type": "application/json"},
            )
            client_cls.return_value.__aenter__.return_value = client
            client_cls.return_value.__aexit__.return_value = False

            await tools[0](ctx=context, raw_arguments=arguments)

        self.assertEqual(client.request.await_args.kwargs["json"], arguments)

    async def test_all_wema_tools_block_when_auth_observer_is_missing(self) -> None:
        tool_names = [
            "wema_get_balance",
            "wema_get_transactions",
            "wema_list_data_plans",
            "wema_list_transfer_banks",
            "wema_prepare_data_purchase",
            "wema_prepare_transfer",
            ACTION_WEMA_EXECUTE_PREPARED,
        ]

        for tool_name in tool_names:
            with self.subTest(tool_name=tool_name):
                tools = build_dynamic_http_tools(
                    {
                        "tools": [
                            {
                                "name": tool_name,
                                "description": "Run a Wema banking request.",
                                "method": "POST",
                                "url": f"https://wema.example.com/{tool_name}",
                                "request_schema": {
                                    "type": "object",
                                    "properties": {},
                                    "required": [],
                                    "additionalProperties": False,
                                },
                            }
                        ]
                    }
                )
                context = self._run_context([tool_name])

                with patch("agent.dynamic_tools.httpx.AsyncClient") as client_cls:
                    result = await tools[0](ctx=context, raw_arguments={})

                self.assertEqual(result["status"], "failed")
                self.assertTrue(result["auth_required"])
                self.assertEqual(result["reason"], "voice_not_recognized")
                client_cls.assert_not_called()

    async def test_balance_and_history_call_http_only_after_voice_auth(self) -> None:
        for tool_name in ["wema_get_balance", "wema_get_transactions"]:
            with self.subTest(tool_name=tool_name):
                observer = self._verified_observer()
                tools = build_dynamic_http_tools(
                    {
                        "tools": [
                            {
                                "name": tool_name,
                                "description": "Read protected Wema account information.",
                                "method": "POST",
                                "url": f"https://wema.example.com/{tool_name}",
                                "request_schema": {
                                    "type": "object",
                                    "properties": {},
                                    "required": [],
                                    "additionalProperties": False,
                                },
                            }
                        ]
                    }
                )
                context = self._run_context(
                    [tool_name],
                    auth_observer=observer,
                    last_user_transcript="Show me my account information.",
                )

                with patch("agent.dynamic_tools.httpx.AsyncClient") as client_cls:
                    client = AsyncMock()
                    client.request.return_value = httpx.Response(
                        200,
                        json={"status": "ok", "data": {}},
                        headers={"content-type": "application/json"},
                    )
                    client_cls.return_value.__aenter__.return_value = client
                    client_cls.return_value.__aexit__.return_value = False

                    result = await tools[0](ctx=context, raw_arguments={})

                self.assertEqual(result["status"], "ok")
                observer.authorize_action.assert_awaited_once_with(
                    action=tool_name,
                    transcript="Show me my account information.",
                    details={},
                )
                observer.publish_action_outcome.assert_not_awaited()
                client.request.assert_awaited_once()

    async def test_execute_prepared_hides_auth_fields_from_model_schema(self) -> None:
        tools = build_dynamic_http_tools(
            self._execute_prepared_config(include_auth_fields=True)
        )

        parameters = tools[0].info.raw_schema["parameters"]
        self.assertEqual(
            parameters["properties"],
            {"operation_id": {"type": "string"}},
        )
        self.assertEqual(parameters["required"], ["operation_id"])

    async def test_execute_prepared_blocks_when_auth_observer_is_missing(self) -> None:
        tools = build_dynamic_http_tools(self._execute_prepared_config())
        context = self._run_context(
            [ACTION_WEMA_EXECUTE_PREPARED],
            last_user_transcript="Yes, send it.",
        )

        with patch("agent.dynamic_tools.httpx.AsyncClient") as client_cls:
            result = await tools[0](
                ctx=context,
                raw_arguments={"operation_id": "op-1"},
            )

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["auth_required"])
        self.assertEqual(result["reason"], "voice_not_recognized")
        client_cls.assert_not_called()

    async def test_execute_prepared_requires_exact_boolean_auth_decision(self) -> None:
        observer = SimpleNamespace(
            authorize_action=AsyncMock(
                return_value={
                    "authorized": "true",
                    "session_status": "recognized",
                    "action_status": "recognized",
                }
            ),
            publish_action_outcome=AsyncMock(),
        )
        tools = build_dynamic_http_tools(self._execute_prepared_config())
        context = self._run_context(
            [ACTION_WEMA_EXECUTE_PREPARED],
            auth_observer=observer,
            last_user_transcript="Yes, send it.",
        )

        with patch("agent.dynamic_tools.httpx.AsyncClient") as client_cls:
            result = await tools[0](
                ctx=context,
                raw_arguments={"operation_id": "op-1"},
            )

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["auth_required"])
        observer.authorize_action.assert_awaited_once()
        observer.publish_action_outcome.assert_not_awaited()
        client_cls.assert_not_called()

    async def test_execute_prepared_calls_endpoint_only_after_voice_auth(self) -> None:
        observer = SimpleNamespace(
            authorize_action=AsyncMock(
                return_value={
                    "authorized": True,
                    "session_status": "recognized",
                    "action_status": "authorized",
                }
            ),
            publish_action_outcome=AsyncMock(),
        )
        tools = build_dynamic_http_tools(self._execute_prepared_config())
        context = self._run_context(
            [ACTION_WEMA_EXECUTE_PREPARED],
            auth_observer=observer,
            last_user_transcript="Yes, send it.",
        )

        with patch("agent.dynamic_tools.httpx.AsyncClient") as client_cls:
            client = AsyncMock()
            client.request.return_value = httpx.Response(
                200,
                json={"status": "completed", "data": {"reference": "ref-1"}},
                headers={"content-type": "application/json"},
            )
            client_cls.return_value.__aenter__.return_value = client
            client_cls.return_value.__aexit__.return_value = False

            result = await tools[0](
                ctx=context,
                raw_arguments={"operation_id": "op-1"},
            )

        self.assertEqual(result["status"], "completed")
        observer.authorize_action.assert_awaited_once_with(
            action=ACTION_WEMA_EXECUTE_PREPARED,
            transcript="Yes, send it.",
            details={"operation_id": "op-1"},
        )
        observer.publish_action_outcome.assert_awaited_once_with(
            action=ACTION_WEMA_EXECUTE_PREPARED,
            tool_result=result,
            details={"operation_id": "op-1"},
        )
        self.assertEqual(
            client.request.await_args.kwargs["json"],
            {"operation_id": "op-1"},
        )

    async def test_execute_prepared_rejects_model_supplied_auth_fields(self) -> None:
        observer = SimpleNamespace(
            authorize_action=AsyncMock(return_value={"authorized": True}),
            publish_action_outcome=AsyncMock(),
        )
        tools = build_dynamic_http_tools(self._execute_prepared_config())
        context = self._run_context(
            [ACTION_WEMA_EXECUTE_PREPARED],
            auth_observer=observer,
        )

        with patch("agent.dynamic_tools.httpx.AsyncClient") as client_cls:
            result = await tools[0](
                ctx=context,
                raw_arguments={
                    "operation_id": "op-1",
                    "authenticated": True,
                },
            )

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["auth_required"])
        observer.authorize_action.assert_not_awaited()
        observer.publish_action_outcome.assert_not_awaited()
        client_cls.assert_not_called()

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
