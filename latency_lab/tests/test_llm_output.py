import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from latency_lab.action_tools import ActionToolCall
from latency_lab.config import LabConfig
from latency_lab.providers import RLLMClient
from latency_lab.pipeline import Attempt, ConversationPipeline
from latency_lab.trace import TraceRecorder


TOOL_MARKUP = (
    "<tool_call>\n<function=wema_list_data_plans>\n"
    "<parameter=network>\nMTN\n</parameter>\n</function>\n</tool_call>"
)
TOOLS = [{"type": "function", "function": {
    "name": "wema_list_data_plans", "parameters": {"type": "object"},
}}]


class FakeResponse:
    status = 200

    def __init__(self, deltas, byte_chunks=False):
        self.deltas = deltas
        self.byte_chunks = byte_chunks
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def raise_for_status(self):
        pass

    async def iter_any(self):
        for delta in self.deltas:
            data = ("data: " + json.dumps({"choices": [{"delta": delta}]}, ensure_ascii=False) + "\n\n").encode()
            for chunk in ([data[i:i + 1] for i in range(len(data))] if self.byte_chunks else [data]):
                yield chunk
        yield b"data: [DONE]\n\n"


class FakeSession:
    def __init__(self, deltas, byte_chunks=False):
        self.response = FakeResponse(deltas, byte_chunks)
        self.payload = None

    def post(self, _url, *, json, **_kwargs):
        self.payload = json
        return self.response


class LLMOutputTest(unittest.IsolatedAsyncioTestCase):
    async def output(self, deltas, *, tools=None, byte_chunks=False):
        session = FakeSession(deltas, byte_chunks)
        client = RLLMClient(LabConfig(llm_enable_thinking=False), session)
        events = [event async for event in client.stream([{"role": "user", "content": "MTN plans"}], tools=tools)]
        self.assertFalse(session.payload["chat_template_kwargs"]["enable_thinking"])
        return events

    async def test_reported_tool_markup_never_becomes_spoken_text(self):
        text = "Let me check. " + TOOL_MARKUP + " Please wait."
        for split in range(len(text) + 1):
            with self.subTest(split=split):
                events = await self.output([{"content": text[:split]}, {"content": text[split:]}])
                self.assertEqual("".join(events), "Let me check.  Please wait.")

    async def test_thinking_and_nested_tool_markup_are_not_speech_or_actions(self):
        text = "<think>I should call " + TOOL_MARKUP + " privately.</think>Hello."
        events = await self.output([{"content": c} for c in text], tools=TOOLS)
        self.assertEqual("".join(events), "Hello.")

    async def test_truncated_internal_blocks_and_tag_prefixes_fail_closed(self):
        for suffix in ("<think>private", "<tool_call>{", "<thi", "<tool_", "<function=wema_list_data_plans>"):
            with self.subTest(suffix=suffix):
                events = await self.output([{"content": "Okay. " + suffix}])
                self.assertEqual("".join(events), "Okay. ")

    async def test_native_tool_call_survives_and_markup_cannot_duplicate_it(self):
        events = await self.output([
            {"reasoning_content": "private", "content": "<think>private</think>One moment. "},
            {"content": TOOL_MARKUP},
            {"tool_calls": [{"index": 0, "id": "call-1", "function": {"name": "wema_list_data_plans", "arguments": '{"network":'}}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": '"MTN"}'}}]},
        ], tools=TOOLS)
        self.assertEqual("".join(e for e in events if isinstance(e, str)), "One moment. ")
        calls = [e for e in events if isinstance(e, ActionToolCall)]
        self.assertEqual(calls, [ActionToolCall("call-1", "wema_list_data_plans", {"network": "MTN"})])

    async def test_ordinary_text_and_unicode_survive_network_byte_boundaries(self):
        text = "It costs \u20a6100. 2 < 3 and 5 > 4."
        events = await self.output([{"content": text}], byte_chunks=True)
        self.assertEqual("".join(events), text)

    async def test_plain_text_is_not_held_until_completion(self):
        events = await self.output([{"content": "Hello. "}, {"content": "How can I help?"}])
        self.assertEqual(events, ["Hello. ", "How can I help?"])

    async def test_filtered_provider_output_is_used_for_transcripts_and_tts(self):
        text = "<think>private reasoning</think>Let me check. " + TOOL_MARKUP
        with tempfile.TemporaryDirectory() as directory:
            send = AsyncMock()
            pipeline = ConversationPipeline(LabConfig(), TraceRecorder(Path(directory)), send)
            pipeline.llm = RLLMClient(LabConfig(), FakeSession([{"content": c} for c in text]))
            spoken = []

            async def synthesize(phrase):
                spoken.append(phrase)
                async def chunks():
                    if False:
                        yield b""
                return 24000, chunks()

            pipeline.tts.stream = synthesize
            attempt = Attempt("test", "MTN plans", False, authorized=True)
            try:
                calls = await pipeline._stream_llm_and_tts(attempt, [], phase="knowledge_followup", tools=TOOLS)
                self.assertEqual(calls, [])  # Raw text is not an executable tool call.
                self.assertEqual(attempt.answer.strip(), "Let me check.")
                self.assertEqual(spoken, ["Let me check."])
                for call in send.await_args_list:
                    self.assertNotIn("<", call.args[0].get("content", ""))
            finally:
                await pipeline.close()


if __name__ == "__main__":
    unittest.main()
