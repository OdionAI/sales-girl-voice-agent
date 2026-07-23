import inspect
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from livekit.agents import Agent, llm
from livekit.agents.llm.tool_context import find_function_tools

from agent.dynamic_tools import _normalize_schema
from agent.salon_agent import (
    SalonAgent,
    parse_textual_function_call,
    select_enabled_runtime_tools,
)
from agent.tool_schema_compat import (
    CREATE_BOOKING_RAW_SCHEMA,
    CREATE_ORDER_RAW_SCHEMA,
    SEARCH_BUSINESS_KNOWLEDGE_RAW_SCHEMA,
    TRANSFER_TO_AICC_RAW_SCHEMA,
    _object_has_additional_properties_false,
    normalize_order_items,
    normalize_price_snapshot,
    normalize_top_k,
    strictify_schema_for_groq,
)


class ToolSchemaCompatTests(unittest.TestCase):
    def test_create_booking_schema_is_groq_strict(self) -> None:
        self.assertTrue(
            _object_has_additional_properties_false(
                CREATE_BOOKING_RAW_SCHEMA["parameters"]
            )
        )

    def test_create_order_schema_is_groq_strict(self) -> None:
        self.assertTrue(
            _object_has_additional_properties_false(
                CREATE_ORDER_RAW_SCHEMA["parameters"]
            )
        )

    def test_search_business_knowledge_schema_allows_string_top_k(self) -> None:
        self.assertTrue(
            _object_has_additional_properties_false(
                SEARCH_BUSINESS_KNOWLEDGE_RAW_SCHEMA["parameters"]
            )
        )
        top_k_schema = SEARCH_BUSINESS_KNOWLEDGE_RAW_SCHEMA["parameters"]["properties"]["top_k"]
        self.assertIn({"type": "string"}, top_k_schema["anyOf"])

    def test_transfer_to_aicc_schema_is_groq_strict(self) -> None:
        self.assertTrue(
            _object_has_additional_properties_false(
                TRANSFER_TO_AICC_RAW_SCHEMA["parameters"]
            )
        )

    def test_strictify_schema_for_groq_fixes_anyof_objects(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "price_snapshot": {
                    "anyOf": [
                        {"type": "object", "properties": {"amount": {"type": "number"}}},
                        {"type": "string"},
                        {"type": "null"},
                    ]
                }
            },
        }
        strict = strictify_schema_for_groq(schema)
        self.assertTrue(_object_has_additional_properties_false(strict))

    def test_normalize_price_snapshot_accepts_dict_and_json_string(self) -> None:
        self.assertEqual(
            normalize_price_snapshot({"amount": 2500, "currency": "NGN"}),
            {"amount": 2500, "currency": "NGN"},
        )
        self.assertEqual(
            normalize_price_snapshot('{"amount": 2500, "currency": "NGN"}'),
            {"amount": 2500, "currency": "NGN"},
        )

    def test_normalize_order_items_normalizes_nested_price_snapshot(self) -> None:
        items = normalize_order_items(
            [
                {
                    "item_name": "Rice",
                    "quantity": 1,
                    "price_snapshot": '{"amount": 2500, "currency": "NGN"}',
                }
            ]
        )
        self.assertEqual(items[0]["price_snapshot"], {"amount": 2500, "currency": "NGN"})

    def test_normalize_top_k_accepts_string_and_bounds_value(self) -> None:
        self.assertEqual(normalize_top_k("4"), 4)
        self.assertEqual(normalize_top_k("99"), 6)
        self.assertEqual(normalize_top_k("not-a-number"), 4)

    def test_dynamic_schema_uses_strict_mode_for_groq(self) -> None:
        with patch.dict(os.environ, {"LLM_PROVIDER": "groq"}, clear=False):
            schema = _normalize_schema(
                {
                    "type": "object",
                    "properties": {
                        "payload": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}},
                        }
                    },
                }
            )
        self.assertTrue(_object_has_additional_properties_false(schema))

    def test_salon_agent_booking_and_order_tools_use_raw_schema(self) -> None:
        tools = find_function_tools(SalonAgent)
        by_name = {tool.info.name: tool for tool in tools}
        search_schema = by_name["search_business_knowledge"].info.raw_schema
        booking_schema = by_name["create_booking"].info.raw_schema
        order_schema = by_name["create_order"].info.raw_schema
        transfer_schema = by_name["transfer_to_aicc"].info.raw_schema
        self.assertTrue(
            _object_has_additional_properties_false(search_schema["parameters"])
        )
        self.assertTrue(
            _object_has_additional_properties_false(booking_schema["parameters"])
        )
        self.assertTrue(
            _object_has_additional_properties_false(order_schema["parameters"])
        )
        self.assertTrue(
            _object_has_additional_properties_false(transfer_schema["parameters"])
        )
        for name in (
            "search_business_knowledge",
            "create_booking",
            "create_order",
            "transfer_to_aicc",
        ):
            self.assertIn("raw_arguments", inspect.signature(by_name[name]).parameters)

    def test_runtime_tool_selection_exposes_only_enabled_tools_and_knowledge(self) -> None:
        tools = find_function_tools(SalonAgent)
        selected = select_enabled_runtime_tools(tools, ["create_ticket"])
        self.assertEqual(
            {tool.info.name for tool in selected},
            {"create_ticket", "search_business_knowledge"},
        )

    def test_textual_function_call_is_recovered_only_for_allowed_tool(self) -> None:
        text = '<function>create_ticket{"title":"Passeport","description":"Suivi"}</function>'
        recovered = parse_textual_function_call(
            text, allowed_tool_names={"create_ticket"}
        )
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered[0], "create_ticket")
        self.assertEqual(
            recovered[1], '{"title":"Passeport","description":"Suivi"}'
        )
        self.assertIsNone(
            parse_textual_function_call(text, allowed_tool_names={"send_email"})
        )

    def test_textual_function_call_rejects_invalid_json(self) -> None:
        self.assertIsNone(
            parse_textual_function_call(
                "<function>create_ticket{not-json}</function>",
                allowed_tool_names={"create_ticket"},
            )
        )


class TextualToolCallStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_knowledge_raw_tool_forwards_top_level_arguments(self) -> None:
        agent = SalonAgent(instructions="test")
        tool = next(
            tool
            for tool in agent.tools
            if tool.info.name == "search_business_knowledge"
        )
        ctx = SimpleNamespace(
            userdata={
                "enabled_tool_names": ["search_business_knowledge"],
                "business_id": "business-123",
                "end_user_id": "caller@example.com",
            },
            room=SimpleNamespace(name="room-123"),
        )

        with patch(
            "agent.salon_agent.search_business_knowledge_api",
            AsyncMock(return_value={"status": "success", "matches": []}),
        ) as search_api:
            result = await tool(
                ctx=ctx,
                raw_arguments={"query": "passeport expiré", "top_k": "4"},
            )

        self.assertEqual(result["status"], "success")
        search_api.assert_awaited_once()
        self.assertEqual(search_api.await_args.kwargs["query"], "passeport expiré")
        self.assertEqual(search_api.await_args.kwargs["top_k"], 4)

    async def test_booking_and_order_raw_tools_forward_top_level_arguments(self) -> None:
        agent = SalonAgent(instructions="test")
        tools = {tool.info.name: tool for tool in agent.tools}
        ctx = SimpleNamespace(
            userdata={
                "enabled_tool_names": ["create_booking", "create_order"],
                "business_id": "business-123",
                "end_user_id": "caller@example.com",
            },
            room=SimpleNamespace(name="room-123"),
        )

        with (
            patch(
                "agent.salon_agent.create_booking_api",
                AsyncMock(return_value={"status": "success", "id": "booking-1"}),
            ) as booking_api,
            patch(
                "agent.salon_agent.create_order_api",
                AsyncMock(return_value={"status": "success", "id": "order-1"}),
            ) as order_api,
        ):
            booking_result = await tools["create_booking"](
                ctx=ctx,
                raw_arguments={
                    "room_type": "Suite",
                    "check_in_date": "2026-08-01",
                    "check_out_date": "2026-08-03",
                    "guest_count": 2,
                },
            )
            order_result = await tools["create_order"](
                ctx=ctx,
                raw_arguments={
                    "items": [{"item_name": "Rice", "quantity": 2}],
                    "customer_name": "Ada",
                },
            )

        self.assertEqual(booking_result["status"], "success")
        self.assertEqual(booking_api.await_args.kwargs["guest_count"], 2)
        self.assertEqual(booking_api.await_args.kwargs["room_type"], "Suite")
        self.assertEqual(order_result["status"], "success")
        self.assertEqual(
            order_api.await_args.kwargs["items"],
            [{"item_name": "Rice", "quantity": 2}],
        )
        self.assertEqual(order_api.await_args.kwargs["customer_name"], "Ada")

    async def test_llm_node_converts_split_text_markup_before_tts(self) -> None:
        async def textual_stream():
            yield llm.ChatChunk(
                id="chunk-1",
                delta=llm.ChoiceDelta(role="assistant", content="<funct"),
            )
            yield llm.ChatChunk(
                id="chunk-1",
                delta=llm.ChoiceDelta(
                    content='ion>create_ticket{"title":"Passeport","description":"Suivi"}</function>'
                ),
            )

        agent = SalonAgent(instructions="test")
        tools = [
            tool
            for tool in agent.tools
            if tool.info.name == "create_ticket"
        ]
        with patch.object(Agent.default, "llm_node", return_value=textual_stream()):
            output = [
                chunk
                async for chunk in agent.llm_node(
                    llm.ChatContext.empty(), tools, model_settings=None
                )
            ]

        self.assertEqual(len(output), 1)
        self.assertEqual(output[0].delta.content, None)
        self.assertEqual(len(output[0].delta.tool_calls), 1)
        self.assertEqual(output[0].delta.tool_calls[0].name, "create_ticket")


if __name__ == "__main__":
    unittest.main()
