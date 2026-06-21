import os
import unittest
from unittest.mock import patch

from livekit.agents.llm.tool_context import find_function_tools

from agent.dynamic_tools import _normalize_schema
from agent.salon_agent import SalonAgent
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


if __name__ == "__main__":
    unittest.main()
