import asyncio
import json
import unittest

from latency_lab.config import LabConfig
from latency_lab.providers import RLLMClient


class FakeTrace:
    def __init__(self):
        self.events = []

    def record(self, event, **data):
        self.events.append((event, data))


class CapturingSession:
    """Records the JSON payload of each request; streams one text block back."""

    def __init__(self):
        self.payloads = []

    def post(self, _url, *, json, **_kwargs):
        self.payloads.append(json)
        return CapturingResponse()


class CapturingResponse:
    status = 200

    def __init__(self):
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def raise_for_status(self):
        pass

    async def iter_any(self):
        yield b'data: {"choices": [{"delta": {"content": "Fine."}}]}\n\n'
        yield b"data: [DONE]\n\n"


SYSTEM = {"role": "system", "content": "You are Amaka."}
MID_SYSTEM = {"role": "system", "content": "Give the result directly."}
TOOL_REPLY = {"role": "tool", "tool_call_id": "c1", "name": "lookup_provider_directory",
              "content": json.dumps({"status": "ok"})}


class NormalizeMessagesTest(unittest.TestCase):
    def test_no_change_when_system_is_only_leading(self):
        messages = [SYSTEM, {"role": "user", "content": "hi"}]
        self.assertIs(RLLMClient._normalize_messages(messages), messages)

    def test_mid_conversation_system_is_folded_into_the_leading_one(self):
        messages = [SYSTEM, {"role": "user", "content": "hi"}, TOOL_REPLY, MID_SYSTEM]
        normalized = RLLMClient._normalize_messages(messages)
        self.assertEqual([m["role"] for m in normalized],
                         ["system", "user", "tool"])
        self.assertIn("You are Amaka.", normalized[0]["content"])
        self.assertIn("Give the result directly.", normalized[0]["content"])
        self.assertEqual(normalized[2], TOOL_REPLY)

    def test_missing_leading_system_still_ends_with_one_system_first(self):
        messages = [{"role": "user", "content": "hi"}, TOOL_REPLY, MID_SYSTEM]
        normalized = RLLMClient._normalize_messages(messages)
        self.assertEqual(normalized[0]["role"], "system")
        self.assertEqual([m["role"] for m in normalized[1:]], ["user", "tool"])


class StreamPayloadTest(unittest.IsolatedAsyncioTestCase):
    async def test_request_never_carries_a_system_message_past_index_zero(self):
        session = CapturingSession()
        client = RLLMClient(LabConfig(), session)
        client.trace = FakeTrace()

        messages = [SYSTEM, {"role": "user", "content": "which hospitals?"},
                    {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "c1", "type": "function",
                         "function": {"name": "lookup_provider_directory", "arguments": "{}"}}]},
                    TOOL_REPLY, MID_SYSTEM]
        [event async for event in client.stream(messages)]

        sent = session.payloads[0]["messages"]
        roles = [m["role"] for m in sent]
        self.assertEqual(roles.count("system"), 1)
        self.assertEqual(roles[0], "system")
        self.assertLessEqual(roles.index("system"), 0)
        self.assertNotIn(MID_SYSTEM["content"], [m.get("content") for m in sent[1:]])


if __name__ == "__main__":
    unittest.main()
