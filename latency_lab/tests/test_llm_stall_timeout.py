import asyncio
import json
import time
import unittest

from latency_lab.config import LabConfig
from latency_lab.providers import RLLMClient


class FakeTrace:
    def __init__(self):
        self.events = []

    def record(self, event, **data):
        self.events.append((event, data))


class StallingResponse:
    """Streams nothing for `stall` seconds, then optionally some blocks."""

    status = 200

    def __init__(self, stall, blocks=()):
        self.stall = stall
        self.blocks = blocks
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def raise_for_status(self):
        pass

    async def iter_any(self):
        await asyncio.sleep(self.stall)
        for block in self.blocks:
            yield ("data: " + json.dumps(block, ensure_ascii=False) + "\n\n").encode()
        yield b"data: [DONE]\n\n"


class StallingSession:
    def __init__(self, stall, blocks=()):
        self.stall = stall
        self.blocks = blocks
        self.calls = 0

    def post(self, _url, *, json, **_kwargs):
        self.calls += 1
        return StallingResponse(self.stall, self.blocks)


def text_block(text):
    return {"choices": [{"delta": {"content": text}}]}


async def drain(client):
    return [event async for event in client.stream([{"role": "user", "content": "hi"}])]


class FirstOutputTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_stalling_attempt_is_abandoned_at_the_deadline(self):
        config = LabConfig(llm_empty_retries=0, llm_first_output_timeout_seconds=0.25)
        session = StallingSession(stall=5.0, blocks=[text_block("Too late.")])
        client = RLLMClient(config, session)
        client.trace = FakeTrace()

        started = time.monotonic()
        events = await drain(client)
        elapsed = time.monotonic() - started

        self.assertEqual(events, [])
        self.assertLess(elapsed, 1.5, "must not wait out the provider stall")
        self.assertEqual(session.calls, 1)
        names = [name for name, _ in client.trace.events]
        self.assertIn("llm_first_output_timeout", names)

    async def test_prompt_output_is_unaffected(self):
        config = LabConfig(llm_empty_retries=0, llm_first_output_timeout_seconds=5.0)
        session = StallingSession(stall=0.0, blocks=[text_block("Hello there.")])
        client = RLLMClient(config, session)
        client.trace = FakeTrace()

        events = await drain(client)

        self.assertEqual("".join(events), "Hello there.")
        names = [name for name, _ in client.trace.events]
        self.assertNotIn("llm_first_output_timeout", names)

    async def test_empty_retry_ladder_is_bounded_by_the_deadline(self):
        config = LabConfig(llm_empty_retries=2, llm_empty_retry_delay_seconds=0,
                           llm_first_output_timeout_seconds=0.25)
        session = StallingSession(stall=5.0)
        client = RLLMClient(config, session)
        client.trace = FakeTrace()

        started = time.monotonic()
        events = await drain(client)
        elapsed = time.monotonic() - started

        self.assertEqual(events, [])
        self.assertEqual(session.calls, 3)  # 1 initial + 2 retries
        self.assertLess(elapsed, 3.0, "three stalled attempts must stay well under 3s")
        names = [name for name, _ in client.trace.events]
        self.assertEqual(names.count("llm_first_output_timeout"), 3)
        self.assertEqual(names.count("llm_empty_completion_retry"), 2)
        self.assertIn("llm_empty_completion_exhausted", names)


if __name__ == "__main__":
    unittest.main()
