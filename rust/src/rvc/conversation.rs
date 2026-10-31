//! Ordered, bounded, best-effort transcript storage. Provider/audio callbacks
//! enqueue work only. Shutdown freezes call duration before storage cleanup.
use super::{
    platform::Services,
    usage::{Collector, Reporter, SessionIds},
    Trace,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::{HashSet, VecDeque},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};
use tokio::{
    sync::{mpsc, oneshot},
    task::JoinHandle,
};
pub(crate) fn as_uuid(value: &str) -> String {
    uuid::Uuid::parse_str(value.trim())
        .map(|v| v.to_string())
        .unwrap_or_else(|_| value.trim().into())
}
pub(crate) fn to_e164(value: &str) -> String {
    let raw = value.trim();
    if !raw
        .chars()
        .all(|c| c.is_ascii_digit() || "+ ()-.".contains(c))
        || raw.matches('+').count() > 1
        || raw.contains('+') && !raw.starts_with('+')
    {
        return raw.into();
    }
    let mut digits = raw.chars().filter(char::is_ascii_digit).collect::<String>();
    if digits.starts_with("00") {
        digits = digits[2..].into();
    }
    if digits.len() == 11 && digits.starts_with('0') {
        digits = format!("234{}", &digits[1..]);
    } else if digits.len() == 10 && digits.starts_with('8') {
        digits = format!("234{digits}");
    }
    if (8..=15).contains(&digits.len()) && !digits.starts_with('0') {
        format!("+{digits}")
    } else {
        raw.into()
    }
}
fn py_repr(value: &str) -> String {
    let quote = if value.contains('\'') && !value.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut result = String::from(quote);
    for c in value.chars() {
        match c {
            '\\' => result.push_str("\\\\"),
            '\n' => result.push_str("\\n"),
            '\r' => result.push_str("\\r"),
            '\t' => result.push_str("\\t"),
            c if c == quote => {
                result.push('\\');
                result.push(c);
            }
            c if c.is_control() => {
                let n = c as u32;
                if n < 256 {
                    result.push_str(&format!("\\x{n:02x}"));
                } else {
                    result.push_str(&format!("\\u{n:04x}"));
                }
            }
            _ => result.push(c),
        }
    }
    result.push(quote);
    result
}
pub(crate) fn caller_from_identity(value: &Value, room: &str) -> String {
    for key in ["end_user_id", "end_user_email", "participant_identity"] {
        if let Some(value) = value[key].as_str().filter(|s| !s.trim().is_empty()) {
            return to_e164(value);
        }
    }
    static PHONE: std::sync::OnceLock<Option<regex::Regex>> = std::sync::OnceLock::new();
    PHONE
        .get_or_init(|| regex::Regex::new(r"__(\d{10,14})(?:_|$)").ok())
        .as_ref()
        .and_then(|r| r.captures(room))
        .and_then(|c| c.get(1))
        .map(|m| to_e164(m.as_str()))
        .unwrap_or_default()
}
pub(crate) fn contact_identity(
    business: &str,
    agent: &str,
    caller: &str,
    client_session: &str,
    web: bool,
) -> (String, String) {
    static EMAIL: std::sync::OnceLock<Option<regex::Regex>> = std::sync::OnceLock::new();
    if EMAIL
        .get_or_init(|| regex::Regex::new(r"^[^@\s]+@[^@\s]+\.[^@\s]+$").ok())
        .as_ref()
        .is_some_and(|r| r.is_match(caller))
    {
        return ("web".into(), caller.to_lowercase());
    }
    let phone = to_e164(caller);
    if !web
        && phone.starts_with('+')
        && (9..=16).contains(&phone.len())
        && phone[1..].bytes().all(|b| b.is_ascii_digit())
        && phone.as_bytes()[1] != b'0'
    {
        return ("voice".into(), phone);
    }
    let tuple = format!(
        "({}, {}, {}, {})",
        py_repr(&as_uuid(business)),
        py_repr(&as_uuid(agent)),
        py_repr(caller),
        py_repr(client_session)
    );
    let digest = format!("{:x}", Sha256::digest(tuple.as_bytes()));
    (
        "web".into(),
        format!("session-{}@sdk.invalid", &digest[..48]),
    )
}
fn handoff_analysis(reason: &str, last: &str, recent: &VecDeque<String>) -> Value {
    let reason = reason.trim();
    let last = last.trim();
    let mut recent = recent
        .iter()
        .map(|v| v.trim())
        .filter(|v| !v.is_empty())
        .collect::<Vec<_>>();
    if recent.is_empty() && !last.is_empty() {
        recent.push(last);
    }
    let mut summary = Vec::new();
    if !reason.is_empty() {
        summary.push(format!("Handoff reason: {reason}"));
    }
    if !recent.is_empty() {
        summary.push(format!(
            "Caller said: {}",
            recent[recent.len().saturating_sub(3)..].join(" | ")
        ));
    }
    if summary.is_empty() {
        summary.push("Caller requested a live human agent during the AI call.".into());
    }
    let blob = format!("{} {}", reason, recent.join(" ")).to_lowercase();
    let intent = [
        (
            "card_or_security_issue",
            &["card", "block", "pin", "otp"][..],
        ),
        (
            "account_inquiry",
            &["loan", "account", "balance", "statement"][..],
        ),
        (
            "complaint_or_fraud",
            &["complaint", "fraud", "unauthorized"][..],
        ),
        (
            "transaction_issue",
            &["transaction", "payment", "debit", "failed transfer"][..],
        ),
    ]
    .into_iter()
    .find(|(_, tokens)| tokens.iter().any(|token| blob.contains(token)))
    .map(|(intent, _)| intent)
    .unwrap_or("request_human_agent");
    json!({"analysis_status":"ready","summary":summary.join(" ").chars().take(900).collect::<String>(),"primary_intent":intent,"intent_confidence":if reason.is_empty()&&recent.is_empty(){0.7}else{0.9},"sentiment":if ["urgent","fraud","immediately"].iter().any(|token|blob.contains(token)){"urgent"}else{"frustrated"},"resolution_status":"escalated"})
}
#[derive(Clone)]
pub(crate) struct Meta {
    pub caller: String,
    pub client_session_id: String,
    pub room: String,
    pub web: bool,
    pub explicitly_routed: bool,
}
enum Command {
    Message {
        role: &'static str,
        text: String,
        turn: String,
        interrupted: bool,
    },
    Handoff {
        reason: String,
        payload: Value,
        done: oneshot::Sender<()>,
    },
    Stop(oneshot::Sender<()>),
}
pub(crate) struct Store {
    tx: mpsc::Sender<Command>,
    task: Mutex<Option<JoinHandle<()>>>,
    reporter: Mutex<Option<Reporter>>,
    pub ids: SessionIds,
    services: Services,
    trace: Trace,
    started: tokio::time::Instant,
    ended: Mutex<Option<(u64, String)>>,
    closing: AtomicBool,
    recording: Arc<super::recording::Recorder>,
}
impl Drop for Store {
    fn drop(&mut self) {
        // A cancelled job must still stop a known (or late-starting) egress.
        // Background SDK calls remain bounded; no blocking I/O runs in Drop.
        if tokio::runtime::Handle::try_current().is_ok() {
            self.recording.mark_disconnected();
        }
        if let Ok(task) = self.task.get_mut() {
            if let Some(task) = task.take() {
                task.abort();
            }
        }
    }
}
impl Store {
    pub(crate) fn start(
        services: Services,
        meta: Meta,
        usage: Collector,
        recording_config: super::recording::Config,
        trace: Trace,
    ) -> Arc<Self> {
        let ids = SessionIds::default();
        let (tx, mut rx) = mpsc::channel(256);
        let reporter = Reporter::start(usage, ids.clone(), services.clone(), trace.clone());
        let started_at = chrono::Utc::now();
        let recording = super::recording::Recorder::new(
            recording_config,
            services.clone(),
            meta.room.clone(),
            started_at,
            trace.clone(),
        );
        let mut runtime = Runtime {
            services: services.clone(),
            meta,
            ids: ids.clone(),
            trace: trace.clone(),
            started_at: started_at.to_rfc3339_opts(chrono::SecondsFormat::Micros, false),
            sequence: 0,
            recent_user: VecDeque::with_capacity(6),
            keys: HashSet::new(),
            started: false,
            recording: recording.clone(),
        };
        let task = tokio::spawn(async move {
            let _ = tokio::time::timeout(Duration::from_secs(30), runtime.start()).await;
            while let Some(command) = rx.recv().await {
                match command {
                    Command::Message {
                        role,
                        text,
                        turn,
                        interrupted,
                    } => {
                        let _ = tokio::time::timeout(
                            Duration::from_secs(30),
                            runtime.message(role, text, turn, interrupted),
                        )
                        .await;
                    }
                    Command::Handoff {
                        reason,
                        payload,
                        done,
                    } => {
                        let _ = tokio::time::timeout(
                            Duration::from_secs(30),
                            runtime.handoff(&reason, payload),
                        )
                        .await;
                        let _ = done.send(());
                    }
                    Command::Stop(done) => {
                        let _ = done.send(());
                        return;
                    }
                }
            }
        });
        Arc::new(Self {
            tx,
            task: Mutex::new(Some(task)),
            reporter: Mutex::new(Some(reporter)),
            ids,
            services,
            trace,
            started: tokio::time::Instant::now(),
            ended: Mutex::new(None),
            closing: AtomicBool::new(false),
            recording,
        })
    }
    pub(crate) fn message(&self, role: &'static str, text: &str, turn: String, interrupted: bool) {
        if text.trim().is_empty() {
            return;
        }
        if self.closing.load(Ordering::SeqCst)
            || self
                .tx
                .try_send(Command::Message {
                    role,
                    text: text.trim().into(),
                    turn,
                    interrupted,
                })
                .is_err()
        {
            self.trace.emit(
                "conversation_persist_dropped",
                0,
                0,
                json!({"reason":"closing_or_queue_full","role":role}),
            );
        }
    }
    pub(crate) async fn handoff(&self, reason: &str, payload: Value) {
        if self.closing.load(Ordering::SeqCst) {
            return;
        }
        let (done, rx) = oneshot::channel();
        let command = Command::Handoff {
            reason: reason.chars().take(240).collect(),
            payload,
            done,
        };
        let _ = tokio::time::timeout(Duration::from_secs(30), async {
            self.tx.send(command).await.map_err(|_| ())?;
            rx.await.map_err(|_| ())
        })
        .await;
    }
    pub(crate) fn mark_disconnected(&self) {
        self.recording.mark_disconnected();
        if let Ok(mut ended) = self.ended.lock() {
            if ended.is_none() {
                *ended = Some((
                    self.started.elapsed().as_secs(),
                    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false),
                ));
            }
        }
    }
    pub(crate) async fn close(&self) {
        if self.closing.swap(true, Ordering::SeqCst) {
            return;
        }
        self.mark_disconnected();
        let (tx, rx) = oneshot::channel();
        let drained = tokio::time::timeout(Duration::from_secs(15), async {
            self.tx.send(Command::Stop(tx)).await.map_err(|_| ())?;
            rx.await.map_err(|_| ())
        })
        .await
        .is_ok_and(|r| r.is_ok());
        let task = self.task.lock().ok().and_then(|mut t| t.take());
        if let Some(task) = task {
            if !drained {
                task.abort();
                self.trace
                    .emit("conversation_persist_drain_timeout", 0, 0, json!({}));
            }
            let _ = task.await;
        }
        let frozen_duration = self
            .ended
            .lock()
            .ok()
            .and_then(|state| state.as_ref().map(|(duration, _)| *duration))
            .unwrap_or_default();
        self.recording.close(frozen_duration).await;
        if let Some((session, _)) = self.ids.get() {
            let ended = self
                .ended
                .lock()
                .ok()
                .and_then(|s| s.clone())
                .unwrap_or_else(|| (0, chrono::Utc::now().to_rfc3339()));
            let body = json!({"session_id":session,"duration_seconds":ended.0,"ended_at":ended.1});
            let response = request(
                &self.services,
                &["v1", "conversations", "sessions", "end"],
                body,
                true,
                &self.trace,
            )
            .await;
            self.trace.emit(
                if response["status"] == "failed" {
                    "conversation_persist_end_failed"
                } else {
                    "conversation_persist_ended"
                },
                0,
                0,
                json!({"session_id":session,"duration_seconds":ended.0}),
            );
        }
        let reporter = self.reporter.lock().ok().and_then(|mut r| r.take());
        if let Some(reporter) = reporter {
            reporter.close().await;
        }
    }
}
struct Runtime {
    services: Services,
    meta: Meta,
    ids: SessionIds,
    trace: Trace,
    started_at: String,
    sequence: u64,
    recent_user: VecDeque<String>,
    keys: HashSet<(&'static str, String)>,
    started: bool,
    recording: Arc<super::recording::Recorder>,
}
impl Runtime {
    async fn start(&mut self) {
        if self.started {
            return;
        }
        self.started = true;
        let scope = self.services.scope();
        let (channel, external) = contact_identity(
            scope.business_id(),
            scope.agent_id(),
            &self.meta.caller,
            &self.meta.client_session_id,
            self.meta.web,
        );
        let resolved=request(&self.services,&["v1","conversations","resolve"],json!({"agent_id":as_uuid(scope.agent_id()),"external_id":external,"external_name":self.meta.caller.chars().take(255).collect::<String>(),"channel":channel}),true,&self.trace).await;
        if resolved["status"] == "failed" {
            self.trace.emit(
                "conversation_persist_start_failed",
                0,
                0,
                json!({"reason":"conversation_resolve_failed"}),
            );
            return;
        }
        let conversation = resolved["conversation_id"]
            .as_str()
            .or_else(|| resolved["id"].as_str())
            .unwrap_or("")
            .trim()
            .to_string();
        if conversation.is_empty() {
            self.trace.emit(
                "conversation_persist_start_failed",
                0,
                0,
                json!({"reason":"conversation_resolve_failed"}),
            );
            return;
        }
        let started=request(&self.services,&["v1","conversations","sessions","start"],json!({"conversation_id":conversation,"channel":channel,"explicitly_routed_agent":self.meta.explicitly_routed,"client_session_id":self.meta.client_session_id,"started_at":self.started_at}),true,&self.trace).await;
        if started["status"] == "failed" {
            self.trace.emit(
                "conversation_persist_start_failed",
                0,
                0,
                json!({"reason":"canonical_session_missing"}),
            );
            return;
        }
        let session = started["id"]
            .as_str()
            .or_else(|| started["session_id"].as_str())
            .or_else(|| started["session"]["id"].as_str())
            .unwrap_or("")
            .trim()
            .to_string();
        if session.is_empty() {
            self.trace.emit(
                "conversation_persist_start_failed",
                0,
                0,
                json!({"reason":"canonical_session_missing"}),
            );
            return;
        }
        self.ids.set(session.clone(), conversation);
        self.trace.emit(
            "conversation_persist_started",
            0,
            0,
            json!({"session_id":session,"room_name":self.meta.room,"channel":channel}),
        );
        request(&self.services,&["v1","conversations","sessions",&session,"events"],json!({"event_type":"session_started","role":"system","title":"Session started","body":format!("Inbound call from {}",if self.meta.caller.is_empty(){"anonymous caller"}else{&self.meta.caller}),"payload":{"room_name":self.meta.room,"caller":self.meta.caller,"channel":channel}}),false,&self.trace).await;
        self.recording.start(session);
    }
    async fn handoff(&self, reason: &str, payload: Value) {
        let Some((session, _)) = self.ids.get() else {
            return;
        };
        let draft = handoff_analysis(reason, "", &self.recent_user);
        request(
            &self.services,
            &["v1", "conversations", "sessions", &session, "analysis"],
            draft.clone(),
            false,
            &self.trace,
        )
        .await;
        let mut details = json!({"handoff_kind":"human_handoff","reason_summary":if reason.trim().is_empty(){Value::Null}else{json!(reason.trim())},"transfer_mode":"sip_bridge","room_name":self.meta.room,"snapshot_kind":"fast"});
        // Connector receipts only. Device proofs/credential material cannot enter storage.
        for key in [
            "status",
            "participant_identity",
            "sip_call_id",
            "target_number",
            "error",
            "sip_status_code",
        ] {
            if let Some(value) = payload.get(key) {
                details[key] = value.clone();
            }
        }
        request(&self.services,&["v1","conversations","sessions",&session,"events"],json!({"event_type":"aicc_handoff","role":"system","title":"Transferred to human","body":if reason.trim().is_empty(){"Caller handed off to a human agent via AICC."}else{reason.trim()},"payload":details}),false,&self.trace).await;
        self.trace.emit(
            "conversation_handoff_snapshot",
            0,
            0,
            json!({"session_id":session,"primary_intent":draft["primary_intent"]}),
        );
    }
    async fn message(&mut self, role: &'static str, text: String, turn: String, interrupted: bool) {
        let Some((session, conversation)) = self.ids.get() else {
            self.trace.emit(
                "conversation_persist_dropped",
                0,
                0,
                json!({"reason":"canonical_session_missing","role":role}),
            );
            return;
        };
        if !self.keys.insert((role, turn.clone())) {
            return;
        }
        if role == "user" {
            if self.recent_user.len() == 6 {
                self.recent_user.pop_front();
            }
            self.recent_user.push_back(text.clone());
        }
        self.sequence += 1;
        let identity = format!("{session}:{role}:{turn}");
        let key = format!("rvc:{:x}", Sha256::digest(identity.as_bytes()));
        let mut metadata = json!({"turn_id":turn,"sequence":self.sequence});
        if interrupted {
            metadata["interrupted"] = json!(true);
            metadata["delivery"] = json!("generated_not_confirmed");
        }
        let response=request(&self.services,&["v1","conversations",&conversation,"messages"],json!({"role":role,"content":text,"session_id":session,"idempotency_key":key,"metadata":metadata}),true,&self.trace).await;
        if response["status"] == "failed" {
            self.trace.emit(
                "conversation_persist_message_failed",
                0,
                0,
                json!({"role":role,"sequence":self.sequence}),
            );
        }
    }
}
pub(super) async fn request(
    services: &Services,
    path: &[&str],
    body: Value,
    retry: bool,
    trace: &Trace,
) -> Value {
    for attempt in 0..if retry { 2 } else { 1 } {
        let response = services.persistence_request(path, body.clone()).await;
        let transient = match response {
            Ok(mut body)
                if body.is_object() && body["status"] != "failed" && body["status"] != "error" =>
            {
                if body["status"].as_str().is_none_or(|s| s.is_empty()) {
                    body["status"] = json!("success");
                }
                return body;
            }
            Err(error) => {
                error == "service transport unavailable"
                    || error == "service body interrupted"
                    || error.starts_with("service HTTP 5")
                    || error.starts_with("service HTTP 429")
            }
            _ => false,
        };
        trace.emit(
            "conversation_persist_failed",
            0,
            0,
            json!({"attempt":attempt+1,"path":path.join("/")}),
        );
        if !retry || !transient || attempt == 1 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    json!({"status":"failed"})
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn fast_handoff_matches_independent_python_language_priority_and_truncation() {
        let fixture: Value =
            serde_json::from_str(include_str!("../../fixtures/python-parity-reference.json"))
                .unwrap();
        for case in fixture["handoff_analysis"].as_array().unwrap() {
            let recent = case["recent"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().into())
                .collect();
            assert_eq!(
                handoff_analysis(
                    case["reason"].as_str().unwrap(),
                    case["last"].as_str().unwrap(),
                    &recent
                ),
                case["expected"]
            );
        }
    }
    use axum::{
        extract::State,
        http::{HeaderMap, StatusCode, Uri},
        response::{IntoResponse, Response},
        Json, Router,
    };
    use std::sync::atomic::AtomicUsize;
    #[derive(Default)]
    struct Fake {
        requests: Mutex<Vec<(String, Value)>>,
        hold: AtomicBool,
        started: tokio::sync::Notify,
        release: tokio::sync::Notify,
        message_failures: AtomicUsize,
    }
    async fn api(
        State(fake): State<Arc<Fake>>,
        headers: HeaderMap,
        uri: Uri,
        Json(body): Json<Value>,
    ) -> Response {
        assert_eq!(headers["x-business-id"], "business-a");
        let path = uri.path().to_string();
        fake.requests.lock().unwrap().push((path.clone(), body));
        if path.ends_with("/resolve") {
            fake.started.notify_one();
            if fake.hold.load(Ordering::SeqCst) {
                fake.release.notified().await;
            }
            Json(json!({"conversation_id":"conversation-a"})).into_response()
        } else if path.ends_with("/sessions/start") {
            Json(json!({"session":{"id":"canonical-session"}})).into_response()
        } else if path.ends_with("/messages")
            && fake
                .message_failures
                .try_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_sub(1))
                .is_ok()
        {
            StatusCode::SERVICE_UNAVAILABLE.into_response()
        } else if path.ends_with("/observed-usage") {
            StatusCode::NO_CONTENT.into_response()
        } else {
            Json(json!({"status":"ok"})).into_response()
        }
    }
    #[test]
    fn phone_and_session_alias_contracts_match_current_python() {
        let reference: Value =
            serde_json::from_str(include_str!("../../fixtures/python-parity-reference.json"))
                .unwrap();
        for case in reference["phone_cases"].as_array().unwrap() {
            assert_eq!(to_e164(case["raw"].as_str().unwrap()), case["expected"]);
        }
        for case in reference["conversation_aliases"].as_array().unwrap() {
            let (channel, external) = contact_identity(
                "business-a",
                "agent-a",
                case["caller"].as_str().unwrap(),
                "session-a",
                case["web"].as_bool().unwrap(),
            );
            assert_eq!(channel, case["channel"]);
            assert_eq!(external, case["external_id"]);
        }
        assert_eq!(
            caller_from_identity(
                &json!({"end_user_id":"sdk-08012345678"}),
                "room__08012345678_"
            ),
            "sdk-08012345678"
        );
        assert_eq!(
            caller_from_identity(&json!({}), "room__08012345678_"),
            "+2348012345678"
        );
    }
    #[tokio::test]
    async fn storage_is_nonblocking_ordered_idempotent_and_uses_canonical_session() {
        let fake = Arc::new(Fake::default());
        fake.hold.store(true, Ordering::SeqCst);
        fake.message_failures.store(1, Ordering::SeqCst);
        let socket = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", socket.local_addr().unwrap());
        let app = Router::new().fallback(api).with_state(fake.clone());
        let server = tokio::spawn(async move {
            axum::serve(socket, app).await.unwrap();
        });
        let endpoint = super::super::platform::Endpoint {
            base_url: base,
            token: "local-store-fixture".into(),
            timeout_seconds: 1.0,
        };
        let services = Services::new(
            super::super::platform::PlatformConfig {
                conversation: endpoint.clone(),
                billing: endpoint,
                ..Default::default()
            },
            super::super::identity::test_session(json!({})),
            Trace::default(),
        )
        .unwrap();
        let collector = Collector::default();
        collector.tts_accepted("Hello");
        let store = Store::start(
            services,
            Meta {
                caller: "Ada@Example.test".into(),
                client_session_id: "client-session".into(),
                room: "room-a".into(),
                web: true,
                explicitly_routed: true,
            },
            collector,
            Default::default(),
            Trace::default(),
        );
        fake.started.notified().await;
        store.message("user", "Check my balance", "turn-1".into(), false);
        store.message("user", "Check my balance", "turn-1".into(), false);
        store.message("assistant", "One moment", "turn-1-attempt-1".into(), true);
        store.message(
            "assistant",
            "Your balance is 100",
            "turn-1-attempt-2".into(),
            false,
        );
        assert!(store.ids.get().is_none());
        assert_eq!(fake.requests.lock().unwrap().len(), 1);
        store.mark_disconnected();
        fake.release.notify_one();
        store
            .handoff(
                "Please connect me",
                json!({"status":"connecting","credential":"never-store-proof"}),
            )
            .await;
        store.close().await;
        let requests = fake.requests.lock().unwrap();
        let messages: Vec<_> = requests
            .iter()
            .filter(|(path, _)| path.ends_with("/messages"))
            .map(|(_, body)| body)
            .collect();
        assert_eq!(messages.len(), 4);
        assert_eq!(
            messages[0]["idempotency_key"],
            messages[1]["idempotency_key"]
        );
        assert_eq!(messages[0]["metadata"]["sequence"], 1);
        assert_eq!(messages[2]["metadata"]["sequence"], 2);
        assert_eq!(
            messages[2]["metadata"]["delivery"],
            "generated_not_confirmed"
        );
        assert_eq!(messages[3]["content"], "Your balance is 100");
        for message in messages {
            assert_eq!(message["session_id"], "canonical-session");
        }
        let analysis_index = requests
            .iter()
            .position(|(p, _)| p.ends_with("/analysis"))
            .unwrap();
        assert!(
            analysis_index
                > requests
                    .iter()
                    .rposition(|(p, _)| p.ends_with("/messages"))
                    .unwrap()
        );
        assert_eq!(
            requests[analysis_index].1["primary_intent"],
            "account_inquiry"
        );
        assert!(requests[analysis_index].1["summary"]
            .as_str()
            .unwrap()
            .contains("Check my balance"));
        let handoff = requests
            .iter()
            .find(|(_, body)| body["event_type"] == "aicc_handoff")
            .unwrap();
        assert_eq!(handoff.1["payload"]["status"], "connecting");
        assert!(handoff.1["payload"]["credential"].is_null());
        let end = requests
            .iter()
            .find(|(p, _)| p.ends_with("/sessions/end"))
            .unwrap();
        assert_eq!(end.1["session_id"], "canonical-session");
        assert_eq!(end.1["duration_seconds"], 0);
        let usage = requests
            .iter()
            .find(|(p, _)| p.ends_with("/observed-usage"))
            .unwrap();
        assert_eq!(usage.1["usage"]["tts_characters"], 5);
        assert_eq!(usage.1["usage"]["final"], true);
        server.abort();
    }
}
