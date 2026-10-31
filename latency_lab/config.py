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


def _ids(name: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in os.getenv(name, "").split(",") if value.strip())


@dataclass(frozen=True)
class LabConfig:
    speech_language_override: str = ""
    engagement_enabled: bool = _bool("RVC_LAB_ENGAGEMENT_ENABLED", False)
    inactivity_pilot_agent_ids: tuple[str, ...] = _ids("RVC_LAB_INACTIVITY_PILOT_AGENT_IDS")
    inactivity_pilot_enabled: bool = False  # Set only for an eligible RVC browser/SIP call.
    inactivity_pilot_close_seconds: float = _float("RVC_LAB_INACTIVITY_PILOT_CLOSE_SECONDS", 15.0)
    # Run bank tools as background tasks so a long or failing call never blocks the
    # conversation: the agent acknowledges naturally and delivers the result later.
    # Opt-in (deployments enable it with RVC_LAB_BACKGROUND_TOOLS=true).
    background_tools: bool = _bool("RVC_LAB_BACKGROUND_TOOLS", False)
    engagement_ack_seconds: float = _float("RVC_LAB_ENGAGEMENT_ACK_SECONDS", 0.3)
    engagement_tool_seconds: float = _float("RVC_LAB_ENGAGEMENT_TOOL_SECONDS", 8.0)
    engagement_idle_seconds: float = _float("RVC_LAB_ENGAGEMENT_IDLE_SECONDS", 18.0)
    engagement_generation_timeout_seconds: float = _float("RVC_LAB_ENGAGEMENT_GENERATION_TIMEOUT_SECONDS", 6.0)
    engagement_max_tool_updates: int = int(_float("RVC_LAB_ENGAGEMENT_MAX_TOOL_UPDATES", 3))
    engagement_max_idle_updates: int = int(_float("RVC_LAB_ENGAGEMENT_MAX_IDLE_UPDATES", 1))
    engagement_bank_tool_rounds: int = int(_float("RVC_LAB_ENGAGEMENT_BANK_TOOL_ROUNDS", 8))
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
    billing_service_base_url: str = _url("BILLING_SERVICE_BASE_URL", "BILLING_API_BASE_URL")
    billing_service_token: str = _token("BILLING_SERVICE_TOKEN", "CONVERSATION_SERVICE_TOKEN")
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
    # Hint-capable authoritative final (Qwen3-ASR chat endpoint). When stt_chat_url
    # is set the committed final comes from there WITH a vocabulary hint
    # (stt_context); the realtime socket still supplies speculative partials.
    stt_chat_url: str = os.getenv("RVC_LAB_STT_CHAT_URL", "").strip()
    stt_chat_model: str = os.getenv("RVC_LAB_STT_CHAT_MODEL", "Qwen3-ASR")
    stt_context: str = os.getenv("RVC_LAB_STT_CONTEXT", "").strip()
    # Only agents/businesses in this list receive the hinted chat final; empty
    # means "every agent". Keeps a Wema-specific hint off other tenants.
    stt_chat_scope: tuple[str, ...] = tuple(
        item.strip() for item in os.getenv("RVC_LAB_STT_CHAT_SCOPE", "").replace(",", " ").split() if item.strip()
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
    llm_provider: str = "qwen"
    llm_reasoning_effort: str = "high"
    llm_enable_thinking: bool = _bool("RVC_LAB_LLM_ENABLE_THINKING", False)
    # If the provider returns a 200 with no spoken text and no tool call, re-issue
    # the request a few times before giving up. Off by default; deployments turn it
    # on with RVC_LAB_LLM_EMPTY_RETRIES.
    llm_empty_retries: int = int(_float("RVC_LAB_LLM_EMPTY_RETRIES", 0))
    llm_empty_retry_delay_seconds: float = _float("RVC_LAB_LLM_EMPTY_RETRY_DELAY_SECONDS", 0.4)
    # A stalling or empty provider attempt must not leave the caller in dead air:
    # abandon a request that has produced no usable output for this many seconds.
    llm_first_output_timeout_seconds: float = _float("RVC_LAB_LLM_FIRST_OUTPUT_TIMEOUT_SECONDS", 5.0)
    # When a committed caller turn still yields nothing after the retries, speak a
    # short graceful line instead of going silent. Off by default.
    empty_response_fallback_enabled: bool = _bool("RVC_LAB_EMPTY_RESPONSE_FALLBACK", False)
    empty_response_fallback_text: str = os.getenv(
        "RVC_LAB_EMPTY_RESPONSE_FALLBACK_TEXT",
        "I'm sorry, I'm not able to help with that right now. Please try again in a moment.",
    ).strip()
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
    tts_seed: str = os.getenv("RVC_LAB_TTS_SEED", "")
    hausa_twb_voices_enabled: bool = _bool("RVC_LAB_ENABLE_HAUSA_TWB_VOICES", False)
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
    # Speech-probability VAD gate. RNNoise reports a per-frame speech
    # probability (0..1). When the input filter supplies it, a frame must ALSO
    # clear this floor to count as speech, so a loud non-speech burst (TV,
    # music, room noise) cannot start or sustain a caller turn. Set <= 0 to
    # disable and fall back to the loudness threshold alone.
    speech_prob_min: float = _float("RVC_LAB_SPEECH_PROB_MIN", 0.35)
    # Reject a committed utterance before ASR final when it is below these
    # floors (a tap, cough or line blip). 0 disables each guard. The primary
    # defense against noise blips is speech_prob_min; these are a safety net.
    min_utterance_ms: float = _float("RVC_LAB_MIN_UTTERANCE_MS", 0.0)
    min_voiced_frame_percent: float = _float("RVC_LAB_MIN_VOICED_FRAME_PERCENT", 0.0)
    reply_grace_agent_ids: tuple[str, ...] = _ids("RVC_LAB_REPLY_GRACE_AGENT_IDS")
    reply_grace_ms: float = _float("RVC_LAB_REPLY_GRACE_MS", 0.0)
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
    if normalized in {"yo", "yor", "yoruba", "yorùbá"}:
        return "Yoruba"
    if normalized in {"ha", "hau", "hausa"}:
        return "Hausa"
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
    if payload.get("tts_language_hint"):
        updates["speech_language_override"] = str(payload["tts_language_hint"])
    # Sessions may opt out, but cannot enable an operator-disabled experiment.
    engagement = payload.get("engagement_enabled")
    if engagement is False or engagement == "false":
        updates["engagement_enabled"] = False

    stt_model = str(payload.get("stt_model") or "").strip()
    stt_base_url = str(payload.get("stt_base_url") or "").strip()
    llm_model = str(payload.get("llm_model") or "").strip()
    llm_base_url = str(payload.get("llm_base_url") or "").strip()
    llm_api_key = str(payload.get("llm_api_key") or "").strip()
    tts_model = str(payload.get("tts_model") or "").strip()
    tts_base_url = str(payload.get("tts_base_url") or "").strip()
    tts_voice = str(payload.get("tts_voice_id") or payload.get("tts_voice") or "").strip()
    tts_frames = str(payload.get("tts_initial_codec_chunk_frames") or "").strip()
    tts_seed = str(payload.get("tts_seed") or "").strip()
    thinking = str(payload.get("llm_disable_thinking") or "").strip().lower()

    if stt_model:
        updates["stt_model"] = stt_model
    if stt_base_url:
        ws_url = normalize_stt_ws_url(stt_base_url)
        updates["stt_ws_url"] = ws_url
        updates["stt_batch_url"] = derive_stt_batch_url(ws_url)
    if uses_realtime_stt_final(config, model=stt_model, ws_url=stt_base_url):
        updates["stt_batch_url"] = ""
    stt_context = str(payload.get("stt_context") or "").strip()
    if stt_context:
        updates["stt_context"] = stt_context
    # The Lagos release's operator-selected provider is authoritative. Session
    # metadata must not silently switch its endpoint or credential.
    if config.llm_provider != "deepseek":
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
    if tts_seed:
        updates["tts_seed"] = tts_seed
    if tts_frames.isdigit():
        updates["tts_initial_codec_chunk_frames"] = int(tts_frames)
    if language:
        updates["tts_language"] = normalize_tts_language(language)

    return replace(config, **updates) if updates else config


# Voices enrolled on the TTS server that an agent's own configured voice may
# use. An agent's `tts_voice_id` is only honoured for voices listed here, so an
# agent carrying a stale/unenrolled voice id keeps the operator default instead
# of failing every utterance. Extend or override with the comma-separated
# RVC_LAB_AGENT_VOICES env var when a new clone is enrolled.
_AGENT_VOICE_ALLOWLIST_DEFAULT = (
    "helen-mavino-0030,"
    "wema-en-ng-female,wema-en-ng-male,wema-en-us-female,wema-en-us-male,"
    "wema-en-ng-female-short,wema-en-ng-male-short,"
    "wema-en-us-female-short,wema-en-us-male-short,"
    "access-ghana-cindy"
)


def agent_voice_allowlist() -> frozenset[str]:
    """Voice ids whose enrolled clone may be used from an agent's config."""
    raw = os.getenv("RVC_LAB_AGENT_VOICES", _AGENT_VOICE_ALLOWLIST_DEFAULT)
    return frozenset(item.strip().lower() for item in raw.split(",") if item.strip())


def apply_agent_configured_voice(
    config: LabConfig,
    agent_context: Any,
    overrides: dict[str, Any] | None = None,
) -> LabConfig:
    """Use the agent's own configured voice as the call's baseline voice.

    A per-session override (the dashboard's Wema accent/gender choice) always
    wins. When no session voice is chosen, the agent's configured tts_voice_id
    is used — but only if it is a voice the platform knows is enrolled, so a
    stale voice id can never silence the call.
    """
    payload = overrides if isinstance(overrides, dict) else {}
    session_choice = str(
        payload.get("tts_voice_id") or payload.get("tts_voice") or ""
    ).strip()
    if session_choice:
        return config
    configured = str(getattr(agent_context, "tts_voice_id", "") or "").strip()
    if not configured or configured.lower() not in agent_voice_allowlist():
        return config
    return replace(config, tts_voice=configured)


def for_inactivity_pilot(
    config: LabConfig, *, agent_id: str, is_browser: bool, is_sip: bool = False,
) -> LabConfig:
    """Enable inactivity handling for selected agents on RVC browser/SIP calls."""
    selected = ("*" in config.inactivity_pilot_agent_ids
                or agent_id in config.inactivity_pilot_agent_ids)
    if not (is_browser or is_sip) or not selected:
        return config
    return replace(
        config,
        engagement_enabled=True,
        engagement_idle_seconds=10.0,
        engagement_tool_seconds=10.0,
        engagement_max_idle_updates=1,
        inactivity_pilot_enabled=True,
    )


def lab_config_from_environ() -> LabConfig:
    """Build a config after platform env files have been loaded."""
    provider = os.getenv("RVC_LAB_LLM_PROVIDER", "qwen").strip().lower()
    if provider not in {"qwen", "deepseek"}:
        raise ValueError(f"Unsupported RVC_LAB_LLM_PROVIDER: {provider}")
    llm_base_url = os.getenv("RVC_LAB_LLM_BASE_URL", "http://102.88.137.124:8080/qwen38/v1").rstrip("/")
    llm_model = os.getenv("RVC_LAB_LLM_MODEL", "qwen3.8-27b")
    llm_api_key = os.getenv("RVC_LAB_LLM_API_KEY", "local-npu")
    llm_enable_thinking = _bool("RVC_LAB_LLM_ENABLE_THINKING", False)
    reasoning_effort = os.getenv("RVC_LAB_LLM_REASONING_EFFORT", "high").strip().lower()
    if provider == "deepseek":
        llm_base_url = os.getenv("RVC_LAB_DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        llm_model = os.getenv("RVC_LAB_DEEPSEEK_MODEL", "deepseek-flash").strip()
        llm_api_key = os.getenv("RVC_LAB_DEEPSEEK_API_KEY", "").strip()
        if not llm_api_key:
            raise ValueError("RVC_LAB_DEEPSEEK_API_KEY is required when DeepSeek is selected")
        llm_enable_thinking = _bool("RVC_LAB_DEEPSEEK_THINKING", False)
        reasoning_effort = os.getenv("RVC_LAB_DEEPSEEK_REASONING_EFFORT", "high").strip().lower()
        if reasoning_effort not in {"low", "high", "max"}:
            raise ValueError("RVC_LAB_DEEPSEEK_REASONING_EFFORT must be low, high, or max")
    return LabConfig(
        llm_provider=provider,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        llm_enable_thinking=llm_enable_thinking,
        llm_reasoning_effort=reasoning_effort,
        hausa_twb_voices_enabled=_bool("RVC_LAB_ENABLE_HAUSA_TWB_VOICES", False),
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
        billing_service_base_url=_url("BILLING_SERVICE_BASE_URL", "BILLING_API_BASE_URL"),
        billing_service_token=_token("BILLING_SERVICE_TOKEN", "CONVERSATION_SERVICE_TOKEN"),
    )
