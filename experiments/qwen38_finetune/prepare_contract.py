"""Snapshot the MoMo support tool schemas from the current runtime code."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from latency_lab.action_tools import action_tool_definitions  # noqa: E402
from latency_lab.agentic import AgentRuntimeContext  # noqa: E402


SOURCE = ROOT / "latency_lab" / "action_tools.py"
OUTPUT = Path(__file__).with_name("contract_snapshot.json")
ACTIVE = (
    ("create_ticket", "internal://create-ticket"),
    ("end_call", "builtin://end_call"),
    ("transfer_to_aicc", "builtin://transfer_to_aicc"),
)


def build() -> dict:
    context = AgentRuntimeContext(
        tools=tuple({"name": name, "url": url, "is_active": True} for name, url in ACTIVE)
    )
    definitions = action_tool_definitions(context)
    names = {definition["function"]["name"] for definition in definitions}
    expected = {name for name, _ in ACTIVE}
    if names != expected:
        raise RuntimeError(f"expected {sorted(expected)}, got {sorted(names)}")
    return {
        "source": "latency_lab/action_tools.py",
        "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        "profile": "MoMo support tools documented active for Amaka/Balogun/Abdul; live Agent Config export pending",
        "tools": {definition["function"]["name"]: definition for definition in definitions},
    }


if __name__ == "__main__":
    snapshot = build()
    OUTPUT.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
    print(f"wrote {OUTPUT} with {len(snapshot['tools'])} tools")
