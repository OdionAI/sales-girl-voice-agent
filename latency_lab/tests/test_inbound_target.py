import unittest
from unittest.mock import AsyncMock, patch

from agent import agent_config_api


class InboundTargetTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_configured_agent_id(self):
        with (
            patch.object(agent_config_api, "is_enabled", return_value=True),
            patch.object(
                agent_config_api,
                "get_inbound_target",
                new=AsyncMock(return_value={"status": "success", "agent_id": "agent-123"}),
            ),
            patch.object(agent_config_api, "_INBOUND_TARGET_CACHE", {}),
        ):
            result = await agent_config_api.resolve_inbound_agent(business_id="biz")
        self.assertEqual(result, "agent-123")

    async def test_unset_returns_none(self):
        with (
            patch.object(agent_config_api, "is_enabled", return_value=True),
            patch.object(
                agent_config_api,
                "get_inbound_target",
                new=AsyncMock(return_value={"status": "unset"}),
            ),
            patch.object(agent_config_api, "_INBOUND_TARGET_CACHE", {}),
        ):
            result = await agent_config_api.resolve_inbound_agent(business_id="biz")
        self.assertIsNone(result)

    async def test_disabled_returns_none_without_lookup(self):
        lookup = AsyncMock()
        with (
            patch.object(agent_config_api, "is_enabled", return_value=False),
            patch.object(agent_config_api, "get_inbound_target", new=lookup),
        ):
            result = await agent_config_api.resolve_inbound_agent(business_id="biz")
        self.assertIsNone(result)
        lookup.assert_not_awaited()

    async def test_failure_keeps_default(self):
        with (
            patch.object(agent_config_api, "is_enabled", return_value=True),
            patch.object(
                agent_config_api,
                "get_inbound_target",
                new=AsyncMock(return_value={"status": "failed"}),
            ),
            patch.object(agent_config_api, "_INBOUND_TARGET_CACHE", {}),
        ):
            result = await agent_config_api.resolve_inbound_agent(business_id="biz")
        self.assertIsNone(result)
