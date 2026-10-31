//! Native configured banking connector and voice evidence. Identity comes only
//! from verified dispatch claims. Audio locks are never held across network I/O.
use super::{
    actions::{ActionResult, Call, Permit},
    identity::VerifiedSession,
    profiles::AgentContext,
    providers::wav,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::VecDeque,
    sync::{Arc, Mutex, Weak},
    time::Duration,
};
use tokio::{sync::Mutex as AsyncMutex, task::JoinHandle};
const NAMES: &[&str] = &[
    "wema_get_balance",
    "wema_get_transactions",
    "wema_list_data_plans",
    "wema_list_transfer_banks",
    "wema_prepare_data_purchase",
    "wema_prepare_airtime_purchase",
    "wema_prepare_transfer",
    "wema_execute_prepared",
];
const AUTH_FIELDS: &[&str] = &[
    "authenticated",
    "auth_status",
    "skip_auth",
    "voice_auth_authorized",
];
const AUDIO_SAMPLES: usize = 8 * 16000;
const MIN_SAMPLES: usize = 19200;
pub(crate) type EventSink = Arc<dyn Fn(Value) + Send + Sync>;
#[derive(Clone, Deserialize, Serialize)]
#[serde(default)]
pub struct Config {
    pub voice_auth_sidecar_url: String,
    pub voice_auth_disabled: bool,
    pub voice_auth_interval_seconds: f64,
    pub voice_auth_timeout_seconds: f64,
    pub tool_timeout_seconds: f64,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            voice_auth_sidecar_url: String::new(),
            voice_auth_disabled: false,
            voice_auth_interval_seconds: 12.0,
            voice_auth_timeout_seconds: 30.0,
            tool_timeout_seconds: 12.0,
        }
    }
}
impl Config {
    pub(crate) fn validate(&self) -> Result<(), String> {
        for n in [
            self.voice_auth_interval_seconds,
            self.voice_auth_timeout_seconds,
            self.tool_timeout_seconds,
        ] {
            if !n.is_finite() || n <= 0.0 {
                return Err("invalid banking deadline".into());
            }
        }
        if !self.voice_auth_sidecar_url.is_empty() {
            valid_url(&self.voice_auth_sidecar_url)?;
        }
        Ok(())
    }
}
fn valid_url(value: &str) -> Result<reqwest::Url, String> {
    let url = reqwest::Url::parse(value).map_err(|_| "invalid configured tool URL")?;
    if !matches!(url.scheme(), "https" | "http")
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
    {
        return Err("unsupported configured tool URL".into());
    }
    Ok(url)
}
pub(crate) fn is_bank(name: &str) -> bool {
    NAMES.contains(&name)
}
pub(crate) fn acknowledges_wait(answer: &str) -> bool {
    static PATTERN: std::sync::OnceLock<Option<regex::Regex>> = std::sync::OnceLock::new();
    let answer = answer.trim();
    let mut previous = ' ';
    let mut start = 0;
    for (index, c) in answer.char_indices() {
        if c.is_whitespace() && matches!(previous, '.' | '!' | '?') {
            start = index;
        }
        previous = c;
    }
    PATTERN.get_or_init(||regex::Regex::new(r"(?i)\b(one moment|please wait|hold on|checking|check that|look into|looking up|preparing|verify|verifying|processing)\b").ok()).as_ref().is_some_and(|pattern|pattern.is_match(&answer[start..]))
}
struct Record {
    raw: Value,
    validator: jsonschema::Validator,
}
fn records(context: &AgentContext) -> Vec<Record> {
    let mut result: Vec<Record> = vec![];
    if !context.loaded {
        return result;
    }
    for raw in &context.tools {
        let name = raw["name"].as_str().unwrap_or("");
        if !is_bank(name)
            || raw["is_active"] == false
            || !raw["method"]
                .as_str()
                .unwrap_or("")
                .eq_ignore_ascii_case("POST")
            || valid_url(raw["url"].as_str().unwrap_or("")).is_err()
            || !raw["request_schema"].is_object()
        {
            continue;
        }
        let Ok(validator) = jsonschema::draft202012::options()
            .should_validate_formats(false)
            .build(&raw["request_schema"])
        else {
            continue;
        };
        let record = Record {
            raw: raw.clone(),
            validator,
        };
        if let Some(index) = result.iter().position(|r| r.raw["name"] == name) {
            result[index] = record;
        } else {
            result.push(record);
        }
    }
    result
}
pub(crate) fn configured_names(context: &AgentContext) -> Vec<String> {
    records(context)
        .iter()
        .filter_map(|record| record.raw["name"].as_str().map(str::to_string))
        .collect()
}
fn definitions_for(records: &[Record]) -> Vec<Value> {
    records.iter().map(|record|{let mut schema=record.raw["request_schema"].clone();let props=schema["properties"].as_object().cloned().unwrap_or_default();let has_phone=props.contains_key("phone_number");let mut props=props;for name in AUTH_FIELDS{props.remove(*name);}if has_phone{props.insert("recipient_scope".into(),json!({"type":"string","enum":["self","other"],"description":"Use self for the caller's own line, other for another person. Other requires their phone_number."}));}schema["properties"]=json!(props);schema["required"]=json!(schema["required"].as_array().cloned().unwrap_or_default().into_iter().filter(|v|v.as_str().is_some_and(|s|!AUTH_FIELDS.contains(&s)&&s!="source_account"&&s!="phone_number")).collect::<Vec<_>>());json!({"type":"function","function":{"name":record.raw["name"],"description":record.raw["description"].as_str().filter(|s|!s.is_empty()).unwrap_or(record.raw["name"].as_str().unwrap_or("")),"parameters":schema}})}).collect()
}
#[derive(Default)]
struct VoiceState {
    audio: VecDeque<i16>,
    clip: Vec<i16>,
    generation: u64,
    checked_generation: u64,
    authorizations_started: u64,
    verified: bool,
    reason: String,
}
#[derive(Default)]
struct Prepared {
    epoch: u64,
    value: Option<Value>,
}
struct Inner {
    config: Config,
    context: AgentContext,
    identity: VerifiedSession,
    session_id: String,
    records: Vec<Record>,
    http: reqwest::Client,
    voice_required: bool,
    device_required: std::sync::atomic::AtomicBool,
    voice: Mutex<VoiceState>,
    check: AsyncMutex<()>,
    prepared: Mutex<Prepared>,
    emit: EventSink,
}
struct Tasks(Mutex<Vec<JoinHandle<()>>>);
impl Drop for Tasks {
    fn drop(&mut self) {
        if let Ok(tasks) = self.0.get_mut() {
            for task in tasks {
                task.abort();
            }
        }
    }
}
#[derive(Clone)]
pub(crate) struct Banking {
    inner: Arc<Inner>,
    tasks: Arc<Tasks>,
}
fn emit(inner: &Inner, topic: &str, payload: Value) {
    let mut body = payload;
    body["type"] = json!(topic);
    (inner.emit)(json!({"type":topic,"payload":body}));
}
fn activity(inner: &Inner, call: &Call, event: &str, result: Option<&ActionResult>) {
    let mut args = call.arguments.as_object().cloned().unwrap_or_default();
    for name in AUTH_FIELDS {
        args.remove(*name);
    }
    if args.contains_key("recipient_email") {
        args.insert("recipient_email".into(), json!("[redacted]"));
    }
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|v| v.as_secs_f64() * 1000.0)
        .unwrap_or_default();
    emit(
        inner,
        "odion.tool.activity",
        json!({"call_id":call.id,"tool_name":call.name,"event":event,"arguments":args,"ts_ms":ts,"result":result,"status":result.map(|r|r.status.as_str()).unwrap_or("running")}),
    );
}
impl Banking {
    pub(crate) fn new(
        config: Config,
        context: AgentContext,
        identity: VerifiedSession,
        session_id: String,
        emit: EventSink,
    ) -> Result<Self, String> {
        config.validate()?;
        if context.business_id != identity.business_id()
            || context.agent_id != identity.agent_id()
            || !context.loaded
        {
            return Err("banking context scope mismatch".into());
        }
        let voice_required =
            identity.claims()["voice_auth_required"] != false && !config.voice_auth_disabled;
        let records = records(&context);
        let http = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|_| "bank client unavailable")?;
        Ok(Self {
            inner: Arc::new(Inner {
                config,
                context,
                identity,
                session_id,
                records,
                http,
                voice_required,
                device_required: std::sync::atomic::AtomicBool::new(false),
                voice: Default::default(),
                check: Default::default(),
                prepared: Default::default(),
                emit,
            }),
            tasks: Arc::new(Tasks(Default::default())),
        })
    }
    pub(crate) fn pause_voice_checks(&self) {
        if let Ok(mut tasks) = self.tasks.0.lock() {
            for task in tasks.drain(..) {
                task.abort();
            }
        }
    }
    pub(crate) fn available(&self) -> bool {
        !self.inner.records.is_empty()
    }
    pub(crate) fn definitions(&self) -> Vec<Value> {
        definitions_for(&self.inner.records)
    }
    pub(crate) fn enabled(&self, name: &str) -> bool {
        self.inner.records.iter().any(|r| r.raw["name"] == name)
    }
    pub(crate) fn prepared(&self) -> Option<Value> {
        self.inner.prepared.lock().ok()?.value.clone()
    }
    pub(crate) fn clear_prepared(&self) {
        if let Ok(mut state) = self.inner.prepared.lock() {
            state.epoch += 1;
            state.value = None;
        }
    }
    pub(crate) fn ingest_pcm(&self, pcm: &[i16]) {
        if !self.inner.voice_required || !self.available() {
            return;
        }
        if let Ok(mut voice) = self.inner.voice.lock() {
            voice.audio.extend(pcm);
            let remove = voice.audio.len().saturating_sub(AUDIO_SAMPLES);
            voice.audio.drain(..remove);
        }
        if let Ok(mut tasks) = self.tasks.0.lock() {
            tasks.retain(|t| !t.is_finished());
            if tasks.is_empty() {
                let inner = Arc::downgrade(&self.inner);
                tasks.push(tokio::spawn(monitor(inner)));
            }
        }
    }
    pub(crate) fn utterance(&self, pcm: &[i16]) {
        if !self.inner.voice_required || !self.available() {
            return;
        }
        let Some((generation, clip, check)) = self.inner.voice.lock().ok().map(|mut voice| {
            voice.generation += 1;
            voice.clip = if voice.audio.is_empty() {
                pcm[pcm.len().saturating_sub(AUDIO_SAMPLES)..].to_vec()
            } else {
                voice.audio.iter().copied().collect()
            };
            (voice.generation, voice.clip.clone(), !voice.verified)
        }) else {
            return;
        };
        if check {
            let inner = self.inner.clone();
            if let Ok(mut tasks) = self.tasks.0.lock() {
                tasks.push(tokio::spawn(async move {
                    check_session(&inner, generation, &clip).await;
                }));
            }
        }
    }
    pub(crate) fn prompt(&self) -> String {
        if !self.available() {
            return String::new();
        }
        let policy = if self.inner.voice_required {
            super::prompts::BANK_VOICE_REQUIRED
        } else {
            super::prompts::BANK_VOICE_EXEMPT
        };
        let profile = self
            .inner
            .identity
            .claims()
            .get("wema_context")
            .filter(|v| v.is_object())
            .cloned()
            .unwrap_or_else(|| json!({}));
        format!("{policy}{}", super::python_json(&profile))
    }
    pub(crate) fn confirmation(&self) -> Option<(Call, String)> {
        let prepared = self.prepared()?;
        let preview = prepared["preview"].as_object()?;
        let value = |key: &str| {
            let v = preview.get(key)?;
            if v.is_null() {
                None
            } else {
                Some(
                    v.as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| v.to_string()),
                )
            }
        };
        let digits = |v: String| {
            v.chars()
                .map(|c| c.to_string())
                .collect::<Vec<_>>()
                .join(". ")
        };
        let details = if preview.contains_key("recipient_account") {
            format!(
                "Transfer {} naira to {}, {}, account {}.",
                value("amount")?,
                value("recipient_name")?,
                value("bank_name")?,
                digits(value("recipient_account")?)
            )
        } else {
            format!(
                "{} on {}, for {}.",
                value("package_name")?,
                value("network")?,
                digits(value("phone_number")?)
            )
        };
        let id = prepared["operation_id"].as_str()?.to_string();
        Some((
            Call {
                context: None,
                id: format!("confirm-bank-{id}"),
                name: "wema_execute_prepared".into(),
                arguments: json!({"operation_id":id}),
            },
            format!("{details} Do you confirm?"),
        ))
    }
    pub(crate) fn arguments(&self, call: &Call) -> Result<Value, String> {
        let record = self
            .inner
            .records
            .iter()
            .find(|r| r.raw["name"] == call.name)
            .ok_or("That tool is not enabled for this call.")?;
        let mut args = call
            .arguments
            .as_object()
            .cloned()
            .ok_or("Tool arguments must be an object.")?;
        if AUTH_FIELDS.iter().any(|key| args.contains_key(*key)) {
            return Err("Authentication flags cannot be supplied by a tool call.".into());
        }
        let recipient = args
            .remove("recipient_scope")
            .unwrap_or_else(|| json!("self"));
        if recipient != "self" && recipient != "other" {
            return Err("Choose self or other for the recipient.".into());
        }
        let profile = &self.inner.identity.claims()["wema_context"];
        for (key, profile_key) in [
            ("source_account", "account_number"),
            ("phone_number", "phone_number"),
        ] {
            if record.raw["request_schema"]["properties"]
                .get(key)
                .is_some()
                && args.get(key).is_none_or(|v| v.is_null() || v == "")
            {
                if key == "phone_number" && recipient == "other" {
                    return Err("Ask for the other person's phone number.".into());
                }
                if let Some(value) = profile
                    .get(profile_key)
                    .filter(|v| !v.is_null() && *v != "")
                {
                    args.insert(key.into(), value.clone());
                }
            }
        }
        let args = json!(args);
        record
            .validator
            .validate(&args)
            .map_err(|_| "The request is missing valid required details.".to_string())?;
        Ok(args)
    }
    pub(crate) async fn read_or_prepare(&self, call: Call) -> ActionResult {
        if call.name == "wema_execute_prepared" {
            return ActionResult::failed(
                "The latest preview must be confirmed in a separate caller turn.",
            );
        }
        self.execute(call, None).await
    }
    pub(crate) async fn execute_confirmed(&self, permit: Permit) -> ActionResult {
        if self
            .inner
            .device_required
            .load(std::sync::atomic::Ordering::SeqCst)
        {
            return ActionResult::failed("This request needs approval in your app.");
        }
        let call = permit.into_call();
        let confirmed = call.arguments["operation_id"].as_str().map(str::to_owned);
        self.execute(call, confirmed).await
    }
    async fn execute(&self, call: Call, confirmed: Option<String>) -> ActionResult {
        activity(&self.inner, &call, "started", None);
        let result = self.execute_inner(&call, confirmed.as_deref()).await;
        activity(&self.inner, &call, "completed", Some(&result));
        if call.name == "wema_execute_prepared" && self.inner.voice_required {
            emit(
                &self.inner,
                "odion.auth.action",
                json!({"action":call.name,"label":"Transaction","outcome":if ["ok","success","completed"].contains(&result.status.as_str()){"completed"}else if ["unknown","pending"].contains(&result.status.as_str()){"unknown"}else{"blocked"},"authorized":result.extra.get("auth_status")==Some(&json!("verified")),"reason":if ["ok","success","completed"].contains(&result.status.as_str()){""}else{&result.message}}),
            );
        }
        result
    }
    pub(crate) fn require_device_approval(&self) {
        self.inner
            .device_required
            .store(true, std::sync::atomic::Ordering::SeqCst);
    }
    pub(crate) fn device_profile(&self) -> Value {
        self.inner
            .identity
            .claims()
            .get("wema_context")
            .filter(|v| v.is_object())
            .cloned()
            .unwrap_or_else(|| json!({}))
    }
    pub(crate) fn consume_device_preparation(&self, id: &str) -> bool {
        let Ok(mut prepared) = self.inner.prepared.lock() else {
            return false;
        };
        if !prepared
            .value
            .as_ref()
            .is_some_and(|v| v["operation_id"] == id)
        {
            return false;
        }
        prepared.value = None;
        prepared.epoch += 1;
        true
    }
    pub(crate) async fn device_post(
        &self,
        fallback: &super::platform::Endpoint,
        path: &str,
        body: Value,
    ) -> Result<Value, String> {
        if !matches!(path, "/v1/fido2/begin" | "/v1/tools/wema_execute_prepared") {
            return Err("unsupported device approval request".into());
        }
        let record = self
            .inner
            .records
            .iter()
            .find(|r| r.raw["name"] == "wema_execute_prepared");
        let base = record
            .and_then(|r| r.raw["url"].as_str())
            .and_then(|url| url.split("/v1/tools/").next())
            .filter(|s| !s.is_empty())
            .unwrap_or(&fallback.base_url);
        let _ = valid_url(base)?;
        let url = format!("{}{path}", base.trim_end_matches('/'));
        let mut headers = reqwest::header::HeaderMap::new();
        if let Some(values) = record.and_then(|r| r.raw["headers"].as_object()) {
            for (name, value) in values {
                if let Some(value) = value.as_str() {
                    headers.insert(
                        reqwest::header::HeaderName::from_bytes(name.as_bytes())
                            .map_err(|_| "invalid connector header")?,
                        reqwest::header::HeaderValue::from_str(value)
                            .map_err(|_| "invalid connector header")?,
                    );
                }
            }
        }
        if headers.is_empty() && !fallback.token.is_empty() {
            headers.insert(
                "x-service-token",
                reqwest::header::HeaderValue::from_str(&fallback.token)
                    .map_err(|_| "invalid connector credential")?,
            );
        }
        let profile = self.device_profile();
        let customer = profile["customer_id"].as_str().unwrap_or("");
        for (name, value) in [
            ("x-agent-id", self.inner.context.agent_id.as_str()),
            ("x-business-id", self.inner.context.business_id.as_str()),
            ("x-session-id", self.inner.session_id.as_str()),
            ("x-end-user-id", self.inner.context.end_user_id.as_str()),
            ("x-wema-customer-id", customer),
        ] {
            headers.insert(
                name,
                reqwest::header::HeaderValue::from_str(value)
                    .map_err(|_| "invalid connector scope")?,
            );
        }
        let mut response = self
            .inner
            .http
            .post(url)
            .headers(headers)
            .json(&body)
            .timeout(Duration::from_secs_f64(fallback.timeout_seconds))
            .send()
            .await
            .map_err(|_| "device approval transport unavailable")?;
        if !response.status().is_success() {
            return Err("device approval HTTP failure".into());
        }
        let mut bytes = Vec::new();
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|_| "device approval body unavailable")?
        {
            if bytes.len() + chunk.len() > 2 * 1024 * 1024 {
                return Err("device approval response bound".into());
            }
            bytes.extend_from_slice(&chunk);
        }
        serde_json::from_slice(&bytes).map_err(|_| "invalid device approval response".into())
    }
    fn uncertain(&self, call: &Call) -> ActionResult {
        let mut result = ActionResult::unknown();
        result.extra.insert("tool_name".into(), json!(call.name));
        result.extra.insert(
            "auth_status".into(),
            json!(if self.inner.voice_required {
                "verified"
            } else {
                "not_required"
            }),
        );
        result
    }
    async fn execute_inner(&self, call: &Call, confirmed: Option<&str>) -> ActionResult {
        let Some(record) = self
            .inner
            .records
            .iter()
            .find(|r| r.raw["name"] == call.name)
        else {
            return ActionResult::failed("That tool is not enabled for this call.");
        };
        let customer = self.inner.identity.claims()["wema_context"]["customer_id"]
            .as_str()
            .unwrap_or("");
        if customer.is_empty()
            || customer.len() > 120
            || !customer
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
        {
            return ActionResult::failed("The caller profile is missing its customer ID.");
        }
        let epoch = if call.name.starts_with("wema_prepare_") {
            self.clear_prepared();
            self.inner.prepared.lock().ok().map(|s| s.epoch)
        } else {
            None
        };
        let args = match self.arguments(call) {
            Ok(args) => args,
            Err(message) => {
                return ActionResult::from_body(
                    json!({"status":"needs_input","message":message,"data":{}}),
                )
            }
        };
        if call.name == "wema_execute_prepared"
            && !self.prepared().is_some_and(|p| {
                p["operation_id"] == args["operation_id"]
                    && args["operation_id"].as_str() == confirmed
                    && confirmed.is_some()
            })
        {
            return ActionResult::failed(
                "The latest preview must be confirmed in a separate caller turn.",
            );
        }
        if !authorize(&self.inner).await {
            return ActionResult::from_body(
                json!({"status":"failed","message":"I am not authorised to handle this transaction on this call. I can still help with general questions.","data":{},"auth_required":true,"auth_status":"failed"}),
            );
        }
        if call.name == "wema_execute_prepared" {
            let Ok(mut prepared) = self.inner.prepared.lock() else {
                return ActionResult::failed("The prepared request is unavailable.");
            };
            if !prepared
                .value
                .as_ref()
                .is_some_and(|p| p["operation_id"] == args["operation_id"])
            {
                return ActionResult::failed("That preview is no longer current.");
            }
            prepared.value = None;
            prepared.epoch += 1;
        }
        let mut request = self
            .inner
            .http
            .post(record.raw["url"].as_str().unwrap_or(""))
            .timeout(Duration::from_secs_f64(
                self.inner.config.tool_timeout_seconds,
            ))
            .json(&args);
        let mut pinned_headers = reqwest::header::HeaderMap::new();
        if let Some(headers) = record.raw["headers"].as_object() {
            for (name, value) in headers {
                if let Some(value) = value.as_str() {
                    let Ok(name) = reqwest::header::HeaderName::from_bytes(name.as_bytes()) else {
                        return ActionResult::failed("Invalid configured banking header.");
                    };
                    let Ok(value) = reqwest::header::HeaderValue::from_str(value) else {
                        return ActionResult::failed("Invalid configured banking header.");
                    };
                    pinned_headers.insert(name, value);
                }
            }
        }
        for (name, value) in [
            ("X-Tool-Name", call.name.as_str()),
            ("X-Client-Id", "sales-girl-voice-agent"),
            ("X-Agent-Id", self.inner.context.agent_id.as_str()),
            ("X-Business-Id", self.inner.context.business_id.as_str()),
            ("X-Session-Id", self.inner.session_id.as_str()),
            ("X-Conversation-Id", self.inner.session_id.as_str()),
            ("X-End-User-Id", self.inner.context.end_user_id.as_str()),
            ("X-Wema-Customer-Id", customer),
        ] {
            let Ok(name) = reqwest::header::HeaderName::from_bytes(name.as_bytes()) else {
                return ActionResult::failed("Invalid scoped banking header.");
            };
            let Ok(value) = reqwest::header::HeaderValue::from_str(value) else {
                return ActionResult::failed("Invalid scoped banking header.");
            };
            pinned_headers.insert(name, value);
        }
        request = request.headers(pinned_headers);
        let mut response = match request.send().await {
            Ok(r) => r,
            Err(_) => return self.uncertain(call),
        };
        if !response.status().is_success() {
            return if response.status().is_client_error() {
                ActionResult::failed("The banking service rejected this request.")
            } else {
                self.uncertain(call)
            };
        }
        let mut bytes = vec![];
        loop {
            match response.chunk().await {
                Ok(Some(chunk)) => {
                    if bytes.len() + chunk.len() > 2 * 1024 * 1024 {
                        return self.uncertain(call);
                    }
                    bytes.extend_from_slice(&chunk);
                }
                Ok(None) => break,
                Err(_) => return self.uncertain(call),
            }
        }
        let mut body: Value = match serde_json::from_slice(&bytes) {
            Ok(Value::Object(v)) => Value::Object(v),
            _ => return self.uncertain(call),
        };
        if let Some(epoch) = epoch {
            if body["status"] == "prepared"
                && body["data"]["operation_id"]
                    .as_str()
                    .is_some_and(|s| !s.is_empty())
                && body["data"]["preview"].is_object()
            {
                if let Ok(mut state) = self.inner.prepared.lock() {
                    if state.epoch == epoch {
                        state.value = Some(body["data"].clone());
                    }
                }
            }
        }
        if body["status"] == "pending" || body["status"] == "unknown" {
            let mut result = self.uncertain(call);
            result.data = body["data"].clone();
            return result;
        }
        body["tool_name"] = json!(call.name);
        let status = if self.inner.voice_required {
            "verified"
        } else {
            "not_required"
        };
        body["auth_status"] = json!(status);
        body["action_status"] = json!(status);
        ActionResult::from_body(body)
    }
}
async fn compare(inner: &Inner, clip: &[i16]) -> Value {
    let identity = inner.identity.claims();
    let owner = identity["voice_auth_owner"].as_str().unwrap_or("").trim();
    let customer = identity["wema_context"]["customer_id"]
        .as_str()
        .unwrap_or("")
        .trim();
    if (owner.is_empty() && customer.is_empty())
        || inner.config.voice_auth_sidecar_url.is_empty()
        || clip.len() < MIN_SAMPLES
    {
        return json!({"matched":false,"reason":"audio_too_short_or_not_enrolled"});
    }
    let mut form = reqwest::multipart::Form::new();
    if owner.contains('@') {
        form = form.text("email", owner.to_string());
    }
    if !customer.is_empty() {
        form = form.text("customer_id", customer.to_string());
    }
    form = form.part(
        "audio",
        reqwest::multipart::Part::bytes(wav(clip))
            .file_name("utterance.wav")
            .mime_str("audio/wav")
            .unwrap_or_else(|_| reqwest::multipart::Part::bytes(vec![])),
    );
    let action = async {
        let mut response = inner
            .http
            .post(format!(
                "{}/v1/voice-auth/verify",
                inner.config.voice_auth_sidecar_url.trim_end_matches('/')
            ))
            .multipart(form)
            .timeout(Duration::from_secs_f64(
                inner.config.voice_auth_timeout_seconds,
            ))
            .send()
            .await
            .ok()?
            .error_for_status()
            .ok()?;
        let mut data = vec![];
        while let Some(chunk) = response.chunk().await.ok()? {
            if data.len() + chunk.len() > 65536 {
                return None;
            }
            data.extend_from_slice(&chunk);
        }
        serde_json::from_slice::<Value>(&data)
            .ok()
            .filter(Value::is_object)
    };
    action
        .await
        .unwrap_or_else(|| json!({"matched":false,"reason":"voice_verification_unavailable"}))
}
async fn check_session(inner: &Inner, generation: u64, clip: &[i16]) {
    let _guard = inner.check.lock().await;
    if inner.voice.lock().ok().is_none_or(|s| {
        s.verified || generation <= s.checked_generation || generation != s.generation
    }) {
        return;
    }
    let result = compare(inner, clip).await;
    let Some((verified, reason)) = inner.voice.lock().ok().and_then(|mut state| {
        if state.generation != generation {
            return None;
        }
        state.checked_generation = generation;
        state.verified = result["matched"] == true;
        state.reason = result["reason"].as_str().unwrap_or("").into();
        Some((state.verified, state.reason.clone()))
    }) else {
        return;
    };
    emit(
        inner,
        "odion.auth.status",
        json!({"status":if verified{"verified"}else{"failed"},"authenticated":verified,"reason":reason}),
    );
}
async fn authorize(inner: &Inner) -> bool {
    if !inner.voice_required {
        return true;
    }
    let Some((generation, clip)) =
        inner
            .voice
            .lock()
            .ok()
            .filter(|s| s.generation > 0)
            .map(|mut s| {
                s.authorizations_started += 1;
                (s.generation, s.clip.clone())
            })
    else {
        return false;
    };
    check_session(inner, generation, &clip).await;
    let result = compare(inner, &clip).await;
    let passed = inner
        .voice
        .lock()
        .ok()
        .is_some_and(|s| s.verified && s.generation == generation && result["matched"] == true);
    emit(
        inner,
        "odion.auth.action_status",
        json!({"status":if passed{"verified"}else{"failed"},"authenticated":passed,"score":result["score"],"reason":result["reason"]}),
    );
    passed
}
async fn monitor(weak: Weak<Inner>) {
    loop {
        let Some(inner) = weak.upgrade() else {
            return;
        };
        let interval = Duration::from_secs_f64(inner.config.voice_auth_interval_seconds.max(3.0));
        drop(inner);
        tokio::time::sleep(interval).await;
        let Some(inner) = weak.upgrade() else {
            return;
        };
        let Some((generation, clip)) = inner
            .voice
            .lock()
            .ok()
            .map(|s| (s.generation, s.audio.iter().copied().collect::<Vec<_>>()))
            .filter(|(_, clip)| clip.len() >= MIN_SAMPLES)
        else {
            continue;
        };
        let result = compare(&inner, &clip).await;
        let event=inner.voice.lock().ok().and_then(|mut state|{if generation!=state.generation{return None;}let previous=state.verified;state.verified=result["matched"]==true;state.reason=result["reason"].as_str().unwrap_or("").into();(previous!=state.verified||!state.verified).then(||json!({"status":if state.verified{"verified"}else{"failed"},"authenticated":state.verified,"reason":state.reason,"source":"interval"}))});
        if let Some(payload) = event {
            emit(&inner, "odion.auth.status", payload);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{extract::State, http::HeaderMap, routing::post, Json, Router};
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    #[derive(Default)]
    pub(super) struct Fake {
        pub(super) posts: AtomicUsize,
        checks: AtomicUsize,
        pub(super) matched: AtomicBool,
        hold: AtomicBool,
        started: tokio::sync::Notify,
        release: tokio::sync::Notify,
        pub(super) events: Mutex<Vec<Value>>,
        requests: Mutex<Vec<Value>>,
        pub(super) response_status: Mutex<Option<String>>,
    }
    async fn auth(
        State(fake): State<Arc<Fake>>,
        mut form: axum::extract::Multipart,
    ) -> Json<Value> {
        let mut audio = false;
        let mut customer = false;
        while let Some(field) = form.next_field().await.unwrap() {
            match field.name().unwrap_or("") {
                "customer_id" => {
                    assert_eq!(field.text().await.unwrap(), "customer-a");
                    customer = true;
                }
                "audio" => {
                    let bytes = field.bytes().await.unwrap();
                    assert_eq!(&bytes[..4], b"RIFF");
                    assert!(bytes.len() >= MIN_SAMPLES * 2 + 44);
                    audio = true;
                }
                _ => {}
            }
        }
        assert!(audio && customer);
        fake.checks.fetch_add(1, Ordering::SeqCst);
        fake.started.notify_one();
        if fake.hold.load(Ordering::SeqCst) {
            fake.release.notified().await;
        }
        Json(json!({"matched":fake.matched.load(Ordering::SeqCst),"score":0.91}))
    }
    async fn bank(
        State(fake): State<Arc<Fake>>,
        headers: HeaderMap,
        Json(args): Json<Value>,
    ) -> Json<Value> {
        assert_eq!(headers["X-Wema-Customer-Id"], "customer-a");
        assert_eq!(headers["X-Business-Id"], "business-a");
        assert_eq!(headers["X-Agent-Id"], "agent-a");
        assert_eq!(headers["X-Session-Id"], "session-a");
        fake.posts.fetch_add(1, Ordering::SeqCst);
        fake.requests.lock().unwrap().push(args);
        if headers["X-Tool-Name"] == "wema_prepare_data_purchase" {
            Json(
                json!({"status":"prepared","message":"Preview ready","data":{"operation_id":"op-latest","preview":{"package_name":"1 GB","network":"MTN","phone_number":"08000000000"}}}),
            )
        } else {
            Json(
                json!({"status":fake.response_status.lock().unwrap().as_deref().unwrap_or("ok"),"message":"Result ready","data":{"availableBalance":"100.00"}}),
            )
        }
    }
    pub(super) struct Harness {
        pub(super) bank: Banking,
        pub(super) fake: Arc<Fake>,
        server: JoinHandle<()>,
    }
    impl Drop for Harness {
        fn drop(&mut self) {
            self.server.abort();
        }
    }
    pub(super) async fn setup(required: bool) -> Harness {
        let fake = Arc::new(Fake::default());
        fake.matched.store(true, Ordering::SeqCst);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let app = Router::new()
            .route("/bank", post(bank))
            .route("/v1/voice-auth/verify", post(auth))
            .with_state(fake.clone());
        let server = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        let fixture: Value =
            serde_json::from_str(include_str!("../../fixtures/python-parity-reference.json"))
                .unwrap();
        let mut tools = fixture["bank_definitions"]["tools"]
            .as_array()
            .unwrap()
            .clone();
        for tool in &mut tools {
            tool["url"] = json!(format!("{base}/bank"));
            tool["headers"] = json!({"X-Wema-Customer-Id":"forged-customer"});
        }
        tools.push(json!({"name":"wema_execute_prepared","url":format!("{base}/bank"),"method":"POST","request_schema":{"type":"object","properties":{"operation_id":{"type":"string"}},"required":["operation_id"],"additionalProperties":false}}));
        let context = AgentContext {
            loaded: true,
            business_id: "business-a".into(),
            agent_id: "agent-a".into(),
            end_user_id: "caller-a".into(),
            tools,
            ..Default::default()
        };
        let identity = super::super::identity::test_session(
            json!({"voice_auth_required":required,"voice_auth_owner":"caller@example.test","wema_context":fixture["bank_profile"]}),
        );
        let events = fake.clone();
        let emit: EventSink = Arc::new(move |event| events.events.lock().unwrap().push(event));
        let bank = Banking::new(
            Config {
                voice_auth_sidecar_url: base,
                ..Default::default()
            },
            context,
            identity,
            "session-a".into(),
            emit,
        )
        .unwrap();
        Harness { bank, fake, server }
    }
    pub(super) fn call(name: &str, args: Value) -> Call {
        Call {
            context: None,
            id: "bank-call".into(),
            name: name.into(),
            arguments: args,
        }
    }
    pub(super) fn permit(id: &str) -> Permit {
        super::super::actions::confirmed(
            &super::super::actions::Pending {
                call: call("wema_execute_prepared", json!({"operation_id":id})),
                question: "Do you confirm?".into(),
                origin: 1,
                reasks: 0,
            },
            2,
            super::super::actions::Decision::Confirmed,
            true,
        )
        .unwrap()
    }
    #[tokio::test]
    async fn independent_python_schemas_arguments_and_prompt_contracts_match() {
        let h = setup(true).await;
        let fixture: Value =
            serde_json::from_str(include_str!("../../fixtures/python-parity-reference.json"))
                .unwrap();
        let context = AgentContext {
            loaded: true,
            tools: fixture["bank_definitions"]["tools"]
                .as_array()
                .unwrap()
                .clone(),
            ..Default::default()
        };
        assert_eq!(
            definitions_for(&records(&context)),
            fixture["bank_definitions"]["expected"]
                .as_array()
                .unwrap()
                .clone()
        );
        for case in fixture["bank_arguments"].as_array().unwrap() {
            let actual = h
                .bank
                .arguments(&call("wema_prepare_data_purchase", case["args"].clone()));
            assert_eq!(actual.is_ok(), case["valid"].as_bool().unwrap());
            if let Ok(actual) = actual {
                assert_eq!(actual, case["expected"]);
            }
        }
        for case in fixture["bank_wait_acknowledgements"].as_array().unwrap() {
            assert_eq!(
                acknowledges_wait(case["text"].as_str().unwrap()),
                case["expected"].as_bool().unwrap()
            );
        }
        for case in fixture["bank_previews"].as_array().unwrap() {
            h.bank.inner.prepared.lock().unwrap().value = Some(case["prepared"].clone());
            let (call, question) = h.bank.confirmation().unwrap();
            assert_eq!(question, case["question"]);
            assert_eq!(call.arguments, case["arguments"]);
        }
        assert_eq!(h.bank.prompt(), fixture["bank_prompts"]["true"]);
        assert_eq!(
            setup(false).await.bank.prompt(),
            fixture["bank_prompts"]["false"]
        );
    }
    #[tokio::test]
    async fn voice_evidence_requires_session_and_fresh_check_before_scoped_http() {
        let h = setup(true).await;
        h.bank.ingest_pcm(&vec![1; 24000]);
        h.bank.utterance(&vec![1; 24000]);
        let result = h
            .bank
            .read_or_prepare(call("wema_get_balance", json!({})))
            .await;
        assert_eq!(result.status, "ok");
        assert_eq!(h.fake.checks.load(Ordering::SeqCst), 2);
        assert_eq!(h.fake.posts.load(Ordering::SeqCst), 1);
        assert_eq!(
            h.fake.requests.lock().unwrap()[0]["source_account"],
            "0123456789"
        );
        assert_eq!(result.extra["auth_status"], "verified");
        assert!(h
            .fake
            .events
            .lock()
            .unwrap()
            .iter()
            .any(|e| e["type"] == "odion.auth.action_status"
                && e["payload"]["authenticated"] == true));
    }
    #[tokio::test]
    async fn typed_input_cannot_manufacture_audio_and_signed_exemption_keeps_schema_guards() {
        let h = setup(true).await;
        assert_eq!(
            h.bank
                .read_or_prepare(call("wema_get_balance", json!({})))
                .await
                .status,
            "failed"
        );
        assert_eq!(h.fake.checks.load(Ordering::SeqCst), 0);
        assert_eq!(h.fake.posts.load(Ordering::SeqCst), 0);
        let h = setup(false).await;
        assert_eq!(
            h.bank
                .read_or_prepare(call("wema_get_balance", json!({})))
                .await
                .status,
            "ok"
        );
        assert_eq!(
            h.bank
                .read_or_prepare(call("wema_get_balance", json!({"skip_auth":false})))
                .await
                .status,
            "needs_input"
        );
        assert_eq!(h.fake.posts.load(Ordering::SeqCst), 1);
        assert_eq!(h.fake.checks.load(Ordering::SeqCst), 0);
    }
    #[tokio::test]
    async fn only_latest_preview_with_separate_confirmation_executes_once() {
        let h = setup(false).await;
        assert_eq!(
            h.bank
                .read_or_prepare(call("wema_prepare_data_purchase", json!({})))
                .await
                .status,
            "prepared"
        );
        let (_, question) = h.bank.confirmation().unwrap();
        assert!(question.contains("0. 8. 0"));
        assert_eq!(
            h.bank
                .read_or_prepare(call(
                    "wema_execute_prepared",
                    json!({"operation_id":"op-latest"})
                ))
                .await
                .status,
            "failed"
        );
        assert_eq!(
            h.bank.execute_confirmed(permit("old")).await.status,
            "failed"
        );
        assert_eq!(
            h.bank.execute_confirmed(permit("op-latest")).await.status,
            "ok"
        );
        assert_eq!(
            h.bank.execute_confirmed(permit("op-latest")).await.status,
            "failed"
        );
        assert_eq!(h.fake.posts.load(Ordering::SeqCst), 2);
        assert!(h.bank.prepared().is_none());
    }
    #[tokio::test]
    async fn a_new_invalid_preparation_invalidates_previous_preview() {
        let h = setup(false).await;
        h.bank
            .read_or_prepare(call("wema_prepare_data_purchase", json!({})))
            .await;
        assert!(h.bank.prepared().is_some());
        assert_eq!(
            h.bank
                .read_or_prepare(call(
                    "wema_prepare_data_purchase",
                    json!({"recipient_scope":"other"})
                ))
                .await
                .status,
            "needs_input"
        );
        assert!(h.bank.prepared().is_none());
        assert_eq!(
            h.bank.execute_confirmed(permit("op-latest")).await.status,
            "failed"
        );
        assert_eq!(h.fake.posts.load(Ordering::SeqCst), 1);
    }
    #[tokio::test]
    async fn new_utterance_during_comparison_blocks_stale_authorization() {
        let h = setup(true).await;
        h.fake.hold.store(true, Ordering::SeqCst);
        h.bank.ingest_pcm(&vec![1; 24000]);
        h.bank.utterance(&vec![1; 24000]);
        h.fake.started.notified().await;
        let bank = h.bank.clone();
        let action = tokio::spawn(async move {
            bank.read_or_prepare(call("wema_get_balance", json!({})))
                .await
        });
        tokio::time::timeout(Duration::from_secs(2), async {
            while h.bank.inner.voice.lock().unwrap().authorizations_started == 0 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        h.bank.utterance(&vec![2; 24000]);
        h.fake.hold.store(false, Ordering::SeqCst);
        h.fake.release.notify_one();
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(3), action)
                .await
                .unwrap()
                .unwrap()
                .status,
            "failed"
        );
        assert_eq!(h.fake.posts.load(Ordering::SeqCst), 0);
    }
}
#[cfg(test)]
mod additional_auth_tests {
    use super::*;
    #[tokio::test]
    async fn pending_backend_outcome_never_claims_success_and_consumes_preparation() {
        let h = super::tests::setup(false).await;
        h.bank
            .read_or_prepare(super::tests::call("wema_prepare_data_purchase", json!({})))
            .await;
        *h.fake.response_status.lock().unwrap() = Some("pending".into());
        let result = h
            .bank
            .execute_confirmed(super::tests::permit("op-latest"))
            .await;
        assert_eq!(result.status, "unknown");
        assert!(!result.message.contains("sent successfully"));
        assert!(h.bank.prepared().is_none());
        assert_eq!(
            h.bank
                .execute_confirmed(super::tests::permit("op-latest"))
                .await
                .status,
            "failed"
        );
        assert_eq!(h.fake.posts.load(std::sync::atomic::Ordering::SeqCst), 2);
    }
    // The service harness remains local; no transaction endpoint is contacted.
    #[tokio::test]
    async fn periodic_mismatch_and_fresh_mismatch_both_block_transactions() {
        let mut h = super::tests::setup(true).await;
        Arc::get_mut(&mut h.bank.inner)
            .unwrap()
            .config
            .voice_auth_interval_seconds = 0.01;
        h.bank.ingest_pcm(&vec![1; 24000]);
        h.bank.utterance(&vec![1; 24000]);
        tokio::time::timeout(Duration::from_secs(2), async {
            while !h.bank.inner.voice.lock().unwrap().verified {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        h.fake
            .matched
            .store(false, std::sync::atomic::Ordering::SeqCst);
        assert_eq!(
            h.bank
                .read_or_prepare(super::tests::call("wema_get_balance", json!({})))
                .await
                .status,
            "failed"
        );
        tokio::time::timeout(Duration::from_secs(4), async {
            while !h.fake.events.lock().unwrap().iter().any(|e| {
                e["payload"]["source"] == "interval" && e["payload"]["authenticated"] == false
            }) {
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
        })
        .await
        .unwrap();
        assert_eq!(h.fake.posts.load(std::sync::atomic::Ordering::SeqCst), 0);
        for _ in 0..10 {
            h.bank.ingest_pcm(&vec![1; 16000]);
        }
        assert_eq!(
            h.bank.inner.voice.lock().unwrap().audio.len(),
            AUDIO_SAMPLES
        );
    }
}
