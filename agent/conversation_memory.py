from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone


DB_PATH = os.getenv("AGENT_MEMORY_DB_PATH", "conversation_memory.db")
MAX_CONTEXT_CHARS = int(os.getenv("AGENT_MEMORY_MAX_CONTEXT_CHARS", "6000"))
RECENT_TURNS = int(os.getenv("AGENT_MEMORY_RECENT_TURNS", "24"))

_LOCK = threading.Lock()


@dataclass
class ResumeContext:
    conversation_id: str
    total_messages: int
    context_text: str

    @property
    def has_history(self) -> bool:
        return self.total_messages > 0 and bool(self.context_text.strip())


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_phone(phone: str) -> str:
    raw = str(phone or "").strip()
    if not raw:
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return ""
    return f"+{digits}"


def conversation_id_for(agent_id: str, phone: str) -> str:
    normalized = normalize_phone(phone)
    return f"{agent_id}:{normalized}" if normalized else ""


def init_store() -> None:
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_messages (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  conversation_id TEXT NOT NULL,
                  agent_id TEXT NOT NULL,
                  phone TEXT NOT NULL,
                  role TEXT NOT NULL,
                  content TEXT NOT NULL,
                  session_id TEXT,
                  created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conv_lookup ON conversation_messages (agent_id, phone, id)"
            )
            conn.commit()


def append_message(
    *,
    conversation_id: str,
    agent_id: str,
    phone: str,
    role: str,
    content: str,
    session_id: str | None = None,
) -> None:
    phone_norm = normalize_phone(phone)
    text = str(content or "").strip()
    if not phone_norm or not text:
        return
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_messages (
                  conversation_id, agent_id, phone, role, content, session_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    agent_id,
                    phone_norm,
                    str(role or "user").lower(),
                    text,
                    session_id,
                    _now_iso(),
                ),
            )
            conn.commit()


def load_resume_context(*, agent_id: str, phone: str) -> ResumeContext:
    phone_norm = normalize_phone(phone)
    conv_id = conversation_id_for(agent_id, phone_norm)
    if not phone_norm:
        return ResumeContext(conversation_id="", total_messages=0, context_text="")

    with _LOCK:
        with _connect() as conn:
            total_row = conn.execute(
                "SELECT COUNT(*) AS c FROM conversation_messages WHERE agent_id = ? AND phone = ?",
                (agent_id, phone_norm),
            ).fetchone()
            total_messages = int(total_row["c"] if total_row else 0)

            rows = conn.execute(
                """
                SELECT role, content, created_at
                FROM conversation_messages
                WHERE agent_id = ? AND phone = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (agent_id, phone_norm, RECENT_TURNS),
            ).fetchall()

    rows = list(reversed(rows))
    if not rows:
        return ResumeContext(conversation_id=conv_id, total_messages=0, context_text="")

    lines: list[str] = []
    for row in rows:
        role = str(row["role"] or "user").lower()
        content = str(row["content"] or "").strip()
        if not content:
            continue
        who = "Customer" if role == "user" else "Assistant"
        lines.append(f"{who}: {content}")

    context_text = "\n".join(lines).strip()
    if len(context_text) > MAX_CONTEXT_CHARS:
        context_text = context_text[-MAX_CONTEXT_CHARS:]

    return ResumeContext(
        conversation_id=conv_id,
        total_messages=total_messages,
        context_text=context_text,
    )
