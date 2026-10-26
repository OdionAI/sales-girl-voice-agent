import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from latency_lab.config import LabConfig
from latency_lab.providers import RLLMClient
from latency_lab.pipeline import Attempt, ConversationPipeline
from latency_lab.trace import TraceRecorder


class FakeTrace:
    def __init__(self):
        self.events = []

    def record(self, event, **data):
        self.events.append((event, data))


class SeqResponse:
    status = 200

    def __init__(self, blocks):
        self.blocks = blocks
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def raise_for_status(self):
        pass

    async def iter_any(self):
        for block in self.blocks:
            yield ("data: " + json.dumps(block, ensure_ascii=False) + "\n\n").encode()
        yield b"data: [DONE]\n\n"


def empty_block():
    return {"choices": [{"delta": {}}]}


def text_block(text):
    return {"choices": [{"delta": {"content": text}}]}


class SeqSession:
    """Returns a different response per call; the last response repeats."""

    def __init__(self, blocks_list):
        self.responses = [SeqResponse(blocks) for blocks in blocks_list]
        self.calls = 0

    def post(self, _url, *, json, **_kwargs):
        index = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return self.responses[index]


class SingleSession(SeqSession):
    def __init__(self, blocks):
        super().__init__([blocks])


async def drain(client):
    return [event async for event in client.stream([{"role": "user", "content": "hi"}])]


class LLMEmptyRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_empty_completion_is_retried_then_succeeds(self):
        config = LabConfig(llm_empty_retries=2, llm_empty_retry_delay_seconds=0)
        session = SeqSession([[empty_block()], [empty_block()], [text_block("Hello there.")]])
        client = RLLMClient(config, session)
        client.trace = FakeTrace()

        events = await drain(client)

        self.assertEqual("".join(events), "Hello there.")
        self.assertEqual(session.calls, 3)
        names = [name for name, _ in client.trace.events]
        self.assertEqual(names.count("llm_empty_completion_retry"), 2)

    async def test_empty_completion_exhausts_retries_and_yields_nothing(self):
        config = LabConfig(llm_empty_retries=2, llm_empty_retry_delay_seconds=0)
        session = SingleSession([empty_block()])
        client = RLLMClient(config, session)
        client.trace = FakeTrace()

        events = await drain(client)

        self.assertEqual(events, [])
        self.assertEqual(session.calls, 3)  # 1 initial + 2 retries
        names = [name for name, _ in client.trace.events]
        self.assertEqual(names.count("llm_empty_completion_retry"), 2)
        self.assertIn("llm_empty_completion_exhausted", names)

    async def test_no_retry_when_disabled_by_default(self):
        config = LabConfig(llm_empty_retries=0, llm_empty_retry_delay_seconds=0)
        session = SingleSession([empty_block()])
        client = RLLMClient(config, session)
        client.trace = FakeTrace()

        events = await drain(client)

        self.assertEqual(events, [])
        self.assertEqual(session.calls, 1)
        names = [name for name, _ in client.trace.events]
        self.assertNotIn("llm_empty_completion_exhausted", names)

    async def test_tool_call_only_response_is_not_retried(self):
        blocks = [{"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call-1", "function": {"name": "create_ticket", "arguments": "{}"}}
        ]}}]}]
        config = LabConfig(llm_empty_retries=2, llm_empty_retry_delay_seconds=0)
        session = SingleSession(blocks)
        client = RLLMClient(config, session)
        client.trace = FakeTrace()

        events = await drain(client)

        self.assertEqual(session.calls, 1)
        self.assertEqual([type(e).__name__ for e in events], ["ActionToolCall"])


class EmptyGenerationFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_pipeline_speaks_fallback_when_a_committed_turn_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            send = AsyncMock()
            config = LabConfig(
                empty_response_fallback_enabled=True,
                empty_response_fallback_text="Sorry, I can't help with that right now.",
            )
            pipeline = ConversationPipeline(config, TraceRecorder(Path(directory)), send)
            pipeline.llm = RLLMClient(config, SingleSession([empty_block()]))
            pipeline.conversations.spawn = lambda coro: coro.close()
            spoken = []

            async def synthesize(phrase):
                spoken.append(phrase)

                async def chunks():
                    if False:
                        yield b""

                return 24000, chunks()

            pipeline.tts.stream = synthesize
            attempt = Attempt("turn-1", "Oh, it's glue.", False, authorized=True)
            try:
                await pipeline._generate(attempt)
                self.assertEqual(spoken, ["Sorry, I can't help with that right now."])
                self.assertEqual(attempt.answer.strip(), "Sorry, I can't help with that right now.")
            finally:
                await pipeline.close()

    async def test_pipeline_stays_silent_when_fallback_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            send = AsyncMock()
            config = LabConfig(empty_response_fallback_enabled=False)
            pipeline = ConversationPipeline(config, TraceRecorder(Path(directory)), send)
            pipeline.llm = RLLMClient(config, SingleSession([empty_block()]))
            pipeline.conversations.spawn = lambda coro: coro.close()
            spoken = []

            async def synthesize(phrase):
                spoken.append(phrase)

                async def chunks():
                    if False:
                        yield b""

                return 24000, chunks()

            pipeline.tts.stream = synthesize
            attempt = Attempt("turn-2", "Oh, it's glue.", False, authorized=True)
            try:
                await pipeline._generate(attempt)
                self.assertEqual(spoken, [])
            finally:
                await pipeline.close()


if __name__ == "__main__":
    unittest.main()
