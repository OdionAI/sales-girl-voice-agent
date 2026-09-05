from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from livekit import rtc
from livekit.agents import RunContext, llm
from livekit.agents.voice import SpeechHandle

from agent.dynamic_tools import HTTP_TOOL_FILLER_MESSAGES, build_dynamic_http_tools
from agent.tool_wait_speech import GeneratedToolWaitSpeech, normalize_tool_wait_speech_mode


class SpeechSession(rtc.EventEmitter):
    """Exercise LiveKit's real filler scope without network or audio devices."""

    def __init__(self, tool_name: str, observer: object | None = None) -> None:
        super().__init__()
        self.userdata = {"enabled_tool_names": [tool_name], "auth_observer": observer}
        self.lines: list[str] = []
        self.spoken: asyncio.Queue[str] = asyncio.Queue()
        self.idle = asyncio.Event()
        self.idle.set()
        self.llm = None
        self.history = llm.ChatContext()
        self.speech_tasks: list[asyncio.Task] = []
        self.say_options: list[dict] = []

    async def wait_for_idle(self) -> None:
        await self.idle.wait()

    def say(self, text, **kwargs) -> SpeechHandle:
        self.say_options.append(kwargs)
        handle = SpeechHandle.create()
        if isinstance(text, str):
            self.lines.append(text)
            self.spoken.put_nowait(text)
        else:
            async def consume():
                try:
                    async for line in text:
                        self.lines.append(line)
                        self.spoken.put_nowait(line)
                finally:
                    handle._mark_done()

            self.speech_tasks.append(asyncio.create_task(consume()))
        return handle


class FakeLLMStream:
    def __init__(self, text="Let me take a look at that balance for you.", release=None):
        self.text = text
        self.release = release
        self.started = asyncio.Event()
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def __aiter__(self):
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        yield llm.ChatChunk(id="filler-test", delta=llm.ChoiceDelta(role="assistant", content=self.text))


class ToolWaitSpeechTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for name, value in (
            ("HTTP_TOOL_FILLER_DELAY_SECONDS", 0.01),
            ("HTTP_TOOL_FILLER_INTERVAL_SECONDS", 0.03),
        ):
            setting = patch(f"agent.dynamic_tools.{name}", value)
            setting.start()
            self.addCleanup(setting.stop)

    def _tool(self, name: str = "lookup_inventory"):
        return build_dynamic_http_tools({"tools": [{
            "name": name,
            "description": "Check the caller's request.",
            "url": "https://example.test/tool",
            "method": "POST",
            "request_schema": {"type": "object", "properties": {}},
        }]})[0]

    def _context(self, session: SpeechSession, name: str = "lookup_inventory") -> RunContext:
        return RunContext(
            session=session,
            speech_handle=SpeechHandle.create(),
            function_call=llm.FunctionCall(call_id="call-test", name=name, arguments="{}"),
        )

    async def _line(self, session: SpeechSession) -> str:
        return await asyncio.wait_for(session.spoken.get(), timeout=1)

    async def test_speaks_while_auth_and_http_are_pending_without_bypassing_auth(self) -> None:
        auth_release = asyncio.Event()
        http_started = asyncio.Event()
        http_release = asyncio.Event()

        async def authorize(**kwargs):
            await auth_release.wait()
            return {"authorized": True, "session_status": "verified", "action_status": "verified"}

        async def request(**kwargs):
            http_started.set()
            await http_release.wait()
            return {"status": "ok", "data": {"balance": "100.00"}}

        observer = SimpleNamespace(authorize_action=AsyncMock(side_effect=authorize))
        session = SpeechSession("wema_get_balance", observer)
        context = self._context(session, "wema_get_balance")
        with patch("agent.dynamic_tools.invoke_dynamic_http_tool", side_effect=request) as http:
            task = asyncio.create_task(self._tool("wema_get_balance")(ctx=context, raw_arguments={}))
            try:
                self.assertEqual(await self._line(session), "Let me check your available balance.")
                http.assert_not_awaited()
                self.assertFalse(task.done())
                auth_release.set()
                await asyncio.wait_for(http_started.wait(), timeout=1)
                self.assertEqual(await self._line(session), HTTP_TOOL_FILLER_MESSAGES[1])
                self.assertFalse(task.done())
                http_release.set()
                result = await asyncio.wait_for(task, timeout=1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(result["data"]["balance"], "100.00")
        observer.authorize_action.assert_awaited_once()
        http.assert_awaited_once()
        await asyncio.sleep(0.08)
        self.assertEqual(len(session.lines), 2)

    async def test_wema_tools_use_their_own_acknowledgement(self) -> None:
        expected_lines = {
            "wema_get_balance": "Let me check your available balance.",
            "wema_get_transactions": "Let me check your recent transactions.",
            "wema_list_data_plans": "Let me check the data plans for you.",
            "wema_list_transfer_banks": "Let me look up that bank for you.",
            "wema_prepare_data_purchase": "Let me check the details for your data purchase.",
            "wema_prepare_transfer": "Let me check the details for your transfer.",
            "wema_execute_prepared": "Let me check your confirmed transaction request.",
        }
        tools = build_dynamic_http_tools({"tools": [{
            "name": name,
            "url": "https://example.test/tool",
            "method": "POST",
            "request_schema": {"type": "object", "properties": {}},
        } for name in expected_lines]})

        for tool, (name, expected) in zip(tools, expected_lines.items()):
            with self.subTest(tool=name):
                release = asyncio.Event()

                async def request(**kwargs):
                    await release.wait()
                    return {"status": "ok"}

                observer = SimpleNamespace(authorize_action=AsyncMock(
                    return_value={"authorized": True},
                ))
                session = SpeechSession(name, observer)
                with patch("agent.dynamic_tools.invoke_dynamic_http_tool", side_effect=request):
                    task = asyncio.create_task(tool(
                        ctx=self._context(session, name), raw_arguments={},
                    ))
                    try:
                        self.assertEqual(await self._line(session), expected)
                        self.assertFalse(task.done())
                        release.set()
                        result = await asyncio.wait_for(task, timeout=1)
                        self.assertEqual(result["status"], "ok")
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                observer.authorize_action.assert_awaited_once()

    async def test_fast_tool_does_not_add_filler_or_delay_the_result(self) -> None:
        session = SpeechSession("lookup_inventory")
        with patch("agent.dynamic_tools.invoke_dynamic_http_tool", return_value={"status": "ok"}):
            result = await self._tool()(ctx=self._context(session), raw_arguments={})
        await asyncio.sleep(0.05)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(session.lines, [])

    async def test_generated_mode_uses_session_llm_with_context_but_no_tools(self) -> None:
        session = SpeechSession("wema_get_balance", SimpleNamespace(
            authorize_action=AsyncMock(return_value={"authorized": True}),
        ))
        session.userdata["tool_wait_speech_mode"] = "llm_generated"
        session.history.add_message(role="system", content="Private agent instructions")
        session.history.add_message(role="user", content="What is my available balance?")
        stream = FakeLLMStream()
        session.llm = Mock(spec=llm.LLM)
        session.llm.chat.return_value = stream
        release = asyncio.Event()

        async def request(**kwargs):
            await release.wait()
            return {"status": "ok"}

        with patch("agent.dynamic_tools.invoke_dynamic_http_tool", side_effect=request):
            task = asyncio.create_task(self._tool("wema_get_balance")(
                ctx=self._context(session, "wema_get_balance"), raw_arguments={},
            ))
            try:
                self.assertEqual(await self._line(session), stream.text)
                self.assertFalse(task.done())
                release.set()
                await asyncio.wait_for(task, timeout=1)
            finally:
                task.cancel()
                await asyncio.gather(task, *session.speech_tasks, return_exceptions=True)
        kwargs = session.llm.chat.call_args.kwargs
        self.assertEqual(kwargs["tools"], [])
        self.assertEqual(kwargs["tool_choice"], "none")
        self.assertEqual(kwargs["conn_options"].max_retry, 0)
        prompt = kwargs["chat_ctx"].items[-1].text_content
        self.assertIn("What is my available balance?", prompt)
        self.assertNotIn("Private agent instructions", prompt)
        self.assertEqual(len(session.history.items), 2)
        self.assertFalse(session.say_options[0]["add_to_chat_ctx"])
        self.assertTrue(stream.closed)

    async def test_fixed_and_fast_generated_tools_do_not_request_llm_filler(self) -> None:
        for mode in ("tool_specific", "unexpected", "llm_generated"):
            with self.subTest(mode=mode):
                session = SpeechSession("lookup_inventory")
                session.userdata["tool_wait_speech_mode"] = mode
                session.llm = Mock(spec=llm.LLM)
                if mode == "llm_generated":
                    with patch("agent.dynamic_tools.invoke_dynamic_http_tool", return_value={"status": "ok"}):
                        await self._tool()(ctx=self._context(session), raw_arguments={})
                    self.assertEqual(session.lines, [])
                else:
                    with patch("agent.dynamic_tools.invoke_dynamic_http_tool", side_effect=asyncio.CancelledError):
                        with self.assertRaises(asyncio.CancelledError):
                            await self._tool()(ctx=self._context(session), raw_arguments={})
                session.llm.chat.assert_not_called()

    async def test_generated_filler_is_cancelled_on_result_or_interruption(self) -> None:
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                session = SpeechSession("lookup_inventory")
                session.userdata["tool_wait_speech_mode"] = "llm_generated"
                stream = FakeLLMStream(release=asyncio.Event())
                session.llm = Mock(spec=llm.LLM)
                session.llm.chat.return_value = stream
                context = self._context(session)
                http_release = asyncio.Event()

                async def request(**kwargs):
                    await http_release.wait()
                    return {"status": "ok"}

                with patch("agent.dynamic_tools.invoke_dynamic_http_tool", side_effect=request):
                    task = asyncio.create_task(self._tool()(ctx=context, raw_arguments={}))
                    try:
                        await asyncio.wait_for(stream.started.wait(), timeout=1)
                        if interrupted:
                            context.speech_handle.interrupt()
                            await asyncio.wait_for(asyncio.gather(*session.speech_tasks), timeout=1)
                            self.assertTrue(stream.closed)
                        http_release.set()
                        await asyncio.wait_for(task, timeout=1)
                    finally:
                        task.cancel()
                        await asyncio.gather(task, *session.speech_tasks, return_exceptions=True)
                self.assertTrue(stream.closed)
                self.assertEqual(session.lines, [])

    async def test_generation_failure_or_invalid_output_uses_fixed_phrase(self) -> None:
        for text in ("", "Your transfer was successful.", "Your balance is 500 naira.", "<think>reasoning</think>", "x" * 241):
            with self.subTest(text=text):
                session = SpeechSession("lookup_inventory")
                session.llm = Mock(spec=llm.LLM)
                session.llm.chat.return_value = FakeLLMStream(text)
                source = GeneratedToolWaitSpeech(self._context(session), "lookup_inventory")
                self.assertEqual(await source._generate(0, "Please wait."), "Please wait.")
        for error in (RuntimeError("unavailable"), TimeoutError()):
            session.llm.chat.side_effect = error
            self.assertEqual(await source._generate(0, "Please wait."), "Please wait.")

    async def test_slow_filler_generation_times_out_and_closes_stream(self) -> None:
        session = SpeechSession("lookup_inventory")
        stream = FakeLLMStream(release=asyncio.Event())
        session.llm = Mock(spec=llm.LLM)
        session.llm.chat.return_value = stream
        source = GeneratedToolWaitSpeech(self._context(session), "lookup_inventory")
        with patch("agent.tool_wait_speech.LLM_FILLER_TIMEOUT_SECONDS", 0.01):
            self.assertEqual(await source._generate(0, "Please wait."), "Please wait.")
        self.assertTrue(stream.closed)

    def test_mode_is_allowlisted(self) -> None:
        self.assertEqual(normalize_tool_wait_speech_mode("llm_generated"), "llm_generated")
        for value in (None, "", {}, "LLM_GENERATED", "unknown", "tool_specific"):
            self.assertEqual(normalize_tool_wait_speech_mode(value), "tool_specific")

    async def test_auth_failure_stops_filler_and_never_calls_http(self) -> None:
        release = asyncio.Event()

        async def authorize(**kwargs):
            await release.wait()
            return {"authorized": False, "session_status": "failed", "action_status": "failed"}

        session = SpeechSession("wema_get_balance", SimpleNamespace(authorize_action=authorize))
        with patch("agent.dynamic_tools.invoke_dynamic_http_tool") as http:
            task = asyncio.create_task(self._tool("wema_get_balance")(
                ctx=self._context(session, "wema_get_balance"), raw_arguments={},
            ))
            try:
                await self._line(session)
                release.set()
                result = await asyncio.wait_for(task, timeout=1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(result["auth_required"])
        http.assert_not_awaited()
        await asyncio.sleep(0.08)
        self.assertEqual(len(session.lines), 1)

    async def test_filler_waits_for_quiet_and_stops_after_tool_cancellation(self) -> None:
        session = SpeechSession("lookup_inventory")
        session.idle.clear()
        request_pending = asyncio.Event()

        async def request(**kwargs):
            request_pending.set()
            await asyncio.Event().wait()

        with patch("agent.dynamic_tools.invoke_dynamic_http_tool", side_effect=request):
            task = asyncio.create_task(self._tool()(ctx=self._context(session), raw_arguments={}))
            try:
                await asyncio.wait_for(request_pending.wait(), timeout=1)
                await asyncio.sleep(0.05)
                self.assertEqual(session.lines, [])
                session.idle.set()
                self.assertEqual(await self._line(session), HTTP_TOOL_FILLER_MESSAGES[0])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0.08)
        self.assertEqual(len(session.lines), 1)

    async def test_interruption_stops_future_filler(self) -> None:
        session = SpeechSession("lookup_inventory")
        context = self._context(session)
        release = asyncio.Event()

        async def request(**kwargs):
            await release.wait()
            return {"status": "ok"}

        with patch("agent.dynamic_tools.invoke_dynamic_http_tool", side_effect=request):
            task = asyncio.create_task(self._tool()(ctx=context, raw_arguments={}))
            try:
                await self._line(session)
                context.speech_handle.interrupt()
                await asyncio.sleep(0.08)
                self.assertEqual(len(session.lines), 1)
                release.set()
                await asyncio.wait_for(task, timeout=1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_parallel_tools_share_one_filler_per_quiet_pause(self) -> None:
        session = SpeechSession("lookup_inventory")
        release = asyncio.Event()

        async def request(**kwargs):
            await release.wait()
            return {"status": "ok"}

        with patch("agent.dynamic_tools.invoke_dynamic_http_tool", side_effect=request):
            tasks = [asyncio.create_task(self._tool()(
                ctx=self._context(session), raw_arguments={},
            )) for _ in range(2)]
            try:
                await self._line(session)
                await asyncio.sleep(0.01)
                self.assertEqual(len(session.lines), 1)
                release.set()
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
