from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from agent import conversation_service_api


class ConversationServiceApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_finalize_caller_record_posts_derived_fields(self) -> None:
        request = AsyncMock(return_value={"status": "success"})
        with patch.object(conversation_service_api, "_request_json", request):
            result = await conversation_service_api.finalize_caller_record(
                session_ref="sid-123",
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
        self.assertEqual(args, ("POST", "/v1/tools/caller-records/finalize"))
        self.assertEqual(kwargs["json"]["session_ref"], "sid-123")
        self.assertEqual(kwargs["json"]["sub_theme"], "Passeport")
        self.assertFalse(kwargs["json"]["transferred_to_human"])


if __name__ == "__main__":
    unittest.main()
