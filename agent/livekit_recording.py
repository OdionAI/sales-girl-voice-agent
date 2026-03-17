from __future__ import annotations

import asyncio
import json
import logging
import os
import posixpath
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from livekit import api


logger = logging.getLogger(__name__)

RECORDING_ENABLED = os.getenv("LIVEKIT_RECORDING_ENABLED", "false").lower() == "true"
RECORDING_BUCKET = str(os.getenv("LIVEKIT_RECORDING_GCS_BUCKET", "")).strip()
RECORDING_GCP_CREDENTIALS = str(os.getenv("LIVEKIT_RECORDING_GCP_CREDENTIALS_JSON", "")).strip()
RECORDING_PREFIX = str(os.getenv("LIVEKIT_RECORDING_FILE_PREFIX", "livekit-recordings")).strip("/") or "livekit-recordings"
RECORDING_FORMAT = str(os.getenv("LIVEKIT_RECORDING_FORMAT", "ogg")).strip().lower() or "ogg"
RECORDING_PUBLIC_BASE_URL = str(os.getenv("LIVEKIT_RECORDING_PUBLIC_BASE_URL", "")).strip().rstrip("/")
RECORDING_POLL_TIMEOUT_SECONDS = max(5, int(os.getenv("LIVEKIT_RECORDING_POLL_TIMEOUT_SECONDS", "45")))
RECORDING_POLL_INTERVAL_SECONDS = max(1, int(os.getenv("LIVEKIT_RECORDING_POLL_INTERVAL_SECONDS", "2")))


@dataclass(slots=True)
class RecordingStartResult:
    enabled: bool
    egress_id: str | None = None
    filepath: str | None = None
    expected_url: str | None = None
    detail: str | None = None


@dataclass(slots=True)
class RecordingFinalizeResult:
    status: str
    recording_url: str | None = None
    duration_seconds: int | None = None
    detail: str | None = None


def is_recording_enabled() -> bool:
    return bool(RECORDING_ENABLED and RECORDING_BUCKET and RECORDING_GCP_CREDENTIALS)


def _api_client() -> api.LiveKitAPI:
    return api.LiveKitAPI()


def _safe_slug(value: str, fallback: str) -> str:
    raw = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(value or "").strip())
    slug = "-".join(part for part in raw.split("-") if part)
    return slug or fallback


def _file_type_for_format() -> Any:
    if RECORDING_FORMAT == "mp3":
        return api.EncodedFileType.MP3
    if RECORDING_FORMAT == "mp4":
        return api.EncodedFileType.MP4
    return api.EncodedFileType.OGG


def _recording_path(*, business_id: str, session_id: str, room_name: str, started_at: datetime) -> str:
    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    business_slug = _safe_slug(business_id, "unknown-business")
    room_slug = _safe_slug(room_name, "room")
    session_slug = _safe_slug(session_id, "session")
    filename = f"{stamp}-{room_slug}-{session_slug}.{RECORDING_FORMAT}"
    return posixpath.join(RECORDING_PREFIX, business_slug, filename)


def _public_url_for_path(filepath: str) -> str:
    normalized_path = filepath.lstrip("/")
    if RECORDING_PUBLIC_BASE_URL:
        return f"{RECORDING_PUBLIC_BASE_URL}/{normalized_path}"
    return f"https://storage.googleapis.com/{RECORDING_BUCKET}/{normalized_path}"


def _serialize_credentials() -> str:
    raw = RECORDING_GCP_CREDENTIALS.strip()
    if not raw:
        return ""
    if raw.startswith("{"):
        return raw
    try:
        with open(raw, "r", encoding="utf-8") as handle:
            parsed = json.load(handle)
        return json.dumps(parsed)
    except Exception:
        logger.warning("LIVEKIT_RECORDING_GCP_CREDENTIALS_JSON is not valid JSON or readable path.")
        return ""


