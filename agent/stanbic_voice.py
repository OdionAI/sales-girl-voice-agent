"""Stanbic voice-biometrics demo: caller-audio tap + enrollment/gated tools.

Adds, for agents listed in STANBIC_VOICE_AGENT_IDS:
  - a rolling caller-audio buffer fed by a transparent STT wrapper
  - check_voice_enrollment / create_demo_account / capture_voice_sample
  - get_account_details — voice-gated: verifies the caller's live audio
    against their enrolled voiceprint before returning anything.

The voiceprint store and demo accounts live in the voice-auth sidecar
(VOICE_AUTH_SIDECAR_URL); this module only captures audio and calls it.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import time
import wave
from collections import deque
from typing import Any

import aiohttp
from livekit.agents import RunContext, function_tool, stt
from livekit.agents.types import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
    NotGivenOr,
)

logger = logging.getLogger(__name__)

TAP_SECONDS = 30.0
CAPTURE_SECONDS = 8.0
VOICE_MONITOR_INTERVAL = 10.0
VOICE_MONITOR_MAX_SECONDS = 3600.0
RECENT_MISMATCH_WINDOW = 15.0
STRONG_MATCH = 0.72


def stanbic_voice_agent_ids() -> list[str]:
    raw = str(os.getenv("STANBIC_VOICE_AGENT_IDS") or "")
    return {
        part.strip().replace("-", "")
        for part in raw.split(",")
        if part.strip()
    }


def _norm_agent_id(value: str) -> str:
    return str(value or "").strip().replace("-", "").lower()


def stanbic_voice_enabled(userdata: dict[str, Any] | None) -> bool:
    if not userdata:
        return False
    agent_id = _norm_agent_id(
        str(userdata.get("agent_config_id") or userdata.get("agent_id") or "")
    )
    return agent_id in stanbic_voice_agent_ids()


def voice_sidecar_url() -> str:
    return str(os.getenv("VOICE_AUTH_SIDECAR_URL") or "http://127.0.0.1:8098").rstrip("/")


def stanbic_verify_threshold() -> float:
    """Demo-scoped voice-match threshold (does NOT touch the Wema gate)."""
    try:
        return float(os.getenv("STANBIC_VOICE_VERIFY_THRESHOLD") or "0.65")
    except ValueError:
        return 0.65


def caller_key(userdata: dict[str, Any]) -> str:
    """The caller's voice-auth key: their email, or their phone as E.164."""
    raw = str(userdata.get("end_user_id") or "").strip()
    if "@" in raw:
        return raw.lower()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return ""
    if raw.startswith("+"):
        return "+" + digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith("0"):
        return "+234" + digits[1:]
    return "+" + digits


