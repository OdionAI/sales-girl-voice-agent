//! Native RVC conversation coordinator and unchanged-provider wire adapters.
pub mod actions;
pub(crate) mod banking;
pub mod bootstrap;
pub mod browser;
pub mod capacity;
pub(crate) mod conversation;
pub mod coordinator;
pub(crate) mod directory;
pub mod engagement;
pub mod environment;
pub mod identity;
pub mod input_noise;
pub mod languages;
pub mod memory;
pub mod outbound;
pub(crate) mod passkey;
pub(crate) mod passkey_demo;
pub mod platform;
pub mod profiles;
mod prompts;
pub mod providers;
pub(crate) mod recording;
pub mod report;
pub mod sip;
mod text;
pub mod transfer;
mod transfer_stream;
pub mod usage;
use serde::{Deserialize, Serialize};
use serde_json::Value;
#[derive(Clone, Deserialize, Serialize)]
pub struct Config {
    pub baseline: String,
    #[serde(default)]
    pub client_session_id: String,
    #[serde(default = "default_enabled")]
    pub persist_conversation: bool,
    #[serde(default)]
    pub report_dir: Option<std::path::PathBuf>,
    #[serde(default)]
    pub explicitly_routed_agent: bool,
    pub opening_greeting_enabled: bool,
    pub resolved_opening: String,
    #[serde(default)]
    pub opening_greeting_text: String,
    pub base_system_prompt: String,
    pub speech_gate_rules: String,
    pub speech_gate_tool: Value,
    pub tools: Vec<Value>,
    #[serde(default)]
    pub agent_context: profiles::AgentContext,
    #[serde(default)]
    pub input_noise_suppression: bool,
    #[serde(default)]
    pub input_noise_agent_ids: Vec<String>,
    #[serde(default = "default_noise_implementation")]
    pub input_noise_implementation: String,
    #[serde(default = "default_transport")]
    pub transport: String,
    #[serde(default)]
    pub reply_grace_agent_ids: Vec<String>,
    #[serde(default)]
    pub reply_grace_ms: f64,
    #[serde(default = "profiles::default_voice_allowlist")]
    pub agent_voice_allowlist: Vec<String>,
    #[serde(default)]
    pub platform: Option<platform::PlatformConfig>,
    #[serde(default)]
    pub banking: banking::Config,
    #[serde(default)]
    pub passkey: passkey::Config,
    #[serde(default)]
    pub directory: directory::Config,
    #[serde(default)]
    pub passkey_demo: passkey_demo::Config,
    #[serde(default)]
    pub recording: recording::Config,
    #[serde(default)]
    pub transfer: transfer::Config,
    pub stt_ws_url: String,
    pub stt_batch_url: String,
    pub stt_chat_url: String,
    pub stt_chat_model: String,
    pub stt_context: String,
    #[serde(default)]
    pub stt_chat_scope: Vec<String>,
    pub stt_model: String,
    pub stt_batch_timeout_seconds: f64,
    pub stt_warmup_ms: f64,
    pub stt_recovery_buffer_seconds: f64,
    /// Experimental bounded realtime flush; zero keeps the checkpoint path.
    #[serde(default)]
    pub stt_speculative_flush_ms: f64,
    pub llm_base_url: String,
    pub llm_model: String,
    pub llm_api_key: String,
    pub llm_enable_thinking: bool,
    #[serde(default = "default_reasoning_effort")]
    pub llm_reasoning_effort: String,
    pub llm_provider: String,
    pub llm_first_output_timeout_seconds: f64,
    pub llm_empty_retries: usize,
    pub llm_empty_retry_delay_seconds: f64,
    /// Opt-in parallel routing for non-thinking DeepSeek; legacy guard stays default.
    #[serde(default)]
    pub transfer_streaming_replies: bool,
    pub tts_url: String,
    pub tts_model: String,
    pub tts_voice: String,
    #[serde(default)]
    pub speech_language_override: String,
    #[serde(default)]
    pub speech_gateway_url: String,
    #[serde(default)]
    pub hausa_twb_voices_enabled: bool,
    #[serde(default)]
    pub tts_ref_audio: String,
    #[serde(default)]
    pub tts_ref_text: String,
    pub tts_language: String,
    pub tts_resolved_seed: String,
    #[serde(default)]
    pub tts_seed: String,
    #[serde(default = "profiles::default_wema_seed")]
    pub tts_wema_seed: String,
    pub tts_initial_codec_chunk_frames: usize,
    pub speech_rms: f64,
    pub min_speech_ms: f64,
    pub end_silence_ms: f64,
    #[serde(default = "default_speech_prob_min")]
    pub speech_prob_min: f64,
    #[serde(default)]
    pub min_utterance_ms: f64,
    #[serde(default)]
    pub min_voiced_frame_percent: f64,
    #[serde(default = "default_max_utterance")]
    pub max_utterance_seconds: f64,
    pub stable_partial_ms: f64,
    pub stable_min_chars: usize,
    pub engagement_enabled: bool,
    pub memory_compaction_enabled: bool,
    #[serde(default = "default_memory_interval")]
    pub memory_compaction_interval_seconds: f64,
    #[serde(default = "default_memory_keep")]
    pub memory_recent_exchanges: usize,
    #[serde(default = "default_memory_budget")]
    pub memory_context_tokens: usize,
    #[serde(default = "default_memory_timeout")]
    pub memory_summary_timeout_seconds: f64,
    #[serde(default = "default_idle_seconds")]
    pub engagement_idle_seconds: f64,
    #[serde(default = "default_tool_seconds")]
    pub engagement_tool_seconds: f64,
    #[serde(default = "default_tool_ack")]
    pub engagement_ack_seconds: f64,
    #[serde(default = "default_tool_updates")]
    pub engagement_max_tool_updates: usize,
    #[serde(default = "default_bank_rounds")]
    pub engagement_bank_tool_rounds: usize,
    #[serde(default)]
    pub background_tools: bool,
    #[serde(default = "default_engagement_timeout")]
    pub engagement_generation_timeout_seconds: f64,
    #[serde(default = "default_idle_updates")]
    pub engagement_max_idle_updates: usize,
    #[serde(default)]
    pub inactivity_pilot_enabled: bool,
    #[serde(default)]
    pub inactivity_pilot_agent_ids: Vec<String>,
    #[serde(default = "default_inactivity_close")]
    pub inactivity_pilot_close_seconds: f64,
    #[serde(default = "default_recovery_backoff")]
    pub stt_recovery_max_backoff_seconds: f64,
    #[serde(default)]
    pub empty_response_fallback_enabled: bool,
    #[serde(default = "default_empty_fallback")]
    pub empty_response_fallback_text: String,
}
fn default_max_utterance() -> f64 {
    1800.0
}
fn default_speech_prob_min() -> f64 {
    0.35
}
impl Config {
    pub fn load(path: &std::path::Path) -> Result<Self, String> {
        let mut c: Self =
            serde_json::from_slice(&std::fs::read(path).map_err(|_| "configuration read failed")?)
                .map_err(|e| format!("configuration schema: {e}"))?;
        c.tts_language = languages::normalize(&c.tts_language);
        let context = c.agent_context.clone();
        languages::apply(&mut c, &context, &Value::Null)?;
        c.tts_resolved_seed = profiles::resolve_tts_seed(&c);
        if c.tts_language != "English" {
            c.resolved_opening = profiles::opening_text(&c, &context);
            c.base_system_prompt
                .push_str(&languages::rules(&c.tts_language));
        }
        c.validate()?;
        Ok(c)
    }
    pub fn validate(&self) -> Result<(), String> {
        if !self.max_utterance_seconds.is_finite()
            || !(1.0..=3600.0).contains(&self.max_utterance_seconds)
        {
            return Err("invalid utterance audio resource limit".into());
        }
        self.banking.validate()?;
        self.passkey.validate()?;
        self.directory.validate()?;
        self.passkey_demo.validate()?;
        self.recording.validate()?;
        if self.baseline != "d3f90112d00c38868748b3101ae156e70d299313"
            || !matches!(self.llm_provider.as_str(), "qwen" | "deepseek")
            || !matches!(
                languages::normalize(&self.tts_language).as_str(),
                "English" | "French" | "Nigerian Pidgin" | "Yoruba" | "Hausa" | "Igbo"
            )
            || (self.stt_ws_url.contains("whisper-rt")
                && !self.stt_model.to_ascii_lowercase().contains("whisper"))
            || if languages::uses_sbpn(&self.tts_language) {
                self.stt_model != languages::SBPN_MODEL
            } else {
                self.stt_model != "Qwen3-ASR" && !providers::realtime_final(self)
            }
            || (self.stt_batch_url.is_empty()
                && self.stt_chat_url.is_empty()
                && !providers::realtime_final(self))
            || !self.tools.is_empty()
        {
            return Err("unsupported configuration: native provider/language route or legacy unscoped tools".into());
        }
        for n in [
            self.speech_rms,
            self.min_speech_ms,
            self.end_silence_ms,
            self.stable_partial_ms,
            self.stt_batch_timeout_seconds,
            self.stt_recovery_buffer_seconds,
            self.stt_recovery_max_backoff_seconds,
            self.memory_compaction_interval_seconds,
            self.memory_summary_timeout_seconds,
            self.engagement_idle_seconds,
            self.engagement_tool_seconds,
            self.engagement_ack_seconds,
            self.engagement_generation_timeout_seconds,
            self.inactivity_pilot_close_seconds,
            self.llm_first_output_timeout_seconds,
        ] {
            if !n.is_finite() || n <= 0.0 {
                return Err("invalid timing/energy configuration".into());
            }
        }
        if !self.stt_speculative_flush_ms.is_finite()
            || !self.reply_grace_ms.is_finite()
            || !(0.0..=500.0).contains(&self.stt_speculative_flush_ms)
            || !self.stt_warmup_ms.is_finite()
            || !(0.0..=10000.0).contains(&self.stt_warmup_ms)
            || !self.llm_empty_retry_delay_seconds.is_finite()
            || self.llm_empty_retry_delay_seconds < 0.0
            || self.llm_empty_retries > 16
        {
            return Err("invalid provider buffer/retry settings".into());
        }
        if !self.speech_prob_min.is_finite()
            || !(0.0..=1.0).contains(&self.speech_prob_min)
            || !self.min_utterance_ms.is_finite()
            || self.min_utterance_ms < 0.0
            || !self.min_voiced_frame_percent.is_finite()
            || !(0.0..=100.0).contains(&self.min_voiced_frame_percent)
        {
            return Err("invalid speech-probability/utterance-floor configuration".into());
        }
        Ok(())
    }
}
#[derive(Clone, Serialize, Deserialize, Debug)]
pub struct TraceEvent {
    pub event: String,
    pub at_us: u64,
    pub turn: u64,
    pub attempt: u64,
    pub data: Value,
}
#[derive(Clone)]
pub struct Trace {
    start: std::time::Instant,
    synchronous_path: Option<std::sync::Arc<std::path::PathBuf>>,
    events: std::sync::Arc<std::sync::Mutex<Vec<TraceEvent>>>,
    live: tokio::sync::broadcast::Sender<TraceEvent>,
}
impl Default for Trace {
    fn default() -> Self {
        Self {
            start: std::time::Instant::now(),
            synchronous_path: None,
            events: Default::default(),
            live: tokio::sync::broadcast::channel(256).0,
        }
    }
}
impl Trace {
    /// Match the reference's per-event open/append/close tracing during call benchmarks.
    pub fn synchronous(path: std::path::PathBuf) -> std::io::Result<Self> {
        std::fs::File::create(&path)?;
        Ok(Self {
            synchronous_path: Some(std::sync::Arc::new(path)),
            ..Self::default()
        })
    }
    pub fn emit(&self, event: &str, turn: u64, attempt: u64, data: Value) {
        let item = TraceEvent {
            event: event.into(),
            at_us: self.start.elapsed().as_micros() as u64,
            turn,
            attempt,
            data,
        };
        // Telemetry observes a bounded broadcast; slow browsers never hold the
        // actor's output queue or copy raw traces to the wire.
        let _ = self.live.send(item.clone());
        if let Ok(mut events) = self.events.lock() {
            if events.len() < 100000 {
                if let Some(path) = &self.synchronous_path {
                    use std::io::Write;
                    if let (Ok(mut file), Ok(mut row)) = (
                        std::fs::OpenOptions::new().append(true).open(path.as_ref()),
                        serde_json::to_vec(&item),
                    ) {
                        row.push(b'\n');
                        let _ = file.write_all(&row);
                    }
                }
                events.push(item);
            }
        }
    }
    pub fn subscribe(&self) -> tokio::sync::broadcast::Receiver<TraceEvent> {
        self.live.subscribe()
    }
    pub fn events(&self) -> Vec<TraceEvent> {
        self.events.lock().unwrap().clone()
    }
    pub fn save(&self, path: &std::path::Path) -> std::io::Result<()> {
        use std::io::Write;
        let mut out = std::io::BufWriter::new(std::fs::File::create(path)?);
        for e in self.events() {
            writeln!(out, "{}", serde_json::to_string(&e)?)?;
        }
        Ok(())
    }
}
#[cfg(test)]
mod tests;

