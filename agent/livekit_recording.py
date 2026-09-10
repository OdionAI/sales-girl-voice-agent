"""LiveKit audio recording with cloud uploads or private local Egress files.

Local mode requires LIVEKIT_RECORDING_ENABLED=true,
LIVEKIT_RECORDING_STORAGE_PROVIDER=local and an explicit absolute
LIVEKIT_RECORDING_LOCAL_DIRECTORY (for example /recordings). The Egress host
must mount that directory writable and have no default cloud storage configured.
Only expected_url/recording_url, never filepath, belongs in persisted playback URLs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import posixpath
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from livekit import api


logger = logging.getLogger(__name__)

RECORDING_ENABLED = os.getenv("LIVEKIT_RECORDING_ENABLED", "false").lower() == "true"
RECORDING_STORAGE_PROVIDER = str(os.getenv("LIVEKIT_RECORDING_STORAGE_PROVIDER", "gcs")).strip().lower() or "gcs"
RECORDING_BUCKET = str(
    os.getenv("LIVEKIT_RECORDING_BUCKET") or os.getenv("LIVEKIT_RECORDING_GCS_BUCKET", "")
).strip()
RECORDING_GCP_CREDENTIALS = str(os.getenv("LIVEKIT_RECORDING_GCP_CREDENTIALS_JSON", "")).strip()
RECORDING_S3_ACCESS_KEY = str(os.getenv("LIVEKIT_RECORDING_S3_ACCESS_KEY", "")).strip()
RECORDING_S3_SECRET_KEY = str(os.getenv("LIVEKIT_RECORDING_S3_SECRET_KEY", "")).strip()
RECORDING_S3_SESSION_TOKEN = str(os.getenv("LIVEKIT_RECORDING_S3_SESSION_TOKEN", "")).strip()
RECORDING_S3_REGION = str(os.getenv("LIVEKIT_RECORDING_S3_REGION", "af-south-1")).strip()
RECORDING_S3_ENDPOINT = str(os.getenv("LIVEKIT_RECORDING_S3_ENDPOINT", "")).strip()
RECORDING_S3_FORCE_PATH_STYLE = os.getenv("LIVEKIT_RECORDING_S3_FORCE_PATH_STYLE", "false").lower() == "true"
RECORDING_PREFIX = str(os.getenv("LIVEKIT_RECORDING_FILE_PREFIX", "livekit-recordings"))
RECORDING_LOCAL_DIRECTORY = str(os.getenv("LIVEKIT_RECORDING_LOCAL_DIRECTORY", ""))
RECORDING_FORMAT = str(os.getenv("LIVEKIT_RECORDING_FORMAT", "mp3")).strip().lower() or "mp3"
RECORDING_PUBLIC_BASE_URL = str(os.getenv("LIVEKIT_RECORDING_PUBLIC_BASE_URL", "")).strip().rstrip("/")
RECORDING_POLL_TIMEOUT_SECONDS = max(5, int(os.getenv("LIVEKIT_RECORDING_POLL_TIMEOUT_SECONDS", "45")))
RECORDING_POLL_INTERVAL_SECONDS = max(1, int(os.getenv("LIVEKIT_RECORDING_POLL_INTERVAL_SECONDS", "2")))
RECORDING_STOP_TIMEOUT_SECONDS = 5.0


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
    if not RECORDING_ENABLED:
        return False
    if RECORDING_STORAGE_PROVIDER == "local":
        try:
            _local_recording_config()
        except ValueError:
            return False
        return True
    if not RECORDING_BUCKET:
        return False
    if RECORDING_STORAGE_PROVIDER == "s3":
        return bool(RECORDING_S3_ACCESS_KEY and RECORDING_S3_SECRET_KEY and RECORDING_S3_ENDPOINT)
    return bool(RECORDING_GCP_CREDENTIALS)


def _safe_relative_path(path: str) -> bool:
    return all(
        part not in {".", ".."} and re.fullmatch(r"[A-Za-z0-9_.-]+", part)
        for part in path.split("/")
    )


def _local_recording_config() -> tuple[str, str]:
    # These paths belong to the Egress host, not necessarily the agent's filesystem.
    directory = RECORDING_LOCAL_DIRECTORY.rstrip("/")
    prefix = RECORDING_PREFIX.rstrip("/")
    if not directory.startswith("/") or not _safe_relative_path(directory[1:]):
        raise ValueError("invalid_local_recording_directory")
    if not _safe_relative_path(prefix):
        raise ValueError("invalid_local_recording_prefix")
    if RECORDING_FORMAT not in {"mp3", "mp4", "ogg"}:
        raise ValueError("invalid_local_recording_format")
    return directory, prefix


def _validated_local_uri(uri: str | None) -> str | None:
    if not uri or not uri.startswith("local-recording:///"):
        return None
    path = uri.removeprefix("local-recording:///")
    parts = path.split("/")
    if len(parts) < 3 or not _safe_relative_path(path):
        return None
    try:
        if str(UUID(parts[-2])) != parts[-2]:
            return None
    except ValueError:
        return None
    if posixpath.splitext(parts[-1])[1] not in {".mp3", ".mp4", ".ogg"}:
        return None
    return uri


def _api_client() -> api.LiveKitAPI:
    return api.LiveKitAPI()


def _safe_slug(value: str, fallback: str, *, ascii_only: bool = False) -> str:
    raw = "".join(
        ch.lower() if ch.isalnum() and (not ascii_only or ch.isascii()) else "-"
        for ch in str(value or "").strip()
    )
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
    local = RECORDING_STORAGE_PROVIDER == "local"
    prefix = RECORDING_PREFIX.strip("/") or "livekit-recordings"
    if local:
        directory, prefix = _local_recording_config()
        try:
            business_slug = str(UUID(str(business_id).strip()))
        except ValueError:
            raise ValueError("invalid_local_recording_business_id") from None
    else:
        business_slug = _safe_slug(business_id, "unknown-business")
    room_slug = _safe_slug(room_name, "room", ascii_only=local)
    session_slug = _safe_slug(session_id, "session", ascii_only=local)
    filename = f"{stamp}-{room_slug}-{session_slug}.{RECORDING_FORMAT}"
    relative_path = posixpath.join(prefix, business_slug, filename)
    return posixpath.join(directory, relative_path) if local else relative_path


def _public_url_for_path(filepath: str) -> str:
    if RECORDING_STORAGE_PROVIDER == "local":
        directory, _ = _local_recording_config()
        if not filepath.startswith(directory + "/"):
            raise ValueError("local_recording_outside_directory")
        uri = _validated_local_uri("local-recording:///" + filepath[len(directory) + 1 :])
        if not uri:
            raise ValueError("invalid_local_recording_path")
        return uri
    normalized_path = filepath.lstrip("/")
    if RECORDING_PUBLIC_BASE_URL:
        return f"{RECORDING_PUBLIC_BASE_URL}/{normalized_path}"
    if RECORDING_STORAGE_PROVIDER == "s3":
        endpoint = RECORDING_S3_ENDPOINT.rstrip("/")
        if endpoint:
            if RECORDING_S3_FORCE_PATH_STYLE:
                return f"{endpoint}/{RECORDING_BUCKET}/{normalized_path}"
            scheme, sep, host = endpoint.partition("://")
            if sep and host:
                return f"{scheme}://{RECORDING_BUCKET}.{host}/{normalized_path}"
            return f"{RECORDING_BUCKET}.{endpoint}/{normalized_path}"
        return f"s3://{RECORDING_BUCKET}/{normalized_path}"
    return f"https://storage.googleapis.com/{RECORDING_BUCKET}/{normalized_path}"


def _serialize_credentials() -> str:
    raw = RECORDING_GCP_CREDENTIALS.strip()
    if not raw:
        return ""

    # Staging/prod startup scripts can inject the secret as a JSON-encoded string,
    # so normalize quoted JSON, raw JSON, or a filesystem path into one payload.
    candidate = raw
    for _ in range(3):
        if not candidate:
            return ""
        try:
            parsed = json.loads(candidate)
        except Exception:
            normalized_candidate = _strip_json_layout_escapes(candidate)
            if normalized_candidate != candidate:
                candidate = normalized_candidate
                continue
            break
        if isinstance(parsed, dict):
            return json.dumps(parsed)
        if isinstance(parsed, str):
            candidate = parsed.strip()
            continue
        break

    try:
        with open(candidate, "r", encoding="utf-8") as handle:
            parsed = json.load(handle)
        return json.dumps(parsed)
    except Exception:
        logger.warning("LIVEKIT_RECORDING_GCP_CREDENTIALS_JSON is not valid JSON or readable path.")
        return ""


def _strip_json_layout_escapes(raw: str) -> str:
    output: list[str] = []
    in_string = False
    escape = False
    idx = 0

    while idx < len(raw):
        ch = raw[idx]
        if in_string:
            output.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            idx += 1
            continue

        if ch == '"':
            in_string = True
            output.append(ch)
            idx += 1
            continue

        if ch == "\\" and idx + 1 < len(raw) and raw[idx + 1] in {"n", "r", "t"}:
            if raw[idx + 1] == "t":
                output.append(" ")
            idx += 2
            continue

        output.append(ch)
        idx += 1

    return "".join(output)


async def start_room_recording(
    *,
    room_name: str,
    business_id: str,
    session_id: str,
    started_at: datetime,
) -> RecordingStartResult:
    if not is_recording_enabled():
        return RecordingStartResult(enabled=False, detail="recording_disabled")

    credentials = (
        _serialize_credentials() if RECORDING_STORAGE_PROVIDER not in {"s3", "local"} else ""
    )
    if RECORDING_STORAGE_PROVIDER not in {"s3", "local"} and not credentials:
        return RecordingStartResult(enabled=False, detail="invalid_gcp_credentials")

    try:
        filepath = _recording_path(
            business_id=business_id,
            session_id=session_id,
            room_name=room_name,
            started_at=started_at,
        )
        expected_url = _public_url_for_path(filepath)
    except ValueError as exc:
        return RecordingStartResult(enabled=False, detail=str(exc))

    upload: dict[str, Any] = {}
    if RECORDING_STORAGE_PROVIDER == "s3":
        upload["s3"] = api.S3Upload(
            access_key=RECORDING_S3_ACCESS_KEY,
            secret=RECORDING_S3_SECRET_KEY,
            session_token=RECORDING_S3_SESSION_TOKEN,
            region=RECORDING_S3_REGION,
            endpoint=RECORDING_S3_ENDPOINT,
            bucket=RECORDING_BUCKET,
            force_path_style=RECORDING_S3_FORCE_PATH_STYLE,
        )
    elif RECORDING_STORAGE_PROVIDER != "local":
        upload["gcp"] = api.GCPUpload(bucket=RECORDING_BUCKET, credentials=credentials)

    req = api.RoomCompositeEgressRequest(
        room_name=room_name,
        audio_only=True,
        file_outputs=[
            api.EncodedFileOutput(
                file_type=_file_type_for_format(),
                filepath=filepath,
                **upload,
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
            expected_url=expected_url,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to start room recording for %s: %s", room_name, exc)
        return RecordingStartResult(
            enabled=True, filepath=filepath, expected_url=expected_url, detail=str(exc)
        )
    finally:
        await lkapi.aclose()


def _extract_completed_file(info: Any) -> Any:
    if getattr(info, "status", None) != api.EgressStatus.EGRESS_COMPLETE:
        return None
    files = list(getattr(info, "file_results", None) or [])
    files.append(getattr(info, "file", None))
    for file_info in files:
        if (
            str(getattr(file_info, "location", "") or "").strip()
            or str(getattr(file_info, "filename", "") or "").strip()
        ):
            return file_info
    return None


def _extract_completed_location(info: Any) -> str | None:
    file_info = _extract_completed_file(info)
    return str(getattr(file_info, "location", "") or "").strip() or None


def _file_duration_seconds(file_info: Any, fallback: int) -> int:
    try:
        duration_ns = int(getattr(file_info, "duration", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return fallback
    # LiveKit FileInfo.duration is in nanoseconds; preserve the caller's integer API.
    return duration_ns // 1_000_000_000 if duration_ns > 0 else fallback


def _normalize_recording_url(location: str | None, fallback: str | None) -> str | None:
    if RECORDING_STORAGE_PROVIDER == "local" or str(fallback or "").startswith("local-recording:"):
        # Persist only our tenant-scoped URI, never an Egress filesystem/public URL.
        return _validated_local_uri(fallback)
    raw = str(location or "").strip()
    if not raw:
        return fallback
    if raw.startswith("gs://"):
        _, _, remainder = raw.partition("gs://")
        bucket, _, path = remainder.partition("/")
        if bucket and path:
            return f"https://storage.googleapis.com/{bucket}/{path}"
    if raw.startswith("s3://"):
        _, _, remainder = raw.partition("s3://")
        bucket, _, path = remainder.partition("/")
        if bucket and path:
            if RECORDING_PUBLIC_BASE_URL:
                return f"{RECORDING_PUBLIC_BASE_URL}/{path}"
            if RECORDING_S3_ENDPOINT:
                endpoint = RECORDING_S3_ENDPOINT.rstrip("/")
                if RECORDING_S3_FORCE_PATH_STYLE:
                    return f"{endpoint}/{bucket}/{path}"
                scheme, sep, host = endpoint.partition("://")
                if sep and host:
                    return f"{scheme}://{bucket}.{host}/{path}"
                return f"{bucket}.{endpoint}/{path}"
            return raw
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return fallback


async def _close_api_client(lkapi: api.LiveKitAPI) -> None:
    try:
        await asyncio.wait_for(lkapi.aclose(), timeout=1.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to close recording API client: %s", exc)


async def _request_stop_egress(lkapi: api.LiveKitAPI, egress_id: str, timeout_seconds: float) -> Any:
    try:
        return await asyncio.wait_for(
            lkapi.egress.stop_egress(api.StopEgressRequest(egress_id=egress_id)),
            timeout=timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("Stop egress returned before completion for %s: %s", egress_id, exc)
        return None


async def request_stop_room_recording(
    *,
    egress_id: str | None,
    timeout_seconds: float = RECORDING_STOP_TIMEOUT_SECONDS,
) -> bool:
    """Best-effort stop acknowledgement, not file readiness; cleanup adds at most 1s.

    Ordinary API errors/timeouts return False. Caller cancellation still propagates.
    """
    if not egress_id:
        return False
    lkapi = None
    try:
        lkapi = _api_client()
        return await _request_stop_egress(lkapi, egress_id, timeout_seconds) is not None
    except Exception as exc:  # noqa: BLE001
        logger.info("Failed to request recording stop for %s: %s", egress_id, exc)
        return False
    finally:
        if lkapi is not None:
            await _close_api_client(lkapi)


async def finalize_room_recording(
    *,
    egress_id: str | None,
    expected_url: str | None,
    duration_seconds: int,
    request_stop: bool = True,
    timeout_seconds: float | None = None,
) -> RecordingFinalizeResult:
    """Wait for COMPLETE plus file metadata, within one stop/poll budget.

    Pass request_stop=False after requesting stop separately. Timeouts remain
    processing, as do lookup errors or missing file metadata. Failed/aborted/limit
    reached Egress statuses return failed; a missing egress_id returns unavailable.
    Client cleanup adds at most 1s to the budget. Cancellation propagates.
    """
    if not egress_id:
        return RecordingFinalizeResult(
            status="unavailable",
            recording_url=None,
            duration_seconds=duration_seconds,
            detail="missing_egress_id",
        )

    lkapi = None
    try:
        lkapi = _api_client()
        budget = RECORDING_POLL_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
        async with asyncio.timeout(budget):
            latest = None
            if request_stop:
                latest = await _request_stop_egress(lkapi, egress_id, RECORDING_STOP_TIMEOUT_SECONDS)
            polled = False
            while True:
                status_code = getattr(latest, "status", None)
                if status_code in {
                    api.EgressStatus.EGRESS_FAILED,
                    api.EgressStatus.EGRESS_ABORTED,
                    api.EgressStatus.EGRESS_LIMIT_REACHED,
                }:
                    return RecordingFinalizeResult(
                        status="failed",
                        recording_url=None,
                        duration_seconds=duration_seconds,
                        detail=(
                            str(getattr(latest, "error", "") or "").strip()
                            or f"egress_status_{status_code}"
                        ),
                    )
                file_info = _extract_completed_file(latest)
                if file_info is not None:
                    recording_url = _normalize_recording_url(
                        getattr(file_info, "location", None), expected_url
                    )
                    if recording_url:
                        return RecordingFinalizeResult(
                            status="available",
                            recording_url=recording_url,
                            duration_seconds=_file_duration_seconds(file_info, duration_seconds),
                        )
                if polled:
                    await asyncio.sleep(RECORDING_POLL_INTERVAL_SECONDS)
                listing = await lkapi.egress.list_egress(api.ListEgressRequest(egress_id=egress_id))
                items = list(getattr(listing, "items", []) or [])
                latest = items[0] if items else None
                polled = True
    except TimeoutError:
        return RecordingFinalizeResult(
            status="processing",
            recording_url=None,
            duration_seconds=duration_seconds,
            detail="egress_completion_timeout",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to finalize room recording %s: %s", egress_id, exc)
        return RecordingFinalizeResult(
            status="processing",
            recording_url=None,
            duration_seconds=duration_seconds,
            detail=str(exc),
        )
    finally:
        if lkapi is not None:
            await _close_api_client(lkapi)
