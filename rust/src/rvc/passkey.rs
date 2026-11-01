//! Device approval is a separate authority from speech. Ceremony metadata and
//! proofs stay private; only a correlated one-use proof can reach the connector.
use super::{
    actions::ActionResult,
    banking::{Banking, EventSink},
    identity::VerifiedSession,
    platform::Endpoint,
    profiles::AgentContext,
    Trace,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    sync::{Arc, Mutex},
    time::Duration,
};
pub(crate) const TOPIC: &str = "attentive.fido2.v1";
pub(crate) const TOOL: &str = "wema_start_passkey";
const MAX_BYTES: usize = 14000;
pub(crate) type DeliverySink = Arc<
    dyn Fn(Value) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), String>> + Send>>
        + Send
        + Sync,
>;
#[derive(Clone, Deserialize, Serialize)]
#[serde(default)]
pub struct Config {
    pub business_ids: Vec<String>,
    pub agent_ids: Vec<String>,
    pub connector: Endpoint,
    pub timeout_seconds: f64,
    pub trim_options: bool,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            business_ids: vec![],
            agent_ids: vec![],
            connector: Endpoint {
                timeout_seconds: 15.0,
                ..Default::default()
            },
            timeout_seconds: 90.0,
            trim_options: true,
        }
    }
}
impl Config {
    pub(crate) fn validate(&self) -> Result<(), String> {
        if !self.timeout_seconds.is_finite()
            || !(0.0..=3600.0).contains(&self.timeout_seconds)
            || self.timeout_seconds == 0.0
            || !self.connector.timeout_seconds.is_finite()
            || self.connector.timeout_seconds <= 0.0
        {
            return Err("invalid passkey deadline".into());
        }
        if !self.connector.base_url.is_empty() {
            let url = reqwest::Url::parse(&self.connector.base_url)
                .map_err(|_| "invalid passkey connector")?;
            if !matches!(url.scheme(), "http" | "https")
                || url.host_str().is_none()
                || !url.username().is_empty()
                || url.password().is_some()
            {
                return Err("unsupported passkey connector".into());
            }
        }
        Ok(())
    }
}
fn canon(value: &str) -> String {
    value.trim().to_lowercase().replace('-', "")
}
pub(crate) fn enabled(config: &Config, identity: &VerifiedSession, transport: &str) -> bool {
    !transport.eq_ignore_ascii_case("sip")
        && config
            .business_ids
            .iter()
            .any(|v| !canon(v).is_empty() && canon(v) == canon(identity.business_id()))
        && config
            .agent_ids
            .iter()
            .any(|v| !canon(v).is_empty() && canon(v) == canon(identity.agent_id()))
}
pub(crate) fn configured(context: &AgentContext) -> bool {
    context.loaded
        && context
            .tools
            .iter()
            .find(|t| t["name"] == TOOL)
            .is_some_and(|t| t["is_active"] != false)
}
pub(crate) fn definition() -> Value {
    json!({"type":"function","function":{"name":TOOL,"description":"Start the caller's device passkey verification (Face ID / fingerprint). Takes no arguments. Use only after the caller agrees to run the passkey check.","parameters":{"type":"object","properties":{},"additionalProperties":false}}})
}
pub(crate) fn start_requested(text: &str) -> bool {
    static NEG: std::sync::OnceLock<Option<regex::Regex>> = std::sync::OnceLock::new();
    static POS: std::sync::OnceLock<Option<regex::Regex>> = std::sync::OnceLock::new();
    let normalized = text
        .to_ascii_lowercase()
        .chars()
        .map(|c| {
            if c.is_ascii_lowercase() || c == ' ' {
                c
            } else {
                ' '
            }
        })
        .collect::<String>()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    !normalized.is_empty()&&!NEG.get_or_init(||regex::Regex::new(r"\b(?:no|nope|not|dont|stop|wait|hold|cancel|later)\b").ok()).as_ref().is_some_and(|r|r.is_match(&normalized))&&POS.get_or_init(||regex::Regex::new(r"\b(?:yes|yeah|yep|sure|okay|ok|alright|go ahead|start|begin|do it|run it|passkey|pass key|continue|proceed)\b").ok()).as_ref().is_some_and(|r|r.is_match(&normalized))
}
pub(crate) fn supported_options(value: &Value, trim: bool) -> Value {
    let Some(object) = value.as_object() else {
        return json!({});
    };
    if !trim {
        return value.clone();
    }
    let keys = [
        "challenge",
        "rp",
        "rpId",
        "user",
        "pubKeyCredParams",
        "excludeCredentials",
        "allowCredentials",
        "authenticatorSelection",
        "attestation",
        "timeout",
        "authenticatorAttachment",
        "userVerification",
    ];
    let mut result: serde_json::Map<String, Value> = object
        .iter()
        .filter(|(key, _)| keys.contains(&key.as_str()))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    if let Some(selection) = result
        .get("authenticatorSelection")
        .and_then(Value::as_object)
    {
        let mut clean: serde_json::Map<String, Value> = selection
            .iter()
            .filter(|(key, _)| ["residentKey", "userVerification"].contains(&key.as_str()))
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        clean.insert("residentKey".into(), json!("required"));
        clean.insert("userVerification".into(), json!("required"));
        result.insert("authenticatorSelection".into(), json!(clean));
    }
    json!(result)
}
struct Pending {
    authentication_id: String,
    correlation_id: String,
    operation_id: String,
    expires: tokio::time::Instant,
    isolation: bool,
}
enum State {
    Idle,
    Starting(String),
    Waiting(Pending),
    Executing(String),
    Uncertain,
    Closed,
}
struct Private {
    state: State,
    device: Option<String>,
}
struct Inner {
    config: Config,
    bank: Banking,
    state: Mutex<Private>,
    emit: EventSink,
    deliver: DeliverySink,
    trace: Trace,
}
#[derive(Clone)]
pub(crate) struct Flow(Arc<Inner>);
/// Neither deserializable nor cloneable. Created only from a matching, unexpired
/// frame after the pending ceremony has been consumed under the state lock.
pub(crate) struct DeviceApproval {
    pending: Pending,
    proof: Value,
}
impl DeviceApproval {
    pub(crate) fn expired(&self) -> bool {
        tokio::time::Instant::now() >= self.pending.expires
    }
    pub(crate) fn operation_id(&self) -> &str {
        &self.pending.operation_id
    }
}
pub(crate) enum Inbound {
    Ignore,
    Notice(ActionResult),
    Execute(DeviceApproval),
}
struct StartLease {
    inner: Arc<Inner>,
    id: String,
    armed: bool,
}
impl Drop for StartLease {
    fn drop(&mut self) {
        if self.armed {
            if let Ok(mut private) = self.inner.state.lock() {
                if matches!(&private.state,State::Starting(id)if id==&self.id) {
                    private.state = State::Idle;
                }
            }
        }
    }
}
fn result(status: &str, message: &str) -> ActionResult {
    ActionResult::from_body(json!({"status":status,"message":message,"data":{}}))
}
impl Flow {
    pub(crate) fn new(
        config: Config,
        bank: Banking,
        emit: EventSink,
        deliver: DeliverySink,
        trace: Trace,
    ) -> Result<Self, String> {
        bank.require_device_approval();
        config.validate()?;
        Ok(Self(Arc::new(Inner {
            config,
            bank,
            state: Mutex::new(Private {
                state: State::Idle,
                device: None,
            }),
            emit,
            deliver,
            trace,
        })))
    }
    fn emit(&self, topic: &str, body: Value) {
        (self.0.emit)(json!({"type":topic,"payload":body}));
    }
    fn auth_status(&self, status: &str, pending: bool) {
        self.emit(
            "odion.auth.action_status",
            json!({"type":"odion.auth.action_status","status":status,"usePasskey":pending}),
        );
    }
    pub(crate) fn awaiting(&self) -> bool {
        self.0.state.lock().ok().is_some_and(
            |s| matches!(&s.state,State::Waiting(p)if tokio::time::Instant::now()<p.expires),
        )
    }
    pub(crate) fn busy(&self) -> bool {
        self.0.state.lock().ok().is_none_or(|s| {
            matches!(
                s.state,
                State::Starting(_) | State::Waiting(_) | State::Executing(_) | State::Uncertain
            )
        })
    }
    pub(crate) fn close(&self) {
        if let Ok(mut state) = self.0.state.lock() {
            state.state = State::Closed;
            state.device = None;
        }
    }
    pub(crate) fn expire(&self) -> Option<ActionResult> {
        let expired = self.0.state.lock().ok().is_some_and(|mut state| {
            if matches!(&state.state,State::Waiting(p)if tokio::time::Instant::now()>=p.expires) {
                state.state = State::Idle;
                true
            } else {
                false
            }
        });
        if expired {
            self.auth_status("expired", false);
            self.0
                .trace
                .emit("passkey_outcome", 0, 0, json!({"status":"expired"}));
            Some(result(
                "expired",
                "That approval timed out. Ask me to try again when you're ready.",
            ))
        } else {
            None
        }
    }
    pub(crate) async fn start(
        &self,
        workflow: &str,
        operation_id: String,
        isolation: bool,
    ) -> ActionResult {
        let feature = match workflow {
            "transfer" => "Transfer",
            "airtime" => "Airtime",
            _ => return result("blocked", "That action cannot be confirmed with a passkey."),
        };
        let nonce = uuid::Uuid::new_v4().simple().to_string();
        let device = {
            let Ok(mut private) = self.0.state.lock() else {
                return result("failed", "This call cannot confirm actions right now.");
            };
            match private.state {
                State::Closed => {
                    return result("failed", "This call can no longer confirm actions.")
                }
                State::Uncertain => return ActionResult::unknown(),
                State::Idle => {}
                _ => return result("busy", "A passkey confirmation is already waiting."),
            };
            private.state = State::Starting(nonce.clone());
            private
                .device
                .clone()
                .unwrap_or_else(|| "unknown-device".into())
        };
        let mut lease = StartLease {
            inner: self.0.clone(),
            id: nonce.clone(),
            armed: true,
        };
        let profile = self.0.bank.device_profile();
        let customer = profile["customer_id"].as_str().unwrap_or("");
        let name = profile["account_name"]
            .as_str()
            .filter(|s| !s.is_empty())
            .unwrap_or(customer);
        let reply = match self
            .0
            .bank
            .device_post(
                &self.0.config.connector,
                "/v1/fido2/begin",
                json!({"feature":feature,"device_imei":device,"display_name":name}),
            )
            .await
        {
            Ok(reply) => reply,
            Err(_) => {
                return result(
                    "failed",
                    "I could not start the passkey confirmation right now.",
                )
            }
        };
        let Some(auth) = reply["data"]["authentication_id"]
            .as_str()
            .map(str::trim)
            .filter(|s| !s.is_empty())
        else {
            return result(
                "failed",
                "I could not start the passkey confirmation right now.",
            );
        };
        if reply["status"] != "ok" || !reply["data"]["options"].is_object() {
            return result(
                "failed",
                "I could not start the passkey confirmation right now.",
            );
        }
        let correlation = format!("ceremony-{}", uuid::Uuid::new_v4().simple());
        let options = supported_options(&reply["data"]["options"], self.0.config.trim_options);
        let timeout = Duration::from_secs_f64(self.0.config.timeout_seconds);
        let expires_at = chrono::Utc::now()
            + chrono::TimeDelta::milliseconds((self.0.config.timeout_seconds * 1000.0) as i64);
        let ceremony = json!({"type":"fido2_ceremony","authenticationId":auth,"correlationId":correlation,"action":reply["data"]["action"].as_i64().unwrap_or(0),"feature":feature,"mode":reply["data"]["ceremony_mode"].as_str().unwrap_or("authenticate"),"expiresAt":expires_at.to_rfc3339_opts(chrono::SecondsFormat::Micros,false),"options":options,"usePasskey":true});
        if ceremony.to_string().len() > MAX_BYTES {
            return result(
                "failed",
                "I could not start the passkey confirmation right now.",
            );
        }
        {
            let Ok(mut private) = self.0.state.lock() else {
                return result("failed", "This call cannot confirm actions right now.");
            };
            if !matches!(&private.state,State::Starting(id)if id==&nonce) {
                return result("failed", "That confirmation is no longer current.");
            }
            private.state = State::Waiting(Pending {
                authentication_id: auth.into(),
                correlation_id: correlation.clone(),
                operation_id,
                expires: tokio::time::Instant::now() + timeout,
                isolation,
            });
        }
        lease.armed = false;
        let delivered = tokio::time::timeout(
            Duration::from_secs(5),
            (self.0.deliver)(json!({"type":TOPIC,"payload":ceremony})),
        )
        .await
        .is_ok_and(|r| r.is_ok());
        if !delivered {
            let cleared = self.0.state.lock().ok().is_some_and(|mut state| {
                if matches!(&state.state,State::Waiting(p)if p.correlation_id==correlation) {
                    state.state = State::Idle;
                    true
                } else {
                    false
                }
            });
            self.0
                .trace
                .emit("passkey_delivery_failed", 0, 0, json!({"stage":"ceremony"}));
            if cleared {
                self.auth_status("failed", false);
                return result(
                    "failed",
                    "I couldn't confirm the approval reached your app. Please try again.",
                );
            }
            return result("ceremony_started", "Your app approval is being processed.");
        }
        self.auth_status("required", true);
        self.0.trace.emit(
            "passkey_ceremony_started",
            0,
            0,
            json!({"status":"ceremony_started"}),
        );
        result("ceremony_started","I've sent the approval to your app. Please confirm it with your Face ID, then I'll finish it.")
    }
    pub(crate) fn receive(&self, raw: &[u8]) -> Inbound {
        if raw.len() > MAX_BYTES {
            return Inbound::Ignore;
        }
        let Ok(value) = serde_json::from_slice::<Value>(raw) else {
            return Inbound::Ignore;
        };
        if !value.is_object() {
            return Inbound::Ignore;
        }
        let Ok(mut private) = self.0.state.lock() else {
            return Inbound::Ignore;
        };
        if matches!(private.state, State::Closed) {
            return Inbound::Ignore;
        }
        if value["type"] == "session_context" {
            private.device = value["deviceImei"]
                .as_str()
                .filter(|s| !s.trim().is_empty() && s.len() <= 512)
                .map(str::to_string);
            self.0.trace.emit(
                "passkey_session_context",
                0,
                0,
                json!({"device_present":private.device.is_some()}),
            );
            return Inbound::Ignore;
        }
        let State::Waiting(pending) = &private.state else {
            return Inbound::Ignore;
        };
        let auth = value["authenticationId"] == pending.authentication_id;
        let correlation = value["correlationId"] == pending.correlation_id;
        let kind = value["type"].as_str().unwrap_or("");
        let safe_kind = match kind {
            "fido2_proof" => "proof",
            "fido2_failed" => "failed",
            "fido2_cancelled" => "cancelled",
            _ => "other",
        };
        self.0.trace.emit("passkey_app_message",0,0,json!({"kind":safe_kind,"authentication_id_match":auth,"correlation_id_match":correlation}));
        if !auth || !correlation {
            return Inbound::Ignore;
        }
        if kind == "fido2_cancelled" || kind == "fido2_failed" {
            private.state = State::Idle;
            drop(private);
            self.auth_status("failed", false);
            return Inbound::Notice(if kind == "fido2_cancelled" {
                result(
                    "cancelled",
                    "No problem. Nothing was approved, and we can keep talking.",
                )
            } else {
                result(
                    "failed",
                    "The passkey confirmation did not go through. Nothing was approved.",
                )
            });
        }
        if kind != "fido2_proof" || !value["proof"].as_object().is_some_and(|p| !p.is_empty()) {
            return Inbound::Ignore;
        }
        let state = std::mem::replace(&mut private.state, State::Idle);
        let State::Waiting(pending) = state else {
            return Inbound::Ignore;
        };
        if tokio::time::Instant::now() >= pending.expires {
            drop(private);
            self.finish_packet(&pending, false, "expired");
            self.auth_status("expired", false);
            return Inbound::Notice(result(
                "expired",
                "That approval timed out. Ask me to try again when you're ready.",
            ));
        }
        private.state = State::Executing(pending.correlation_id.clone());
        Inbound::Execute(DeviceApproval {
            pending,
            proof: value["proof"].clone(),
        })
    }
    fn finish_packet(&self, pending: &Pending, accepted: bool, outcome: &str) {
        self.emit(TOPIC,json!({"type":"fido2_result","authenticationId":pending.authentication_id,"correlationId":pending.correlation_id,"accepted":accepted,"outcome":outcome}));
    }
    pub(crate) fn reject(&self, approval: DeviceApproval, message: &str) -> ActionResult {
        if let Ok(mut private) = self.0.state.lock() {
            if matches!(&private.state,State::Executing(id)if id==&approval.pending.correlation_id)
            {
                private.state = State::Idle;
            }
        }
        self.finish_packet(&approval.pending, false, "rejected");
        self.auth_status("failed", false);
        result("failed", message)
    }
    pub(crate) async fn execute(&self, approval: DeviceApproval) -> ActionResult {
        let pending = &approval.pending;
        let active =
            self.0.state.lock().ok().is_some_and(
                |s| matches!(&s.state,State::Executing(id)if id==&pending.correlation_id),
            );
        if !active {
            return result("failed", "That approval is no longer current.");
        }
        let reply = if tokio::time::Instant::now() >= pending.expires {
            Some(
                json!({"status":"expired","message":"That approval timed out. Nothing was submitted."}),
            )
        } else if !pending.isolation
            && !self
                .0
                .bank
                .consume_device_preparation(&pending.operation_id)
        {
            Some(
                json!({"status":"failed","message":"That transaction preview has changed. Please request it again."}),
            )
        } else {
            self.0.bank.device_post(&self.0.config.connector,"/v1/tools/wema_execute_prepared",json!({"operation_id":pending.operation_id,"authentication_id":pending.authentication_id,"fido2_credential_proof":approval.proof})).await.ok()
        };
        let status = reply
            .as_ref()
            .and_then(|v| v["status"].as_str())
            .unwrap_or("unknown");
        let accepted = ["ok", "success", "completed"].contains(&status);
        let unknown = ["pending", "unknown"].contains(&status);
        let mut outcome = if unknown {
            ActionResult::unknown()
        } else {
            let mut body = reply.unwrap_or_else(|| json!({"status":"unknown"}));
            scrub_credentials(&mut body);
            let mut outcome = ActionResult::from_body(body);
            if outcome.message.is_empty() {
                outcome.message = if accepted {
                    "Approved and sent."
                } else {
                    "The bank did not accept the approval, so nothing was sent."
                }
                .into();
            }
            outcome
        };
        if unknown {
            outcome.status = "unknown".into();
        }
        outcome.extra.insert(
            "auth_status".into(),
            json!(if accepted { "verified" } else { "failed" }),
        );
        let deliver = {
            let Ok(mut private) = self.0.state.lock() else {
                return outcome;
            };
            if matches!(&private.state,State::Executing(id)if id==&pending.correlation_id) {
                private.state = if unknown {
                    State::Uncertain
                } else {
                    State::Idle
                };
                true
            } else {
                false
            }
        };
        if deliver {
            self.finish_packet(
                pending,
                accepted,
                if accepted {
                    "executed"
                } else if unknown {
                    "unknown"
                } else {
                    "rejected"
                },
            );
            self.auth_status(
                if accepted {
                    "verified"
                } else if unknown {
                    "unknown"
                } else {
                    "failed"
                },
                false,
            );
        }
        self.0.trace.emit(
            "passkey_outcome",
            0,
            0,
            json!({"status":if accepted{"verified"}else if unknown{"unknown"}else{"rejected"}}),
        );
        outcome
    }
}
fn scrub_credentials(value: &mut Value) {
    match value {
        Value::Object(object) => {
            object.retain(|key, _| {
                !matches!(
                    key.chars()
                        .filter(|c| c.is_ascii_alphanumeric())
                        .flat_map(char::to_lowercase)
                        .collect::<String>()
                        .as_str(),
                    "proof"
                        | "fido2credentialproof"
                        | "authenticationid"
                        | "correlationid"
                        | "deviceimei"
                        | "options"
                        | "clientdatajson"
                        | "authenticatordata"
                        | "signature"
                        | "userhandle"
                        | "publickey"
                        | "challenge"
                )
            });
            for value in object.values_mut() {
                scrub_credentials(value);
            }
        }
        Value::Array(values) => {
            for value in values {
                scrub_credentials(value);
            }
        }
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{extract::State as HttpState, http::HeaderMap, routing::post, Json, Router};
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use tokio::task::JoinHandle;
    #[derive(Default)]
    struct Fake {
        begins: AtomicUsize,
        executions: AtomicUsize,
        events: Mutex<Vec<Value>>,
        reply_status: Mutex<String>,
        hold_begin: AtomicBool,
        begin_started: tokio::sync::Notify,
        begin_release: tokio::sync::Notify,
        delivery_failed: AtomicBool,
    }
    async fn begin(
        HttpState(fake): HttpState<Arc<Fake>>,
        headers: HeaderMap,
        Json(body): Json<Value>,
    ) -> Json<Value> {
        assert_eq!(headers["x-business-id"], "business-a");
        assert_eq!(headers["x-wema-customer-id"], "customer-a");
        assert_eq!(body["feature"], "Transfer");
        fake.begins.fetch_add(1, Ordering::SeqCst);
        fake.begin_started.notify_one();
        if fake.hold_begin.load(Ordering::SeqCst) {
            fake.begin_release.notified().await;
        }
        Json(
            json!({"status":"ok","data":{"authentication_id":"authentication-secret","action":1,"ceremony_mode":"authenticate","options":{"challenge":"challenge-secret","rpId":"bank.example","extensions":{"secret":"no-forward"},"authenticatorSelection":{"requireResidentKey":false,"userVerification":"discouraged","authenticatorAttachment":"cross-platform"}}}}),
        )
    }
    async fn execute(
        HttpState(fake): HttpState<Arc<Fake>>,
        headers: HeaderMap,
        Json(body): Json<Value>,
    ) -> Json<Value> {
        assert_eq!(headers["x-business-id"], "business-a");
        assert_eq!(headers["x-wema-customer-id"], "customer-a");
        assert_eq!(body["operation_id"], "operation-a");
        assert_eq!(body["authentication_id"], "authentication-secret");
        assert_eq!(body["fido2_credential_proof"]["id"], "credential-secret");
        fake.executions.fetch_add(1, Ordering::SeqCst);
        let status = fake.reply_status.lock().unwrap().clone();
        Json(
            json!({"status":status,"message":"The bank returned a result.","data":{"transaction_reference":"TXN-001","fido2_credential_proof":body["fido2_credential_proof"],"authentication_id":"authentication-secret","options":{"challenge":"challenge-secret"}}}),
        )
    }
    async fn prepare() -> Json<Value> {
        Json(
            json!({"status":"prepared","message":"Preview ready","data":{"operation_id":"operation-a","preview":{"amount":5000,"recipient_name":"Ada","bank_name":"Wema Bank","recipient_account":"0012345678"}}}),
        )
    }
    struct Harness {
        flow: Flow,
        bank: Banking,
        fake: Arc<Fake>,
        trace: Trace,
        server: JoinHandle<()>,
    }
    impl Drop for Harness {
        fn drop(&mut self) {
            self.flow.close();
            self.server.abort();
        }
    }
    async fn setup(timeout: f64) -> Harness {
        let fake = Arc::new(Fake::default());
        *fake.reply_status.lock().unwrap() = "ok".into();
        let socket = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", socket.local_addr().unwrap());
        let app = Router::new()
            .route("/v1/fido2/begin", post(begin))
            .route("/v1/tools/wema_execute_prepared", post(execute))
            .route("/prepare", post(prepare))
            .with_state(fake.clone());
        let server = tokio::spawn(async move {
            axum::serve(socket, app).await.unwrap();
        });
        let identity = super::super::identity::test_session(
            json!({"voice_auth_required":false,"wema_context":{"customer_id":"customer-a","account_name":"Ada","account_number":"0012345678"}}),
        );
        let context = AgentContext {
            loaded: true,
            agent_id: "agent-a".into(),
            business_id: "business-a".into(),
            end_user_id: "caller-a".into(),
            tools: vec![
                json!({"name":"wema_execute_prepared","url":format!("{base}/v1/tools/wema_execute_prepared"),"method":"POST","headers":{"X-Business-Id":"forged"},"request_schema":{"type":"object","properties":{"operation_id":{"type":"string"}},"required":["operation_id"],"additionalProperties":false}}),
                json!({"name":"wema_prepare_transfer","url":format!("{base}/prepare"),"method":"POST","request_schema":{"type":"object","properties":{},"additionalProperties":false}}),
            ],
            ..Default::default()
        };
        let events = fake.clone();
        let emit: EventSink = Arc::new(move |event| events.events.lock().unwrap().push(event));
        let bank = Banking::new(
            Default::default(),
            context,
            identity,
            "session-a".into(),
            emit.clone(),
        )
        .unwrap();
        assert_eq!(
            bank.read_or_prepare(super::super::actions::Call {
                context: None,
                id: "prepare-a".into(),
                name: "wema_prepare_transfer".into(),
                arguments: json!({})
            })
            .await
            .status,
            "prepared"
        );
        let trace = Trace::default();
        let deliver_emit = emit.clone();
        let delivery_state = fake.clone();
        let deliver: DeliverySink = Arc::new(move |body| {
            let emit = deliver_emit.clone();
            let state = delivery_state.clone();
            Box::pin(async move {
                if state.delivery_failed.load(Ordering::SeqCst) {
                    return Err("local data delivery failed".into());
                }
                emit(body);
                Ok(())
            })
        });
        let flow = Flow::new(
            Config {
                connector: Endpoint {
                    base_url: base,
                    timeout_seconds: 1.0,
                    ..Default::default()
                },
                timeout_seconds: timeout,
                ..Default::default()
            },
            bank.clone(),
            emit,
            deliver,
            trace.clone(),
        )
        .unwrap();
        Harness {
            flow,
            bank,
            fake,
            trace,
            server,
        }
    }
    fn ceremony(h: &Harness) -> Value {
        h.fake
            .events
            .lock()
            .unwrap()
            .iter()
            .rev()
            .find(|e| e["type"] == TOPIC && e["payload"]["type"] == "fido2_ceremony")
            .unwrap()["payload"]
            .clone()
    }
    fn frame(h: &Harness) -> Value {
        let ceremony = ceremony(h);
        json!({"type":"fido2_proof","authenticationId":ceremony["authenticationId"],"correlationId":ceremony["correlationId"],"proof":{"id":"credential-secret","response":{"signature":"signature-secret","clientDataJSON":"client-secret"}}})
    }
    async fn start(h: &Harness) {
        assert_eq!(
            h.flow
                .start("transfer", "operation-a".into(), false)
                .await
                .status,
            "ceremony_started"
        );
    }
    fn approval(h: &Harness) -> DeviceApproval {
        let Inbound::Execute(approval) = h.flow.receive(&serde_json::to_vec(&frame(h)).unwrap())
        else {
            panic!("proof not accepted for submission");
        };
        approval
    }
    #[test]
    fn current_python_passkey_policies_match() {
        let reference: Value =
            serde_json::from_str(include_str!("../../fixtures/python-parity-reference.json"))
                .unwrap();
        assert_eq!(
            vec![definition()],
            reference["passkey_definition"].as_array().unwrap().clone()
        );
        for case in reference["passkey_options"].as_array().unwrap() {
            assert_eq!(supported_options(&case["options"], true), case["expected"]);
        }
        for case in reference["passkey_start_requests"].as_array().unwrap() {
            assert_eq!(
                start_requested(case["text"].as_str().unwrap()),
                case["expected"].as_bool().unwrap()
            );
        }
        let config = Config {
            business_ids: vec!["business-a".into()],
            agent_ids: vec!["agent-a".into()],
            ..Default::default()
        };
        for case in reference["passkey_scope"].as_array().unwrap() {
            if case["business"] == "" {
                continue;
            }
            let identity = super::super::identity::test_session(
                json!({"business_id":case["business"],"config_agent_id":case["agent"]}),
            );
            assert_eq!(
                enabled(&config, &identity, "livekit"),
                case["expected"].as_bool().unwrap()
            );
            assert!(!enabled(&config, &identity, "sip"));
        }
    }
    #[tokio::test]
    async fn failed_private_delivery_cannot_claim_approval_was_sent() {
        let h = setup(90.0).await;
        h.fake.delivery_failed.store(true, Ordering::SeqCst);
        let result = h.flow.start("transfer", "operation-a".into(), false).await;
        assert_eq!(result.status, "failed");
        assert!(!h.flow.busy());
        assert_eq!(h.fake.executions.load(Ordering::SeqCst), 0);
    }
    #[tokio::test]
    async fn correlated_proof_executes_once_and_never_exposes_credentials_to_model_or_trace() {
        let h = setup(90.0).await;
        h.flow
            .receive(br#"{"type":"session_context","deviceImei":"device-secret"}"#);
        start(&h).await;
        assert_eq!(h.fake.executions.load(Ordering::SeqCst), 0);
        let packet = ceremony(&h);
        assert!(packet["options"].get("extensions").is_none());
        assert_eq!(
            packet["options"]["authenticatorSelection"]["userVerification"],
            "required"
        );
        let proof = frame(&h);
        let Inbound::Execute(permit) = h.flow.receive(&serde_json::to_vec(&proof).unwrap()) else {
            panic!();
        };
        assert!(matches!(
            h.flow.receive(&serde_json::to_vec(&proof).unwrap()),
            Inbound::Ignore
        ));
        let result = h.flow.execute(permit).await;
        assert_eq!(result.status, "ok");
        assert_eq!(result.data["transaction_reference"], "TXN-001");
        let context = serde_json::to_string(&result).unwrap();
        let trace = serde_json::to_string(&h.trace.events()).unwrap();
        for secret in [
            "credential-secret",
            "signature-secret",
            "authentication-secret",
            "challenge-secret",
            "client-secret",
            "device-secret",
        ] {
            assert!(!context.contains(secret));
            assert!(!trace.contains(secret));
        }
        assert_eq!(h.fake.executions.load(Ordering::SeqCst), 1);
        assert!(!h.flow.busy());
    }
    #[tokio::test]
    async fn wrong_correlation_malformed_and_replayed_proof_cannot_submit() {
        let h = setup(90.0).await;
        start(&h).await;
        let mut bad = frame(&h);
        bad["correlationId"] = json!("wrong");
        assert!(matches!(
            h.flow.receive(&serde_json::to_vec(&bad).unwrap()),
            Inbound::Ignore
        ));
        let mut bad = frame(&h);
        bad["proof"] = json!("credential-secret");
        assert!(matches!(
            h.flow.receive(&serde_json::to_vec(&bad).unwrap()),
            Inbound::Ignore
        ));
        assert!(matches!(
            h.flow.receive(&vec![b'x'; MAX_BYTES + 1]),
            Inbound::Ignore
        ));
        assert!(matches!(h.flow.receive(b"[]"), Inbound::Ignore));
        assert_eq!(h.fake.executions.load(Ordering::SeqCst), 0);
        assert_eq!(h.flow.execute(approval(&h)).await.status, "ok");
        assert!(matches!(
            h.flow.receive(&serde_json::to_vec(&frame(&h)).unwrap()),
            Inbound::Ignore
        ));
        assert_eq!(h.fake.executions.load(Ordering::SeqCst), 1);
    }
    #[tokio::test]
    async fn changed_preview_expiry_and_closed_call_cannot_execute() {
        let h = setup(90.0).await;
        start(&h).await;
        h.bank.clear_prepared();
        assert_eq!(h.flow.execute(approval(&h)).await.status, "failed");
        assert_eq!(h.fake.executions.load(Ordering::SeqCst), 0);
        let h = setup(0.01).await;
        start(&h).await;
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert!(
            matches!(h.flow.receive(&serde_json::to_vec(&frame(&h)).unwrap()),Inbound::Notice(r)if r.status=="expired")
        );
        assert_eq!(h.fake.executions.load(Ordering::SeqCst), 0);
        h.flow.close();
        assert!(matches!(
            h.flow.receive(&serde_json::to_vec(&frame(&h)).unwrap()),
            Inbound::Ignore
        ));
        assert_eq!(
            h.flow
                .start("transfer", "operation-a".into(), false)
                .await
                .status,
            "failed"
        );
    }
    #[tokio::test]
    async fn unknown_or_pending_backend_reply_is_never_a_success_and_blocks_retry() {
        for status in ["pending", "unknown"] {
            let h = setup(90.0).await;
            *h.fake.reply_status.lock().unwrap() = status.into();
            start(&h).await;
            assert_eq!(h.flow.execute(approval(&h)).await.status, "unknown");
            assert_eq!(
                h.flow
                    .start("transfer", "operation-a".into(), false)
                    .await
                    .status,
                "unknown"
            );
            let events = h.fake.events.lock().unwrap();
            let result = events
                .iter()
                .rev()
                .find(|e| e["payload"]["type"] == "fido2_result")
                .unwrap();
            assert_eq!(result["payload"]["accepted"], false);
            assert_eq!(result["payload"]["outcome"], "unknown");
            assert_eq!(h.fake.begins.load(Ordering::SeqCst), 1);
            assert_eq!(h.fake.executions.load(Ordering::SeqCst), 1);
        }
    }
    #[tokio::test]
    async fn cancelled_start_releases_ownership_and_retry_uses_new_correlation() {
        let h = setup(90.0).await;
        h.fake.hold_begin.store(true, Ordering::SeqCst);
        let flow = h.flow.clone();
        let request =
            tokio::spawn(async move { flow.start("transfer", "operation-a".into(), false).await });
        h.fake.begin_started.notified().await;
        request.abort();
        let _ = request.await;
        assert!(!h.flow.busy());
        h.fake.hold_begin.store(false, Ordering::SeqCst);
        h.fake.begin_release.notify_one();
        start(&h).await;
        let first = ceremony(&h);
        let cancel = json!({"type":"fido2_cancelled","authenticationId":first["authenticationId"],"correlationId":first["correlationId"]});
        assert!(matches!(
            h.flow.receive(&serde_json::to_vec(&cancel).unwrap()),
            Inbound::Notice(_)
        ));
        start(&h).await;
        assert_ne!(ceremony(&h)["correlationId"], first["correlationId"]);
        assert_eq!(h.fake.executions.load(Ordering::SeqCst), 0);
    }
    #[tokio::test]
    async fn spoken_confirmation_permit_cannot_bypass_required_device_approval() {
        let h = setup(90.0).await;
        let pending = super::super::actions::Pending {
            call: super::super::actions::Call {
                context: None,
                id: "spoken".into(),
                name: "wema_execute_prepared".into(),
                arguments: json!({"operation_id":"operation-a"}),
            },
            question: "Do you confirm?".into(),
            origin: 1,
            reasks: 0,
        };
        let permit = super::super::actions::confirmed(
            &pending,
            2,
            super::super::actions::Decision::Confirmed,
            true,
        )
        .unwrap();
        assert_eq!(h.bank.execute_confirmed(permit).await.status, "failed");
        assert_eq!(h.fake.executions.load(Ordering::SeqCst), 0);
    }
}
