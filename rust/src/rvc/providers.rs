use super::{
    text::{tts_budget, DeltaAccumulator, SpokenFilter},
    Config, Trace,
};
use base64::{engine::general_purpose::STANDARD, Engine};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::Duration,
};
use tokio::sync::mpsc;
use tokio_tungstenite::{connect_async, tungstenite::Message};
#[derive(Clone, Debug)]
pub enum LlmEvent {
    Context(super::actions::RequestContext),
    Text(String),
    Tool {
        id: String,
        name: String,
        arguments: Value,
    },
}
#[derive(Clone, Copy)]
pub struct LlmOptions {
    pub required: bool,
    /// Routing must end with both tool_calls and [DONE] before releasing speech.
    pub require_tool_completion: bool,
    pub purpose: &'static str,
    pub turn: u64,
    pub attempt: u64,
    pub max_tokens: usize,
    pub temperature: f64,
}
impl Default for LlmOptions {
    fn default() -> Self {
        Self {
            required: false,
            require_tool_completion: false,
            purpose: "response",
            turn: 0,
            attempt: 0,
            max_tokens: 180,
            temperature: 0.35,
        }
    }
}
pub(crate) fn deepseek_wire(c: &Config) -> bool {
    c.llm_provider == "deepseek"
        || reqwest::Url::parse(&c.llm_base_url)
            .is_ok_and(|u| u.host_str() == Some("api.deepseek.com"))
}
fn normalized_messages(messages: Vec<Value>) -> Vec<Value> {
    if !messages.iter().skip(1).any(|m| m["role"] == "system") {
        return messages;
    }
    let system = messages
        .iter()
        .filter(|m| m["role"] == "system")
        .filter_map(|m| m["content"].as_str())
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join("\n\n");
    let mut result = vec![];
    if !system.is_empty() {
        result.push(json!({"role":"system","content":system}));
    }
    result.extend(messages.into_iter().filter(|m| m["role"] != "system"));
    result
}
pub(crate) fn llm_body(
    c: &Config,
    messages: Vec<Value>,
    tools: Vec<Value>,
    options: LlmOptions,
) -> Value {
    let mut messages = normalized_messages(messages);
    if deepseek_wire(c) && c.llm_enable_thinking {
        for m in &mut messages {
            if m["role"] == "assistant"
                && m["tool_calls"].as_array().is_some_and(|a| !a.is_empty())
                && m.get("reasoning_content").is_none()
            {
                m["reasoning_content"] = json!("");
            }
        }
    }
    let mut body = json!({"model":c.llm_model,"messages":messages,"stream":true,"stream_options":{"include_usage":true},"temperature":options.temperature,"max_tokens":options.max_tokens,"chat_template_kwargs":{"enable_thinking":c.llm_enable_thinking,"thinking":c.llm_enable_thinking}});
    if deepseek_wire(c) {
        if let Some(b) = body.as_object_mut() {
            b.remove("chat_template_kwargs");
            b.remove("temperature");
        }
        body["thinking"] = json!({"type":if c.llm_enable_thinking{"enabled"}else{"disabled"}});
        let effort = c.llm_reasoning_effort.trim().to_ascii_lowercase();
        let effort = if matches!(effort.as_str(), "low" | "high" | "max") {
            effort.as_str()
        } else {
            "high"
        };
        body["reasoning_effort"] = json!(if c.llm_enable_thinking {
            effort
        } else {
            "none"
        });
    }
    if reqwest::Url::parse(&c.llm_base_url).is_ok_and(|u| u.host_str() == Some("api.groq.com")) {
        if let Some(b) = body.as_object_mut() {
            b.remove("chat_template_kwargs");
        }
    }
    if !tools.is_empty() {
        body["tools"] = json!(tools);
        body["tool_choice"] = json!(if options.required { "required" } else { "auto" });
    }
    body
}
#[derive(Clone)]
pub struct Providers {
    pub config: Arc<Config>,
    client: reqwest::Client,
    pub trace: Trace,
    pub usage: super::usage::Collector,
    tts_reference: Option<Arc<String>>,
}
struct LlmSpan {
    trace: Trace,
    usage: super::usage::Collector,
    usage_id: usize,
    request_id: String,
    index: usize,
    turn: u64,
    attempt: u64,
    start: std::time::Instant,
    finished: Option<f64>,
    first: bool,
    done: bool,
    purpose: &'static str,
}
impl LlmSpan {
    fn finish(&mut self) {
        self.finished.get_or_insert_with(|| {
            (self.start.elapsed().as_secs_f64() * 1_000_000.0).round() / 1000.0
        });
    }
}
impl Drop for LlmSpan {
    fn drop(&mut self) {
        self.finish();
        let mut data = self.usage.llm_observation(self.usage_id);
        data["request_id"] = json!(self.request_id);
        data["purpose"] = json!(self.purpose);
        data["request_index"] = json!(self.index + 1);
        data["completed"] = json!(self.done);
        data["first_text_observed"] = json!(self.first);
        data["total_ms"] = json!(self.finished);
        self.trace.emit(
            "llm_provider_request_complete",
            self.turn,
            self.attempt,
            data,
        );
    }
}
impl Providers {
    pub fn new(config: Arc<Config>, trace: Trace) -> Result<Self, String> {
        let tts_reference = if !super::languages::uses_sbpn(&config.tts_language)
            && !config.tts_ref_audio.is_empty()
        {
            let path = if let Some(rest) = config.tts_ref_audio.strip_prefix("~/") {
                std::env::var_os("HOME")
                    .map(std::path::PathBuf::from)
                    .ok_or("reference home unavailable")?
                    .join(rest)
            } else {
                std::path::PathBuf::from(&config.tts_ref_audio)
            };
            let metadata =
                std::fs::metadata(&path).map_err(|_| "configured voice reference unavailable")?;
            if metadata.len() > 32 * 1024 * 1024 {
                return Err("configured voice reference too large".into());
            }
            let bytes =
                std::fs::read(path).map_err(|_| "configured voice reference unavailable")?;
            Some(Arc::new(format!(
                "data:audio/wav;base64,{}",
                STANDARD.encode(bytes)
            )))
        } else {
            None
        };
        Ok(Self {
            config,
            trace,
            tts_reference,
            usage: Default::default(),
            client: reqwest::Client::builder()
                .timeout(Duration::from_secs(120))
                .build()
                .map_err(|e| e.to_string())?,
        })
    }
    pub async fn llm(
        &self,
        messages: Vec<Value>,
        tools: Vec<Value>,
        required: bool,
        turn: u64,
        attempt: u64,
        tx: mpsc::Sender<LlmEvent>,
    ) -> Result<(), String> {
        self.llm_options(
            messages,
            tools,
            LlmOptions {
                required,
                turn,
                attempt,
                ..Default::default()
            },
            tx,
        )
        .await
    }
    pub async fn collect(
        &self,
        messages: Vec<Value>,
        max_tokens: usize,
        temperature: f64,
    ) -> Result<String, String> {
        let (tx, mut rx) = mpsc::channel(32);
        let produce = self.llm_options(
            messages,
            vec![],
            LlmOptions {
                max_tokens,
                temperature,
                ..Default::default()
            },
            tx,
        );
        let consume = async move {
            let mut text = String::new();
            while let Some(item) = rx.recv().await {
                match item {
                    LlmEvent::Context(_) => {}
                    LlmEvent::Text(t) => {
                        if text.len() + t.len() > 128 * 1024 {
                            return Err("background response exceeds bound".into());
                        }
                        text.push_str(&t);
                    }
                    LlmEvent::Tool { .. } => {
                        return Err("tools forbidden in background decision".into())
                    }
                }
            }
            Ok(text)
        };
        let (_, text) = tokio::try_join!(produce, consume)?;
        Ok(text)
    }
    pub async fn llm_options(
        &self,
        messages: Vec<Value>,
        tools: Vec<Value>,
        options: LlmOptions,
        tx: mpsc::Sender<LlmEvent>,
    ) -> Result<(), String> {
        let LlmOptions {
            require_tool_completion,
            purpose,
            required,
            turn,
            attempt,
            max_tokens,
            temperature,
        } = options;
        let c = &self.config;
        let body = llm_body(
            c,
            messages,
            tools,
            LlmOptions {
                require_tool_completion,
                purpose,
                required,
                turn,
                attempt,
                max_tokens,
                temperature,
            },
        );
        let mut empty_retries = 0;
        let mut sent = false;
        self.trace.emit(
            "llm_request_start",
            turn,
            attempt,
            json!({"request":0,"purpose":purpose}),
        );
        for index in 0..(4 + c.llm_empty_retries) {
            let usage_request = self.usage.llm_started();
            let mut span = LlmSpan {
                trace: self.trace.clone(),
                usage: self.usage.clone(),
                usage_id: usage_request,
                request_id: format!("llm-{usage_request}"),
                index,
                turn,
                attempt,
                start: std::time::Instant::now(),
                finished: None,
                first: false,
                done: false,
                purpose,
            };
            let request = self
                .client
                .post(format!(
                    "{}/chat/completions",
                    c.llm_base_url.trim_end_matches('/')
                ))
                .bearer_auth(&c.llm_api_key)
                .json(&body)
                .send()
                .await;
            let response = match request {
                Ok(r) if r.status().is_success() => r,
                Ok(r) => {
                    if !sent && index < 3 && [502, 503, 504].contains(&r.status().as_u16()) {
                        span.finish();
                        tokio::time::sleep(Duration::from_secs_f64([0.3, 1.0, 2.0][index])).await;
                        continue;
                    }
                    return Err(format!("LLM HTTP {}", r.status().as_u16()));
                }
                Err(_) => {
                    if !sent && index < 3 {
                        span.finish();
                        tokio::time::sleep(Duration::from_secs_f64([0.3, 1.0, 2.0][index])).await;
                        continue;
                    }
                    return Err("LLM connection failure".into());
                }
            };
            let mut stream = response.bytes_stream();
            let mut buffer = Vec::new();
            let mut spoken = SpokenFilter::default();
            let mut reasoning = String::new();
            let mut assistant_content = String::new();
            let mut calls: BTreeMap<u64, (String, String, String)> = BTreeMap::new();
            let mut done = false;
            let mut finish_reason = None;
            let mut first = true;
            let mut first_tool = true;
            let deadline = tokio::time::Instant::now()
                + Duration::from_secs_f64(c.llm_first_output_timeout_seconds.max(0.2));
            loop {
                let next = if sent {
                    stream.next().await
                } else {
                    match tokio::time::timeout_at(deadline, stream.next()).await {
                        Ok(n) => n,
                        Err(_) => break,
                    }
                };
                let bytes = match next {
                    None => break,
                    Some(Ok(b)) => b,
                    Some(Err(_)) => return Err("LLM stream interrupted".into()),
                };
                buffer.extend_from_slice(&bytes);
                if buffer.len() > 1024 * 1024 {
                    return Err("LLM SSE frame limit".into());
                }
                while let Some(end) = buffer.iter().position(|b| *b == b'\n') {
                    let line: Vec<_> = buffer.drain(..=end).collect();
                    let Some(data) = line.strip_prefix(b"data:") else {
                        continue;
                    };
                    let raw = std::str::from_utf8(data)
                        .map_err(|_| "invalid SSE UTF8")?
                        .trim();
                    if raw == "[DONE]" {
                        done = true;
                        span.done = true;
                        break;
                    }
                    let item: Value = match serde_json::from_str(raw) {
                        Ok(v) => v,
                        Err(_) => continue,
                    };
                    self.usage.llm_usage(usage_request, &item["usage"]);
                    if let Some(reason) = item["choices"][0]["finish_reason"].as_str() {
                        finish_reason = Some(reason.to_owned());
                    }
                    let delta = &item["choices"][0]["delta"];
                    if deepseek_wire(c) {
                        if let Some(text) = delta["reasoning_content"].as_str() {
                            reasoning.push_str(text);
                        }
                        if let Some(text) = delta["content"].as_str() {
                            assistant_content.push_str(text);
                        }
                        if reasoning.len() + assistant_content.len() > 256 * 1024 {
                            return Err("provider continuation exceeds bound".into());
                        }
                    }
                    if let Some(fragments) = delta["tool_calls"].as_array() {
                        for f in fragments {
                            let call = calls.entry(f["index"].as_u64().unwrap_or(0)).or_default();
                            if let Some(id) = f["id"].as_str() {
                                call.0 = id.into();
                            }
                            if let Some(n) = f["function"]["name"].as_str() {
                                call.1.push_str(n);
                            }
                            if let Some(a) = f["function"]["arguments"].as_str() {
                                call.2.push_str(a);
                            }
                            if !call.1.is_empty() || !call.2.is_empty() {
                                sent = true;
                                if first_tool {
                                    first_tool = false;
                                    self.trace.emit(
                                        "llm_first_tool",
                                        turn,
                                        attempt,
                                        json!({"assembled":false,"purpose":purpose}),
                                    );
                                }
                            }
                            if call.2.len() > 16384 {
                                return Err("tool arguments limit".into());
                            }
                        }
                    }
                    if let Some(content) = delta["content"].as_str() {
                        let text = spoken.push(content);
                        if !text.is_empty() {
                            if first {
                                first = false;
                                span.first = true;
                                self.trace.emit("llm_first_token", turn, attempt, json!({"request_id":span.request_id,"purpose":purpose,"provider_ttft_ms":(span.start.elapsed().as_secs_f64()*1_000_000.0).round()/1000.0}));
                            }
                            sent = true;
                            tx.send(LlmEvent::Text(text))
                                .await
                                .map_err(|_| "generation cancelled")?;
                        }
                    }
                }
                if done {
                    break;
                }
            }
            span.finish();
            if require_tool_completion && (!done || finish_reason.as_deref() != Some("tool_calls"))
            {
                return Err("incomplete routing stream".into());
            }
            if !sent
                && calls.values().all(|(_, name, _)| name.is_empty())
                && empty_retries < c.llm_empty_retries
            {
                empty_retries += 1;
                tokio::time::sleep(Duration::from_secs_f64(c.llm_empty_retry_delay_seconds)).await;
                continue;
            }
            span.done = done;
            if deepseek_wire(c) && (!reasoning.is_empty() || !assistant_content.is_empty()) {
                tx.send(LlmEvent::Context(super::actions::RequestContext {
                    reasoning,
                    content: (!assistant_content.is_empty()).then_some(assistant_content),
                }))
                .await
                .map_err(|_| "generation cancelled")?;
            }
            for (call_index, (id, name, raw)) in calls {
                if !name.is_empty() {
                    let arguments = serde_json::from_str(&raw)
                        .map_err(|_| "invalid structured tool arguments")?;
                    self.trace.emit(
                        "tool_call_prepared",
                        turn,
                        attempt,
                        json!({"purpose":purpose}),
                    );
                    tx.send(LlmEvent::Tool {
                        id: if id.is_empty() {
                            format!("tool-call-{call_index}")
                        } else {
                            id
                        },
                        name,
                        arguments,
                    })
                    .await
                    .map_err(|_| "generation cancelled")?;
                }
            }
            self.trace
                .emit("llm_complete", turn, attempt, json!({"provider_done":done}));
            return Ok(());
        }
        Err("LLM retries exhausted".into())
    }
    pub async fn tts(
        &self,
        text: &str,
        turn: u64,
        attempt: u64,
        tx: &mpsc::Sender<Vec<i16>>,
    ) -> Result<(), String> {
        let c = &self.config;
        if super::languages::uses_sbpn(&c.tts_language) {
            return self.tts_native(text, turn, attempt, tx).await;
        }
        let mut body = json!({"input":text,"model":c.tts_model,"language":c.tts_language,"response_format":"pcm","stream":true,"stream_format":"audio","initial_codec_chunk_frames":c.tts_initial_codec_chunk_frames,"max_new_tokens":tts_budget(text)});
        if !c.tts_voice.is_empty() {
            body["task_type"] = json!("Base");
            body["voice"] = json!(c.tts_voice);
            body["x_vector_only_mode"] = json!(false);
        } else if let Some(reference) = &self.tts_reference {
            body["task_type"] = json!("Base");
            body["ref_audio"] = json!(reference.as_str());
            body["ref_text"] = json!(c.tts_ref_text);
            body["x_vector_only_mode"] = json!(false);
        } else {
            body["task_type"] = json!("VoiceDesign");
            body["instructions"]=json!("A warm, clear female conversational voice. Natural pacing and no exaggerated emotion.");
        }
        if !c.tts_resolved_seed.is_empty() {
            body["seed"] = json!(c.tts_resolved_seed);
        }
        self.trace.emit(
            "tts_request_start",
            turn,
            attempt,
            json!({"text_chars":text.len()}),
        );
        let response = self
            .client
            .post(&c.tts_url)
            .json(&body)
            .send()
            .await
            .map_err(|_| "TTS connection failure")?;
        if !response.status().is_success() {
            return Err(format!("TTS HTTP {}", response.status().as_u16()));
        }
        self.usage.tts_accepted(text);
        let rate = response
            .headers()
            .get("x-sample-rate")
            .and_then(|s| s.to_str().ok())
            .unwrap_or("24000")
            .parse::<u32>()
            .map_err(|_| "invalid TTS sample rate")?;
        if rate != 24000 {
            return Err("unsupported TTS rate; no silent resampling".into());
        }
        let mut stream = response.bytes_stream();
        let mut bytes = 0usize;
        let mut chunks = 0usize;
        let mut carry = Vec::new();
        let mut first = true;
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.map_err(|_| "TTS stream failed")?;
            bytes += chunk.len();
            chunks += 1;
            carry.extend_from_slice(&chunk);
            let even = carry.len() / 2 * 2;
            if even > 0 {
                if first {
                    first = false;
                    self.trace
                        .emit("tts_first_pcm", turn, attempt, json!({"sample_rate":rate}));
                }
                // Preserve PCM16 byte alignment across arbitrary transport splits.
                let samples: Vec<_> = carry[..even]
                    .as_chunks::<2>()
                    .0
                    .iter()
                    .map(|p| i16::from_le_bytes([p[0], p[1]]))
                    .collect();
                for p in samples.chunks(2400) {
                    tx.send(p.to_vec()).await.map_err(|_| "TTS cancelled")?;
                }
                carry.drain(..even);
            }
        }
        if !carry.is_empty() {
            return Err("incomplete final PCM16 sample".into());
        }
        if bytes == 0 {
            return Err("TTS returned no PCM".into());
        }
        self.trace.emit(
            "tts_request_complete",
            turn,
            attempt,
            json!({"bytes":bytes,"chunks":chunks,"audio_seconds":bytes as f64/(2.0*rate as f64)}),
        );
        Ok(())
    }
    async fn tts_native(
        &self,
        text: &str,
        turn: u64,
        attempt: u64,
        tx: &mpsc::Sender<Vec<i16>>,
    ) -> Result<(), String> {
        for request in super::languages::tts_requests(&self.config, text)? {
            self.trace.emit(
                "tts_request_start",
                turn,
                attempt,
                json!({"text_chars":request.text.chars().count()}),
            );
            let response = self
                .client
                .post(&self.config.tts_url)
                .json(&request.body)
                .timeout(Duration::from_secs_f64(request.timeout_seconds))
                .send()
                .await
                .map_err(|_| "native-language TTS connection failed")?
                .error_for_status()
                .map_err(|_| "native-language TTS HTTP error")?;
            for (key, expected) in &request.headers {
                let actual = response
                    .headers()
                    .get(key)
                    .and_then(|v| v.to_str().ok())
                    .unwrap_or("");
                let actual = if key == "content-type" {
                    actual.split(';').next().unwrap_or("").trim()
                } else {
                    actual
                };
                if actual != expected {
                    return Err("native-language TTS voice or PCM contract mismatch".into());
                }
            }
            self.usage.tts_accepted(&request.text);
            let mut stream = response.bytes_stream();
            let mut carry = Vec::new();
            let mut received = 0usize;
            let mut output_samples = 0usize;
            let mut chunks = 0usize;
            let mut first = true;
            let mut resampler = (request.sample_rate != 24000).then(CallResampler::default);
            while let Some(chunk) = stream.next().await {
                let chunk = chunk.map_err(|_| "native-language TTS stream failed")?;
                received += chunk.len();
                chunks += 1;
                if received > 30 * request.sample_rate as usize * 2 {
                    return Err("native-language TTS segment exceeded audio budget".into());
                }
                carry.extend_from_slice(&chunk);
                let length = carry.len() / 2 * 2;
                if length > 0 {
                    let pcm = carry[..length]
                        .as_chunks::<2>()
                        .0
                        .iter()
                        .map(|p| i16::from_le_bytes(*p))
                        .collect::<Vec<_>>();
                    carry.drain(..length);
                    let pcm = if let Some(resampler) = resampler.as_mut() {
                        resampler.push(pcm)
                    } else {
                        pcm
                    };
                    if !pcm.is_empty() {
                        if first {
                            first = false;
                            self.trace.emit(
                                "tts_first_pcm",
                                turn,
                                attempt,
                                json!({"sample_rate":24000}),
                            );
                        }
                        output_samples += pcm.len();
                        for part in pcm.chunks(2400) {
                            tx.send(part.to_vec()).await.map_err(|_| "TTS cancelled")?;
                        }
                    }
                }
            }
            if received == 0 || !carry.is_empty() {
                return Err("native-language TTS returned empty or truncated PCM16".into());
            }
            if let Some(resampler) = resampler.as_mut() {
                let tail = resampler.finish();
                if !tail.is_empty() {
                    if first {
                        self.trace.emit(
                            "tts_first_pcm",
                            turn,
                            attempt,
                            json!({"sample_rate":24000}),
                        );
                    }
                    output_samples += tail.len();
                    tx.send(tail).await.map_err(|_| "TTS cancelled")?;
                }
            }
            if output_samples == 0 {
                return Err("native-language TTS returned no call-rate PCM".into());
            }
            self.trace.emit("tts_request_complete",turn,attempt,json!({"bytes":output_samples*2,"chunks":chunks,"audio_seconds":output_samples as f64/24000.0}));
        }
        Ok(())
    }
    async fn final_http(&self, pcm: Vec<i16>) -> Result<String, String> {
        let c = &self.config;
        if super::languages::uses_sbpn(&c.tts_language) {
            return self.final_sbpn_http(&pcm).await;
        }
        if !c.stt_chat_url.is_empty() {
            match self.final_chat(&pcm).await {
                Ok(text)
                    if !text.is_empty()
                        && !super::text::is_asr_context_echo(&text, &c.stt_context) =>
                {
                    return Ok(text)
                }
                _ => self.trace.emit(
                    "stt_chat_final_fallback",
                    0,
                    0,
                    json!({"reason":"hinted_final_unavailable_or_echo"}),
                ),
            }
        }
        if realtime_final(c) {
            return self.final_realtime(&pcm).await;
        }
        if c.stt_batch_url.is_empty() {
            return Err("ASR final fallback is not configured".into());
        }
        let response = self
            .client
            .post(&c.stt_batch_url)
            .multipart(
                reqwest::multipart::Form::new()
                    .part(
                        "file",
                        reqwest::multipart::Part::bytes(wav(&pcm))
                            .file_name("turn.wav")
                            .mime_str("audio/wav")
                            .map_err(|_| "ASR WAV encoding")?,
                    )
                    .text("model", c.stt_model.clone())
                    .text(
                        "language",
                        if super::languages::french(&c.tts_language) {
                            "fr"
                        } else {
                            "en"
                        },
                    ),
            )
            .timeout(Duration::from_secs_f64(c.stt_batch_timeout_seconds))
            .send()
            .await
            .map_err(|_| "ASR final request failed")?
            .error_for_status()
            .map_err(|_| "ASR final HTTP error")?;
        self.usage.stt_submitted(pcm.len() * 2);
        let payload: Value = response.json().await.map_err(|_| "ASR final JSON error")?;
        let text = super::text::clean_transcript_for_language(
            payload["text"].as_str().unwrap_or(""),
            &self.config.tts_language,
        );
        if super::text::is_asr_context_echo(&text, &c.stt_context) {
            return Err("ASR final copied private context".into());
        }
        Ok(text)
    }
    async fn final_sbpn_http(&self, pcm: &[i16]) -> Result<String, String> {
        if pcm.is_empty() {
            return Err("ASR final audio empty".into());
        }
        tokio::time::timeout(
            Duration::from_secs_f64(self.config.stt_batch_timeout_seconds),
            async {
                let mut texts = Vec::new();
                for audio in pcm.chunks(29 * 16000) {
                    let form = reqwest::multipart::Form::new()
                        .text("model", super::languages::SBPN_MODEL)
                        .part(
                            "file",
                            reqwest::multipart::Part::bytes(wav(audio))
                                .file_name("turn.wav")
                                .mime_str("audio/wav")
                                .map_err(|_| "ASR WAV encoding failed")?,
                        );
                    let response = self
                        .client
                        .post(&self.config.stt_batch_url)
                        .multipart(form)
                        .send()
                        .await
                        .map_err(|_| "SBPN final request failed")?
                        .error_for_status()
                        .map_err(|_| "SBPN final HTTP error")?;
                    self.usage.stt_submitted(audio.len() * 2);
                    let body: Value = response.json().await.map_err(|_| "SBPN final JSON error")?;
                    let text = body["text"].as_str().unwrap_or("").trim();
                    if !text.is_empty() {
                        texts.push(text.to_string());
                    }
                }
                if texts.is_empty() {
                    Err("SBPN final transcript empty".into())
                } else {
                    Ok(texts.join(" "))
                }
            },
        )
        .await
        .map_err(|_| "SBPN final timed out")?
    }
    async fn final_chat(&self, pcm: &[i16]) -> Result<String, String> {
        let c = &self.config;
        let mut messages = vec![];
        if !c.stt_context.is_empty() {
            messages.push(json!({"role":"system","content":c.stt_context}));
        }
        messages.push(json!({"role":"user","content":[{"type":"input_audio","input_audio":{"data":STANDARD.encode(wav(pcm)),"format":"wav"}}]}));
        let response=self.client.post(&c.stt_chat_url).json(&json!({"model":c.stt_chat_model,"messages":messages,"max_tokens":256,"temperature":0.0}))
            .timeout(Duration::from_secs_f64(c.stt_batch_timeout_seconds)).send().await.map_err(|_|"ASR chat request failed")?.error_for_status().map_err(|_|"ASR chat HTTP error")?;
        self.usage.stt_submitted(pcm.len() * 2);
        let payload: Value = response.json().await.map_err(|_| "ASR chat JSON error")?;
        Ok(super::text::clean_transcript_for_language(
            payload["choices"][0]["message"]["content"]
                .as_str()
                .unwrap_or(""),
            &self.config.tts_language,
        ))
    }
    /// A separate request receives the full caller-owned utterance. Its done
    /// event is authoritative only after this explicit final commit; streaming
    /// partials from the speculative socket can never enter this path.
    async fn final_realtime(&self, pcm: &[i16]) -> Result<String, String> {
        tokio::time::timeout(
            Duration::from_secs_f64((self.config.stt_batch_timeout_seconds + 1.0).max(20.0)),
            async {
                let mut ws = open(self).await?;
                append(&mut ws, pcm, &self.usage).await?;
                json_send(
                    &mut ws,
                    json!({"type":"input_audio_buffer.commit","final":true}),
                )
                .await?;
                while let Some(message) = ws.next().await {
                    let message = message.map_err(|_| "ASR final socket interrupted")?;
                    let payload = match message {
                        Message::Text(text) => serde_json::from_str::<Value>(&text),
                        Message::Binary(bytes) => serde_json::from_slice::<Value>(&bytes),
                        Message::Close(_) => break,
                        _ => continue,
                    }
                    .map_err(|_| "ASR final event JSON error")?;
                    if payload["type"] == "error" || payload["error"].is_object() {
                        return Err("ASR final provider error".into());
                    }
                    if payload["type"] == "transcription.done" {
                        let text = super::text::clean_transcript_for_language(
                            payload["text"].as_str().unwrap_or(""),
                            &self.config.tts_language,
                        );
                        let _ = ws.close(None).await;
                        if super::text::is_asr_context_echo(&text, &self.config.stt_context) {
                            return Err("ASR final copied private context".into());
                        }
                        return Ok(text);
                    }
                }
                Err("ASR final socket closed before completion".into())
            },
        )
        .await
        .map_err(|_| "ASR realtime final timeout")?
    }
}
pub enum AsrCommand {
    Flush {
        turn: u64,
        revision: u64,
        through: u64,
    },
    Resume {
        pcm: Vec<i16>,
        through: u64,
    },
    Commit {
        turn: u64,
        pcm: Vec<i16>,
        through: u64,
    },
    Recover,
    Close,
}
#[derive(Debug)]
pub enum AsrEvent {
    Ready,
    Degraded(String),
    Partial(String),
    /// Realtime completion is useful preparation, never final authority.
    Hypothesis {
        turn: u64,
        revision: u64,
        text: String,
    },
    Final {
        turn: u64,
        text: String,
    },
    Unavailable(u64),
    Error(String),
}
struct PendingAudio {
    frames: std::collections::VecDeque<(u64, Vec<i16>)>,
    samples: usize,
    sequence: u64,
    capacity: usize,
}
impl PendingAudio {
    fn push(&mut self, pcm: Vec<i16>) -> usize {
        self.sequence += 1;
        self.samples += pcm.len();
        self.frames.push_back((self.sequence, pcm));
        let mut dropped = 0;
        while self.samples > self.capacity {
            if let Some((_, frame)) = self.frames.pop_front() {
                self.samples -= frame.len();
                dropped += frame.len();
            }
        }
        dropped
    }
    fn pop(&mut self) -> Option<(u64, Vec<i16>)> {
        let (sequence, pcm) = self.frames.pop_front()?;
        self.samples -= pcm.len();
        Some((sequence, pcm))
    }
    fn discard_through(&mut self, through: u64) {
        while self.frames.front().is_some_and(|(seq, _)| *seq <= through) {
            let _ = self.pop();
        }
    }
}
pub struct Asr {
    commands: mpsc::Sender<AsrCommand>,
    pending: Arc<std::sync::Mutex<PendingAudio>>,
    notify: Arc<tokio::sync::Notify>,
    pub events: mpsc::Receiver<AsrEvent>,
    pub busy: Arc<AtomicBool>,
    pub task: tokio::task::JoinHandle<()>,
}
impl Asr {
    pub async fn flush(&self, turn: u64, revision: u64) {
        let through = self
            .pending
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .sequence;
        let _ = self
            .commands
            .send(AsrCommand::Flush {
                turn,
                revision,
                through,
            })
            .await;
    }
    pub async fn resume(&self, pcm: Vec<i16>) {
        let through = self
            .pending
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .sequence;
        let _ = self
            .commands
            .send(AsrCommand::Resume { pcm, through })
            .await;
    }
    /// Realtime is optional speculation. Its bounded queue never blocks owned PCM/VAD.
    pub fn append(&self, pcm: Vec<i16>) -> usize {
        let dropped = self
            .pending
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push(pcm);
        self.notify.notify_one();
        dropped
    }
    pub async fn commit(&self, turn: u64, pcm: Vec<i16>) -> Result<(), String> {
        let through = self
            .pending
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .sequence;
        self.commands
            .send(AsrCommand::Commit { turn, pcm, through })
            .await
            .map_err(|_| "ASR closed".into())
    }
    pub async fn recover(&self) {
        let _ = self.commands.send(AsrCommand::Recover).await;
    }
}
#[derive(Default)]
struct CallResampler {
    resampler: livekit::webrtc::native::audio_resampler::AudioResampler,
    pending: Vec<i16>,
}
impl CallResampler {
    fn push(&mut self, pcm: Vec<i16>) -> Vec<i16> {
        self.pending.extend(pcm);
        let count = self.pending.len() / 160 * 160;
        let tail = self.pending.split_off(count);
        let input = std::mem::replace(&mut self.pending, tail);
        let mut output = Vec::with_capacity(count / 2 * 3);
        for frame in input.as_chunks::<160>().0 {
            output.extend_from_slice(
                self.resampler
                    .remix_and_resample(frame, 160, 1, 16000, 1, 24000),
            );
        }
        output
    }
    fn finish(&mut self) -> Vec<i16> {
        let count = self.pending.len();
        if count == 0 {
            return vec![];
        }
        let mut output = self.push(vec![0; 160 - count]);
        output.truncate((count * 3).div_ceil(2));
        output
    }
}
type RawSocket =
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;
struct Socket {
    inner: RawSocket,
    sbpn: bool,
    french: bool,
}
impl std::ops::Deref for Socket {
    type Target = RawSocket;
    fn deref(&self) -> &Self::Target {
        &self.inner
    }
}
impl std::ops::DerefMut for Socket {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.inner
    }
}
fn stream_limit(p: &Providers) -> usize {
    if super::languages::uses_sbpn(&p.config.tts_language) {
        29 * 16000
    } else {
        MAX_REALTIME_SAMPLES
    }
}

