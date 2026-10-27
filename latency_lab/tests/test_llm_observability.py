import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from latency_lab.action_tools import ActionToolCall, suppress_transfer_preamble
from latency_lab.config import LabConfig
from latency_lab.providers import RLLMClient, scoped_llm_stream
from latency_lab.trace import TraceRecorder
from latency_lab.tests.test_llm_stall_timeout import StallingSession, text_block


class ObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.trace = TraceRecorder(Path(self.folder.name))

    async def asyncTearDown(self):
        self.folder.cleanup()

    def rows(self):
        return [json.loads(line) for line in self.trace.path.read_text().splitlines()]

    def client(self, blocks):
        client = RLLMClient(LabConfig(llm_empty_retries=0), StallingSession(0, blocks))
        client.trace = self.trace
        return client

    async def test_provider_measurements_are_before_guard_and_privacy_allowlisted(self):
        client = self.client([text_block("Sannu!"), {"choices": [], "usage": {
            "prompt_tokens": 9000, "completion_tokens": 3,
            "prompt_tokens_details": {"cached_tokens": 8000}, "secret": "private"}}])
        decisions = []
        stream = scoped_llm_stream(client.stream([{"role": "user", "content": "private"}]),
            turn_id="turn-0001", attempt_id="a1", phase="prompt_first")
        events = [e async for e in suppress_transfer_preamble(stream, on_decision=lambda **data: decisions.append(data))]
        self.assertEqual(events, ["Sannu!"])
        rows = self.rows()
        self.assertEqual([r["event"] for r in rows], ["llm_provider_request_start", "llm_provider_first_text", "llm_provider_request_complete"])
        self.assertTrue(all(r["turn_id"] == "turn-0001" and r["attempt_id"] == "a1" for r in rows))
        self.assertEqual(rows[-1]["data"]["cached_tokens"], 8000)
        self.assertTrue(rows[-1]["data"]["completed"])
        self.assertNotIn("private", json.dumps(rows))
        self.assertFalse(decisions[0]["suppressed"])
        self.assertGreaterEqual(decisions[0]["text_buffer_ms"], 0)

    async def test_parallel_requests_are_scoped_and_unknown_cache_is_not_zero(self):
        client = self.client([text_block("Hello")])
        async def run(turn):
            return [e async for e in scoped_llm_stream(client.stream([]), turn_id=turn,
                    attempt_id=turn, phase="test")]
        await asyncio.gather(run("turn-0001"), run("turn-0002"))
        rows = self.rows()
        for turn in ["turn-0001", "turn-0002"]:
            group = [r for r in rows if r["turn_id"] == turn]
            self.assertEqual(len(group), 3)
            self.assertEqual(len({r["data"]["request_id"] for r in group}), 1)
            self.assertNotIn("cached_tokens", group[-1]["data"])

    async def test_closing_a_stream_does_not_leak_scope_or_emit_complete_success(self):
        client = self.client([text_block("Hello"), text_block(" again")])
        stream = scoped_llm_stream(client.stream([]), turn_id="turn-0001", attempt_id="a", phase="test")
        self.assertEqual(await anext(stream), "Hello")
        await stream.aclose()
        self.assertFalse(self.rows()[-1]["data"]["completed"])
        await self.client([text_block("Unscoped")]).stream([]).__anext__()
        self.assertIsNone(self.rows()[-1]["turn_id"])


class TransferReleaseInvariantTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_transfer_invalidates_even_a_complete_speech_sentence(self):
        text_seen = asyncio.Event()
        finish = asyncio.Event()
        call = ActionToolCall("call-1", "transfer_to_aicc", {})
        async def provider():
            yield "Zan haɗa ka da wakili yanzu."
            text_seen.set()
            await finish.wait()
            yield call
        decisions = []
        stream = suppress_transfer_preamble(provider(), on_decision=lambda **data: decisions.append(data))
        pending = asyncio.create_task(anext(stream))
        await text_seen.wait()
        self.assertFalse(pending.done(), "An early prefix or punctuation cannot authorize speech")
        finish.set()
        self.assertEqual(await pending, call)
        self.assertEqual([e async for e in stream], [])
        self.assertTrue(decisions[0]["suppressed"])

    async def test_failure_or_cancellation_never_releases_buffered_text(self):
        async def broken():
            yield "I'll connect you now."
            raise RuntimeError("provider failed")
        with self.assertRaises(RuntimeError):
            await anext(suppress_transfer_preamble(broken()))
        waiting = asyncio.Event()
        async def interrupted():
            yield "Do not release this."
            waiting.set()
            await asyncio.Event().wait()
        stream = suppress_transfer_preamble(interrupted())
        pending = asyncio.create_task(anext(stream))
        await waiting.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        await stream.aclose()

    async def test_plain_speech_and_other_tool_decisions_are_preserved(self):
        call = ActionToolCall("call-1", "end_call", {})
        async def provider():
            yield "Na gode."
            yield call
        self.assertEqual([e async for e in suppress_transfer_preamble(provider())], ["Na gode.", call])
