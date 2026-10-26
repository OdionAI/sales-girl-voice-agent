"""Sample conversations against the REAL pipeline with a scripted model and a
controllable clock, so we can watch (a) the agent not going dormant while a tool
runs, (b) it answering a caller mid-tool, (c) natural result/failure handling and
(d) the confirmation read-back. Prints a transcript per scenario.

Run:  PYTHONPATH=/app python3 sim_conversation.py
"""
import asyncio
import json
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from latency_lab.action_tools import ActionToolCall
from latency_lab.config import LabConfig
from latency_lab.pipeline import Attempt, ConversationPipeline
from latency_lab.trace import TraceRecorder

AGENT_STYLE = {"tool_started": "Alright, let me check that for you.",
               "tool_wait": ["Still on it — almost there.", "Thanks for waiting, I'm still working on that.",
                             "Just a moment more, I haven't forgotten you."],
               "idle": "I'm still here whenever you're ready."}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class ScriptedLLM:
    """Deterministic stand-in for the model: engagement JSON, natural lines, replies."""

    def __init__(self):
        self.waits = 0

    async def stream(self, messages, **kwargs):
        system, user = messages[0]["content"], messages[-1]["content"]
        if "You decide whether a voice assistant should speak" in system:
            payload = json.loads(user)
            reason = payload.get("reason")
            if reason == "tool_started":
                text = AGENT_STYLE["tool_started"]
            elif reason == "tool_wait":
                text = AGENT_STYLE["tool_wait"][self.waits % len(AGENT_STYLE["tool_wait"])]
                self.waits += 1
            elif reason == "idle":
                text = AGENT_STYLE["idle"]
            else:
                yield json.dumps({"speak": False, "text": "", "quiet": "none"})
                return
            yield json.dumps({"speak": True, "text": text, "quiet": "none"})
            return
        if "mid-call with a caller" in system:  # _natural_line: mirror the real model
            try:
                situation = json.loads(user).get("situation", "")
            except Exception:
                situation = ""
            yield {"acknowledge_tool_start": "Let me take care of that for you.",
                   "report_result": "That's done — your balance is twelve thousand four hundred naira.",
                   "tool_failed_offer_ticket": "I couldn't get that through just now — I can raise a ticket so the team follows up.",
                   "ask_confirmation": "Shall I go ahead?",
                   "ask_missing_detail": "I just need one more detail from you."}.get(situation, "Sure, I'm on it.")
            return
        yield "Sure, I'm on it."


class FakeBanking:
    def __init__(self, body, gate=None, prepared=None):
        self.body, self.gate, self.prepared = body, gate, prepared
        self.profile = {"customer_id": "R008448055"}
        self.records = {"wema_execute_prepared": {"url": "http://127.0.0.1:8097/v1/x", "headers": {}}}
        self.context = type("C", (), {"agent_id": "a", "business_id": "b", "end_user_id": "u"})()
        self.session_id = "s"

    async def execute(self, call, *, confirmed_operation="", on_result=None):
        if self.gate is not None:
            await self.gate.wait()
        if on_result:
            on_result(self.body)
        if call.name.startswith("wema_prepare_") and self.body.get("status") == "prepared":
            self.prepared = self.body["data"]
        return self.body

    async def emit(self, topic, payload):
        return None

    async def close(self):
        return None


async def build(tmp, banking, clock):
    log = []

    async def send(message):
        if message.get("type") == "agent_text":
            log.append(("agent", message.get("text", "")))

    pipe = ConversationPipeline(
        replace(LabConfig(), engagement_enabled=True, background_tools=True,
                memory_compaction_enabled=False, engagement_tool_seconds=5.0,
                engagement_idle_seconds=12.0, engagement_max_tool_updates=3,
                engagement_max_idle_updates=1),
        TraceRecorder(Path(tmp.name)), send, transport="livekit")
    pipe.llm = ScriptedLLM()
    pipe.banking = banking
    pipe.engagement.clock = clock
    pipe.engagement.audio_until = clock.t
    pipe.engagement.next_check = clock.t + 5.0

    async def speak(attempt, text, *, phase):
        log.append(("agent", text))

    async def announce(text):
        log.append(("agent", text))

    async def play(policy, attempt, reason, revision, text, sample_rate, chunks):
        log.append(("agent", f"[{reason}] {text}"))
        return True

    async def tts_stream(text):
        return 24000, _chunks()

    async def _chunks():
        yield b"\x00\x00" * 100

    pipe._speak_text = AsyncMock(side_effect=speak)
    pipe.announce = AsyncMock(side_effect=announce)
    pipe._play_engagement = AsyncMock(side_effect=play)
    pipe.tts = type("T", (), {"stream": staticmethod(tts_stream)})()
    return pipe, log


