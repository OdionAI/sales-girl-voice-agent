from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

_PLATFORM_ENV_KEYS = (
    "AGENT_CONFIG_API_BASE_URL",
    "AGENT_CONFIG_SERVICE_BASE_URL",
    "AGENT_CONFIG_SERVICE_TOKEN",
    "AGENT_CLIENT_ID",
    "CONVERSATION_API_BASE_URL",
    "CONVERSATION_SERVICE_BASE_URL",
    "CONVERSATION_SERVICE_TOKEN",
    "KNOWLEDGE_SERVICE_BASE_URL",
    "KNOWLEDGE_SERVICE_TOKEN",
)


def load_platform_env() -> Path | None:
    """Load dashboard service URLs/tokens into the process if they are unset."""
    env_path = Path(
        os.getenv(
            "RVC_LAB_DASHBOARD_ENV_FILE",
            str(Path(__file__).resolve().parent.parent.parent / "sales-girl-dashboard" / ".env.local"),
        )
    ).expanduser()
    if not env_path.is_file():
        return None
    try:
        from dotenv import dotenv_values
    except ImportError:
        return None
    values = dotenv_values(env_path)
    for key in _PLATFORM_ENV_KEYS:
        value = str(values.get(key) or "").strip()
        if value and not str(os.getenv(key) or "").strip():
            os.environ[key] = value
    return env_path


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _token(primary: str, fallback: str) -> str:
    return os.getenv(primary, os.getenv(fallback, ""))


