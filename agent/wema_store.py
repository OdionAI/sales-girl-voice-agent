"""Read Wema's stored voiceprints (Blob Storage) and compare against a live voice.

Flow:
    customer_id (from the SDK)  ->  profile_id(s)            [blob metadata index]
    profile_id                 ->  stored-print URLs          [VoiceStorageEndpoint]
    stored-print URLs          ->  512-number arrays          [Blob Storage, public]
    live audio -> 512-d voiceprint (our copy of their models) -> cosine vs stored prints

Config (env):
    WEMA_VOICE_API_BASE        base URL of the voice-banking setup service
    WEMA_BLOB_CONNECTION_STRING Azure connection string for the audio container
    WEMA_BLOB_CONTAINER        container name (default alatdevaudio)
    WEMA_STORE_CACHE           sqlite path for the customer->profile index
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://voicebankingsetup-route-alat-two.apps.alatarodev.westeurope.aroapp.io"
DEFAULT_CONTAINER = "alatdevaudio"
_INDEX_TTL_SECONDS = 6 * 3600
_index_lock = threading.Lock()
_index_built_at = 0.0


def api_base() -> str:
    return str(os.getenv("WEMA_VOICE_API_BASE") or DEFAULT_API_BASE).rstrip("/")


def container_name() -> str:
    return str(os.getenv("WEMA_BLOB_CONTAINER") or DEFAULT_CONTAINER).strip()


def cache_path() -> Path:
    raw = str(os.getenv("WEMA_STORE_CACHE") or "").strip()
    if raw:
        return Path(raw)
    return Path(os.getenv("VOICE_AUTH_DIR", ".")) / "wema-profile-index.sqlite"


def _connect_cache() -> sqlite3.Connection:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_profile (
            customer_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            PRIMARY KEY (customer_id, profile_id)
        )
        """
    )
    conn.commit()
    return conn


def _blob_service():
    from azure.storage.blob import BlobServiceClient

    conn = str(os.getenv("WEMA_BLOB_CONNECTION_STRING") or "").strip()
    if not conn:
        raise RuntimeError("WEMA_BLOB_CONNECTION_STRING is not set.")
    return BlobServiceClient.from_connection_string(conn)


def rebuild_index() -> int:
    """Scan blob metadata (CustomerId/ProfileId) into the local cache. Slow; run rarely."""
    global _index_built_at
    svc = _blob_service()
    container = svc.get_container_client(container_name())
    pairs: set[tuple[str, str]] = set()
    for blob in container.list_blobs(include=["metadata"]):
        if not blob.name.endswith("-embedded.json"):
            continue
        md = blob.metadata or {}
        customer = str(md.get("CustomerId") or "").strip()
        profile = str(md.get("ProfileId") or "").strip()
        if customer and profile:
            pairs.add((customer, profile))
    with _index_lock:
        with _connect_cache() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO customer_profile (customer_id, profile_id) VALUES (?, ?)",
                sorted(pairs),
            )
            conn.commit()
        _index_built_at = time.time()
    logger.info("[WEMA-STORE] index rebuilt: %s customer/profile pairs", len(pairs))
    return len(pairs)


def profiles_for_customer(customer_id: str) -> list[str]:
    cid = str(customer_id or "").strip()
    if not cid:
        return []
    with _connect_cache() as conn:
        rows = [r[0] for r in conn.execute(
            "SELECT profile_id FROM customer_profile WHERE customer_id = ?", (cid,))]
    if rows:
        return rows
    # Cold cache: build once, then look again.
    if time.time() - _index_built_at > _INDEX_TTL_SECONDS:
        try:
            rebuild_index()
        except Exception:
            logger.exception("[WEMA-STORE] index rebuild failed")
        with _connect_cache() as conn:
            rows = [r[0] for r in conn.execute(
                "SELECT profile_id FROM customer_profile WHERE customer_id = ?", (cid,))]
    return rows


def stored_prints(profile_id: str) -> list[np.ndarray]:
    """VoiceStorageEndpoint -> URLs -> 512-float arrays from Blob Storage."""
    import httpx

    pid = str(profile_id or "").strip()
    if not pid:
        return []
    response = httpx.get(f"{api_base()}/api/voicebanking/customersamplevoices/{pid}", timeout=15.0)
    response.raise_for_status()
    payload = response.json() or {}
    urls = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(urls, list):
        return []
    prints: list[np.ndarray] = []
    for url in urls:
        if not isinstance(url, str) or not url:
            continue
        try:
            body = httpx.get(url, timeout=20.0).content
            values = json.loads(body.decode())
        except Exception:
            logger.warning("[WEMA-STORE] could not read stored print %s", url)
            continue
        vector = np.asarray(values, dtype=np.float32).reshape(-1)
        if vector.size == 512:
            prints.append(vector)
    return prints


def prints_for_customer(customer_id: str) -> list[np.ndarray]:
    prints: list[np.ndarray] = []
    for profile_id in profiles_for_customer(customer_id):
        try:
            prints.extend(stored_prints(profile_id))
        except Exception:
            logger.warning("[WEMA-STORE] fetch failed for profile %s", profile_id)
    return prints
