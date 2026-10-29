"""Two-phase device-passkey confirmation for money-moving actions.

Phase A (``start``): the worker asks the banking connector to begin a ceremony
with Wema, publishes the ceremony to the signed-in device, and returns at once so
the conversation is never blocked while the customer uses Face ID.

Phase B (``receive``): the device sends the proof back over the private topic.
Only then does the worker call the banking connector's execute endpoint with the
ceremony id and proof. The customer's spoken "yes" can never approve a payment;
only Wema's acceptance can.

Nothing here verifies a proof, and no ceremony option, proof or device id is ever
logged or exposed to the model.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

TOPIC = "attentive.fido2.v1"
FEATURE_BY_WORKFLOW = {"transfer": "Transfer", "airtime": "Airtime"}
MAX_BYTES = 14_000

# The SDK's ceremony checker models only these option fields. Extra (still-standard)
# WebAuthn fields such as attestationFormats, hints and extensions make it reject the
# whole ceremony with reason `unsupported_options` at check=options (verified against
# the 0.1.5 binary's failure enum + field list), so forward only what it models.
_OPTION_KEYS = {
    "challenge", "rp", "rpId", "user", "pubKeyCredParams", "excludeCredentials",
    "allowCredentials", "authenticatorSelection", "attestation", "timeout",
    "authenticatorAttachment", "userVerification",
}
_AUTHENTICATOR_SELECTION_KEYS = {"residentKey", "requireResidentKey", "userVerification",
                                 "authenticatorAttachment"}


def supported_options(options: dict) -> dict:
    """Trim a ceremony options object to the fields the SDK's validator accepts."""
    if not isinstance(options, dict):
        return {}
    if str(os.getenv("ATTENTIVE_PASSKEY_TRIM_OPTIONS", "1")).strip().lower() in {"0", "false", "no", "off"}:
        return options
    trimmed = {key: value for key, value in options.items() if key in _OPTION_KEYS}
    selection = trimmed.get("authenticatorSelection")
    if isinstance(selection, dict):
        # Seen live: the SDK rejects the selection the bank returns, at check=authenticatorSelection
        # (`userVerification: discouraged` + the legacy requireResidentKey). A passkey confirming a
        # money move must verify the user, so require it and drop the deprecated key.
        cleaned = {key: value for key, value in selection.items() if key in _AUTHENTICATOR_SELECTION_KEYS}
        cleaned.pop("requireResidentKey", None)
        # The SDK's model is platform-only (no `cross-platform` anywhere in the 0.1.5 binary)
        # and it decodes `residentKey`; a device passkey must be discoverable, so require it.
        cleaned.pop("authenticatorAttachment", None)
        cleaned["residentKey"] = "required"
        cleaned["userVerification"] = "required"
        trimmed["authenticatorSelection"] = cleaned
    return trimmed


@dataclass
class Pending:
    authentication_id: str
    correlation_id: str
    operation_id: str
    workflow: str
    feature: str
    expires: float
    consumed: bool = False


