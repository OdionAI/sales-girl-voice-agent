from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class UsageMeter:
    stt_seconds: float = 0.0
    tts_characters: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_cached_tokens: int = 0
    has_session_usage: bool = False
    provider_usage: list[dict[str, Any]] = field(default_factory=list)

    def add_assistant_text(self, content: str) -> None:
        self.tts_characters += len(str(content or ""))

    def add_metrics_event(self, event: Any) -> None:
        payload = self._as_dict(event)
        if not payload:
            return
        self.provider_usage.append(payload)
        if self.has_session_usage:
            # session_usage_updated is the authoritative session-level source
            # from LiveKit; keep plugin metrics only for debugging context.
            return
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        merged = {}
        if isinstance(metrics, dict):
            merged.update(metrics)
        if isinstance(usage, dict):
            merged.update(usage)
        merged.update(payload)

        self.llm_input_tokens += max(
            0,
            _to_int(
                merged.get("input_tokens")
                or merged.get("prompt_tokens")
                or merged.get("llm_input_tokens")
            ),
        )
        self.llm_output_tokens += max(
            0,
            _to_int(
                merged.get("output_tokens")
                or merged.get("completion_tokens")
                or merged.get("llm_output_tokens")
            ),
        )
        self.llm_cached_tokens += max(
            0,
            _to_int(
                merged.get("cached_tokens")
                or merged.get("prompt_cached_tokens")
            ),
        )

        stt_secs = (
            merged.get("stt_seconds")
            or merged.get("audio_duration_seconds")
            or merged.get("audio_duration")
            or merged.get("speech_seconds")
        )
        self.stt_seconds += max(0.0, _to_float(stt_secs))

        if not self.tts_characters:
            self.tts_characters += max(
                0,
                _to_int(
                    merged.get("tts_characters")
                    or merged.get("characters")
                    or merged.get("characters_count")
                ),
            )

    def apply_session_usage_updated(self, event: Any) -> None:
        payload = self._as_dict(event)
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        model_usage = usage.get("model_usage")
        if not isinstance(model_usage, list):
            model_usage = payload.get("model_usage")
        if not isinstance(model_usage, list):
            return

        llm_in = 0
        llm_out = 0
        llm_cached = 0
        stt_secs = 0.0
        tts_chars = 0

        for item in model_usage:
            row = self._as_dict(item)
            llm_in += max(
                0,
                _to_int(
                    row.get("input_tokens")
                    or row.get("prompt_tokens")
                    or row.get("llm_input_tokens")
                ),
            )
            llm_out += max(
                0,
                _to_int(
                    row.get("output_tokens")
                    or row.get("completion_tokens")
                    or row.get("llm_output_tokens")
                ),
            )
            llm_cached += max(
                0,
                _to_int(
                    row.get("cached_tokens")
                    or row.get("prompt_cached_tokens")
                    or row.get("llm_cached_tokens")
                ),
            )
            stt_secs += max(
                0.0,
                _to_float(
                    row.get("stt_seconds")
                    or row.get("audio_duration_seconds")
                    or row.get("audio_duration")
                    or row.get("duration_seconds")
                    or row.get("speech_seconds")
                ),
            )
            tts_chars += max(
                0,
                _to_int(
                    row.get("tts_characters")
                    or row.get("characters_count")
                    or row.get("characters")
                ),
            )

        self.llm_input_tokens = llm_in
        self.llm_output_tokens = llm_out
        self.llm_cached_tokens = llm_cached
        self.stt_seconds = stt_secs
        if tts_chars > 0:
            self.tts_characters = tts_chars
        self.has_session_usage = True

    def snapshot(self) -> dict[str, Any]:
        return {
            "stt_seconds": round(self.stt_seconds, 3),
            "tts_characters": int(self.tts_characters),
            "llm_input_tokens": int(self.llm_input_tokens),
            "llm_output_tokens": int(self.llm_output_tokens),
            "llm_cached_tokens": int(self.llm_cached_tokens),
            "provider_usage": self.provider_usage[-20:],
        }

    @staticmethod
    def _as_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if hasattr(value, "dict"):
            try:
                raw = value.dict()
                if isinstance(raw, dict):
                    return raw
            except Exception:
                return {}
        if hasattr(value, "__dict__"):
            raw = getattr(value, "__dict__", {})
            if isinstance(raw, dict):
                return {k: v for k, v in raw.items() if not str(k).startswith("_")}
        return {}
