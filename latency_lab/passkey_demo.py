"""Opt-in, call-scoped simulated bank. Native credentials; no banking authorization."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from webauthn import verify_authentication_response, verify_registration_response
from webauthn.helpers.cose import COSEAlgorithmIdentifier

TOPIC = "attentive.fido2.v1"  # Private transport contract; never part of customer SDK APIs.
TOOL = "attentive_passkey_demo"
MAX_BYTES = 14_000


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def requested(text: str) -> bool:
    text = re.sub(r"[^a-z ]", " ", text.lower()).replace("pass key", "passkey")
    text = " ".join(text.split())
    return bool(re.fullmatch(
        r"(?:(?:please|can we|could we|can you|could you|let s|i want to|i would like to) )?"
        r"(?:test (?:my |the |a )?passkey|run (?:the |a )?passkey (?:test|demo)|verify my demo passkey)"
        r"(?: please| now)?", text))


def enabled(identity: dict) -> bool:
    """Only authenticated server dispatch can select an enabled agent and test subject."""
    businesses = set(os.getenv("ATTENTIVE_PASSKEY_DEMO_BUSINESS_IDS", "").split(","))
    agents = set(os.getenv("ATTENTIVE_PASSKEY_DEMO_AGENT_IDS", "").split(","))
    subjects = set(os.getenv("ATTENTIVE_PASSKEY_DEMO_SUBJECTS", "").split(","))
    return bool(identity.get("business_id") in businesses and identity.get("config_agent_id") in agents
                and identity.get("end_user_id") in subjects)


@dataclass
class Credential:
    id: bytes
    public_key: bytes
    count: int
    user: bytes


@dataclass
class Pending:
    authentication_id: str
    correlation_id: str
    mode: str
    challenge: bytes
    expires: float
    future: asyncio.Future
    consumed: bool = False


class DemoSession:
    def __init__(self, send: Callable[[dict], Awaitable[None]], *, rp_id: str,
                 origin: str, timeout: float = 90):
        parsed = urlsplit(origin)
        if (parsed.scheme != "https" or parsed.hostname != rp_id or parsed.port
                or parsed.username or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*\.[a-z]{2,}", rp_id)
                or not 1 <= timeout <= 120):
            raise ValueError("Invalid trusted demo origin or timeout")
        self.send, self.rp_id, self.origin, self.timeout = send, rp_id, origin.rstrip("/"), timeout
        self.context_id: str | None = None
        self.credential: Credential | None = None
        self.user = secrets.token_bytes(32)
        self.pending: Pending | None = None
        self.running = False
        self.closed = False
        self.requests = 0

    async def receive(self, raw: bytes) -> None:
        if self.closed or len(raw) > MAX_BYTES:
            return
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            return
        if not isinstance(value, dict) or ("version" in value and type(value["version"]) is not int):
            return
        if value.get("version", 1) != 1:
            return
        if value.get("type") == "session_context":
            correlation = value.get("correlationId")
            if not isinstance(correlation, str) or not 1 <= len(correlation.encode()) <= 256:
                return
            if self.context_id and self.context_id != correlation:
                self._finish("interrupted")
            self.context_id = correlation
            # The optional device identifier is not an identity credential; do not store or log it.
            return
        current = self.pending
        if (not current or current.consumed or value.get("authenticationId") != current.authentication_id
                or value.get("correlationId") != current.correlation_id):
            return
        kind = value.get("type")
        if kind in {"fido2_cancelled", "fido2_failed"}:
            current.consumed = True
            self._finish("cancelled" if kind == "fido2_cancelled" else "native_failed")
        elif kind == "fido2_proof":
            current.consumed = True  # Consume even rejected, malformed and expired proofs.
            accepted = False
            if time.monotonic() < current.expires:
                try:
                    proof = value["proof"]
                    if not isinstance(proof, dict):
                        raise ValueError("Invalid proof")
                    if current.mode == "register":
                        result = verify_registration_response(
                            credential=proof, expected_challenge=current.challenge,
                            expected_rp_id=self.rp_id, expected_origin=self.origin,
                            require_user_presence=True, require_user_verification=True,
                            supported_pub_key_algs=[COSEAlgorithmIdentifier.ECDSA_SHA_256])
                        candidate = Credential(result.credential_id, result.credential_public_key,
                                                     result.sign_count, self.user)
                    else:
                        stored = self.credential
                        if stored is None or proof.get("id") != b64(stored.id) or proof.get("rawId") != b64(stored.id):
                            raise ValueError("Unknown credential")
                        handle = proof.get("response", {}).get("userHandle")
                        if handle is not None and handle != b64(stored.user):
                            raise ValueError("Wrong credential owner")
                        result = verify_authentication_response(
                            credential=proof, expected_challenge=current.challenge,
                            expected_rp_id=self.rp_id, expected_origin=self.origin,
                            credential_public_key=stored.public_key, credential_current_sign_count=stored.count,
                            require_user_verification=True)
                        candidate = Credential(stored.id, stored.public_key, result.new_sign_count, stored.user)
                    accepted = time.monotonic() < current.expires and not self.closed
                    if accepted:
                        self.credential = candidate
                except Exception:
                    # Never emit credential data, decoder errors or proof material to logs/the model.
                    accepted = False
            await self.send({"type": "fido2_result", "authenticationId": current.authentication_id,
                             "correlationId": current.correlation_id, "accepted": accepted,
                             "outcome": "demo_verified" if accepted else "demo_rejected"})
            self._finish("verified" if accepted else "rejected")

    def _finish(self, status: str) -> None:
        if self.pending:
            self.pending.consumed = True
            if not self.pending.future.done():
                self.pending.future.set_result(status)

    async def _ceremony(self, mode: str) -> str:
        if self.closed or self.requests >= 6:
            return "unavailable"
        self.requests += 1
        current = Pending(str(uuid.uuid4()), str(uuid.uuid4()), mode, secrets.token_bytes(32),
                          time.monotonic() + self.timeout, asyncio.get_running_loop().create_future())
        self.pending = current
        options = {"challenge": b64(current.challenge), "timeout": int(self.timeout * 1000)}
        if mode == "register":
            options.update({"rp": {"id": self.rp_id, "name": "Attentive simulated bank"},
                            "user": {"id": b64(self.user), "name": "demo-" + uuid.uuid4().hex[:10],
                                     "displayName": "Attentive demo — no bank account"},
                            "pubKeyCredParams": [{"type": "public-key", "alg": -7}],
                            "authenticatorSelection": {"authenticatorAttachment": "platform",
                                                       "residentKey": "required", "userVerification": "required"},
                            "attestation": "none"})
        else:
            options.update({"rpId": self.rp_id, "userVerification": "required",
                            "allowCredentials": [{"id": b64(self.credential.id), "type": "public-key"}]})
        try:
            await self.send({"type": "fido2_ceremony", "authenticationId": current.authentication_id,
                             "correlationId": current.correlation_id, "action": 1,
                             "feature": "AttentiveDemoOnly", "mode": mode,
                             "expiresAt": datetime.fromtimestamp(time.time() + self.timeout, timezone.utc).isoformat(),
                             "options": options})
            return await asyncio.wait_for(asyncio.shield(current.future), self.timeout)
        except (TimeoutError, asyncio.CancelledError):
            try:
                await self.send({"type": "fido2_abort", "authenticationId": current.authentication_id,
                                 "correlationId": current.correlation_id})
            finally:
                if not current.future.done():
                    current.future.cancel()
            if asyncio.current_task().cancelling():
                raise
            return "expired"
        finally:
            if self.pending is current:
                self.pending = None

    async def run(self, progress: Callable[[str], Awaitable[None]]) -> str:
        if self.closed or not self.context_id:
            return "The passkey demo is not available in this app. Check the app and domain setup. No test was approved."
        if self.running:
            return "A passkey test is already waiting. Complete or cancel the current system sheet first."
        self.running = True
        try:
            if not self.credential:
                await progress("This is a simulated bank test, with no money movement. First, create a demo passkey in Apple's sheet.")
                status = await self._ceremony("register")
                if status != "verified":
                    return self.message(status)
            await progress("Now use your passkey to verify this simulated test in Apple's sheet.")
            return self.message(await self._ceremony("authenticate"))
        finally:
            self.running = False

    @staticmethod
    def message(status: str) -> str:
        if status == "verified":
            return "Your passkey proof passed the simulated bank verification. This was a demo only. No bank transaction was authorized."
        if status == "cancelled":
            return "The passkey test was cancelled. Nothing was approved, and we can keep talking."
        if status == "expired":
            return "The passkey test expired. Nothing was approved. Ask to test your passkey again when you're ready."
        return "The passkey test did not pass. Nothing was approved. Check the app setup, then request a fresh test."

    async def close(self) -> None:
        self.closed = True
        self._finish("ended")
        self.credential = None
        self.context_id = None
