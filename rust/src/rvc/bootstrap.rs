//! Explicit operator setup, separate from the fixed loopback harness. No raw
//! caller metadata can construct SIP authority or select infrastructure keys.
use super::{
    capacity, outbound,
    platform::{self, PreparedCall, Services},
    sip, Config, Trace,
};
use crate::worker::ProductionConfig;
use livekit::prelude::{RemoteParticipant, Room};
use serde::Deserialize;
use std::collections::HashMap;

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OperatorConfig {
    pub worker: ProductionConfig,
    pub voice: Config,
    #[serde(default)]
    pub sip: Option<sip::Config>,
    #[serde(default)]
    pub capacity: capacity::Config,
}
impl OperatorConfig {
    pub fn validate(&self) -> Result<(), String> {
        self.worker.validate()?;
        let mut voice = self.voice.clone();
        let context = voice.agent_context.clone();
        super::languages::apply(&mut voice, &context, &serde_json::Value::Null)?;
        voice.validate()?;
        self.capacity.validate()?;
        let platform = self
            .voice
            .platform
            .as_ref()
            .ok_or("platform services required")?;
        platform.validate()?;
        if platform.agent.base_url.is_empty() || platform.agent.token.is_empty() {
            return Err("scoped agent service and signing key required".into());
        }
        if let Some(sip) = &self.sip {
            sip.validate()?;
        }
        Ok(())
    }
    /// Admission checks syntax/scope only; the signature is consumed once by
    /// prepare_call, and SIP identity is established from the actual SDK caller.
    pub fn accepts(&self, metadata: &str, room: &str) -> bool {
        if metadata.len() > 20 * 1024 || room.is_empty() || room.len() > 512 {
            return false;
        }
        let Ok(data) = serde_json::from_str::<serde_json::Value>(metadata) else {
            return false;
        };
        if data["entry_surface"] == "aicc_inbound" {
            return self
                .sip
                .as_ref()
                .is_some_and(|sip| matches!(sip.dispatch(metadata, room), Ok(Some(_))));
        }
        data["session_token"].is_string()
    }
    pub fn sip_dispatch(
        &self,
        metadata: &str,
        room: &str,
    ) -> Result<Option<sip::Dispatch>, String> {
        match &self.sip {
            Some(config) => config.dispatch(metadata, room),
            None => Ok(None),
        }
    }
    pub async fn prepare_sip(
        &self,
        dispatch: &sip::Dispatch,
        room: &Room,
        participant: &RemoteParticipant,
        trace: Trace,
    ) -> Result<SipCall, String> {
        if !dispatch.accepts(participant) {
            return Err("unverified SIP caller".into());
        }
        let platform = self
            .voice
            .platform
            .clone()
            .ok_or("platform services required")?;
        let headers = dispatch
            .remote_headers(&room.local_participant(), participant)
            .await;
        let called = dispatch.called(participant, &headers);
        let dashboard = sip::dashboard_route(&platform.agent, &called).await;
        let scope = dispatch.scope(participant, &headers, dashboard.as_ref())?;
        let services = Services::new(platform.clone(), scope, trace.clone())?;
        self.prepare_sip_services(services, headers, &room.name(), trace)
            .await
    }
    async fn prepare_sip_services(
        &self,
        mut services: Services,
        headers: HashMap<String, String>,
        room: &str,
        trace: Trace,
    ) -> Result<SipCall, String> {
        // Both guards install cancellation ownership before their HTTP waits.
        let capacity = capacity::Lease::claim(
            &self.capacity,
            services.clone(),
            &headers,
            room,
            trace.clone(),
        )
        .await?;
        let outbound = if services.scope().claims()["outbound_required"] == true {
            let bound = outbound::Bound::claim(services.clone(), trace.clone()).await?;
            services = Services::new(services.config().clone(), bound.scope()?, trace)?;
            Some(bound)
        } else {
            None
        };
        let mut context = services.load_agent().await?;
        if let Some(bound) = &outbound {
            context = bound.apply(context)?;
        }
        let prepared = platform::resolve_call(self.voice.clone(), services, context, room)?;
        Ok(SipCall {
            prepared,
            capacity,
            outbound,
        })
    }
}
pub struct SipCall {
    pub prepared: PreparedCall,
    pub capacity: Option<capacity::Lease>,
    pub outbound: Option<outbound::Bound>,
}