// This server re-decodes cumulative audio within a streaming request. Stop
// optional speculation conservatively, rather than approaching its token limit.
// The owned HTTP final still receives the entire utterance.
pub(super) const MAX_REALTIME_SAMPLES: usize = 15 * 16000;
async fn json_send(ws: &mut Socket, v: Value) -> Result<(), String> {
    tokio::time::timeout(
        Duration::from_secs(2),
        ws.send(Message::Text(v.to_string().into())),
    )
    .await
    .map_err(|_| "ASR websocket write timeout")?
    .map_err(|_| "ASR websocket write failed".into())
}
async fn open(p: &Providers) -> Result<Socket, String> {
    let (mut ws, _) =
        tokio::time::timeout(Duration::from_secs(8), connect_async(&p.config.stt_ws_url))
            .await
            .map_err(|_| "ASR connect timeout")?
            .map_err(|_| "ASR connect failed")?;
    let hello = tokio::time::timeout(Duration::from_secs(8), ws.next())
        .await
        .map_err(|_| "ASR hello timeout")?
        .ok_or("ASR closed before hello")?
        .map_err(|_| "ASR hello failed")?;
    let sbpn = super::languages::uses_sbpn(&p.config.tts_language);
    if sbpn {
        let Message::Text(raw) = hello else {
            return Err("SBPN session hello is not JSON text".into());
        };
        let hello: Value = serde_json::from_str(&raw).map_err(|_| "SBPN session hello invalid")?;
        if hello["type"] != "session.created" {
            return Err("SBPN session was not created".into());
        }
    }
    let mut ws = Socket {
        inner: ws,
        sbpn,
        french: super::languages::french(&p.config.tts_language),
    };
    if sbpn {
        json_send(
            &mut ws,
            json!({"type":"session.update","model":p.config.stt_model}),
        )
        .await?;
    } else {
        let language = if super::languages::french(&p.config.tts_language) {
            "fr"
        } else {
            "en"
        };
        json_send(&mut ws,json!({"type":"session.update","model":p.config.stt_model,"language":language,"to_language":language})).await?;
        json_send(
            &mut ws,
            json!({"type":"input_audio_buffer.commit","final":false}),
        )
        .await?;
    }
    Ok(ws)
}
async fn close_socket(ws: &mut Socket) {
    let _ = tokio::time::timeout(Duration::from_secs(5), async {
        let _ = ws.close(None).await;
        while let Some(Ok(message)) = ws.next().await {
            if matches!(message, Message::Close(_)) {
                break;
            }
        }
    })
    .await;
}
async fn append(
    ws: &mut Socket,
    pcm: &[i16],
    usage: &super::usage::Collector,
) -> Result<(), String> {
    let bytes: Vec<_> = pcm.iter().flat_map(|p| p.to_le_bytes()).collect();
    if ws.sbpn {
        for part in bytes.chunks(6400) {
            tokio::time::timeout(
                Duration::from_secs(2),
                ws.send(Message::Binary(part.to_vec().into())),
            )
            .await
            .map_err(|_| "ASR websocket write timeout")?
            .map_err(|_| "ASR websocket write failed")?;
            usage.stt_submitted(part.len());
        }
        return Ok(());
    }
    json_send(
        ws,
        json!({"type":"input_audio_buffer.append","audio":STANDARD.encode(bytes)}),
    )
    .await?;
    usage.stt_submitted(pcm.len() * 2);
    Ok(())
}
async fn flush_hypothesis(
    ws: &mut Socket,
    audio: &std::sync::Mutex<PendingAudio>,
    through: u64,
    sent_samples: &mut usize,
    usage: &super::usage::Collector,
) -> Result<String, &'static str> {
    // Commit has priority over streaming writes. Send the unsent tail through
    // this turn's fence before asking the existing generation to finish.
    loop {
        let frame = {
            let mut pending = audio.lock().unwrap_or_else(|e| e.into_inner());
            if pending
                .frames
                .front()
                .is_some_and(|(seq, _)| *seq <= through)
            {
                pending.pop()
            } else {
                None
            }
        };
        let Some((_, pcm)) = frame else { break };
        if *sent_samples + pcm.len()
            > if ws.sbpn {
                29 * 16000
            } else {
                MAX_REALTIME_SAMPLES
            }
        {
            return Err("audio_limit");
        }
        append(ws, &pcm, usage)
            .await
            .map_err(|_| "tail_write_failed")?;
        *sent_samples += pcm.len();
    }
    json_send(ws, json!({"type":"input_audio_buffer.commit","final":true}))
        .await
        .map_err(|_| "commit_failed")?;
    while let Some(message) = ws.next().await {
        match message {
            Ok(Message::Text(raw)) => {
                let Ok(item) = serde_json::from_str::<Value>(&raw) else {
                    continue;
                };
                match item["type"].as_str().unwrap_or("") {
                    "transcription.done" => {
                        let raw = item["text"].as_str().unwrap_or("");
                        let text = if ws.sbpn {
                            raw.trim().to_string()
                        } else {
                            super::text::clean_transcript_for_language(
                                raw,
                                if ws.french { "French" } else { "English" },
                            )
                        };
                        return if text.is_empty() {
                            Err("empty_completion")
                        } else {
                            Ok(text)
                        };
                    }
                    "error" => return Err("provider_error"),
                    _ => {}
                }
            }
            Ok(Message::Close(_)) | Err(_) => return Err("disconnected"),
            _ => {}
        }
    }
    Err("disconnected")
}
/// Realtime reconnects and superseded recognition are optional work. New
/// ownership/control commands must preempt their network deadlines.
async fn interruptible_asr<T>(
    rx: &mut mpsc::Receiver<AsrCommand>,
    deferred: &mut Option<AsrCommand>,
    future: impl std::future::Future<Output = T>,
) -> Option<T> {
    tokio::select! {biased;command=rx.recv()=>{*deferred=command;None},result=future=>Some(result)}
}
pub fn start_asr(p: Providers) -> Asr {
    let (tx, mut rx) = mpsc::channel(8);
    let (out, events) = mpsc::channel(64);
    let busy = Arc::new(AtomicBool::new(false));
    let pending = Arc::new(std::sync::Mutex::new(PendingAudio {
        frames: Default::default(),
        samples: 0,
        sequence: 0,
        capacity: (p.config.stt_recovery_buffer_seconds.max(1.0) * 16000.0) as usize,
    }));
    let notify = Arc::new(tokio::sync::Notify::new());
    let (flag, audio, wake) = (busy.clone(), pending.clone(), notify.clone());
    let task = tokio::spawn(async move {
        let mut sent_samples = 0;
        let mut capped = false;
        let mut suspended = false;
        let mut deferred = None;
        let mut socket = match interruptible_asr(&mut rx, &mut deferred, open(&p)).await {
            Some(Ok(mut ws)) => {
                let warm = if ws.sbpn {
                    0
                } else {
                    (16.0 * p.config.stt_warmup_ms) as usize
                };
                let mut good = true;
                for frame in vec![0; warm].chunks(3200) {
                    if append(&mut ws, frame, &p.usage).await.is_err() {
                        good = false;
                        break;
                    }
                    sent_samples += frame.len();
                }
                if good {
                    if !ws.sbpn {
                        let _ = json_send(
                            &mut ws,
                            json!({"type":"input_audio_buffer.commit","final":false}),
                        )
                        .await;
                    }
                    Some(ws)
                } else {
                    close_socket(&mut ws).await;
                    None
                }
            }
            _ => None,
        };
        if socket.is_none() {
            let _ = out
                .send(AsrEvent::Degraded(
                    "realtime unavailable; HTTP final remains authoritative".into(),
                ))
                .await;
        }
        let _ = out.send(AsrEvent::Ready).await;
        let mut accumulated = DeltaAccumulator::for_language(&p.config.tts_language);
        let mut backoff = 0.5;
        let mut retry_at = tokio::time::Instant::now() + Duration::from_secs_f64(backoff);
        let mut heartbeat = tokio::time::interval(Duration::from_secs(20));
        heartbeat.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        heartbeat.tick().await;
        loop {
            let has_audio = !audio
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .frames
                .is_empty();
            tokio::select! {biased;
             command=async{if deferred.is_some(){deferred.take()}else{rx.recv().await}}=>match command{
              Some(AsrCommand::Flush{turn,revision,through})=>{
               suspended=true;
               flag.store(true,Ordering::SeqCst);
               if let Some(mut ws)=socket.take(){
                if p.config.stt_speculative_flush_ms>0.0&&!capped{
                 p.trace.emit("stt_speculative_flush_start",turn,0,json!({"budget_ms":p.config.stt_speculative_flush_ms}));
                 let deadline=tokio::time::Instant::now()+Duration::from_secs_f64(p.config.stt_speculative_flush_ms/1000.0);
                 let result={
                  let flush=flush_hypothesis(&mut ws,&audio,through,&mut sent_samples,&p.usage);tokio::pin!(flush);
                  tokio::select!{biased;
                   command=rx.recv()=>{
                    let committing=matches!(&command,Some(AsrCommand::Commit{..}));deferred=command;
                    if committing{
                     // The final request must not inherit the entire optional
                     // flush budget. Speech resumption does not wait at all.
                     let grace=(tokio::time::Instant::now()+Duration::from_millis(80)).min(deadline);
                     tokio::time::timeout_at(grace,&mut flush).await
                    }else{Ok(Err("superseded"))}
                   },
                   result=tokio::time::timeout_at(deadline,&mut flush)=>result,
                  }
                 };
                 match result{
                  Ok(Ok(text))=>{p.trace.emit("stt_speculative_flush_complete",turn,0,json!({"text_chars":text.len()}));let _=out.send(AsrEvent::Hypothesis{turn,revision,text}).await;},
                  Ok(Err(reason))=>p.trace.emit("stt_speculative_flush_skipped",turn,0,json!({"reason":reason})),
                  Err(_)=>p.trace.emit("stt_speculative_flush_timeout",turn,0,json!({})),
                 }
                }
                close_socket(&mut ws).await;
               }
               flag.store(false,Ordering::SeqCst);
              },
              Some(AsrCommand::Resume{pcm,through})=>{
               audio.lock().unwrap_or_else(|e|e.into_inner()).discard_through(through);
               if let Some(mut ws)=socket.take(){close_socket(&mut ws).await;}
               suspended=false;sent_samples=0;accumulated=DeltaAccumulator::for_language(&p.config.tts_language);
               capped=pcm.len()>stream_limit(&p);
               if !capped{
                let resumed=interruptible_asr(&mut rx,&mut deferred,async{let mut ws=open(&p).await?;for frame in pcm.chunks(3200){append(&mut ws,frame,&p.usage).await?;}Ok::<_,String>(ws)}).await;
                match resumed{Some(Ok(ws))=>{sent_samples=pcm.len();socket=Some(ws);},Some(Err(_))=>retry_at=tokio::time::Instant::now()+Duration::from_secs_f64(backoff),None=>{}}

               }
              },
              Some(AsrCommand::Commit{turn,pcm,through})=>{
               flag.store(true,Ordering::SeqCst);suspended=false;
               let current=socket.take();let use_current=super::languages::uses_sbpn(&p.config.tts_language)&&!capped&&sent_samples>0;
               p.trace.emit("asr_final_request",turn,0,json!({"samples":pcm.len()}));
               let result=interruptible_asr(&mut rx,&mut deferred,async{
                if let Some(mut ws)=current{
                 let final_=if use_current{Some(tokio::time::timeout(Duration::from_secs(30),flush_hypothesis(&mut ws,&audio,through,&mut sent_samples,&p.usage)).await)}else{None};
                 close_socket(&mut ws).await;
                 if let Some(Ok(Ok(text)))=final_{p.trace.emit("stt_realtime_final",turn,0,json!({"authoritative":true}));return Ok(text);}
                }
                p.final_http(pcm).await
               }).await;
               capped=false;sent_samples=0;audio.lock().unwrap_or_else(|e|e.into_inner()).discard_through(through);accumulated=DeltaAccumulator::for_language(&p.config.tts_language);
               let Some(result)=result else{flag.store(false,Ordering::SeqCst);p.trace.emit("asr_final_cancelled",turn,0,json!({"reason":"superseded_control"}));continue;};
               if result.is_ok(){backoff=0.5;}
               let event=match result{Ok(text)if !text.is_empty()=>AsrEvent::Final{turn,text},_=>AsrEvent::Unavailable(turn)};let _=out.send(event).await;
               match interruptible_asr(&mut rx,&mut deferred,open(&p)).await{Some(Ok(ws))=>{socket=Some(ws);},Some(Err(_))=>{socket=None;retry_at=tokio::time::Instant::now()+Duration::from_secs_f64(backoff);let _=out.send(AsrEvent::Degraded("realtime resume pending".into())).await;},None=>{flag.store(false,Ordering::SeqCst);continue;}}
               flag.store(false,Ordering::SeqCst);
              },
              Some(AsrCommand::Recover)=>{if let Some(mut ws)=socket.take(){close_socket(&mut ws).await;}capped=false;suspended=false;sent_samples=0;accumulated=DeltaAccumulator::for_language(&p.config.tts_language);retry_at=tokio::time::Instant::now();},
              Some(AsrCommand::Close)|None=>{if let Some(mut ws)=socket.take(){close_socket(&mut ws).await;}break;}
             },
             _=tokio::time::sleep_until(retry_at),if socket.is_none()&&!capped&&!suspended=>{match interruptible_asr(&mut rx,&mut deferred,open(&p)).await{Some(Ok(ws))=>{socket=Some(ws);sent_samples=0;p.trace.emit("stt_recovery_complete",0,0,json!({}));},Some(Err(_))=>{backoff=(backoff*2.0).min(p.config.stt_recovery_max_backoff_seconds.max(0.5));retry_at=tokio::time::Instant::now()+Duration::from_secs_f64(backoff);},None=>{}}},
             message=async{match socket.as_mut(){Some(ws)=>ws.next().await,None=>std::future::pending().await}}=>{
              let raw=match message{Some(Ok(Message::Text(raw)))=>Some(raw),Some(Ok(Message::Ping(_)|Message::Pong(_)|Message::Binary(_)|Message::Frame(_)))=>None,_=>{socket=None;retry_at=tokio::time::Instant::now()+Duration::from_secs_f64(backoff);accumulated=DeltaAccumulator::for_language(&p.config.tts_language);let _=out.send(AsrEvent::Degraded("realtime disconnected; recovering".into())).await;None}};
              if let Some(raw)=raw{if let Ok(item)=serde_json::from_str::<Value>(&raw){match item["type"].as_str().unwrap_or(""){
               "transcription.delta"=>{backoff=0.5;if super::languages::uses_sbpn(&p.config.tts_language){let text=item["text"].as_str().unwrap_or("").trim();if !text.is_empty(){let _=out.send(AsrEvent::Partial(text.into())).await;}}else{let raw=item["delta"].as_str().filter(|s|!s.is_empty()).or_else(||item["text"].as_str()).unwrap_or("");if let Some(text)=accumulated.push(raw){let _=out.send(AsrEvent::Partial(text)).await;}}},
               "transcription.done"=>{if !super::languages::uses_sbpn(&p.config.tts_language){let _=accumulated.finish(item["text"].as_str());if let Some(ws)=socket.as_mut(){if json_send(ws,json!({"type":"input_audio_buffer.commit","final":false})).await.is_err(){socket=None;retry_at=tokio::time::Instant::now();}}}},
               "error"=>{if let Some(mut ws)=socket.take(){close_socket(&mut ws).await;}backoff=(backoff*2.0).min(p.config.stt_recovery_max_backoff_seconds.max(0.5));retry_at=tokio::time::Instant::now()+Duration::from_secs_f64(backoff);let _=out.send(AsrEvent::Degraded("realtime provider error; HTTP final available".into())).await;},_=>{}
              }}}
             },
             _=async{},if socket.is_some()&&has_audio=>{
              let frame=audio.lock().unwrap_or_else(|e|e.into_inner()).pop();
              if let Some((_,ref frame))=frame{
               if sent_samples+frame.len()>stream_limit(&p){
                capped=true;
                if let Some(mut ws)=socket.take(){close_socket(&mut ws).await;}
                accumulated=DeltaAccumulator::for_language(&p.config.tts_language);
                p.trace.emit("stt_realtime_limit_reached",0,0,json!({"sent_samples":sent_samples,"limit_samples":stream_limit(&p),"owned_final_pcm_retained":true}));
                continue;
               }
              }
              if let Some((_,ref frame))=frame{sent_samples+=frame.len();}
              if let(Some(ws),Some((sequence,frame)))=(socket.as_mut(),frame){if append(ws,&frame,&p.usage).await.is_err(){{let mut pending=audio.lock().unwrap_or_else(|e|e.into_inner());pending.samples+=frame.len();pending.frames.push_front((sequence,frame));while pending.samples>pending.capacity{let _=pending.pop();}}socket=None;retry_at=tokio::time::Instant::now()+Duration::from_secs_f64(backoff);let _=out.send(AsrEvent::Degraded("realtime send failed; recovering".into())).await;}}
             },
             _=wake.notified()=>{},
             _=heartbeat.tick(),if socket.is_some()=>{if let Some(ws)=socket.as_mut(){if !matches!(tokio::time::timeout(Duration::from_secs(2),ws.send(Message::Ping(Vec::new().into()))).await,Ok(Ok(_))){socket=None;retry_at=tokio::time::Instant::now();}}}
            }
        }
    });
    Asr {
        commands: tx,
        pending,
        notify,
        events,
        busy,
        task,
    }
}