/// Python json.dumps spacing without modifying commas/colons inside string values.
pub fn python_json(value: &Value) -> String {
    json_format(value, true)
}
fn default_reasoning_effort() -> String {
    "high".into()
}
pub fn python_json_unicode(value: &Value) -> String {
    json_format(value, false)
}
fn json_format(value: &Value, ascii: bool) -> String {
    let raw = value.to_string();
    let (mut string, mut escaped) = (false, false);
    let mut out = String::new();
    for c in raw.chars() {
        if c.is_ascii() || !ascii {
            out.push(c);
        } else {
            let mut buffer = [0; 2];
            for u in c.encode_utf16(&mut buffer) {
                out.push_str(&format!("\\u{u:04x}"));
            }
        }
        if string {
            if escaped {
                escaped = false;
            } else if c == '\\' {
                escaped = true;
            } else if c == '"' {
                string = false;
            }
        } else if c == '"' {
            string = true;
        } else if matches!(c, ',' | ':') {
            out.push(' ');
        }
    }
    out
}

fn default_memory_interval() -> f64 {
    60.0
}
fn default_transport() -> String {
    "livekit".into()
}
fn default_memory_keep() -> usize {
    8
}
fn default_memory_budget() -> usize {
    6000
}
fn default_memory_timeout() -> f64 {
    15.0
}
fn default_idle_seconds() -> f64 {
    18.0
}
fn default_tool_seconds() -> f64 {
    8.0
}
fn default_engagement_timeout() -> f64 {
    6.0
}
fn default_idle_updates() -> usize {
    1
}
fn default_recovery_backoff() -> f64 {
    5.0
}
fn default_inactivity_close() -> f64 {
    15.0
}
fn default_empty_fallback() -> String {
    "I'm sorry, I'm not able to help with that right now. Please try again in a moment.".into()
}

fn default_noise_implementation() -> String {
    "webrtc".into()
}

fn default_tool_ack() -> f64 {
    0.3
}
fn default_tool_updates() -> usize {
    3
}

fn default_bank_rounds() -> usize {
    8
}

fn default_enabled() -> bool {
    true
}
