import asyncio
import logging
import os
import threading
from aiohttp import web

from .voice_auth import (
    compare_clips,
    compare_pcm,
    enroll_wav_bytes,
    is_enrolled,
    normalize_owner_email,
    pcm_from_wav_bytes,
    voice_auth_threshold,
)

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8098
_started = False


def enroll_http_port() -> int:
    raw = str(os.getenv("VOICE_AUTH_HTTP_PORT") or DEFAULT_PORT).strip()
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_PORT


async def _enroll(request: web.Request) -> web.Response:
    reader = await request.multipart()
    email = ""
    audio = b""
    async for part in reader:
        name = str(part.name or "")
        if name == "email":
            email = normalize_owner_email((await part.text()) or "")
        elif name in {"audio", "file"}:
            audio = await part.read()
    if not email or "@" not in email:
        return web.json_response({"detail": "A valid email is required."}, status=400)
    if not audio:
        return web.json_response({"detail": "Voice audio is required."}, status=400)
    try:
        result = enroll_wav_bytes(email, audio)
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
    email = ""
    audio = b""
    threshold = None
    async for part in reader:
        name = str(part.name or "")
        if name == "email":
            email = normalize_owner_email((await part.text()) or "")
        elif name in {"audio", "file"}:
            audio = await part.read()
        elif name == "threshold":
            raw = (await part.text() or "").strip()
            try:
                threshold = float(raw) if raw else None
            except ValueError:
                threshold = None
    if not email or "@" not in email:
        return web.json_response({"detail": "A valid email is required."}, status=400)
    if not audio:
        return web.json_response({"detail": "Voice audio is required."}, status=400)
    try:
        samples, sample_rate = pcm_from_wav_bytes(audio)
        result = compare_pcm(email, samples, sample_rate, threshold=threshold)
    except ValueError as exc:
        return web.json_response({"detail": str(exc)}, status=400)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[VOICE-AUTH] verify failed")
        return web.json_response({"detail": str(exc)}, status=500)
    return web.json_response(result)


async def _status(request: web.Request) -> web.Response:
    email = normalize_owner_email(request.query.get("email") or "")
    if not email or "@" not in email:
        return web.json_response({"detail": "A valid email is required."}, status=400)
    return web.json_response({"email": email, "enrolled": is_enrolled(email)})


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/v1/voice-auth/enroll", _enroll)
    app.router.add_post("/v1/voice-auth/compare", _compare)
    app.router.add_post("/v1/voice-auth/verify", _verify)
    app.router.add_get("/v1/voice-auth/status", _status)
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

        if voice_auth_embedder() == "ecapa":
            await asyncio.to_thread(_ecapa_encoder)
            logger.info("[VOICE-AUTH] ECAPA encoder ready")
    except Exception:
        logger.exception("[VOICE-AUTH] ECAPA warmup failed; enroll will retry on demand")
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
