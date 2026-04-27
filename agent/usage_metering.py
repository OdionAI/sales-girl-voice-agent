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
    provider_usage: list[dict[str, Any]] = field(default_factory=list)

    def add_assistant_text(self, content: str) -> None:
        self.tts_characters += len(str(content or ""))

    def add_metrics_event(self, event: Any) -> None:
        payload = self._as_dict(event)
        if not payload:
            return
        self.provider_usage.append(payload)
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else payload

        self.llm_input_tokens += max(
            0,
            _to_int(
                usage.get("input_tokens")
                or usage.get("prompt_tokens")
                or usage.get("llm_input_tokens")
            ),
        )
        self.llm_output_tokens += max(
            0,
            _to_int(
                usage.get("output_tokens")
                or usage.get("completion_tokens")
                or usage.get("llm_output_tokens")
            ),
        )
        self.llm_cached_tokens += max(0, _to_int(usage.get("cached_tokens")))

        stt_secs = (
            usage.get("stt_seconds")
            or usage.get("audio_duration_seconds")
            or usage.get("audio_duration")
            or usage.get("speech_seconds")
        )
        self.stt_seconds += max(0.0, _to_float(stt_secs))

        if not self.tts_characters:
            self.tts_characters += max(0, _to_int(usage.get("tts_characters") or usage.get("characters")))

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