async def start_room_recording(
    *,
    room_name: str,
    business_id: str,
    session_id: str,
    started_at: datetime,
) -> RecordingStartResult:
    if not is_recording_enabled():
        return RecordingStartResult(enabled=False, detail="recording_disabled")

    credentials = _serialize_credentials()
    if not credentials:
        return RecordingStartResult(enabled=False, detail="invalid_gcp_credentials")

    filepath = _recording_path(
        business_id=business_id,
        session_id=session_id,
        room_name=room_name,
        started_at=started_at,
    )
    req = api.RoomCompositeEgressRequest(
        room_name=room_name,
        audio_only=True,
        file_outputs=[
            api.EncodedFileOutput(
                file_type=_file_type_for_format(),
                filepath=filepath,
                gcp=api.GCPUpload(
                    bucket=RECORDING_BUCKET,
                    credentials=credentials,
                ),
            )
        ],
    )

    lkapi = _api_client()
    try:
        info = await lkapi.egress.start_room_composite_egress(req)
        egress_id = str(getattr(info, "egress_id", "") or "").strip()
        return RecordingStartResult(
            enabled=True,
            egress_id=egress_id or None,
            filepath=filepath,
            expected_url=_public_url_for_path(filepath),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to start room recording for %s: %s", room_name, exc)
        return RecordingStartResult(enabled=True, filepath=filepath, expected_url=_public_url_for_path(filepath), detail=str(exc))
    finally:
        await lkapi.aclose()


def _extract_completed_location(info: Any) -> str | None:
    file_results = getattr(info, "file_results", None) or []
    if file_results:
        first = file_results[0]
        location = str(getattr(first, "location", "") or "").strip()
        if location:
            return location
    file_info = getattr(info, "file", None)
    if file_info is not None:
        location = str(getattr(file_info, "location", "") or "").strip()
        if location:
            return location
    return None


def _normalize_recording_url(location: str | None, fallback: str | None) -> str | None:
    raw = str(location or "").strip()
    if not raw:
        return fallback
    if raw.startswith("gs://"):
        _, _, remainder = raw.partition("gs://")
        bucket, _, path = remainder.partition("/")
        if bucket and path:
            return f"https://storage.googleapis.com/{bucket}/{path}"
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return fallback


async def finalize_room_recording(
    *,
    egress_id: str | None,
    expected_url: str | None,
    duration_seconds: int,
) -> RecordingFinalizeResult:
    if not egress_id:
        return RecordingFinalizeResult(status="unavailable", recording_url=expected_url, duration_seconds=duration_seconds, detail="missing_egress_id")

    lkapi = _api_client()
    try:
        try:
            await lkapi.egress.stop_egress(api.StopEgressRequest(egress_id=egress_id))
        except Exception as exc:  # noqa: BLE001
            logger.info("Stop egress returned before completion for %s: %s", egress_id, exc)

        deadline = asyncio.get_running_loop().time() + RECORDING_POLL_TIMEOUT_SECONDS
        latest: Any = None
        while asyncio.get_running_loop().time() < deadline:
            listing = await lkapi.egress.list_egress(api.ListEgressRequest(egress_id=egress_id))
            items = list(getattr(listing, "items", []) or [])
            latest = items[0] if items else latest
            location = _extract_completed_location(latest)
            if location:
                return RecordingFinalizeResult(
                    status="available",
                    recording_url=_normalize_recording_url(location, expected_url),
                    duration_seconds=duration_seconds,
                )
            status_code = int(getattr(latest, "status", 0) or 0) if latest is not None else 0
            error_text = str(getattr(latest, "error", "") or "").strip() if latest is not None else ""
            if status_code in {
                int(api.EgressStatus.EGRESS_FAILED),
                int(api.EgressStatus.EGRESS_ABORTED),
                int(api.EgressStatus.EGRESS_LIMIT_REACHED),
            }:
                return RecordingFinalizeResult(
                    status="failed",
                    recording_url=None,
                    duration_seconds=duration_seconds,
                    detail=error_text or f"egress_status_{status_code}",
                )
            await asyncio.sleep(RECORDING_POLL_INTERVAL_SECONDS)

        return RecordingFinalizeResult(
            status="processing",
            recording_url=expected_url,
            duration_seconds=duration_seconds,
            detail="egress_completion_timeout",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to finalize room recording %s: %s", egress_id, exc)
        return RecordingFinalizeResult(
            status="failed",
            recording_url=expected_url,
            duration_seconds=duration_seconds,
            detail=str(exc),
        )
    finally:
        await lkapi.aclose()
