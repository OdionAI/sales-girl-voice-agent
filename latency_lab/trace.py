from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from collections.abc import Callable


@dataclass(frozen=True)
class TraceEvent:
    seq: int
    session_id: str
    turn_id: str | None
    attempt_id: str | None
    event: str
    mono_ns: int
    wall_utc: str
    state_from: str | None
    state_to: str | None
    reason: str
    data: dict[str, Any]


class TraceRecorder:
    """One append-only JSONL trace per browser session."""

    def __init__(self, directory: Path, session_id: str | None = None) -> None:
        self.session_id = session_id or uuid.uuid4().hex[:12]
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{self.session_id}.jsonl"
        self._seq = 0
        self._listeners: set[Callable[[TraceEvent], None]] = set()

    def subscribe(self, listener: Callable[[TraceEvent], None]) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    def record(
        self,
        event: str,
        *,
        turn_id: str | None = None,
        attempt_id: str | None = None,
        state_from: str | None = None,
        state_to: str | None = None,
        reason: str = "",
        **data: Any,
    ) -> TraceEvent:
        self._seq += 1
        item = TraceEvent(
            seq=self._seq,
            session_id=self.session_id,
            turn_id=turn_id,
            attempt_id=attempt_id,
            event=event,
            mono_ns=time.perf_counter_ns(),
            wall_utc=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            state_from=state_from,
            state_to=state_to,
            reason=reason,
            data=data,
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")
        for listener in tuple(self._listeners):
            try:
                listener(item)
            except Exception:
                logging.getLogger(__name__).exception("Trace listener failed")
        return item
