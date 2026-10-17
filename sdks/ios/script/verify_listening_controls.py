"""Exercise the public Swift SDK against RVC's production RPC handler over LiveKit.

Uses an isolated loopback-only server and fixture call bootstrap. No microphone,
model endpoints, customer services, credentials, or existing rooms are used.
Run from the repository root after `cd sdks/ios && swift build`:
    .venv/bin/python sdks/ios/script/verify_listening_controls.py
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from aiohttp import web
from livekit import api, rtc

from latency_lab.config import LabConfig
from latency_lab.listening import CAPABILITY_ATTRIBUTE, RPC_METHOD, ListeningConfiguration
from latency_lab.livekit_worker import register_listening_rpc
from latency_lab.pipeline import ConversationPipeline
from latency_lab.trace import TraceRecorder


def free_port(kind: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def wait_for_server(port: int, process: asyncio.subprocess.Process) -> None:
    for _ in range(100):
        if process.returncode is not None:
            raise RuntimeError("Isolated LiveKit server stopped during startup")
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.1)
    raise TimeoutError("Isolated LiveKit did not start")


async def verify(directory: Path) -> None:
    binary = ROOT / "sdks/ios/.build/debug/attentive-smoke"
    server_binary = shutil.which("livekit-server")
    if not server_binary or not binary.exists():
        raise RuntimeError("Install livekit-server and build the SDK diagnostic first")
    signal_port, media_port = free_port(), free_port()
    udp_port = free_port(socket.SOCK_DGRAM)
    key, secret = "listening-test", secrets.token_hex(32)
    room_name, caller = "listening-" + secrets.token_hex(4), "sdk-listening-caller"
    url = f"ws://127.0.0.1:{signal_port}"
    config_path = directory / "livekit.yaml"
    config_path.write_text(
        f"port: {signal_port}\nbind_addresses: [127.0.0.1]\n"
        f"rtc:\n  tcp_port: {media_port}\n  udp_port: {udp_port}\n"
        "  use_external_ip: false\n  node_ip: 127.0.0.1\n  enable_loopback_candidate: true\n"
        f"keys:\n  {key}: {secret}\nlogging:\n  level: error\n"
    )
    config_path.chmod(0o600)

    def token(identity: str, *, agent: bool = False) -> str:
        access = (api.AccessToken(key, secret).with_identity(identity)
                  .with_grants(api.VideoGrants(room_join=True, room=room_name,
                                              can_publish=True, can_subscribe=True,
                                              can_publish_data=True,
                                              can_update_own_metadata=agent)))
        if agent:
            access = access.with_kind("agent")
        return access.to_jwt()

    history: list[dict] = []

    class ObservedPipeline(ConversationPipeline):
        def configure_listening(self, configuration: ListeningConfiguration) -> None:
            super().configure_listening(configuration)
            history.append(configuration.payload())

    async def discard_output(message: dict) -> None:
        pass

    async def bootstrap(request: web.Request) -> web.Response:
        await request.json()
        return web.json_response({"serverUrl": url, "roomName": room_name,
                                  "participantToken": token(caller)})

    worker_room, intruder_room = rtc.Room(), rtc.Room()
    runner = web.AppRunner(web.Application())
    runner.app.router.add_post("/call", bootstrap)
    pipeline = ObservedPipeline(LabConfig(
        opening_greeting_enabled=False, memory_compaction_enabled=False,
        agent_config_api_base_url="", conversation_service_base_url="",
        billing_service_base_url="", knowledge_service_base_url="",
        trace_dir=directory, report_dir=directory,
    ), TraceRecorder(directory), discard_output, transport="livekit")
    with (directory / "livekit.log").open("wb") as server_log:
        server = await asyncio.create_subprocess_exec(
            server_binary, "--config", str(config_path), stdout=server_log, stderr=server_log)
        smoke = None
        try:
            await wait_for_server(signal_port, server)
            await asyncio.wait_for(worker_room.connect(
                url, token("rvc-livekit-comparison-test", agent=True)), 20)
            ready = asyncio.Event()
            ready.set()
            register_listening_rpc(worker_room.local_participant, pipeline,
                                   caller_identity=caller, ready=ready, input_lock=asyncio.Lock())
            await worker_room.local_participant.set_attributes({
                CAPABILITY_ATTRIBUTE: "1", "lk.agent.state": "listening"})

            # A different participant must not be able to alter this caller's settings.
            await asyncio.wait_for(intruder_room.connect(url, token("unrelated-participant")), 20)
            try:
                await intruder_room.local_participant.perform_rpc(
                    destination_identity="rvc-livekit-comparison-test", method=RPC_METHOD,
                    payload=json.dumps(ListeningConfiguration().payload()), response_timeout=5)
                raise AssertionError("Unrelated participant changed listening settings")
            except rtc.RpcError as exc:
                if exc.code != 403:
                    raise
            await intruder_room.disconnect()
            assert history == []

            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            env = os.environ.copy()
            env.update(ATTENTIVE_CALL_ENDPOINT=f"http://127.0.0.1:{port}/call",
                       ATTENTIVE_ALLOW_INSECURE="1", ATTENTIVE_BUSINESS_SLUG="local-fixture",
                       ATTENTIVE_AGENT_ID="agt_listening_fixture",
                       ATTENTIVE_CALLER_CONTACT="listening-test@example.invalid",
                       ATTENTIVE_VERIFY_LISTENING="1")
            smoke = await asyncio.create_subprocess_exec(str(binary), env=env,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(smoke.communicate(), 60)
            # SDK output contains event labels and preset names, not credentials.
            print(stdout.decode(), end="")
            if smoke.returncode:
                raise RuntimeError(f"Swift diagnostic failed ({smoke.returncode}); "
                                   f"stderr contained {len(stderr)} bytes")
            expected = [
                ListeningConfiguration("noisyEnvironment", "balanced").payload(),
                ListeningConfiguration("noisyEnvironment", "strict").payload(),
                ListeningConfiguration("standard", "responsive").payload(),
                ListeningConfiguration().payload(),
            ]
            assert history == expected, f"Unexpected effective-setting sequence: {history}"
            print("PASS: production RVC RPC applied all four configurations; unrelated caller rejected.")
        finally:
            if smoke is not None and smoke.returncode is None:
                smoke.terminate()
                await smoke.wait()
            await intruder_room.disconnect()
            await worker_room.disconnect()
            await runner.cleanup()
            pipeline.listening.close()
            await pipeline.http.close()
            if server.returncode is None:
                server.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(server.wait(), 5)
                if server.returncode is None:
                    server.kill()
                    await server.wait()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="attentive-listening-") as directory:
        asyncio.run(verify(Path(directory)))