pub fn wav(pcm: &[i16]) -> Vec<u8> {
    let bytes = pcm.len() as u32 * 2;
    let mut out = Vec::with_capacity(bytes as usize + 44);
    out.extend_from_slice(b"RIFF");
    out.extend_from_slice(&(bytes + 36).to_le_bytes());
    out.extend_from_slice(b"WAVEfmt ");
    out.extend_from_slice(&16u32.to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes());
    out.extend_from_slice(&16000u32.to_le_bytes());
    out.extend_from_slice(&32000u32.to_le_bytes());
    out.extend_from_slice(&2u16.to_le_bytes());
    out.extend_from_slice(&16u16.to_le_bytes());
    out.extend_from_slice(b"data");
    out.extend_from_slice(&bytes.to_le_bytes());
    for s in pcm {
        out.extend_from_slice(&s.to_le_bytes());
    }
    out
}

pub(crate) fn realtime_final(c: &Config) -> bool {
    c.stt_model.to_ascii_lowercase().contains("whisper")
        || c.stt_ws_url.to_ascii_lowercase().contains("whisper-rt")
}

#[cfg(test)]
mod recovery_tests {
    use super::PendingAudio;
    #[test]
    fn bounded_realtime_replay_discards_only_committed_sequences() {
        let mut queue = PendingAudio {
            frames: Default::default(),
            samples: 0,
            sequence: 0,
            capacity: 6,
        };
        assert_eq!(queue.push(vec![1; 3]), 0);
        let committed_through = queue.sequence;
        assert_eq!(queue.push(vec![2; 3]), 0);
        assert_eq!(queue.push(vec![3; 3]), 3);
        queue.discard_through(committed_through);
        assert_eq!(queue.pop(), Some((2, vec![2; 3])));
        assert_eq!(queue.pop(), Some((3, vec![3; 3])));
        assert_eq!(queue.samples, 0);
        queue.push(vec![4; 3]);
        let committed_through = queue.sequence;
        queue.push(vec![5; 3]);
        queue.discard_through(committed_through);
        assert_eq!(queue.pop(), Some((5, vec![5; 3])));
    }
}