def attempt(pipe):
    a = Attempt("one", "", False, authorized=True, turn_id="one")
    a.authorization_event.set()
    pipe.attempt, pipe.turn_id, pipe.state = a, "one", "idle"
    return a


async def tick(pipe, clock, seconds, log):
    """Advance the fake clock and let the engagement speak when it wants to."""
    for _ in range(int(seconds)):
        clock.t += 1.0
        if clock.t >= pipe.engagement.next_check and not pipe.engagement.decision_task:
            candidate = pipe.engagement.opportunity()
            if candidate:
                log.append(("clock", f"t+{int(clock.t - 1000)}s"))
                pipe.engagement.decision_task = asyncio.create_task(pipe.engagement.reconsider(*candidate))
                await asyncio.gather(pipe.engagement.decision_task, return_exceptions=True)
                pipe.engagement.decision_task = None


def show(title, log, note=""):
    print(f"\n=== {title} ===")
    if note:
        print(f"    ({note})")
    for who, text in log:
        print(f"  {who:>6} | {text}")


async def scenario_slow_tool(tmp):
    clock = Clock()
    gate = asyncio.Event()
    banking = FakeBanking({"status": "ok", "message": "Balance is NGN 12,400",
                           "data": {"balance": 12400}}, gate=gate)
    pipe, log = await build(tmp, banking, clock)
    a = attempt(pipe)
    call = ActionToolCall("c1", "wema_get_balance", {"source_account": "0000000001"})
    log.append(("caller", "What's my balance?"))
    await pipe._dispatch_background_tool(a, call)
    log.append(("clock", "t+0s"))
    await tick(pipe, clock, 6, log)          # first update ~5s
    log.append(("caller", "So how long is this going to take?"))
    log.append(("note", f"tool still running = {bool(pipe.inflight_tools)}; caller is NOT blocked"))
    await tick(pipe, clock, 6, log)          # second update ~10s
    gate.set()                                # tool finishes
    await asyncio.gather(*list(pipe._background_tasks), return_exceptions=True)
    await asyncio.sleep(0)
    show("Scenario 1 — slow balance check (agent must not go dormant)", log)
    return pipe, log


async def scenario_airtime(tmp):
    clock = Clock()
    banking = FakeBanking({"status": "prepared", "message": "ready",
                           "data": {"operation_id": "op-9", "preview": {
                               "package_name": "100 naira airtime", "network": "MTN",
                               "amount": "100", "phone_number": "08112812216"}}})
    pipe, log = await build(tmp, banking, clock)
    a = attempt(pipe)
    log.append(("caller", "Buy me 100 naira airtime on MTN, 08112812216"))
    await pipe._dispatch_background_tool(a, ActionToolCall("c1", "wema_prepare_airtime_purchase", {"amount": "100"}))
    await asyncio.gather(*list(pipe._background_tasks), return_exceptions=True)
    show("Scenario 2 — airtime purchase reaches the confirmation read-back", log)
    return pipe, log


async def scenario_failure(tmp):
    clock = Clock()
    banking = FakeBanking({"status": "failed", "message": "upstream HTTP 500 at connector"})
    pipe, log = await build(tmp, banking, clock)
    a = attempt(pipe)
    log.append(("caller", "Please send 5,000 to my sister"))
    await pipe._dispatch_background_tool(a, ActionToolCall("c1", "wema_prepare_transfer", {"amount": "5000"}))
    await asyncio.gather(*list(pipe._background_tasks), return_exceptions=True)
    show("Scenario 3 — tool fails: graceful, offers a ticket, no raw internals", log)
    return pipe, log


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        for fn in (scenario_slow_tool, scenario_airtime, scenario_failure):
            pipe, log = await fn(Path(tmp))
            await pipe.close()


asyncio.run(main())
