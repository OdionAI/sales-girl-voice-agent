from __future__ import annotations

import os
import secrets

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import ValidationError

from .contracts import TOOL_SPECS, ToolResult, tool_definitions
from .live_bank import LiveBank
from .service import MockSession, WemaWorkflows, WorkflowError, result


def create_app(
    *, token: str | None = None, workflows: WemaWorkflows | None = None,
    mode: str | None = None,
) -> FastAPI:
    service_token = token if token is not None else os.getenv("WEMA_TOOLS_SERVICE_TOKEN", "")
    if len(service_token) < 24:
        raise RuntimeError("Set WEMA_TOOLS_SERVICE_TOKEN to a random secret of at least 24 characters.")
    selected_mode = str(mode or os.getenv("WEMA_BANK_MODE", "mock")).strip().lower()
    if selected_mode not in {"mock", "live"}:
        raise RuntimeError("WEMA_BANK_MODE must be mock or live.")
    service = workflows if workflows is not None else WemaWorkflows(
        LiveBank() if selected_mode == "live" else None
    )
    app = FastAPI(title="Wema composite banking tools", version="0.2.0")

    def authorize(request: Request):
        supplied = request.headers.get("X-Service-Token", "")
        if not secrets.compare_digest(supplied.encode(), service_token.encode()):
            raise HTTPException(status_code=401, detail="Invalid service token.")

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": service.mode, "bank_writes_enabled": False}

    @app.get("/v1/tool-definitions", dependencies=[Depends(authorize)])
    def definitions(request: Request):
        return {
            "mode": service.mode,
            "tools": tool_definitions(str(request.base_url)),
            "setup_note": "Add X-Service-Token as a server-side custom header for each tool. "
                          "Live account workflows also require X-Wema-Customer-Id from trusted session context.",
        }

    @app.post("/v1/tools/{tool_name}", response_model=ToolResult, dependencies=[Depends(authorize)])
    async def invoke(tool_name: str, request: Request):
        if tool_name not in TOOL_SPECS:
            raise HTTPException(status_code=404, detail="Unknown Wema tool.")
        context = [request.headers.get(header, "").strip() for header in (
            "X-Business-Id", "X-Agent-Id", "X-Session-Id", "X-End-User-Id",
        )]
        if not all(context):
            raise HTTPException(status_code=400, detail="Missing business/agent/session/caller metadata.")
        model = TOOL_SPECS[tool_name][0]
        try:
            args = model.model_validate_json(await request.body())
        except ValidationError as exc:
            # Do not echo arbitrary supplied values, which could include secrets.
            return result("failed", "Invalid tool arguments.", mode=service.mode, errors=[
                {"field": ".".join(map(str, e["loc"])), "type": e["type"]}
                for e in exc.errors(include_input=False, include_url=False)
            ])
        try:
            customer_id = request.headers.get("X-Wema-Customer-Id", "").strip()
            return await getattr(service, tool_name)(MockSession(*context, customer_id=customer_id), args)
        except WorkflowError as exc:
            return result("failed", str(exc), mode=service.mode)

    return app