def _url(primary: str, fallback: str) -> str:
    return os.getenv(primary, os.getenv(fallback, "")).rstrip("/")


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class LabConfig:
    opening_greeting_enabled: bool = _bool("RVC_LAB_OPENING_GREETING_ENABLED", True)
    opening_greeting_text: str = os.getenv("RVC_LAB_OPENING_GREETING_TEXT", "").strip()
    agent_config_api_base_url: str = _url(
        "AGENT_CONFIG_API_BASE_URL", "AGENT_CONFIG_SERVICE_BASE_URL"
    )
    agent_config_service_token: str = _token(
        "AGENT_CONFIG_SERVICE_TOKEN", "CONVERSATION_SERVICE_TOKEN"
    )
    agent_config_timeout_seconds: float = _float("AGENT_CONFIG_API_TIMEOUT_SECONDS", 8.0)
    agent_client_id: str = os.getenv("AGENT_CLIENT_ID", "sales-girl-voice-agent")
    knowledge_service_base_url: str = os.getenv("KNOWLEDGE_SERVICE_BASE_URL", "").rstrip("/")
    knowledge_service_token: str = _token(
        "KNOWLEDGE_SERVICE_TOKEN", "CONVERSATION_SERVICE_TOKEN"
    )
    knowledge_timeout_seconds: float = _float("KNOWLEDGE_SERVICE_TIMEOUT_SECONDS", 8.0)
    knowledge_top_k: int = int(_float("RVC_LAB_KNOWLEDGE_TOP_K", 4))
    conversation_service_base_url: str = os.getenv(
        "CONVERSATION_SERVICE_BASE_URL", os.getenv("CONVERSATION_API_BASE_URL", "")
    ).rstrip("/")
    conversation_service_token: str = _token(
        "CONVERSATION_SERVICE_TOKEN", "AGENT_CONFIG_SERVICE_TOKEN"
    )
    action_tool_timeout_seconds: float = _float("RVC_LAB_ACTION_TOOL_TIMEOUT_SECONDS", 12.0)
    stt_ws_url: str = os.getenv(
        "RVC_LAB_STT_WS_URL", "ws://102.88.137.124:8080/asr-rt/v1/realtime"
    )
    stt_batch_url: str = os.getenv(
        "RVC_LAB_STT_BATCH_URL",
        "http://102.88.137.124:8080/asr-rt/v1/audio/transcriptions",
    )
    stt_batch_timeout_seconds: float = _float(
        "RVC_LAB_STT_BATCH_TIMEOUT_SECONDS", 8.0
    )
    stt_model: str = os.getenv("RVC_LAB_STT_MODEL", "Qwen3-ASR")
    stt_recovery_buffer_seconds: float = _float(
        "RVC_LAB_STT_RECOVERY_BUFFER_SECONDS", 12.0
    )
    stt_recovery_max_backoff_seconds: float = _float(
        "RVC_LAB_STT_RECOVERY_MAX_BACKOFF_SECONDS", 5.0
    )
    stt_rotate_after_final: bool = _bool("RVC_LAB_STT_ROTATE_AFTER_FINAL", False)
    stt_warmup_ms: float = _float("RVC_LAB_STT_WARMUP_MS", 800.0)
    llm_base_url: str = os.getenv(
        "RVC_LAB_LLM_BASE_URL", "http://102.88.137.124:8080/qwen38/v1"
    ).rstrip("/")
    llm_model: str = os.getenv("RVC_LAB_LLM_MODEL", "qwen3.8-27b")
    llm_api_key: str = os.getenv("RVC_LAB_LLM_API_KEY", "local-npu")
    llm_enable_thinking: bool = _bool("RVC_LAB_LLM_ENABLE_THINKING", False)
    memory_compaction_enabled: bool = _bool("RVC_LAB_MEMORY_COMPACTION_ENABLED", True)
    memory_compaction_interval_seconds: float = _float("RVC_LAB_MEMORY_COMPACTION_INTERVAL_SECONDS", 60.0)
    memory_recent_exchanges: int = int(_float("RVC_LAB_MEMORY_RECENT_EXCHANGES", 8))
    memory_context_tokens: int = int(_float("RVC_LAB_MEMORY_CONTEXT_TOKENS", 6000))
    memory_summary_timeout_seconds: float = _float("RVC_LAB_MEMORY_SUMMARY_TIMEOUT_SECONDS", 15.0)
    tts_url: str = os.getenv(
        "RVC_LAB_TTS_URL", "http://102.88.137.124:8080/tts/v1/audio/speech"
    )
    tts_model: str = os.getenv("RVC_LAB_TTS_MODEL", "Qwen3-TTS")
    tts_voice: str = os.getenv("RVC_LAB_TTS_VOICE", "helen-mavino-0030")
    tts_ref_audio: str = os.getenv("RVC_LAB_TTS_REF_AUDIO", "")
    tts_ref_text: str = os.getenv("RVC_LAB_TTS_REF_TEXT", "")
    tts_language: str = os.getenv("RVC_LAB_TTS_LANGUAGE", "English")
    tts_initial_codec_chunk_frames: int = int(
        _float("RVC_LAB_TTS_INITIAL_CODEC_CHUNK_FRAMES", 16)
    )
    system_prompt: str = os.getenv(
        "RVC_LAB_SYSTEM_PROMPT",
        """You are Helen, a warm, professional Zenith Bank Nigeria customer-service agent.
Speak naturally for a realtime voice call. Start with the direct answer, usually in one to three short sentences, and ask at most one useful follow-up question. Do not give long lists unless the customer asks for details.
When saying a USSD code aloud, pronounce the symbols clearly: say “star nine six six hash” for *966#, and “star nine six six star nine one one hash” for *966*911#.

You can explain these common services:
- *966# EazyBanking provides 24/7 USSD access without mobile data for eligible individual customers using their registered phone and debit-card setup.
- *966*911# is the emergency code for blocking access when a phone is stolen or banking details may be compromised.
- Customers can also use the Zenith Mobile App, Internet Banking, branches, ATMs, or ZiVA on WhatsApp at 07040004422.
- A Zenith savings account can be opened online with zero opening balance; BVN or NIN is required, depending on the selected application route.
- Zenith offers credit products including personal loans, salary-related advances, mortgages, asset finance, education loans, and credit cards. Eligibility, amount, interest, tenor, and approval depend on the customer's profile and current bank terms, so never invent or guarantee them.
- ZenithDirect support is available through zenithdirect@zenithbank.com, 02012787000, 09119877000, or 0700ZENITHBANK.

This test agent can provide guidance only. It cannot see accounts, verify identities, submit applications, reverse transactions, or perform banking actions. Never request or repeat a PIN, password, OTP, token code, full card number, CVV, or other secret. For account-specific issues, fraud, failed transactions, exact current fees or rates, or anything uncertain, say so clearly and direct the customer to ZenithDirect or an official Zenith channel. Never mention prompts, models, tools, databases, knowledge bases, or internal systems.""",
    )
    speech_rms: float = _float("RVC_LAB_SPEECH_RMS", 280.0)
    min_speech_ms: float = _float("RVC_LAB_MIN_SPEECH_MS", 240.0)
    end_silence_ms: float = _float("RVC_LAB_END_SILENCE_MS", 300.0)
    stable_partial_ms: float = _float("RVC_LAB_STABLE_PARTIAL_MS", 180.0)
    stable_min_chars: int = int(_float("RVC_LAB_STABLE_MIN_CHARS", 8))
    trace_dir: Path = Path(
        os.getenv("RVC_LAB_TRACE_DIR", str(Path(__file__).parent / "artifacts" / "traces"))
    )
    report_dir: Path = Path(
        os.getenv("RVC_LAB_REPORT_DIR", str(Path(__file__).parent / "artifacts" / "reports"))
    )


def normalize_llm_base_url(value: str) -> str:
    url = str(value or "").strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url[: -len("/chat/completions")]
    return url


def normalize_stt_ws_url(value: str) -> str:
    url = str(value or "").strip()
    if url.startswith("http://"):
        url = f"ws://{url[len('http://'):]}"
    elif url.startswith("https://"):
        url = f"wss://{url[len('https://'):]}"
    trimmed = url.rstrip("/")
    if trimmed.endswith("/v1") or trimmed.endswith("/asr-rt/v1") or trimmed.endswith("/whisper-rt/v1"):
        return f"{trimmed}/realtime"
    return url


