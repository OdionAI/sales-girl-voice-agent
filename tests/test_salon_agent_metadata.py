import unittest
from types import SimpleNamespace

from agent.salon_agent import _tool_metadata


class SalonAgentMetadataTests(unittest.TestCase):
    def test_tool_metadata_uses_session_userdata_room_name(self) -> None:
        ctx = SimpleNamespace(
            userdata={
                "room_name": "voice_assistant_room_test_123",
                "conversation_id": "conversation-1",
                "session_id": "session-1",
                "agent_config_id": "agent-1",
                "business_id": "business-1",
                "enabled_tool_names": ["transfer_to_aicc"],
            }
        )

        metadata = _tool_metadata(ctx)

        self.assertEqual(metadata["room_name"], "voice_assistant_room_test_123")
        self.assertEqual(metadata["conversation_id"], "conversation-1")
        self.assertEqual(metadata["session_id"], "session-1")
        self.assertEqual(metadata["agent_id"], "agent-1")


if __name__ == "__main__":
    unittest.main()
