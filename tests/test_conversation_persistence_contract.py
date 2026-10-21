from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from agent import conversation_service_api as service


class ConversationPersistenceContractTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, payload, status_code=200):
        response = Mock(status_code=status_code)
        response.json.return_value = payload
        client = AsyncMock()
        client.request.return_value = response
        with (
            patch.object(service, "is_enabled", return_value=True),
            patch.object(service, "BASE_URL", "https://conversation.example"),
            patch.object(service.httpx, "AsyncClient") as factory,
        ):
            factory.return_value.__aenter__.return_value = client
            result = await service._request_json("POST", "/v1/test")
        client.request.assert_awaited_once()
        return result

    async def test_domain_statuses_are_preserved_by_successful_writes(self):
        for status in ("success", "active", "ended", "ready", "available"):
            with self.subTest(status=status):
                result = await self.request({"status": status})
                self.assertEqual(result["status"], status)

    async def test_http_failure_overrides_domain_status(self):
        result = await self.request({"status": "active", "detail": "unavailable"}, 503)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["http_status"], 503)

    async def test_missing_status_is_normalized_but_invalid_payload_fails(self):
        self.assertEqual((await self.request({}))["status"], "success")
        self.assertEqual((await self.request(None))["status"], "failed")

    async def test_disabled_service_does_not_attempt_a_write(self):
        with patch.object(service, "is_enabled", return_value=False), patch.object(service.httpx, "AsyncClient") as client:
            self.assertEqual(await service._request_json("POST", "/v1/test"), {"status": "disabled"})
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
