//! Pinned AICC dial through the official SDK. A pending or delivered-but-stale
//! connection owns cleanup until the authoritative conversation accepts it.
use super::{identity::VerifiedSession, Trace};
use livekit_api::services::{
    room::RoomClient,
    sip::{CreateSIPParticipantOptions, SIPClient},
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};
type CleanupTasks = Arc<Mutex<Vec<tokio::task::JoinHandle<()>>>>;
pub(crate) const TOOL: &str = "transfer_to_aicc";
pub(crate) const RULES: &str = include_str!("transfer_rules.txt");
pub(crate) fn enabled(context: &super::profiles::AgentContext) -> bool {
    context.loaded
        && context.tools.iter().any(|tool| {
            tool["is_active"] != false
                && tool["name"]
                    .as_str()
                    .is_some_and(|name| name.trim() == TOOL)
                // Existing saved agents use both addresses for this application-owned
                // capability. Neither address is an HTTP destination.
                && matches!(tool["url"].as_str(),
                    Some("builtin://transfer_to_aicc" | "internal://transfer-to-aicc"))
        })
}
pub(crate) fn definition() -> Value {
    json!({"type":"function","function":{"name":TOOL,"description":"Immediately connect this caller to a human colleague when requested or when they accept an offered escalation. Their request is sufficient: do not ask for confirmation again or merely offer a transfer. Give a factual reason; never choose a destination. Call without a spoken preamble: the application announces the connection once and reports failure honestly.","parameters":{"type":"object","properties":{"reason_summary":{"type":"string","description":"Short summary of why the caller needs a human agent."}},"required":["reason_summary"],"additionalProperties":false}}})
}
pub(crate) fn final_matches(hypothesis: &str, final_: &str) -> bool {
    fn words(text: &str) -> String {
        text.split_whitespace()
            .map(|word| {
                word.trim_matches(|c: char| c.is_ascii_punctuation() && c != '\'' && c != '-')
                    .to_lowercase()
            })
            .filter(|word| !word.is_empty())
            .collect::<Vec<_>>()
            .join(" ")
    }
    words(hypothesis) == words(final_)
}
pub(crate) async fn generate_guarded(
    p: &super::providers::Providers,
    messages: Vec<Value>,
    tools: Vec<Value>,
    quiet: bool,
    turn: u64,
    attempt: u64,
    out: tokio::sync::mpsc::Sender<super::providers::LlmEvent>,
) -> Result<(), String> {
    use super::providers::LlmEvent;
    let directory = tools.iter().any(|tool| {
        tool["function"]["name"]
            .as_str()
            .is_some_and(super::directory::is_tool)
    });
    let directory_routing = tools
        .iter()
        .any(|tool| tool["function"]["name"] == super::directory::LOOKUP);
    if !directory && !tools.iter().any(|tool| tool["function"]["name"] == TOOL) {
        return p.llm(messages, tools, quiet, turn, attempt, out).await;
    }
    let (tx, mut rx) = tokio::sync::mpsc::channel(32);
    let producer = p.llm(
        messages,
        tools,
        quiet || directory_routing,
        turn,
        attempt,
        tx,
    );
    let collector = async {
        let mut captured = Vec::new();
        let mut first = None;
        let mut bytes = 0usize;
        while let Some(event) = rx.recv().await {
            if let LlmEvent::Text(text) = &event {
                first.get_or_insert_with(std::time::Instant::now);
                bytes += text.len();
            }
            if captured.len() >= 16384 || bytes > 1024 * 1024 {
                return Err("human transfer stream buffer limit".to_string());
            }
            captured.push(event);
        }
        Ok((captured, first))
    };
    let (_, (events, first)) = tokio::try_join!(producer, collector)?;
    let suppress = directory_routing || events
        .iter()
        .any(|event| matches!(event,LlmEvent::Tool{name,..}if name==TOOL||super::directory::is_tool(name)));
    p.trace.emit(if directory{"llm_directory_guard_decision"}else{"llm_transfer_guard_decision"},turn,attempt,json!({"suppressed":suppress,"buffered_events":events.len(),"text_buffer_ms":first.map(|started|(started.elapsed().as_secs_f64()*1_000_000.0).round()/1000.0)}));
    for event in events {
        if !suppress || matches!(event, LlmEvent::Tool { .. } | LlmEvent::Context(_)) {
            out.send(event)
                .await
                .map_err(|_| "transfer generation cancelled")?;
        }
    }
    Ok(())
}
#[derive(Clone, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    pub outbound_trunk_id: String,
    pub target_number: String,
    pub from_number: String,
    pub preserve_caller_id: bool,
}
struct Credentials {
    url: String,
    key: String,
    secret: String,
}
#[derive(Clone)]
pub struct Client {
    config: Config,
    credentials: Arc<Credentials>,
    tasks: CleanupTasks,
    cleanup_ok: Arc<AtomicBool>,
    trace: Trace,
}
struct Cleanup {
    credentials: Arc<Credentials>,
    room: String,
    identity: String,
    tasks: CleanupTasks,
    cleanup_ok: Arc<AtomicBool>,
    notifier: Option<tokio::sync::oneshot::Sender<bool>>,
    trace: Trace,
}
struct NoDialNotice(Option<tokio::sync::oneshot::Sender<bool>>);
impl Drop for NoDialNotice {
    fn drop(&mut self) {
        if let Some(notice) = self.0.take() {
            let _ = notice.send(true);
        }
    }
}
impl Cleanup {
    fn schedule(self) -> Option<tokio::sync::oneshot::Receiver<bool>> {
        let runtime = tokio::runtime::Handle::try_current().ok()?;
        let tasks = self.tasks.clone();
        let (done, rx) = tokio::sync::oneshot::channel();
        let task = runtime.spawn(async move {
            let result = self.remove().await;
            let _ = done.send(result);
        });
        if let Ok(mut tasks) = tasks.lock() {
            tasks.retain(|task| !task.is_finished());
            tasks.push(task);
        }
        Some(rx)
    }
    async fn remove(mut self) -> bool {
        let client = RoomClient::with_api_key(
            &self.credentials.url,
            &self.credentials.key,
            &self.credentials.secret,
        )
        .with_failover(false)
        .with_request_timeout(Duration::from_secs(5));
        let outcome = tokio::time::timeout(
            Duration::from_secs(5),
            client.remove_participant(&self.room, &self.identity),
        )
        .await;
        self.trace.emit(
            "aicc_bridge_cleanup",
            0,
            0,
            json!({"removed":matches!(outcome,Ok(Ok(())))}),
        );
        let confirmed = matches!(outcome, Ok(Ok(())));
        if !confirmed {
            self.cleanup_ok.store(false, Ordering::SeqCst);
        }
        if let Some(notice) = self.notifier.take() {
            let _ = notice.send(confirmed);
        }
        confirmed
    }
}
/// Non-cloneable ownership. The actor commits only while its final turn still
/// owns the request; dropped/stale replies remove even a just-connected leg.
pub struct Connection {
    receipt: Value,
    cleanup: Option<Cleanup>,
}
impl std::fmt::Debug for Connection {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PendingBridgeConnection")
            .finish_non_exhaustive()
    }
}
/// SDK adapter lifetime after the actor accepts the connection. Caller hangup
/// removes the leg; an intentional worker departure may detach a live handoff.
pub struct Established {
    receipt: Value,
    cleanup: Option<Cleanup>,
}
impl std::fmt::Debug for Established {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("EstablishedBridgeConnection")
            .finish_non_exhaustive()
    }
}
impl Established {
    pub fn receipt(&self) -> &Value {
        &self.receipt
    }
    pub fn detach(mut self) -> Value {
        self.cleanup.take();
        self.receipt.clone()
    }
}
impl Drop for Established {
    fn drop(&mut self) {
        if let Some(cleanup) = self.cleanup.take() {
            cleanup.schedule();
        }
    }
}
impl Connection {
    pub fn receipt(&self) -> &Value {
        &self.receipt
    }
    async fn reject(mut self, mut failure: Value) -> Value {
        if let Some(cleanup) = self.cleanup.take() {
            // Await cleanup on an ordinary error, but keep it owned if the
            // awaiting caller is cancelled during this request.
            let confirmed = match cleanup.schedule() {
                Some(done) => done.await.unwrap_or(false),
                None => false,
            };
            failure["cleanup_confirmed"] = json!(confirmed);
            if !confirmed {
                failure["status"] = json!("unknown");
            }
        }
        failure
    }
    pub fn commit(mut self) -> Established {
        Established {
            receipt: self.receipt.clone(),
            cleanup: self.cleanup.take(),
        }
    }
}
impl Drop for Connection {
    fn drop(&mut self) {
        if let Some(cleanup) = self.cleanup.take() {
            cleanup.schedule();
        }
    }
}
impl Client {
    pub fn new(
        config: Config,
        api_url: &str,
        key: &str,
        secret: &str,
        trace: Trace,
    ) -> Result<Self, String> {
        let url = reqwest::Url::parse(api_url).map_err(|_| "invalid bridge API URL")?;
        if !matches!(url.scheme(), "http" | "https" | "ws" | "wss")
            || url.host_str().is_none()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || key.is_empty()
            || secret.is_empty()
        {
            return Err("invalid bridge API credentials".into());
        }
        Ok(Self {
            config,
            credentials: Arc::new(Credentials {
                url: api_url
                    .trim_end_matches('/')
                    .replacen("wss://", "https://", 1)
                    .replacen("ws://", "http://", 1),
                key: key.into(),
                secret: secret.into(),
            }),
            trace,
            tasks: Default::default(),
            cleanup_ok: Arc::new(AtomicBool::new(true)),
        })
    }
    /// Drain after SDK dial tasks and the actor have closed, including stale
    /// replies that were rejected after a successful API response.
    pub async fn drain_cleanup(&self) -> bool {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(6);
        loop {
            let Some(tasks) = self
                .tasks
                .lock()
                .ok()
                .map(|mut tasks| std::mem::take(&mut *tasks))
            else {
                return false;
            };
            if tasks.is_empty() {
                return self.cleanup_ok.load(Ordering::SeqCst);
            }
            let mut complete = true;
            for mut task in tasks {
                if tokio::time::timeout_at(deadline, &mut task).await.is_err() {
                    task.abort();
                    let _ = task.await;
                    complete = false;
                }
            }
            if !complete {
                return false;
            }
        }
    }
    pub async fn connect(
        &self,
        room: &str,
        session: &str,
        scope: Option<&VerifiedSession>,
    ) -> Result<Connection, Value> {
        self.connect_inner(room, session, scope, NoDialNotice(None))
            .await
    }
    pub fn connect_notified<'a>(
        &'a self,
        room: &'a str,
        session: &'a str,
        scope: Option<&'a VerifiedSession>,
        notice: tokio::sync::oneshot::Sender<bool>,
    ) -> impl std::future::Future<Output = Result<Connection, Value>> + Send + 'a {
        // This guard exists before the future is first polled. A biased caller
        // cancellation cannot turn an unstarted request into false uncertainty.
        self.connect_inner(room, session, scope, NoDialNotice(Some(notice)))
    }
    async fn connect_inner(
        &self,
        room: &str,
        session: &str,
        scope: Option<&VerifiedSession>,
        mut no_dial: NoDialNotice,
    ) -> Result<Connection, Value> {
        let cfg = &self.config;
        if scope.is_some_and(|scope| scope.room() != room) {
            return Err(
                json!({"status":"failed","error":"bridge_scope_mismatch","cleanup_confirmed":true}),
            );
        }
        let target = scope
            .and_then(|s| s.claims()["transfer_target_number"].as_str())
            .filter(|s| !s.trim().is_empty())
            .unwrap_or(&cfg.target_number)
            .trim();
        let from = scope
            .and_then(|s| s.claims()["transfer_from_number"].as_str())
            .filter(|s| !s.trim().is_empty())
            .unwrap_or(&cfg.from_number)
            .trim();
        let caller = if cfg.preserve_caller_id {
            scope
                .filter(|s| {
                    s.claims()["entry_surface"] == "aicc_inbound"
                        && s.claims()["sip_caller_verified"] == true
                        && s.room() == room
                })
                .and_then(|s| s.claims()["caller_phone"].as_str())
                .map(super::sip::phone)
                .unwrap_or_default()
        } else {
            from.into()
        };
        let fail = |error: &str| json!({"status":"failed","error":error,"cleanup_confirmed":true});
        if cfg.preserve_caller_id && caller.is_empty() {
            return Err(fail("verified_sip_caller_required"));
        }
        if room.is_empty()
            || cfg.outbound_trunk_id.trim().is_empty()
            || target.is_empty()
            || caller.is_empty()
            || session.is_empty()
            || session.len() > 128
            || !session
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"-_".contains(&c))
        {
            return Err(fail("aicc_destination_not_configured"));
        }
        // A cancelled request's delayed removal cannot delete a later retry's leg.
        // The canonical session remains unchanged in X-Odion-Session-ID.
        let identity = format!("aicc_bridge_{session}_{}", uuid::Uuid::new_v4().simple());
        let mut connection = Connection {
            receipt: Value::Null,
            cleanup: Some(Cleanup {
                credentials: self.credentials.clone(),
                room: room.into(),
                identity: identity.clone(),
                tasks: self.tasks.clone(),
                cleanup_ok: self.cleanup_ok.clone(),
                notifier: no_dial.0.take(),
                trace: self.trace.clone(),
            }),
        };
        let sip = SIPClient::with_api_key(
            &self.credentials.url,
            &self.credentials.key,
            &self.credentials.secret,
        )
        .with_failover(false)
        .with_request_timeout(Duration::from_secs(50));
        let options = CreateSIPParticipantOptions {
            participant_identity: identity.clone(),
            participant_name: Some("AICC colleague".into()),
            participant_attributes: Some(HashMap::from([(
                "call_role".into(),
                "aicc_bridge".into(),
            )])),
            sip_number: Some(caller),
            wait_until_answered: Some(true),
            play_dialtone: Some(true),
            ringing_timeout: Some(Duration::from_secs(40)),
            timeout: Some(Duration::from_secs(50)),
            headers: Some(HashMap::from([(
                "X-Odion-Session-ID".into(),
                session.into(),
            )])),
            ..Default::default()
        };
        let outcome = tokio::time::timeout(
            Duration::from_secs(50),
            sip.create_sip_participant(
                cfg.outbound_trunk_id.clone(),
                target.into(),
                room.into(),
                options,
                None,
            ),
        )
        .await;
        match outcome {
            Ok(Ok(info)) => {
                if !info.participant_identity.is_empty() && info.participant_identity != identity
                    || !info.room_name.is_empty() && info.room_name != room
                {
                    return Err(connection.reject(fail("bridge_scope_mismatch")).await);
                }
                connection.receipt = json!({"status":"connected","participant_identity":identity,"sip_call_id":info.sip_call_id,"target_number":target});
                self.trace.emit("aicc_bridge_answered", 0, 0, json!({}));
                Ok(connection)
            }
            Ok(Err(error)) => {
                let mut failure = fail("bridge_request_failed");
                if let Some(code) = livekit_api::services::SipCallError::from_error(&error)
                    .and_then(|error| error.sip_status_code())
                    .filter(|code| (100..=699).contains(code))
                {
                    failure["sip_status_code"] = json!(code.to_string());
                }
                Err(connection.reject(failure).await)
            }
            Err(_) => Err(connection.reject(fail("bridge_request_timeout")).await),
        }
    }
}
#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use axum::{
        body::Bytes,
        extract::State,
        http::{HeaderMap, StatusCode, Uri},
        response::{IntoResponse, Response},
        Router,
    };
    use livekit_protocol as proto;
    use prost::Message;
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Mutex,
    };
    #[test]
    fn saved_transfer_aliases_are_supported_without_accepting_arbitrary_routes() {
        use super::super::profiles::AgentContext;
        for url in ["builtin://transfer_to_aicc", "internal://transfer-to-aicc"] {
            let mut context = AgentContext {
                loaded: true,
                tools: vec![json!({"name":TOOL,"url":url})],
                ..Default::default()
            };
            assert!(enabled(&context), "{url}");
            context.tools[0]["is_active"] = json!(false);
            assert!(!enabled(&context));
        }
        for url in [
            "https://example.test/transfer",
            "internal://transfer-to-aicc/other",
            "internal://transfer-to-aicc?destination=other",
            "builtin://other",
        ] {
            let context = AgentContext {
                loaded: true,
                tools: vec![json!({"name":TOOL,"url":url})],
                ..Default::default()
            };
            assert!(!enabled(&context), "{url}");
        }
    }
    #[derive(Default)]
    pub(crate) struct Fake {
        pub(crate) mode: AtomicUsize,
        pub(crate) requests: Mutex<Vec<proto::CreateSipParticipantRequest>>,
        pub(crate) removed: Mutex<Vec<String>>,
        pub(crate) entered: tokio::sync::Notify,
        pub(crate) release: tokio::sync::Notify,
    }
    async fn api(
        State(fake): State<Arc<Fake>>,
        headers: HeaderMap,
        uri: Uri,
        body: Bytes,
    ) -> Response {
        let claims = livekit_api::access_token::TokenVerifier::with_api_key(
            "public-bridge-key",
            "public-bridge-secret",
        )
        .verify(
            headers["authorization"]
                .to_str()
                .unwrap()
                .strip_prefix("Bearer ")
                .unwrap(),
        )
        .unwrap();
        if uri.path().ends_with("CreateSIPParticipant") {
            assert!(claims.sip.call);
            let request = proto::CreateSipParticipantRequest::decode(body).unwrap();
            assert_eq!(request.sip_trunk_id, "outbound-fixture");
            assert_eq!(request.sip_call_to, "+233300000000");
            assert!(request.wait_until_answered);
            assert!(request.play_dialtone);
            assert_eq!(request.ringing_timeout.as_ref().unwrap().seconds, 40);
            assert_eq!(request.headers["X-Odion-Session-ID"], "session-a");
            assert_eq!(request.participant_attributes["call_role"], "aicc_bridge");
            let identity = request.participant_identity.clone();
            assert!(identity.starts_with("aicc_bridge_session-a_"));
            fake.requests.lock().unwrap().push(request);
            fake.entered.notify_one();
            let mode = fake.mode.load(Ordering::SeqCst);
            if mode == 1 {
                fake.release.notified().await;
            }
            if mode == 2 || mode == 4 {
                return StatusCode::BAD_GATEWAY.into_response();
            }
            if mode == 3 {
                return (StatusCode::BAD_GATEWAY,axum::Json(json!({"code":"unavailable","msg":"busy fixture","meta":{"sip_status_code":"486","private":"never-store"}}))).into_response();
            }
            let info = proto::SipParticipantInfo {
                participant_identity: identity,
                room_name: "room-a".into(),
                sip_call_id: "sip-fixture".into(),
                ..Default::default()
            };
            (
                [("content-type", "application/protobuf")],
                info.encode_to_vec(),
            )
                .into_response()
        } else {
            assert!(claims.video.room_admin);
            assert_eq!(claims.video.room, "room-a");
            let request = proto::RoomParticipantIdentity::decode(body).unwrap();
            assert_eq!(request.room, "room-a");
            fake.removed.lock().unwrap().push(request.identity);
            if fake.mode.load(Ordering::SeqCst) == 4 {
                return StatusCode::SERVICE_UNAVAILABLE.into_response();
            }
            ([("content-type", "application/protobuf")], Vec::<u8>::new()).into_response()
        }
    }
    pub(crate) async fn setup() -> (Client, Arc<Fake>, tokio::task::JoinHandle<()>) {
        let fake = Arc::new(Fake::default());
        let socket = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let client = Client::new(
            Config {
                outbound_trunk_id: "outbound-fixture".into(),
                target_number: "+233300000000".into(),
                from_number: "+233399999999".into(),
                preserve_caller_id: false,
            },
            &format!("http://{}", socket.local_addr().unwrap()),
            "public-bridge-key",
            "public-bridge-secret",
            Trace::default(),
        )
        .unwrap();
        let app = Router::new().fallback(api).with_state(fake.clone());
        let server = tokio::spawn(async move {
            axum::serve(socket, app).await.unwrap();
        });
        (client, fake, server)
    }
    async fn removed(fake: &Fake, count: usize) {
        tokio::time::timeout(Duration::from_secs(2), async {
            while fake.removed.lock().unwrap().len() < count {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
    }
    #[test]
    fn tool_schema_and_policy_match_current_python_and_final_word_changes_do_not_reuse() {
        let fixture: Value =
            serde_json::from_str(include_str!("../../fixtures/python-parity-reference.json"))
                .unwrap();
        assert_eq!(RULES, fixture["transfer_rules"]);
        assert_eq!(
            vec![definition()],
            fixture["transfer_definition"].as_array().unwrap().clone()
        );
        assert!(final_matches(
            "Please connect me to a human.",
            "please connect me to a human"
        ));
        assert!(!final_matches(
            "Please do connect me to a human.",
            "Please don't connect me to a human."
        ));
        assert!(!final_matches("má ṣe e", "ma se e"));
    }
    #[tokio::test]
    async fn unpolled_dial_cancellation_confirms_that_no_request_started() {
        let (client, fake, server) = setup().await;
        let (done, rx) = tokio::sync::oneshot::channel();
        let future = client.connect_notified("room-a", "session-a", None, done);
        drop(future);
        assert!(rx.await.unwrap());
        assert!(fake.requests.lock().unwrap().is_empty());
        server.abort();
    }
    #[tokio::test]
    async fn answered_bridge_is_accepted_once_and_dropped_late_replies_are_removed() {
        let (client, fake, server) = setup().await;
        let connection = client.connect("room-a", "session-a", None).await.unwrap();
        assert_eq!(connection.receipt()["status"], "connected");
        let established = connection.commit();
        let receipt = established.receipt().clone();
        assert_eq!(receipt["sip_call_id"], "sip-fixture");
        assert!(fake.removed.lock().unwrap().is_empty());
        let stale = client.connect("room-a", "session-a", None).await.unwrap();
        let stale_id = stale.receipt()["participant_identity"]
            .as_str()
            .unwrap()
            .to_string();
        assert_ne!(stale_id, receipt["participant_identity"]);
        drop(stale);
        assert!(client.drain_cleanup().await);
        removed(&fake, 1).await;
        assert_eq!(fake.removed.lock().unwrap()[0], stale_id);
        drop(established);
        assert!(client.drain_cleanup().await);
        assert_eq!(
            fake.removed.lock().unwrap()[1],
            receipt["participant_identity"]
        );
        server.abort();
    }
    #[tokio::test]
    async fn cancelled_or_failed_dial_removes_only_its_own_leg_without_retry() {
        for mode in [1, 2, 3] {
            let (client, fake, server) = setup().await;
            fake.mode.store(mode, Ordering::SeqCst);
            let task =
                tokio::spawn(async move { client.connect("room-a", "session-a", None).await });
            fake.entered.notified().await;
            if mode == 1 {
                task.abort();
                let _ = task.await;
            } else {
                let error = task.await.unwrap().err().unwrap();
                assert_eq!(error["status"], "failed");
                assert_eq!(error["cleanup_confirmed"], true);
                if mode == 3 {
                    assert_eq!(error["sip_status_code"], "486");
                    assert!(error["private"].is_null());
                }
            }
            removed(&fake, 1).await;
            assert_eq!(fake.requests.lock().unwrap().len(), 1);
            assert_eq!(
                fake.removed.lock().unwrap()[0],
                fake.requests.lock().unwrap()[0].participant_identity
            );
            server.abort();
        }
    }
    #[tokio::test]
    async fn unconfirmed_cleanup_reports_an_unknown_outcome_without_redial() {
        let (client, fake, server) = setup().await;
        fake.mode.store(4, Ordering::SeqCst);
        let error = client
            .connect("room-a", "session-a", None)
            .await
            .err()
            .unwrap();
        assert_eq!(error["status"], "unknown");
        assert_eq!(error["cleanup_confirmed"], false);
        assert_eq!(fake.requests.lock().unwrap().len(), 1);
        assert!(!client.drain_cleanup().await);
        server.abort();
    }
    #[tokio::test]
    async fn preserving_caller_id_requires_verified_sip_scope_and_matching_room() {
        let (mut client, fake, server) = setup().await;
        client.config.preserve_caller_id = true;
        let browser = super::super::identity::test_session(json!({"caller_phone":"08012345678"}));
        assert!(client
            .connect("room-a", "session-a", Some(&browser))
            .await
            .is_err());
        let sip = super::super::identity::test_session(
            json!({"entry_surface":"aicc_inbound","sip_caller_verified":true,"caller_phone":"08012345678"}),
        );
        assert!(client
            .connect("other-room", "session-a", Some(&sip))
            .await
            .is_err());
        let connection = client
            .connect("room-a", "session-a", Some(&sip))
            .await
            .unwrap();
        assert_eq!(
            fake.requests.lock().unwrap()[0].sip_number,
            "+2348012345678"
        );
        connection.commit().detach();
        assert_eq!(fake.requests.lock().unwrap().len(), 1);
        server.abort();
    }
}