#[cfg(test)]
mod pcm_completion_tests {
    use super::*;
    #[tokio::test]
    async fn empty_successful_tts_body_is_not_successful_audio() {
        let app = axum::Router::new().fallback(|| async { axum::body::Bytes::new() });
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async { axum::serve(listener, app).await.unwrap() });
        let mut config: Config =
            serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
        config.tts_url = url;
        let trace = Trace::default();
        let providers = Providers::new(Arc::new(config), trace.clone()).unwrap();
        let (tx, mut rx) = mpsc::channel(1);
        assert_eq!(
            providers
                .tts("A connection cue.", 1, 1, &tx)
                .await
                .unwrap_err(),
            "TTS returned no PCM"
        );
        assert!(rx.try_recv().is_err());
        assert!(!trace
            .events()
            .iter()
            .any(|event| event.event == "tts_first_pcm" || event.event == "tts_request_complete"));
        server.abort();
    }
}

#[cfg(test)]
mod reconnect_responsiveness_tests {
    use super::*;
    use axum::{
        extract::{
            ws::{Message as ServerMessage, WebSocket},
            State, WebSocketUpgrade,
        },
        response::Response,
        routing::{get, post},
        Json, Router,
    };
    use std::sync::atomic::AtomicUsize;
    #[derive(Default)]
    struct Fake {
        connections: AtomicUsize,
        stalled: tokio::sync::Notify,
    }
    async fn upgrade(State(fake): State<Arc<Fake>>, ws: WebSocketUpgrade) -> Response {
        ws.on_upgrade(move |socket| serve(fake, socket))
    }
    async fn serve(fake: Arc<Fake>, mut socket: WebSocket) {
        let number = fake.connections.fetch_add(1, Ordering::SeqCst) + 1;
        if number == 2 {
            fake.stalled.notify_one();
            std::future::pending::<()>().await;
            return;
        }
        if socket
            .send(ServerMessage::Text(
                json!({"type":"session.created"}).to_string().into(),
            ))
            .await
            .is_err()
        {
            return;
        }
        while let Some(Ok(message)) = socket.next().await {
            if matches!(message, ServerMessage::Close(_)) {
                break;
            }
        }
    }
    #[tokio::test]
    async fn new_authoritative_commit_preempts_stalled_realtime_resume() {
        let fake = Arc::new(Fake::default());
        let app = Router::new()
            .route("/ws", get(upgrade))
            .route(
                "/final",
                post(|| async { Json(json!({"text":"A complete caller turn."})) }),
            )
            .with_state(fake.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async { axum::serve(listener, app).await.unwrap() });
        let mut config: Config =
            serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
        config.stt_ws_url = format!("ws://{address}/ws");
        config.stt_batch_url = format!("http://{address}/final");
        config.stt_chat_url.clear();
        config.stt_warmup_ms = 0.0;
        let mut asr = start_asr(Providers::new(Arc::new(config), Trace::default()).unwrap());
        tokio::time::timeout(Duration::from_secs(2), async {
            while !matches!(asr.events.recv().await, Some(AsrEvent::Ready)) {}
        })
        .await
        .unwrap();
        asr.commit(1, vec![1000; 1600]).await.unwrap();
        tokio::time::timeout(Duration::from_secs(2), async {
            while !matches!(
                asr.events.recv().await,
                Some(AsrEvent::Final { turn: 1, .. })
            ) {}
        })
        .await
        .unwrap();
        fake.stalled.notified().await;
        asr.commit(2, vec![1000; 1600]).await.unwrap();
        let result = tokio::time::timeout(Duration::from_secs(1), async {
            while !matches!(
                asr.events.recv().await,
                Some(AsrEvent::Final { turn: 2, .. })
            ) {}
        })
        .await;
        asr.task.abort();
        let _ = asr.task.await;
        server.abort();
        assert!(
            result.is_ok(),
            "the optional realtime reconnect blocked the next owned final"
        );
    }
}

