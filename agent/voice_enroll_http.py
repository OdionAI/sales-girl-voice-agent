import asyncio
import logging
import os
import random
import sqlite3
import string
import threading
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from .voice_auth import (
    compare_clips,
    compare_customer,
    compare_pcm,
    enroll_wav_bytes,
    is_enrolled,
    is_enrolled_customer,
    normalize_owner_email,
    normalize_owner_key,
    pcm_from_wav_bytes,
    voice_auth_root,
    voice_auth_store,
    voice_auth_threshold,
)

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8098
_started = False


def _valid_key(key: str) -> bool:
    return bool(key) and ("@" in key or key.startswith("+"))


def _demo_db_path() -> Path:
    return voice_auth_root() / "demo_accounts.db"


def _demo_connect() -> sqlite3.Connection:
    path = _demo_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS demo_accounts (
            key TEXT PRIMARY KEY,
            full_name TEXT NOT NULL,
            account_number TEXT NOT NULL,
            balance_kobo INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _demo_row_payload(row: tuple) -> dict:
    key, full_name, account_number, balance_kobo, created_at = row
    return {
        "key": key,
        "full_name": full_name,
        "account_number": account_number,
        "balance": round(int(balance_kobo) / 100.0, 2),
        "joined_at": created_at,
    }


async def _demo_create_account(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"detail": "A JSON body is required."}, status=400)
    key = normalize_owner_key(str(body.get("key") or body.get("email") or ""))
    full_name = str(body.get("full_name") or "").strip()
    if not _valid_key(key):
        return web.json_response(
            {"detail": "A valid email or phone number is required."}, status=400
        )
    if not full_name:
        return web.json_response({"detail": "A full name is required."}, status=400)
    try:
        with _demo_connect() as conn:
            row = conn.execute(
                "SELECT key, full_name, account_number, balance_kobo, created_at"
                " FROM demo_accounts WHERE key = ?",
                (key,),
            ).fetchone()
            if row is not None:
                if row[1] != full_name:
                    conn.execute(
                        "UPDATE demo_accounts SET full_name = ? WHERE key = ?",
                        (full_name, key),
                    )
                    conn.commit()
                    logger.info(
                        "[DEMO] account name updated key=%s name=%s", key, full_name
                    )
                    row = conn.execute(
                        "SELECT key, full_name, account_number, balance_kobo, created_at"
                        " FROM demo_accounts WHERE key = ?",
                        (key,),
                    ).fetchone()
                    return web.json_response(
                        {"status": "updated", **_demo_row_payload(row)}
                    )
                return web.json_response(
                    {"status": "exists", **_demo_row_payload(row)}
                )
            account_number = "".join(random.choices(string.digits, k=10))
            created_at = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO demo_accounts"
                " (key, full_name, account_number, balance_kobo, created_at)"
                " VALUES (?, ?, ?, 0, ?)",
                (key, full_name, account_number, created_at),
            )
            conn.commit()
            row = conn.execute(
                "SELECT key, full_name, account_number, balance_kobo, created_at"
                " FROM demo_accounts WHERE key = ?",
                (key,),
            ).fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.exception("[DEMO] account create failed")
        return web.json_response({"detail": str(exc)}, status=500)
    logger.info("[DEMO] account created key=%s name=%s", key, full_name)
    return web.json_response({"status": "created", **_demo_row_payload(row)})


async def _demo_get_account(request: web.Request) -> web.Response:
    key = normalize_owner_key(
        str(request.query.get("key") or request.query.get("email") or "")
    )
    if not _valid_key(key):
        return web.json_response(
            {"detail": "A valid email or phone number is required."}, status=400
        )
    with _demo_connect() as conn:
        row = conn.execute(
            "SELECT key, full_name, account_number, balance_kobo, created_at"
            " FROM demo_accounts WHERE key = ?",
            (key,),
        ).fetchone()
    if row is None:
        return web.json_response(
            {"detail": "No account found for this caller."}, status=404
        )
    return web.json_response({"status": "ok", **_demo_row_payload(row)})


def enroll_http_port() -> int:
    raw = str(os.getenv("VOICE_AUTH_HTTP_PORT") or DEFAULT_PORT).strip()
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_PORT


async def _enroll(request: web.Request) -> web.Response:
    reader = await request.multipart()
    key = ""
    audio = b""
    async for part in reader:
        name = str(part.name or "")
        if name in {"email", "key", "owner"}:
            key = normalize_owner_key((await part.text()) or "")
        elif name in {"audio", "file"}:
            audio = await part.read()
    if not _valid_key(key):
        return web.json_response(
            {"detail": "A valid email or phone number is required."}, status=400
        )
    if not audio:
        return web.json_response({"detail": "Voice audio is required."}, status=400)
    try:
        result = enroll_wav_bytes(key, audio)
    except ValueError as exc:
        return web.json_response({"detail": str(exc)}, status=400)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[VOICE-AUTH] enroll failed")
        return web.json_response({"detail": str(exc)}, status=500)
    return web.json_response(result)