class PasskeyFlow:
    def __init__(
        self,
        *,
        send: Callable[[dict], Awaitable[None]],
        post_json: Callable[[str, dict], Awaitable[dict]],
        on_outcome: Callable[[str, dict], Awaitable[None]] | None = None,
        trace: Callable[..., None] | None = None,
        timeout: float = 90.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.send = send
        self.post_json = post_json
        self.on_outcome = on_outcome
        self.trace = trace
        self.timeout = timeout
        self.clock = clock
        self.device_id: str | None = None
        self.pending: Pending | None = None
        self.closed = False
        self._tasks: list[asyncio.Task] = []

    def _record(self, event: str, **data) -> None:
        """Trace what the app reported (never the proof material)."""
        if self.trace is None:
            return
        try:
            self.trace.record(event, **data)
        except Exception:
            pass

    # -- phase A -----------------------------------------------------------
    async def start(
        self, *, workflow: str, operation_id: str, customer_id: str, display_name: str
    ) -> dict:
        feature = FEATURE_BY_WORKFLOW.get(workflow)
        if feature is None:
            return {"status": "blocked", "message": "That action cannot be confirmed with a passkey."}
        if self.closed:
            return {"status": "failed", "message": "This call can no longer confirm actions."}
        if self.pending and not self.pending.consumed:
            return {"status": "busy", "message": "A passkey confirmation is already waiting."}
        try:
            reply = await self.post_json(
                "/v1/fido2/begin",
                {
                    "feature": feature,
                    "device_imei": self.device_id or "unknown-device",
                    "display_name": display_name or customer_id,
                },
            )
        except Exception:
            return {
                "status": "failed",
                "message": "I could not start the passkey confirmation right now.",
            }
        if not isinstance(reply, dict) or reply.get("status") != "ok":
            return {
                "status": "failed",
                "message": "I could not start the passkey confirmation right now.",
            }
        data = reply.get("data") or {}
        authentication_id = str(data.get("authentication_id") or "").strip()
        options = data.get("options")
        if not authentication_id or not isinstance(options, dict):
            return {"status": "failed", "message": "I could not start the passkey confirmation right now."}
        self.pending = Pending(
            authentication_id=authentication_id,
            # A retry for the same prepared operation is a new ceremony. The SDK
            # remembers prior correlation IDs for the connection and rejects reuse.
            correlation_id=f"ceremony-{uuid.uuid4().hex}",
            operation_id=operation_id,
            workflow=workflow,
            feature=feature,
            expires=self.clock() + self.timeout,
        )
        await self.send(
            {
                "type": "fido2_ceremony",
                "authenticationId": authentication_id,
                "correlationId": self.pending.correlation_id,
                "action": int(data.get("action") or 0),
                "feature": feature,
                "mode": str(data.get("ceremony_mode") or "authenticate"),
                "expiresAt": _iso(time.time() + self.timeout),
                "options": supported_options(options),
                "usePasskey": True,
            }
        )
        return {
            "status": "ceremony_started",
            "authentication_id": authentication_id,
            "message": "I've sent the approval to your app. Please confirm it with your Face ID, then I'll finish it.",
        }

    def awaiting(self) -> bool:
        return bool(self.pending and not self.pending.consumed and self.clock() < self.pending.expires)

    # -- phase B -----------------------------------------------------------
    async def receive(self, raw: bytes) -> None:
        if self.closed or len(raw) > MAX_BYTES:
            return
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            return
        if not isinstance(value, dict):
            return
        if value.get("type") == "session_context":
            device = value.get("deviceImei")
            # Opaque device id from the host app; never logged, never trusted as identity.
            self.device_id = str(device) if isinstance(device, str) and device.strip() else None
            self._record("passkey_session_context", device_present=bool(self.device_id))
            return
        kind = value.get("type")
        if isinstance(kind, str) and kind.startswith("fido2_"):
            # Capture what the app actually reported (never the proof material) so an
            # app-side failure is diagnosable from the trace alone.
            detail = {
                "kind": kind,
                "authentication_id_match": bool(
                    self.pending and value.get("authenticationId") == self.pending.authentication_id),
                "correlation_id_match": bool(
                    self.pending and value.get("correlationId") == self.pending.correlation_id),
            }
            # Record every top-level key the app sent, with its type, so nothing is missed.
            detail["all_keys"] = sorted(str(k) for k in value)
            detail["types"] = {str(k): type(v).__name__ for k, v in value.items()}
            # The SDK reports its own diagnostic as NESTED objects (reason/diagnostics/
            # systemError) — record those FIRST and unconditionally (compact JSON, never
            # proof material) so a nested value can never be shadowed by a scalar pass.
            for key in ("reason", "diagnostics", "systemError", "failure", "error", "fields"):
                if key in value:
                    # NB: the trace event's OWN `reason` field is reserved by the recorder, so a
                    # `reason` kwarg here would be swallowed — store it as `app_reason`.
                    target = "app_reason" if key == "reason" else key
                    try:
                        detail[target] = json.dumps(value[key], separators=(",", ":"), default=str)[:1200]
                    except Exception:
                        detail[target] = repr(value[key])[:1200]
            for key in ("error", "errorCode", "code", "message", "domain",
                        "name", "status", "debugDescription", "userCancelled", "cancelled"):
                if key in value and key not in detail and isinstance(value[key], (str, int, float, bool)):
                    detail[key] = str(value[key])[:300]
            for key, item in value.items():
                if key != "type" and key not in detail and isinstance(item, (str, int, float, bool)):
                    detail[str(key)] = str(item)[:200]
            self._record("passkey_app_message", **detail)
        pending = self.pending
        if (
            not pending
            or pending.consumed
            or value.get("authenticationId") != pending.authentication_id
            or value.get("correlationId") != pending.correlation_id
        ):
            return
        kind = value.get("type")
        if kind in {"fido2_cancelled", "fido2_failed"}:
            pending.consumed = True
            await self._finish(
                "cancelled" if kind == "fido2_cancelled" else "failed", pending
            )
            return
        if kind != "fido2_proof":
            return
        proof = value.get("proof")
        if not isinstance(proof, dict) or not proof:
            return
        pending.consumed = True  # consume even a rejected proof: one proof, one use
        if self.clock() >= pending.expires:
            await self._finish("expired", pending)
            return
        try:
            reply = await self.post_json(
                "/v1/tools/wema_execute_prepared",
                {
                    "operation_id": pending.operation_id,
                    "authentication_id": pending.authentication_id,
                    "fido2_credential_proof": proof,
                },
            )
        except Exception:
            reply = None
        accepted = bool(
            isinstance(reply, dict)
            and reply.get("status") in {"ok", "success", "completed", "pending", "unknown"}
        )
        await self.send(
            {
                "type": "fido2_result",
                "authenticationId": pending.authentication_id,
                "correlationId": pending.correlation_id,
                "accepted": accepted,
                "outcome": "executed" if accepted else "rejected",
            }
        )
        await self._finish("verified" if accepted else "rejected", pending, reply)

    async def _finish(self, status: str, pending: Pending, reply: dict | None = None) -> None:
        self.pending = None
        message = ""
        if isinstance(reply, dict):
            message = str(reply.get("message") or "")
        self._record("passkey_outcome", status=status, reply_message=message[:200])
        if self.on_outcome is None:
            return
        text = {
            "cancelled": "No problem — nothing was approved, and we can keep talking.",
            "failed": "The passkey confirmation did not go through. Nothing was approved.",
            "expired": "That approval timed out, so nothing went through. Ask me to try again when you're ready.",
            "rejected": message or "The bank did not accept the approval, so nothing was sent.",
            "verified": message or "Approved and sent.",
        }.get(status, "")
        if text:
            await self.on_outcome(text, {"status": status, "reply": reply or {}})

    async def close(self) -> None:
        self.closed = True
        self.pending = None
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


def _canon(value) -> str:
    """Normalise an id so dashed and undashed UUID forms compare equal."""
    return str(value or "").strip().lower().replace("-", "")


def start_requested(text: str) -> bool:
    """True when the caller clearly agrees to run the passkey check.

    Used by the isolation agent so the WORKER starts the ceremony instead of
    relying on the model's tool choice (the model otherwise claims it started
    the check without ever calling the tool).
    """
    normalized = " ".join(re.sub(r"[^a-z ]", " ", str(text or "").lower()).split())
    if not normalized:
        return False
    if re.search(r"\b(?:no|nope|not|dont|stop|wait|hold|cancel|later)\b", normalized):
        return False
    return bool(re.search(
        r"\b(?:yes|yeah|yep|sure|okay|ok|alright|go ahead|start|begin|do it|run it|"
        r"passkey|pass key|continue|proceed)\b", normalized))


def enabled(identity: dict) -> bool:
    """Only the configured test workspace/agent may start a real passkey flow.

    Signing the ceremony is pointless until the app's domain association exists,
    so this stays empty (disabled) until an operator enables a specific workspace.
    """
    businesses = {_canon(x) for x in str(os.getenv("ATTENTIVE_PASSKEY_REAL_BUSINESS_IDS", "")).split(",") if x.strip()}
    agents = {_canon(x) for x in str(os.getenv("ATTENTIVE_PASSKEY_REAL_AGENT_IDS", "")).split(",") if x.strip()}
    return bool(
        _canon(identity.get("business_id")) in businesses
        and _canon(identity.get("config_agent_id")) in agents
    )


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()
