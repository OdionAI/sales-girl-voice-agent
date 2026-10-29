"""Bounded passkey progress/failure labels. Never log caller payloads or exceptions."""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)
STAGES = frozenset({"configuration", "connection", "context", "receipt", "validation", "presentation",
                    "proof", "verification", "cleanup", "begin", "ceremony_send", "execution"})
OUTCOMES = frozenset({"started", "requested", "succeeded", "failed", "ignored", "cancelled"})
REASONS = frozenset({"invalid_request", "unsupported_options", "untrusted_relying_party", "expired", "busy",
                     "replayed", "unavailable", "presentation_unavailable", "cancelled", "authorization_failed",
                     "invalid_credential", "begin_failed", "send_failed", "unknown"})
CHECKS = frozenset({"envelope", "options", "challenge", "credentials", "registrationIdentity", "algorithms",
                    "authenticatorSelection", "relyingParty", "presentationAnchor", "foregroundScene",
                    "visibleWindow"})
DOMAINS = frozenset({"authorization", "localAuthentication", "other"})


def _label(value, permitted, default="unknown"):
    return value if isinstance(value, str) and value in permitted else default


def client_failure(value: dict) -> dict:
    """Whitelist even a malicious client's fields; never stringify arbitrary values."""
    fields = {"reason": _label(value.get("reason"), REASONS)}
    details = value.get("diagnostics")
    if isinstance(details, dict):
        if isinstance(details.get("stage"), str) and details["stage"] in STAGES:
            fields["client_stage"] = details["stage"]
        if isinstance(details.get("check"), str) and details["check"] in CHECKS:
            fields["check"] = details["check"]
        if isinstance(details.get("systemDomain"), str) and details["systemDomain"] in DOMAINS:
            fields["system_domain"] = details["systemDomain"]
        code = details.get("systemCode")
        if type(code) is int and -(2**31) <= code < 2**31:
            fields["system_code"] = code
    return fields


def record(stage: str, outcome: str, fields: dict | None = None, *, trace=None) -> None:
    if os.getenv("ATTENTIVE_PASSKEY_DIAGNOSTICS_ENABLED", "0") != "1":
        return
    clean = {"stage": _label(stage, STAGES), "outcome": _label(outcome, OUTCOMES)}
    if isinstance(fields, dict):
        # Internal callers use this function too; apply the same final whitelist.
        clean.update(client_failure({"reason": fields.get("reason"), "diagnostics": {
            "stage": fields.get("client_stage"), "check": fields.get("check"),
            "systemDomain": fields.get("system_domain"), "systemCode": fields.get("system_code")}}))
        code = fields.get("http_status")
        if type(code) is int and 100 <= code <= 599:
            clean["http_status"] = code
    logger.info("passkey_diagnostic %s", json.dumps(clean, sort_keys=True))
    if trace is not None:
        trace.record("passkey_diagnostic", **clean)
