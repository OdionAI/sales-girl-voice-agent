"""Loopback-only audio fixture, delaying the greeting until the renderer is ready."""
import argparse
import asyncio
import importlib.util
from pathlib import Path

from aiohttp import web

sdk = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("local_fixture", sdk / "Tests/PasskeySimulator/bank_server.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)

class DelayedAgent(fixture.Bank):
    async def join_agent(self, name):
        await asyncio.sleep(14)
        await super().join_agent(name)

parser = argparse.ArgumentParser()
parser.add_argument("--greeting", type=Path, required=True)
parser.add_argument("--evidence", type=Path, required=True)
args = parser.parse_args()
agent = DelayedAgent(args.greeting, args.evidence)
app = web.Application()
app.add_routes([web.post("/start", agent.start), web.get("/status", agent.status)])
web.run_app(app, host="127.0.0.1", port=18766, access_log=None)
