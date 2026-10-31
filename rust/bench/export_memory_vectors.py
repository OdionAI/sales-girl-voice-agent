"""Export independent expectations from the untouched Python ConversationMemory."""

import asyncio
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from latency_lab.config import LabConfig
from latency_lab.memory import ConversationMemory
from latency_lab.trace import TraceRecorder


async def main():
    plans = [
        [("user", 1, "hello", False), ("answer", 1, "Hi."), ("finish", 1, False)],
        [
            ("user", 1, "Do not execute this transfer", True),
            ("finish", 1, False),
            ("user", 2, "What did I say?", False),
        ],
        [
            ("user", 1, "digits 0012", False),
            ("answer", 1, "Did you mean 0012?"),
            ("finish", 1, True),
            ("user", 2, "No, 0013", False),
            ("answer", 2, "Understood."),
            ("finish", 2, False),
        ],
    ]
    plans.append([
        (kind, turn, *args)
        for turn in range(1, 25)
        for kind, args in [
            ("user", (f"topic {turn} ü東京", turn % 4 == 0)),
            ("answer", (f"response {turn}",)),
            ("finish", (turn % 7 == 0,)),
        ]
    ])
    plans.append([
        ("user", 1, "large " * 1000, False),
        ("tool", 1,
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "abc", "type": "function",
                "function": {"name": "look", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": "abc", "content": "x" * 1800}),
        ("answer", 1, "last answer"),
        ("finish", 1, False),
    ])
    cases = []
    with tempfile.TemporaryDirectory() as folder:
        for plan in plans:
            config = replace(LabConfig(), memory_recent_exchanges=2)
            memory = ConversationMemory(config, TraceRecorder(Path(folder)))
            for kind, turn, *args in plan:
                if kind == "user":
                    memory.user(turn, args[0], listening_only=args[1])
                elif kind == "finish":
                    memory.finish(turn, interrupted=args[0])
                else:
                    getattr(memory, kind)(turn, *args)
            before = []
            for budget in [512, 900, 6000]:
                memory.config = replace(config, memory_context_tokens=budget)
                for exclude in [None, 1, len(memory.turns)]:
                    before.append({
                        "budget": budget, "exclude": exclude,
                        "expected": memory.context(exclude_turn_id=exclude),
                    })
            memory.config = config
            summary = {
                "topics": ["Older context"], "facts": ["Preserved uncertainty"],
                "corrections": [], "open_questions": [],
            }

            class LLM:
                async def stream(self, *_, **kwargs):
                    yield json.dumps(summary)

            compacted = await memory.compact(LLM())
            cases.append({
                "plan": plan, "before": before, "summary": summary,
                "compacted": compacted, "after": memory.context(),
            })
    Path(sys.argv[2]).write_text(json.dumps(cases, ensure_ascii=False, indent=2) + "\n")


asyncio.run(main())
