from __future__ import annotations

import asyncio
import json
import struct
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .action_tools import action_tool_definitions
from .agentic import AgentRuntimeContext, load_agent_runtime_context, session_routing_context
from .config import apply_runtime_overrides, lab_config_from_environ, load_platform_env
from .pipeline import ConversationPipeline
from .report import generate_report
from .trace import TraceRecorder

ROOT = Path(__file__).parent
STATIC = ROOT / "static"
HELEN_ASSETS = ROOT.parent.parent / "sales-girl-dashboard" / "public" / "assets"
load_platform_env()
CONFIG = lab_config_from_environ()
app = FastAPI(title="RVC Natural Conversation Latency Lab")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
if HELEN_ASSETS.exists():
    app.mount("/helen-assets", StaticFiles(directory=HELEN_ASSETS), name="helen-assets")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "pipeline": "rstt-rllm-rtts",
        "accepts_voice_lab_overrides": "true",
        "ack_then_work": "true",
    }


_SESSION_CONFIG_META_KEYS = {
    "type",
    "language",
    "agent_name",
    "configured_agent_name",
    "business_id",
    "config_agent_id",
    "agent_id",
    "end_user_email",
    "end_user_id",
    "runtime_overrides",
}


def _session_config_from_message(data: dict[str, Any]) -> LabConfig:
    overrides = data.get("runtime_overrides")
    if not isinstance(overrides, dict):
        overrides = {
            key: value
            for key, value in data.items()
            if key not in _SESSION_CONFIG_META_KEYS
        }
    return apply_runtime_overrides(
        lab_config_from_environ(),
        overrides,
        language=str(data.get("language") or ""),
    )


@app.get("/reports/latest")
async def latest_report():
    reports = sorted(CONFIG.report_dir.glob("*.md"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not reports:
        return JSONResponse({"error": "No report exists yet. Complete or stop a test session."}, status_code=404)
    return FileResponse(reports[0], media_type="text/markdown", filename=reports[0].name)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    config = lab_config_from_environ()
    routing = session_routing_context({})
    pending_message: dict[str, Any] | None = None
    try:
        first = await asyncio.wait_for(ws.receive(), timeout=0.5)
    except asyncio.TimeoutError:
        first = None
    if first and first.get("type") == "websocket.disconnect":
        return
    if first:
        first_text = first.get("text")
        if first_text:
            data = json.loads(first_text)
            if str(data.get("type") or "") == "session_config":
                config = _session_config_from_message(data)
                routing = session_routing_context(data)
            else:
                pending_message = first
        elif first.get("bytes"):
            pending_message = first
    trace = TraceRecorder(config.trace_dir)
    try:
        agent_context = await load_agent_runtime_context(
            room_name="",
            config=config,
            trace=trace,
            routing_context=routing,
        )
    except Exception as exc:
        trace.record(
            "agent_config_load_failed",
            reason="voice_lab_session_scoped_runtime_config",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        agent_context = AgentRuntimeContext(
            business_id=routing.business_id,
            agent_id=routing.agent_id,
            name=routing.configured_name,
            end_user_id=routing.end_user_id,
        )

    async def send(message: dict) -> None:
        await ws.send_json(message)

    pipeline = ConversationPipeline(config, trace, send, agent_context=agent_context)
    try:
        try:
            await pipeline.start()
        except Exception as exc:
            trace.record(
                "session_start_error",
                reason="rstt_startup_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            try:
                await ws.send_json(
                    {
                        "type": "lab_error",
                        "content": f"RSTT unavailable: {type(exc).__name__}: {exc}",
                    }
                )
                await ws.close(code=1013, reason="RSTT temporarily unavailable")
            except RuntimeError:
                pass
            return
        enabled_tools = [
            str(item.get("function", {}).get("name") or "").strip()
            for item in action_tool_definitions(agent_context)
            if str(item.get("function", {}).get("name") or "").strip()
        ]
        await ws.send_json(
            {
                "type": "session_id",
                "content": trace.session_id,
                "llm_model": config.llm_model,
                "llm_base_url": config.llm_base_url,
                "stt_model": config.stt_model,
                "stt_ws_url": config.stt_ws_url,
                "tts_model": config.tts_model,
                "tts_url": config.tts_url,
                "agent_config_loaded": agent_context.loaded,
                "agent_name": agent_context.name or routing.configured_name,
                "knowledge_base_count": len(agent_context.knowledge_base_ids),
                "enabled_tools": enabled_tools,
            }
        )
        messages = [pending_message] if pending_message else []
        while True:
            message = messages.pop(0) if messages else await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            raw = message.get("bytes")
            if raw:
                if len(raw) < 16:
                    trace.record("audio_packet_rejected", reason="shorter_than_header", bytes=len(raw))
                    continue
                client_ms, _flags, _sequence = struct.unpack("!dII", raw[:16])
                server_received_ms = time.time() * 1000.0
                await pipeline.audio(
                    raw[16:],
                    client_sent_ms=client_ms,
                    client_network_ms=round(server_received_ms - client_ms, 3),
                )
                continue
            text = message.get("text")
            if text:
                data = json.loads(text)
                await pipeline.client_event(str(data.get("type") or ""), data)
    except WebSocketDisconnect:
        pass
    finally:
        await pipeline.close()
        if any('"event": "user_speech_start"' in line for line in trace.path.read_text(encoding="utf-8").splitlines()):
            report = generate_report(trace.path, CONFIG.report_dir)
            print(f"RVC latency report: {report}")


def main() -> None:
    load_platform_env()
    uvicorn.run("latency_lab.server:app", host="127.0.0.1", port=8010, reload=False)


if __name__ == "__main__":
    main()