/// Cover early bootstrap/publish failures and cancellation as well as normal
/// shutdown. Closing the SDK is bounded, and never deletes a room or human leg.
pub struct RoomGuard(Option<std::sync::Arc<Room>>);
impl RoomGuard {
    pub fn new(room: std::sync::Arc<Room>) -> Self {
        Self(Some(room))
    }
    pub async fn close(&mut self) {
        if let Some(room) = self.0.take() {
            let _ = tokio::time::timeout(std::time::Duration::from_secs(5), room.close()).await;
        }
    }
}
impl Drop for RoomGuard {
    fn drop(&mut self) {
        if let (Some(room), Ok(runtime)) = (self.0.take(), tokio::runtime::Handle::try_current()) {
            runtime.spawn(async move {
                let _ = tokio::time::timeout(std::time::Duration::from_secs(5), room.close()).await;
            });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::super::{
        identity::VerifiedSession,
        platform::{Endpoint, PlatformConfig},
    };
    use super::*;
    use axum::{
        extract::State,
        http::{HeaderMap, Uri},
        Json, Router,
    };
    use serde_json::{json, Value};
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    };
    fn operator() -> OperatorConfig {
        let mut voice: Config =
            serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
        voice.platform = Some(PlatformConfig {
            agent: Endpoint {
                base_url: "http://127.0.0.1:1".into(),
                token: "fixture-key".into(),
                ..Default::default()
            },
            ..Default::default()
        });
        OperatorConfig {
            worker: ProductionConfig {
                server_url: "ws://127.0.0.1:17880".into(),
                api_key: "fixture".into(),
                api_secret: "fixture".into(),
                agent_name: "platform-review".into(),
                ..Default::default()
            },
            voice,
            sip: Some(sip::Config {
                trunk_ids: vec!["inbound-trunk".into()],
                room_prefix: "inbound-".into(),
                business_id: "business-a".into(),
                config_agent_id: "agent-a".into(),
                ..Default::default()
            }),
            capacity: capacity::Config {
                business_id: "business-a".into(),
                ..Default::default()
            },
        }
    }
    #[test]
    fn explicit_operator_mode_rejects_synthetic_and_unpinned_sip_admission() {
        let mut op = operator();
        op.validate().unwrap();
        assert!(!op.accepts("native-rvc-one-call", "sg-rvc-review"));
        assert!(op.accepts(
            r#"{"session_token":"unverified-until-bootstrap"}"#,
            "signed-room"
        ));
        assert!(op.accepts(
            r#"{"entry_surface":"aicc_inbound","sip_trunk_id":"inbound-trunk"}"#,
            "inbound-call"
        ));
        assert!(!op.accepts(r#"{"entry_surface":"aicc_inbound","sip_trunk_id":"other-trunk","session_token":"cannot-bypass-sip-pin"}"#,"inbound-call"));
        assert!(!op.accepts(
            r#"{"entry_surface":"aicc_inbound","sip_trunk_id":"inbound-trunk"}"#,
            "unrelated-room"
        ));
        op.sip = None;
        assert!(!op.accepts(
            r#"{"entry_surface":"aicc_inbound","sip_trunk_id":"inbound-trunk"}"#,
            "inbound-call"
        ));
        op.voice.platform = None;
        assert!(op.validate().is_err());
    }
    #[derive(Default)]
    struct Fake {
        requests: Mutex<Vec<(String, Value)>>,
        bad: AtomicBool,
        wrong_recipient: AtomicBool,
    }
    async fn api(
        State(fake): State<Arc<Fake>>,
        headers: HeaderMap,
        uri: Uri,
        body: axum::body::Bytes,
    ) -> Json<Value> {
        assert_eq!(headers["x-business-id"], "business-a");
        assert_eq!(headers["x-service-token"], "fixture-key");
        let body = serde_json::from_slice::<Value>(&body).unwrap_or(Value::Null);
        fake.requests
            .lock()
            .unwrap()
            .push((uri.path().into(), body));
        if uri.path().ends_with("runtime-config") {
            return Json(
                json!({"business_id":"business-a","agent_id":"agent-a","name":"Fixture agent","instructions":"Saved instructions.","outbound_enabled":true,"tts_voice_id":"access-ghana-cindy","tools":if fake.bad.load(Ordering::SeqCst){json!([{"name":"directory_not_ported","url":"builtin://directory"}])}else{json!([{"name":"end_call","url":"builtin://end_call"},{"name":"transfer_to_aicc","url":"builtin://transfer_to_aicc"}])}}),
            );
        }
        if uri.path().ends_with("claim-by-recipient") {
            return Json(
                json!({"call_ref":"prepared-a","business_id":"business-a","agent_id":"agent-a","recipient_phone":if fake.wrong_recipient.load(Ordering::SeqCst){"+2348012345679"}else{"+2348012345678"},"recipient_name":"Ada","additional_prompt":"Discuss the scheduled review.","context":{"appointment":"tomorrow"}}),
            );
        }
        Json(json!({"status":"ok"}))
    }
    fn scope() -> VerifiedSession {
        VerifiedSession::from_pinned_sip(json!({"entry_surface":"aicc_inbound","sip_caller_verified":true,"outbound_required":true,"business_id":"business-a","config_agent_id":"agent-a","end_user_id":"+2348012345678","caller_number":"+2348012345678","participant_identity":"sip-caller","room_name":"inbound-call"})).unwrap()
    }
    #[tokio::test]
    async fn sip_bootstrap_orders_capacity_outbound_profile_and_one_use_finalization() {
        let fake = Arc::new(Fake::default());
        let app = Router::new().fallback(api).with_state(fake.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async { axum::serve(listener, app).await.unwrap() });
        let mut op = operator();
        let endpoint = Endpoint {
            base_url: url,
            token: "fixture-key".into(),
            timeout_seconds: 1.0,
        };
        op.voice.platform = Some(PlatformConfig {
            agent: endpoint.clone(),
            conversation: endpoint.clone(),
            billing: endpoint,
            ..Default::default()
        });
        let services = Services::new(
            op.voice.platform.clone().unwrap(),
            scope(),
            Trace::default(),
        )
        .unwrap();
        let mut call = op
            .prepare_sip_services(
                services,
                HashMap::from([("x-odion-capacity-reservation".into(), "reserved-a".into())]),
                "inbound-call",
                Trace::default(),
            )
            .await
            .unwrap();
        assert_eq!(call.prepared.caller, "sip-caller");
        assert_eq!(call.prepared.config.transport, "sip");
        assert!(call.prepared.config.explicitly_routed_agent);
        assert!(call.prepared.config.base_system_prompt.contains("Ada"));
        assert!(call
            .prepared
            .config
            .base_system_prompt
            .contains("appointment"));
        assert_eq!(
            call.prepared.config.agent_context.end_user_id,
            "+2348012345678"
        );
        call.capacity.as_mut().unwrap().release().await;
        call.capacity.as_mut().unwrap().release().await;
        call.outbound
            .as_mut()
            .unwrap()
            .finish("completed", None, Some("conversation-a"))
            .await;
        call.outbound
            .as_mut()
            .unwrap()
            .finish("failed", None, None)
            .await;
        drop(call);
        let requests = fake.requests.lock().unwrap();
        assert!(requests[0].0.ends_with("/claim"));
        assert!(requests[1].0.ends_with("claim-by-recipient"));
        assert!(requests[2].0.ends_with("runtime-config"));
        assert_eq!(
            requests
                .iter()
                .filter(|r| r.0.ends_with("/release"))
                .count(),
            1
        );
        let finish = requests
            .iter()
            .filter(|r| r.0.ends_with("/finish"))
            .collect::<Vec<_>>();
        assert_eq!(finish.len(), 1);
        assert_eq!(finish[0].1["conversation_id"], "conversation-a");
        assert_eq!(finish[0].1["status"], "completed");
        drop(requests);
        server.abort();
    }
    #[tokio::test]
    async fn unsupported_profile_and_mismatched_outbound_release_owned_resources() {
        for wrong_recipient in [false, true] {
            let fake = Arc::new(Fake::default());
            fake.bad.store(true, Ordering::SeqCst);
            fake.wrong_recipient
                .store(wrong_recipient, Ordering::SeqCst);
            let app = Router::new().fallback(api).with_state(fake.clone());
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let url = format!("http://{}", listener.local_addr().unwrap());
            let server = tokio::spawn(async { axum::serve(listener, app).await.unwrap() });
            let mut op = operator();
            let endpoint = Endpoint {
                base_url: url,
                token: "fixture-key".into(),
                timeout_seconds: 1.0,
            };
            op.voice.platform = Some(PlatformConfig {
                agent: endpoint.clone(),
                conversation: endpoint,
                ..Default::default()
            });
            let services = Services::new(
                op.voice.platform.clone().unwrap(),
                scope(),
                Trace::default(),
            )
            .unwrap();
            assert!(op
                .prepare_sip_services(
                    services,
                    HashMap::from([("x-odion-capacity-reservation".into(), "reserved-a".into())]),
                    "inbound-call",
                    Trace::default()
                )
                .await
                .is_err());
            tokio::time::timeout(std::time::Duration::from_secs(2), async {
                loop {
                    if fake
                        .requests
                        .lock()
                        .unwrap()
                        .iter()
                        .any(|r| r.0.ends_with("/release"))
                    {
                        break;
                    }
                    tokio::task::yield_now().await;
                }
            })
            .await
            .unwrap();
            let requests = fake.requests.lock().unwrap();
            assert_eq!(
                requests.iter().filter(|r| r.0.ends_with("/finish")).count(),
                1
            );
            assert_eq!(
                requests
                    .iter()
                    .find(|r| r.0.ends_with("/finish"))
                    .unwrap()
                    .1["status"],
                "failed"
            );
            if wrong_recipient {
                assert!(!requests.iter().any(|r| r.0.ends_with("runtime-config")));
            }
            drop(requests);
            server.abort();
        }
    }
}
