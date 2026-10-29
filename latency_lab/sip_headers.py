"""Read selected SIP headers from a LiveKit SIP participant."""
from __future__ import annotations

import asyncio
import json
from typing import Any


async def sip_remote_headers(ctx: Any, participant: Any) -> dict[str, str]:
    rpc = getattr(getattr(ctx.room, "local_participant", None), "perform_rpc", None)
    if not callable(rpc):
        return {}
    try:
        result = await asyncio.wait_for(
            rpc(
                destination_identity=participant.identity,
                method="lk.sip.GetRemoteHeaders",
                payload=json.dumps({"include": ["To", "X-Odion-Capacity-Reservation"]}),
            ), timeout=2.0,
        )
    except Exception:
        return {}
    try:
        headers = json.loads(result) if isinstance(result, str) else result
    except (TypeError, ValueError):
        return {}
    values = headers.get("headers", {}) if isinstance(headers, dict) else {}
    if not isinstance(values, dict):
        return {}
    return {str(key).lower(): str(value or "").strip() for key, value in values.items()}