#[cfg(test)]
mod native_language_wire_tests {
    use super::super::{languages, profiles::AgentContext};
    use super::*;
    use axum::{
        body::{Body, Bytes},
        extract::State,
        http::StatusCode,
        response::Response,
        Json, Router,
    };
    use std::{convert::Infallible, sync::Mutex};
    #[derive(Default)]
    struct Fake {
        requests: Mutex<Vec<Value>>,
        hold: AtomicBool,
        wrong_voice: AtomicBool,
        release: tokio::sync::Notify,
    }
    async fn tts(State(fake): State<Arc<Fake>>, Json(body): Json<Value>) -> Response {
        fake.requests.lock().unwrap().push(body.clone());
        let voice = body["voice"].as_str().unwrap_or("");
        let mut response = Response::builder()
            .status(StatusCode::OK)
            .header("content-type", "audio/pcm")
            .header("x-audio-sample-rate", "24000")
            .header("x-audio-channels", "1");
        if matches!(voice, "maro" | "rukky") {
            for (key, value) in [
                (
                    "x-model",
                    languages::data()["dynamic_model"]
                        .as_str()
                        .unwrap()
                        .to_string(),
                ),
                (
                    "x-voice",
                    if fake.wrong_voice.load(Ordering::SeqCst) {
                        "wrong".into()
                    } else {
                        voice.into()
                    },
                ),
                ("x-speaker-id", voice.into()),
                (
                    "x-checkpoint-sha256",
                    languages::data()["dynamic_checkpoint"]
                        .as_str()
                        .unwrap()
                        .into(),
                ),
                ("x-voice-profile", format!("{voice}-flash-v1")),
                ("x-seed", "0".into()),
                ("x-speed", "1".into()),
            ] {
                response = response.header(key, value);
            }
        }
        let pcm = [1u8, 0].repeat(4800);
        let first = Bytes::copy_from_slice(&pcm[..9599]);
        let last = Bytes::copy_from_slice(&pcm[9599..]);
        let stream = futures_util::stream::unfold(
            (0, fake, first, last),
            |(index, fake, first, last)| async move {
                match index {
                    0 => Some((Ok::<_, Infallible>(first.clone()), (1, fake, first, last))),
                    1 => {
                        if fake.hold.load(Ordering::SeqCst) {
                            fake.release.notified().await;
                        }
                        Some((Ok(last.clone()), (2, fake, first, last)))
                    }
                    _ => None,
                }
            },
        );
        response.body(Body::from_stream(stream)).unwrap()
    }
    async fn setup(
        language: &str,
        voice: &str,
    ) -> (Providers, Arc<Fake>, tokio::task::JoinHandle<()>) {
        let fake = Arc::new(Fake::default());
        let app = Router::new().fallback(tts).with_state(fake.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async { axum::serve(listener, app).await.unwrap() });
        let mut c: Config =
            serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
        c.tts_language = languages::normalize(language);
        let context = AgentContext {
            tts_language_hint: language.into(),
            tts_voice_id: voice.into(),
            ..Default::default()
        };
        languages::apply(&mut c, &context, &Value::Null).unwrap();
        c.tts_url = url;
        (
            Providers::new(Arc::new(c), Trace::default()).unwrap(),
            fake,
            server,
        )
    }
    #[tokio::test]
    async fn native_language_pcm_streams_before_eof_and_preserves_odd_byte_boundaries() {
        let (p, fake, server) = setup("yo", "balogun").await;
        fake.hold.store(true, Ordering::SeqCst);
        let (tx, mut rx) = mpsc::channel(8);
        let task = tokio::spawn(async move { p.tts("Ẹ káàárọ̀.", 1, 1, &tx).await });
        let mut pcm = tokio::time::timeout(Duration::from_secs(2), rx.recv())
            .await
            .unwrap()
            .unwrap();
        assert!(!task.is_finished());
        fake.release.notify_one();
        while let Some(part) = rx.recv().await {
            pcm.extend(part);
        }
        task.await.unwrap().unwrap();
        assert_eq!(pcm, vec![1; 4800]);
        server.abort();
    }
    #[tokio::test]
    async fn wrong_dynamic_voice_is_rejected_before_pcm_and_cancellation_stops_later_segments() {
        let (p, fake, server) = setup("pcm", "maro").await;
        fake.wrong_voice.store(true, Ordering::SeqCst);
        let (tx, mut rx) = mpsc::channel(8);
        assert!(p.tts("How you dey?", 1, 1, &tx).await.is_err());
        assert!(rx.try_recv().is_err());
        fake.wrong_voice.store(false, Ordering::SeqCst);
        fake.hold.store(true, Ordering::SeqCst);
        fake.requests.lock().unwrap().clear();
        let task = tokio::spawn(async move { p.tts(&"How you dey? ".repeat(60), 2, 2, &tx).await });
        tokio::time::timeout(Duration::from_secs(2), rx.recv())
            .await
            .unwrap()
            .unwrap();
        task.abort();
        let _ = task.await;
        fake.release.notify_one();
        tokio::task::yield_now().await;
        assert_eq!(fake.requests.lock().unwrap().len(), 1);
        server.abort();
    }
    #[test]
    fn favour_resampling_preserves_call_rate_duration_across_packet_boundaries() {
        let input = (0..16079)
            .map(|i| ((i as f64 * 0.1).sin() * 1000.0) as i16)
            .collect::<Vec<_>>();
        let mut expected = CallResampler::default();
        let mut full = expected.push(input.clone());
        full.extend(expected.finish());
        assert_eq!(full.len(), (input.len() * 3).div_ceil(2));
        for size in [1, 73, 160, 1600, 2401] {
            let mut resampler = CallResampler::default();
            let mut actual = Vec::new();
            for part in input.chunks(size) {
                actual.extend(resampler.push(part.to_vec()));
            }
            actual.extend(resampler.finish());
            assert_eq!(actual, full);
            assert!(resampler.finish().is_empty());
        }
    }
}