async def _compare(request: web.Request) -> web.Response:
    reader = await request.multipart()
    left = b""
    right = b""
    threshold = None
    async for part in reader:
        name = str(part.name or "")
        if name in {"left", "a", "audio_a"}:
            left = await part.read()
        elif name in {"right", "b", "audio_b"}:
            right = await part.read()
        elif name == "threshold":
            raw = (await part.text() or "").strip()
            try:
                threshold = float(raw) if raw else None
            except ValueError:
                threshold = None
    if not left or not right:
        return web.json_response({"detail": "Two voice clips are required."}, status=400)
    try:
        left_samples, left_rate = pcm_from_wav_bytes(left)
        right_samples, right_rate = pcm_from_wav_bytes(right)
        result = compare_clips(
            left_samples,
            left_rate,
            right_samples,
            right_rate,
            threshold=threshold if threshold is not None else voice_auth_threshold(),
        )
    except ValueError as exc:
        return web.json_response({"detail": str(exc)}, status=400)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[VOICE-AUTH] compare failed")
        return web.json_response({"detail": str(exc)}, status=500)
    return web.json_response(result)


async def _verify(request: web.Request) -> web.Response:
    reader = await request.multipart()
    key = ""
    customer_id = ""
    audio = b""
    threshold = None
    async for part in reader:
        name = str(part.name or "")
        if name in {"email", "key", "owner"}:
            key = normalize_owner_key((await part.text()) or "")
        elif name in {"customer_id", "customerId", "profile_id", "voice_profile_id"}:
            customer_id = (await part.text() or "").strip()
        elif name in {"audio", "file"}:
            audio = await part.read()
        elif name == "threshold":
            raw = (await part.text() or "").strip()
            try:
                threshold = float(raw) if raw else None
            except ValueError:
                threshold = None
    if not audio:
        return web.json_response({"detail": "Voice audio is required."}, status=400)
    use_wema_store = voice_auth_store() == "wema" and bool(customer_id)
    if not use_wema_store and not _valid_key(key):
        return web.json_response(
            {"detail": "A valid email or phone number is required."}, status=400
        )
    try:
        samples, sample_rate = pcm_from_wav_bytes(audio)
        result = (
            compare_customer(customer_id, samples, sample_rate, threshold=threshold)
            if use_wema_store else
            compare_pcm(key, samples, sample_rate, threshold=threshold)
        )
    except ValueError as exc:
        return web.json_response({"detail": str(exc)}, status=400)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[VOICE-AUTH] verify failed")
        return web.json_response({"detail": str(exc)}, status=500)
    return web.json_response(result)


async def _status(request: web.Request) -> web.Response:
    customer_id = str(request.query.get("customer_id") or request.query.get("profile_id") or "").strip()
    if voice_auth_store() == "wema" and customer_id:
        return web.json_response(
            {"customer_id": customer_id, "enrolled": is_enrolled_customer(customer_id)}
        )
    key = normalize_owner_key(
        str(request.query.get("email") or request.query.get("key") or "")
    )
    if not _valid_key(key):
        return web.json_response(
            {"detail": "A valid email or phone number is required."}, status=400
        )
    return web.json_response({"email": key, "key": key, "enrolled": is_enrolled(key)})


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/v1/voice-auth/enroll", _enroll)
    app.router.add_post("/v1/voice-auth/compare", _compare)
    app.router.add_post("/v1/voice-auth/verify", _verify)
    app.router.add_get("/v1/voice-auth/status", _status)
    app.router.add_post("/v1/demo/accounts", _demo_create_account)
    app.router.add_get("/v1/demo/accounts", _demo_get_account)
    return app


async def _serve() -> None:
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", enroll_http_port())
    await site.start()
    logger.info("[VOICE-AUTH] enroll HTTP listening on 127.0.0.1:%s", enroll_http_port())
    try:
        from .voice_auth import _ecapa_encoder, voice_auth_embedder

        embedder = voice_auth_embedder()
        if embedder == "wema":
            from . import wema_voiceprint as wema

            await asyncio.to_thread(wema.warmup)
            logger.info("[VOICE-AUTH] Wema models warm (tag=%s)", wema.model_tag())
        elif embedder == "ecapa":
            await asyncio.to_thread(_ecapa_encoder)
            logger.info("[VOICE-AUTH] ECAPA encoder ready")
    except Exception:
        logger.exception("[VOICE-AUTH] embedder warmup failed; will retry on demand")
    await asyncio.Event().wait()


def start_voice_enroll_http() -> None:
    global _started
    if _started:
        return
    _started = True

    def _run() -> None:
        asyncio.run(_serve())

    thread = threading.Thread(target=_run, name="voice-auth-enroll-http", daemon=True)
    thread.start()
