"""Short-lived, single-use dashboard bootstrap for tool-capable RVC sessions."""

from __future__ import annotations

import base64
import hmac
import json
import time
from typing import Any


_used: dict[str, float] = {}


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify_session_token(token: str, service_token: str) -> dict[str, Any]:
    if not service_token or len(token) > 16384:
        raise ValueError("Invalid call bootstrap.")
    try:
        encoded, signature = token.split(".")
        key = hmac.digest(service_token.encode(), b"odion-rvc-session-v1", "sha256")
        expected = hmac.digest(key, encoded.encode("ascii"), "sha256")
        if not hmac.compare_digest(expected, _decode(signature)):
            raise ValueError("Invalid signature.")
        claims = json.loads(_decode(encoded))
        now = time.time()
        expiry = float(claims["exp"])
        nonce = claims["nonce"]
        if claims["aud"] != "odion-rvc-tools" or not now < expiry <= now + 180:
            raise ValueError("Expired call bootstrap.")
        if not isinstance(nonce, str) or not 16 <= len(nonce) <= 128:
            raise ValueError("Invalid nonce.")
        for name in ("business_id", "config_agent_id", "end_user_id"):
            if not isinstance(claims.get(name), str) or not claims[name].strip():
                raise ValueError("Missing call scope.")
        for old in [key for key, end in _used.items() if end <= now]:
            del _used[old]
        if nonce in _used:
            raise ValueError("Call bootstrap already used.")
        _used[nonce] = expiry
        return claims
    except (KeyError, TypeError, UnicodeError, ValueError) as exc:
        raise ValueError("Invalid or expired call bootstrap. Start a new call.") from exc