#[cfg(test)]
mod sbpn_wire_tests {
    use super::*;
    use axum::{
        extract::{
            ws::{Message as Frame, WebSocket},
            Multipart, State, WebSocketUpgrade,
        },
        response::Response,
        routing::{get, post},
        Json, Router,
    };
    use std::sync::{atomic::AtomicUsize, Mutex};
    #[derive(Default)]
    struct Fake {
        updates: Mutex<Vec<Value>>,
        binary: AtomicUsize,
        batch: Mutex<Vec<usize>>,
        fail_final: AtomicBool,
        nonfinal_commits: AtomicUsize,
    }
    async fn upgrade(State(fake): State<Arc<Fake>>, ws: WebSocketUpgrade) -> Response {
        ws.on_upgrade(move |socket| serve(fake, socket))
    }
    async fn serve(fake: Arc<Fake>, mut socket: WebSocket) {
        if socket
            .send(Frame::Text(
                json!({"type":"session.created"}).to_string().into(),
            ))
            .await
            .is_err()
        {
            return;
        }
        while let Some(Ok(frame)) = socket.next().await {
            match frame {
                Frame::Binary(bytes) => {
                    assert!(bytes.len() <= 6400);
                    let n = fake.binary.fetch_add(bytes.len(), Ordering::SeqCst);
                    let text = if n == 0 { "How you dey" } else { "How una dey" };
                    if socket
                        .send(Frame::Text(
                            json!({"type":"transcription.delta","text":text})
                                .to_string()
                                .into(),
                        ))
                        .await
                        .is_err()
                    {
                        return;
                    }
                }
                Frame::Text(raw) => {
                    let value: Value = serde_json::from_str(&raw).unwrap();
                    match value["type"].as_str().unwrap_or("") {
                        "session.update" => fake.updates.lock().unwrap().push(value),
                        "input_audio_buffer.commit" => {
                            if value["final"] == true {
                                let frame = if fake.fail_final.load(Ordering::SeqCst) {
                                    json!({"type":"error","error":"public fixture failure"})
                                } else {
                                    json!({"type":"transcription.done","text":"How una dey today?"})
                                };
                                if socket
                                    .send(Frame::Text(frame.to_string().into()))
                                    .await
                                    .is_err()
                                {
                                    return;
                                }
                            } else {
                                fake.nonfinal_commits.fetch_add(1, Ordering::SeqCst);
                            }
                        }
                        _ => panic!("unexpected SBPN request"),
                    }
                }
                Frame::Close(_) => return,
                _ => {}
            }
        }
    }
    async fn batch(State(fake): State<Arc<Fake>>, mut form: Multipart) -> Json<Value> {
        let mut size = 0;
        let mut model = false;
        while let Some(field) = form.next_field().await.unwrap() {
            let name = field.name().unwrap().to_string();
            let bytes = field.bytes().await.unwrap();
            match name.as_str() {
                "model" => {
                    assert_eq!(
                        bytes.as_ref(),
                        super::super::languages::SBPN_MODEL.as_bytes()
                    );
                    model = true;
                }
                "file" => {
                    assert_eq!(&bytes[..4], b"RIFF");
                    assert_eq!(u32::from_le_bytes(bytes[24..28].try_into().unwrap()), 16000);
                    size = bytes.len() - 44;
                }
                _ => panic!("SBPN batch must not add English language or hint fields"),
            }
        }
        assert!(model);
        let n = {
            let mut requests = fake.batch.lock().unwrap();
            requests.push(size);
            requests.len()
        };
        Json(json!({"text":format!("part{n}")}))
    }
    async fn setup() -> (Asr, Arc<Fake>, tokio::task::JoinHandle<()>) {
        let fake = Arc::new(Fake::default());
        let app = Router::new()
            .route("/ws", get(upgrade))
            .route("/final", post(batch))
            .with_state(fake.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async { axum::serve(listener, app).await.unwrap() });
        let mut c: Config =
            serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
        c.tts_language = "Nigerian Pidgin".into();
        c.stt_model = super::super::languages::SBPN_MODEL.into();
        c.stt_ws_url = format!("ws://{address}/ws");
        c.stt_batch_url = format!("http://{address}/final");
        c.stt_chat_url = "http://127.0.0.1:1/must-not-use-english-hint-model".into();
        c.stt_batch_timeout_seconds = 2.0;
        let mut asr = start_asr(Providers::new(Arc::new(c), Trace::default()).unwrap());
        tokio::time::timeout(Duration::from_secs(2), async {
            while !matches!(asr.events.recv().await, Some(AsrEvent::Ready)) {}
        })
        .await
        .unwrap();
        (asr, fake, server)
    }
    async fn partial(asr: &mut Asr) -> String {
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                if let Some(AsrEvent::Partial(text)) = asr.events.recv().await {
                    return text;
                }
            }
        })
        .await
        .unwrap()
    }
    #[tokio::test]
    async fn sbpn_uses_binary_audio_revisable_prefixes_and_only_explicit_owned_final() {
        let (mut asr, fake, server) = setup().await;
        asr.append(vec![1000; 1600]);
        assert_eq!(partial(&mut asr).await, "How you dey");
        asr.append(vec![1000; 1600]);
        assert_eq!(partial(&mut asr).await, "How una dey");
        asr.commit(7, vec![1000; 3200]).await.unwrap();
        let text = tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                if let Some(AsrEvent::Final { turn, text }) = asr.events.recv().await {
                    assert_eq!(turn, 7);
                    return text;
                }
            }
        })
        .await
        .unwrap();
        assert_eq!(text, "How una dey today?");
        assert!(fake.batch.lock().unwrap().is_empty());
        assert_eq!(fake.nonfinal_commits.load(Ordering::SeqCst), 0);
        assert_eq!(
            fake.updates.lock().unwrap()[0],
            json!({"type":"session.update","model":super::super::languages::SBPN_MODEL})
        );
        asr.task.abort();
        let _ = asr.task.await;
        server.abort();
    }
    #[tokio::test]
    async fn sbpn_failure_recovers_the_entire_owned_utterance_in_bounded_wav_chunks() {
        let (mut asr, fake, server) = setup().await;
        fake.fail_final.store(true, Ordering::SeqCst);
        asr.append(vec![1000; 1600]);
        let _ = partial(&mut asr).await;
        asr.commit(9, vec![1000; 60 * 16000]).await.unwrap();
        let text = tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                if let Some(AsrEvent::Final { turn, text }) = asr.events.recv().await {
                    assert_eq!(turn, 9);
                    return text;
                }
            }
        })
        .await
        .unwrap();
        assert_eq!(text, "part1 part2 part3");
        assert_eq!(
            *fake.batch.lock().unwrap(),
            vec![29 * 16000 * 2, 29 * 16000 * 2, 2 * 16000 * 2]
        );
        asr.task.abort();
        let _ = asr.task.await;
        server.abort();
    }
}