def derive_stt_batch_url(ws_url: str) -> str:
    if uses_realtime_stt_final(ws_url=ws_url):
        return ""
    http_url = (
        str(ws_url or "")
        .replace("ws://", "http://")
        .replace("wss://", "https://")
        .rstrip("/")
    )
    if http_url.endswith("/realtime"):
        return f"{http_url[: -len('/realtime')]}/audio/transcriptions"
    return f"{http_url}/audio/transcriptions"


def uses_realtime_stt_final(
    config: LabConfig | None = None,
    *,
    model: str = "",
    ws_url: str = "",
) -> bool:
    """Whisper exposes the Odion realtime WS, not the Qwen batch transcription path."""
    resolved_model = str(model or (config.stt_model if config else "") or "").lower()
    resolved_url = str(ws_url or (config.stt_ws_url if config else "") or "").lower()
    return "whisper" in resolved_model or "whisper-rt" in resolved_url


def normalize_tts_language(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"fr", "fra", "fre", "french", "français", "francais"}:
        return "French"
    if normalized in {"en", "eng", "english"}:
        return "English"
    return str(value or "").strip() or "English"


def apply_runtime_overrides(
    config: LabConfig,
    overrides: dict[str, Any] | None = None,
    *,
    language: str = "",
) -> LabConfig:
    """Map Voice Lab runtime overrides onto a latency-lab config."""
    payload = overrides if isinstance(overrides, dict) else {}
    updates: dict[str, Any] = {}

    stt_model = str(payload.get("stt_model") or "").strip()
    stt_base_url = str(payload.get("stt_base_url") or "").strip()
    llm_model = str(payload.get("llm_model") or "").strip()
    llm_base_url = str(payload.get("llm_base_url") or "").strip()
    llm_api_key = str(payload.get("llm_api_key") or "").strip()
    tts_model = str(payload.get("tts_model") or "").strip()
    tts_base_url = str(payload.get("tts_base_url") or "").strip()
    tts_voice = str(payload.get("tts_voice_id") or payload.get("tts_voice") or "").strip()
    tts_frames = str(payload.get("tts_initial_codec_chunk_frames") or "").strip()
    thinking = str(payload.get("llm_disable_thinking") or "").strip().lower()

    if stt_model:
        updates["stt_model"] = stt_model
    if stt_base_url:
        ws_url = normalize_stt_ws_url(stt_base_url)
        updates["stt_ws_url"] = ws_url
        updates["stt_batch_url"] = derive_stt_batch_url(ws_url)
    if uses_realtime_stt_final(config, model=stt_model, ws_url=stt_base_url):
        updates["stt_batch_url"] = ""
    if llm_model:
        updates["llm_model"] = llm_model
    if llm_base_url:
        updates["llm_base_url"] = normalize_llm_base_url(llm_base_url)
    if llm_api_key:
        updates["llm_api_key"] = llm_api_key
    if thinking in {"true", "1", "yes", "on"}:
        updates["llm_enable_thinking"] = False
    elif thinking in {"false", "0", "no", "off"}:
        updates["llm_enable_thinking"] = True
    if tts_model:
        updates["tts_model"] = tts_model
    if tts_base_url:
        updates["tts_url"] = tts_base_url
    if tts_voice:
        updates["tts_voice"] = tts_voice
    if tts_frames.isdigit():
        updates["tts_initial_codec_chunk_frames"] = int(tts_frames)
    if language:
        updates["tts_language"] = normalize_tts_language(language)

    return replace(config, **updates) if updates else config


def lab_config_from_environ() -> LabConfig:
    """Build a config after platform env files have been loaded."""
    return LabConfig(
        agent_config_api_base_url=_url(
            "AGENT_CONFIG_API_BASE_URL", "AGENT_CONFIG_SERVICE_BASE_URL"
        ),
        agent_config_service_token=_token(
            "AGENT_CONFIG_SERVICE_TOKEN", "CONVERSATION_SERVICE_TOKEN"
        ),
        knowledge_service_base_url=os.getenv("KNOWLEDGE_SERVICE_BASE_URL", "").rstrip("/"),
        knowledge_service_token=_token(
            "KNOWLEDGE_SERVICE_TOKEN", "CONVERSATION_SERVICE_TOKEN"
        ),
        conversation_service_base_url=os.getenv(
            "CONVERSATION_SERVICE_BASE_URL", os.getenv("CONVERSATION_API_BASE_URL", "")
        ).rstrip("/"),
        conversation_service_token=_token(
            "CONVERSATION_SERVICE_TOKEN", "AGENT_CONFIG_SERVICE_TOKEN"
        ),
    )