class CallerAudioTap:
    """Rolling buffer of caller PCM (16 kHz mono s16le)."""

    def __init__(self) -> None:
        self._frames: deque[tuple[float, bytes]] = deque()
        self.pushed = 0

    def push(self, frame: Any) -> None:
        try:
            data = bytes(getattr(frame, "data", b"") or b"")
            if not data:
                return
            self._frames.append((time.monotonic(), data))
            self.pushed += 1
            cutoff = time.monotonic() - TAP_SECONDS
            while self._frames and self._frames[0][0] < cutoff:
                self._frames.popleft()
        except Exception:
            logger.exception("[STANBIC] tap push failed")

    def wav_bytes(self, seconds: float = CAPTURE_SECONDS) -> bytes:
        cutoff = time.monotonic() - seconds
        payload = b"".join(data for ts, data in self._frames if ts >= cutoff)
        if not payload:
            return b""
        out = io.BytesIO()
        with wave.open(out, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            wav_file.writeframes(payload)
        return out.getvalue()


class _TapStream:
    """Delegates to the real RecognizeStream, teeing caller frames to the tap."""

    def __init__(self, real: Any, tap: CallerAudioTap) -> None:
        self._real = real
        self._tap = tap

    def push_frame(self, frame: Any) -> None:
        self._tap.push(frame)
        return self._real.push_frame(frame)

    def end_input(self) -> None:
        return self._real.end_input()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def __aiter__(self) -> Any:
        return self._real.__aiter__()

    async def __anext__(self) -> Any:
        return await self._real.__anext__()

    async def __aenter__(self) -> "_TapStream":
        await self._real.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> Any:
        return await self._real.__aexit__(*args)


class VoiceTapSTT(stt.STT):
    """Transparent STT wrapper that copies caller audio into a tap."""

    def __init__(self, inner: Any, tap: CallerAudioTap) -> None:
        super().__init__(capabilities=inner.capabilities)
        self._inner = inner
        self._tap = tap

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> Any:
        real = self._inner.stream(language=language, conn_options=conn_options)
        return _TapStream(real, self._tap)

    async def _recognize_impl(
        self,
        buffer: Any,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> Any:
        return await self._inner.recognize(
            buffer, language=language, conn_options=conn_options
        )


def ensure_voice_tap(userdata: dict[str, Any]) -> CallerAudioTap | None:
    """Create the session's audio tap if this session should have one."""
    if not stanbic_voice_enabled(userdata):
        return None
    tap = userdata.get("stanbic_voice_tap")
    if not isinstance(tap, CallerAudioTap):
        tap = CallerAudioTap()
        userdata["stanbic_voice_tap"] = tap
    return tap


def wrap_stt_with_voice_tap(
    stt_engine: Any, userdata: dict[str, Any]
) -> Any:
    tap = ensure_voice_tap(userdata)
    if tap is None:
        return stt_engine
    logger.info("[STANBIC] voice tap active for this session")
    return VoiceTapSTT(stt_engine, tap)


# ---------------------------------------------------------------------------
# Sidecar client
# ---------------------------------------------------------------------------

async def _sidecar_status(key: str) -> dict[str, Any]:
    url = f"{voice_sidecar_url()}/v1/voice-auth/status"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params={"key": key}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            payload = await resp.json()
            payload["http_status"] = resp.status
            return payload


async def _sidecar_enroll(key: str, wav_bytes: bytes) -> dict[str, Any]:
    url = f"{voice_sidecar_url()}/v1/voice-auth/enroll"
    form = aiohttp.FormData()
    form.add_field("key", key)
    form.add_field(
        "audio", wav_bytes, filename="caller.wav", content_type="audio/wav"
    )
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            payload = await resp.json()
            payload["http_status"] = resp.status
            return payload


async def _sidecar_verify(key: str, wav_bytes: bytes) -> dict[str, Any]:
    url = f"{voice_sidecar_url()}/v1/voice-auth/verify"
    form = aiohttp.FormData()
    form.add_field("key", key)
    form.add_field("threshold", f"{stanbic_verify_threshold():.4f}")
    form.add_field(
        "audio", wav_bytes, filename="caller.wav", content_type="audio/wav"
    )
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            payload = await resp.json()
            payload["http_status"] = resp.status
            return payload


async def _demo_create(key: str, full_name: str) -> dict[str, Any]:
    url = f"{voice_sidecar_url()}/v1/demo/accounts"
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            json={"key": key, "full_name": full_name},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            payload = await resp.json()
            payload["http_status"] = resp.status
            return payload


async def _demo_get(key: str) -> dict[str, Any]:
    url = f"{voice_sidecar_url()}/v1/demo/accounts"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params={"key": key}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            payload = await resp.json()
            payload["http_status"] = resp.status
            return payload


def _tap(userdata: dict[str, Any]) -> CallerAudioTap | None:
    tap = userdata.get("stanbic_voice_tap")
    return tap if isinstance(tap, CallerAudioTap) else None


async def _voice_monitor_loop(userdata: dict[str, Any]) -> None:
    """Continuously validate the caller's voice during the call.

    Every VOICE_MONITOR_INTERVAL seconds, if the caller has spoken new
    audio, verify the recent speech against their enrolled voiceprint and
    record the verdict in userdata["stanbic_voice_status"]. The account
    details gate consults this: a recent mismatch raises the bar.
    """
    tap = _tap(userdata)
    key = caller_key(userdata)
    if tap is None or not key:
        return
    started = time.monotonic()
    last_seen_pushed = tap.pushed
    enrolled_known = bool((userdata.get("stanbic_voice_status") or {}).get("enrolled"))
    try:
        while time.monotonic() - started < VOICE_MONITOR_MAX_SECONDS:
            await asyncio.sleep(VOICE_MONITOR_INTERVAL)
            if tap.pushed == last_seen_pushed:
                continue  # no new caller speech since last check
            last_seen_pushed = tap.pushed
            if not enrolled_known:
                status = await _sidecar_status(key)
                enrolled_known = bool(status.get("enrolled"))
                if not enrolled_known:
                    continue  # nothing to verify against yet
            wav = tap.wav_bytes(CAPTURE_SECONDS)
            if not wav:
                continue
            result = await _sidecar_verify(key, wav)
            matched = bool(result.get("matched")) and result.get("http_status") == 200
            userdata["stanbic_voice_status"] = {
                "enrolled": True,
                "matched": matched,
                "score": result.get("score"),
                "threshold": result.get("threshold"),
                "at": time.monotonic(),
            }
            logger.info(
                "[STANBIC] background voice check matched=%s score=%s",
                matched,
                result.get("score"),
            )
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("[STANBIC] voice monitor crashed")


def _start_voice_monitor(userdata: dict[str, Any]) -> None:
    if userdata.get("stanbic_voice_monitor_task") is not None:
        return
    if not stanbic_voice_enabled(userdata):
        return
    try:
        task = asyncio.create_task(_voice_monitor_loop(userdata))
        userdata["stanbic_voice_monitor_task"] = task
        logger.info("[STANBIC] continuous voice monitor started")
    except Exception:
        logger.exception("[STANBIC] failed to start voice monitor")


def _recent_background_mismatch(userdata: dict[str, Any]) -> bool:
    status = userdata.get("stanbic_voice_status") or {}
    if not isinstance(status, dict):
        return False
    if status.get("matched") is not False:
        return False
    at = float(status.get("at") or 0.0)
    return (time.monotonic() - at) < RECENT_MISMATCH_WINDOW


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@function_tool
async def check_voice_enrollment(ctx: RunContext) -> dict:
    """Check whether the current caller already exists in the system. Call this silently at the start of every call and never mention it to the caller."""  # noqa: E501
    userdata = getattr(getattr(ctx, "session", None), "userdata", {}) or {}
    if not stanbic_voice_enabled(userdata):
        return {"status": "disabled"}
    key = caller_key(userdata)
    if not key:
        return {"status": "failed", "reason": "no_caller_key"}
    _start_voice_monitor(userdata)
    try:
        result = await _sidecar_status(key)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[STANBIC] enrollment check failed")
        return {"status": "failed", "reason": str(exc)}
    enrolled = bool(result.get("enrolled"))
    userdata["stanbic_voice_status"] = {
        "enrolled": enrolled,
        "matched": None,
        "at": time.monotonic(),
    }
    logger.info("[STANBIC] enrollment check key=%s enrolled=%s", key, enrolled)
    return {
        "status": "ok",
        "enrolled": bool(result.get("enrolled")),
        "caller_key": key,
    }


@function_tool
async def create_demo_account(ctx: RunContext, full_name: str) -> dict:
    """Create the caller's digital account after collecting their full name during onboarding. The account starts with a zero balance. Call this once you have the caller's full name confirmed."""  # noqa: E501
    userdata = getattr(getattr(ctx, "session", None), "userdata", {}) or {}
    if not stanbic_voice_enabled(userdata):
        return {"status": "disabled"}
    key = caller_key(userdata)
    if not key:
        return {"status": "failed", "reason": "no_caller_key"}
    full_name = str(full_name or "").strip()
    if not full_name:
        return {"status": "failed", "reason": "full_name_required"}
    try:
        result = await _demo_create(key, full_name)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[STANBIC] demo account create failed")
        return {"status": "failed", "reason": str(exc)}
    if result.get("http_status") not in (200, 201):
        return {"status": "failed", "reason": result.get("detail") or "create_failed"}
    logger.info(
        "[STANBIC] demo account %s key=%s name=%s",
        result.get("status"), key, full_name,
    )
    return {
        "status": result.get("status"),
        "full_name": full_name,
        "account_number": result.get("account_number"),
        "balance": result.get("balance", 0.0),
        "joined_at": result.get("joined_at"),
    }


@function_tool
async def capture_voice_sample(ctx: RunContext) -> dict:
    """Record the caller's voice sample from what they just said. Call this immediately after the caller repeats the enrollment phrase each time. Never explain the technical details to the caller."""  # noqa: E501
    userdata = getattr(getattr(ctx, "session", None), "userdata", {}) or {}
    if not stanbic_voice_enabled(userdata):
        return {"status": "disabled"}
    key = caller_key(userdata)
    tap = _tap(userdata)
    if not key:
        return {"status": "failed", "reason": "no_caller_key"}
    if tap is None:
        return {"status": "failed", "reason": "no_audio_tap"}
    wav_bytes = tap.wav_bytes(CAPTURE_SECONDS)
    if not wav_bytes:
        return {"status": "failed", "reason": "no_caller_audio"}
    try:
        result = await _sidecar_enroll(key, wav_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[STANBIC] voice enroll failed")
        return {"status": "failed", "reason": str(exc)}
    if result.get("http_status") not in (200, 201):
        return {
            "status": "failed",
            "reason": result.get("detail") or "enroll_failed",
        }
    logger.info(
        "[STANBIC] voice sample enrolled key=%s prints=%s",
        key, result.get("prints"),
    )
    return {
        "status": "enrolled",
        "prints": result.get("prints"),
        "model_tag": result.get("model_tag"),
    }


@function_tool
async def get_account_details(ctx: RunContext) -> dict:
    """Fetch the caller's account details (balance, account number, join date). An automatic security verification runs as part of this call — never mention it or any verification process to the caller. If the result is blocked, simply tell the caller you weren't able to verify them and cannot share the information."""  # noqa: E501
    userdata = getattr(getattr(ctx, "session", None), "userdata", {}) or {}
    if not stanbic_voice_enabled(userdata):
        return {"status": "disabled"}
    key = caller_key(userdata)
    tap = _tap(userdata)
    if not key:
        return {"status": "failed", "reason": "no_caller_key"}
    if tap is None:
        return {"status": "failed", "reason": "no_audio_tap"}
    wav_bytes = tap.wav_bytes(CAPTURE_SECONDS)
    if not wav_bytes:
        return {"status": "failed", "reason": "no_caller_audio"}
    try:
        verify = await _sidecar_verify(key, wav_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[STANBIC] voice verify failed")
        return {"status": "failed", "reason": str(exc)}
    if verify.get("http_status") != 200:
        return {
            "status": "blocked",
            "voice_match": False,
            "reason": verify.get("detail") or "verify_failed",
        }
    matched = bool(verify.get("matched"))
    score = float(verify.get("score") or 0.0)
    # Continuous-validation gate: a recent background mismatch means the
    # voice speaking right now must clear a HIGHER bar to be trusted.
    if _recent_background_mismatch(userdata) and matched and score < STRONG_MATCH:
        logger.info(
            "[STANBIC] blocked: weak match (%.3f) after recent background mismatch", score
        )
        return {
            "status": "blocked",
            "voice_match": False,
            "score": verify.get("score"),
            "threshold": verify.get("threshold"),
            "reason": "verification_failed",
        }
    logger.info(
        "[STANBIC] voice verify key=%s matched=%s score=%s threshold=%s",
        key, matched, verify.get("score"), verify.get("threshold"),
    )
    if not matched:
        return {
            "status": "blocked",
            "voice_match": False,
            "score": verify.get("score"),
            "threshold": verify.get("threshold"),
            "reason": verify.get("reason") or "voice_mismatch",
        }
    try:
        account = await _demo_get(key)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[STANBIC] demo account fetch failed")
        return {"status": "failed", "reason": str(exc)}
    if account.get("http_status") == 404:
        return {"status": "no_account", "voice_match": True}
    if account.get("http_status") != 200:
        return {"status": "failed", "reason": account.get("detail") or "fetch_failed"}
    return {
        "status": "ok",
        "voice_match": True,
        "score": verify.get("score"),
        "full_name": account.get("full_name"),
        "account_number": account.get("account_number"),
        "balance": account.get("balance"),
        "joined_at": account.get("joined_at"),
    }


def build_stanbic_voice_tools(userdata: dict[str, Any]) -> list[Any]:
    if not stanbic_voice_enabled(userdata):
        return []
    return [
        check_voice_enrollment,
        create_demo_account,
        capture_voice_sample,
        get_account_details,
    ]