#[cfg(test)]
mod configured_reference_tests {
    use super::*;
    use axum::{body::Bytes, extract::State, Json, Router};
    use std::sync::Mutex;
    async fn api(State(requests): State<Arc<Mutex<Vec<Value>>>>, Json(body): Json<Value>) -> Bytes {
        requests.lock().unwrap().push(body);
        Bytes::from_static(&[1, 0, 2, 0])
    }
    #[tokio::test]
    async fn generic_tts_preserves_reference_icl_and_voice_design_request_shapes() {
        let requests = Arc::new(Mutex::new(Vec::<Value>::new()));
        let app = Router::new().fallback(api).with_state(requests.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async { axum::serve(listener, app).await.unwrap() });
        let mut c: Config =
            serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
        c.tts_url = url;
        c.tts_voice.clear();
        c.tts_resolved_seed.clear();
        let (tx, mut rx) = mpsc::channel(8);
        let p = Providers::new(Arc::new(c.clone()), Trace::default()).unwrap();
        p.tts("A design voice.", 1, 1, &tx).await.unwrap();
        let _ = rx.recv().await.unwrap();
        let path =
            std::env::temp_dir().join(format!("sg-rust-voice-ref-{}.wav", uuid::Uuid::new_v4()));
        let audio = wav(&[1, 2, 3, 4]);
        std::fs::write(&path, &audio).unwrap();
        c.tts_ref_audio = path.to_string_lossy().into();
        c.tts_ref_text = "Public fixture reference.".into();
        let trace = Trace::default();
        let p = Providers::new(Arc::new(c), trace.clone()).unwrap();
        std::fs::remove_file(path).unwrap();
        p.tts("A reference voice.", 2, 2, &tx).await.unwrap();
        let _ = rx.recv().await.unwrap();
        let bodies = requests.lock().unwrap().clone();
        assert_eq!(bodies[0]["task_type"], "VoiceDesign");
        assert!(bodies[0].get("voice").is_none());
        assert!(bodies[0].get("seed").is_none());
        assert_eq!(bodies[1]["task_type"], "Base");
        assert_eq!(bodies[1]["ref_text"], "Public fixture reference.");
        let encoded = bodies[1]["ref_audio"]
            .as_str()
            .unwrap()
            .strip_prefix("data:audio/wav;base64,")
            .unwrap();
        assert_eq!(STANDARD.decode(encoded).unwrap(), audio);
        assert_eq!(bodies[1]["x_vector_only_mode"], false);
        assert!(!trace
            .events()
            .iter()
            .any(|event| event.data.to_string().contains("Public fixture reference")));
        server.abort();
    }
}
