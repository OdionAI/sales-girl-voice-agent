from __future__ import annotations

import unittest
from unittest.mock import patch

from agent import conversation_analysis


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"summary":"The customer reported a failed transfer.","primary_intent":"transaction_issue","intent_confidence":0.93,"sentiment":"frustrated","resolution_status":"escalated"}'
                    }
                }
            ]
        }


class _Client:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, *_args, **_kwargs):
        return _Response()


class ConversationAnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def test_analysis_returns_validated_structured_fields(self):
        with patch.object(conversation_analysis, "ANALYSIS_API_KEY", "test-key"), patch.object(
            conversation_analysis.httpx, "AsyncClient", return_value=_Client()
        ):
            result = await conversation_analysis.analyze_messages(
                [
                    {"role": "user", "content": "My transfer failed."},
                    {"role": "assistant", "content": "I will escalate this for review."},
                ]
            )

        self.assertEqual(result["analysis_status"], "ready")
        self.assertEqual(result["primary_intent"], "transaction_issue")
        self.assertEqual(result["intent_confidence"], 0.93)

    def test_analysis_is_disabled_by_default_without_flag_and_key(self):
        with patch.object(conversation_analysis, "ANALYSIS_API_KEY", ""):
            self.assertFalse(conversation_analysis.is_enabled())
