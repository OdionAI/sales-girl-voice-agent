from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from agent import conversation_service_api


class ConversationServiceApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_caller_record_posts_analyzed_record_with_session_headers(self) -> None:
        request = AsyncMock(return_value={"status": "success"})
        with patch.object(conversation_service_api, "_request_json", request):
            result = await conversation_service_api.create_caller_record(
                first_name="Aïcha",
                last_name="Dossou",
                phone_number="+2290197000000",
                email="aicha.dossou@example.com",
                session_ref="sid-123",
                agent_id="agt-123",
                conversation_ref="conv-123",
                end_user_ref="caller@example.com",
                theme="Document de voyage",
                sub_theme="Passeport",
                request_summary="Le requérant demande le suivi de son passeport.",
                treatment="Information et assistance",
                treatment_comment="La procédure ePass a été expliquée.",
                status="Terminé",
                transferred_to_human=False,
                business_id="60528b7f-418d-4e83-bd9b-af2d96b45995",
            )

        self.assertEqual(result["status"], "success")
        request.assert_awaited_once()
        args, kwargs = request.await_args
        self.assertEqual(args, ("POST", "/v1/tools/caller-records"))
        self.assertEqual(kwargs["headers"]["X-Session-Id"], "sid-123")
        self.assertEqual(kwargs["headers"]["X-Agent-Id"], "agt-123")
        self.assertEqual(kwargs["headers"]["X-Conversation-Id"], "conv-123")
        self.assertEqual(kwargs["json"]["first_name"], "Aïcha")
        self.assertEqual(kwargs["json"]["sub_theme"], "Passeport")
        self.assertFalse(kwargs["json"]["transferred_to_human"])


if __name__ == "__main__":
    unittest.main()
