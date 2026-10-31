"""Export exact public default settings/prompts from the pinned Python baseline."""

import asyncio, dataclasses, json, sys, tempfile, os
from pathlib import Path

sys.path.insert(0, sys.argv[1])
if len(sys.argv) > 3:
    os.environ.update(json.loads(Path(sys.argv[3]).read_text())["environment"])
from latency_lab.config import lab_config_from_environ
from latency_lab.pipeline import ConversationPipeline
from latency_lab.trace import TraceRecorder
from latency_lab.engagement import SPEECH_GATE_RULES, SPEECH_GATE_TOOL
from latency_lab.providers import resolve_tts_seed


async def main():
    c = lab_config_from_environ()

    async def send(_message):
        pass

    with tempfile.TemporaryDirectory() as d:
        p = ConversationPipeline(c, TraceRecorder(Path(d)), send, transport="livekit")
        body = dataclasses.asdict(c)
        for k in ("trace_dir", "report_dir"):
            body.pop(k, None)
        assert not any(
            v for k, v in body.items() if k.endswith("_token") or "secret" in k
        )
        body.update(
            base_system_prompt=p.system_prompt,
            speech_gate_rules=SPEECH_GATE_RULES,
            speech_gate_tool=SPEECH_GATE_TOOL,
            tools=p.action_tools,
            tts_resolved_seed=resolve_tts_seed(c),
            resolved_opening=p.opening_greeting_text(),
            baseline="d3f90112d00c38868748b3101ae156e70d299313",
        )
        Path(sys.argv[2]).write_text(
            json.dumps(body, ensure_ascii=False, indent=2) + "\n"
        )
        await p.close()


asyncio.run(main())
