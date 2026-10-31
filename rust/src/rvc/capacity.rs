//! SIP capacity reservations are pinned by the operator and verified call scope.
//! Claim/heartbeat/release run outside the conversation/audio actor.
use super::{platform::Services, Trace};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::{collections::HashMap, sync::Mutex, time::Duration};
use tokio::{sync::watch, task::JoinHandle};
#[derive(Clone, Deserialize, Serialize)]
#[serde(default)]
pub struct Config {
    pub business_id: String,
    pub heartbeat_seconds: f64,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            business_id: String::new(),
            heartbeat_seconds: 45.0,
        }
    }
}
impl Config {
    pub fn validate(&self) -> Result<(), String> {
        if !self.heartbeat_seconds.is_finite()
            || self.heartbeat_seconds < 10.0
            || self.heartbeat_seconds > 3600.0
        {
            Err("invalid capacity heartbeat interval".into())
        } else {
            Ok(())
        }
    }
    pub fn reservation(
        &self,
        business: &str,
        headers: &HashMap<String, String>,
    ) -> Result<Option<String>, String> {
        if self.business_id.trim().is_empty() || business.trim() != self.business_id.trim() {
            return Ok(None);
        }
        let id = headers
            .get("x-odion-capacity-reservation")
            .map(|s| s.trim())
            .filter(|s| {
                !s.is_empty()
                    && s.len() <= 1024
                    && !matches!(*s, "." | "..")
                    && !s.chars().any(char::is_control)
            })
            .ok_or("SIP call is missing its capacity reservation")?;
        Ok(Some(id.into()))
    }
}
pub struct Lease {
    services: Services,
    reservation: Mutex<Option<String>>,
    room: String,
    stop: watch::Sender<bool>,
    task: Option<JoinHandle<()>>,
    trace: Trace,
}
async fn post(services: &Services, id: &str, action: &str, room: &str) -> Result<(), String> {
    let body = tokio::time::timeout(
        Duration::from_secs(10),
        services.request(
            &services.config().conversation,
            reqwest::Method::POST,
            &[
                "v1",
                "internal",
                "aicc",
                "capacity",
                "reservations",
                id,
                action,
            ],
            Some(json!({"room_name":room})),
        ),
    )
    .await
    .map_err(|_| "capacity request timed out")??;
    if !body.is_object()
        || matches!(
            body["status"].as_str(),
            Some("failed" | "error" | "denied" | "expired")
        )
    {
        Err("capacity reservation rejected".into())
    } else {
        Ok(())
    }
}
impl Lease {
    /// Caller must already be classified as SIP by LiveKit on the pinned trunk;
    /// editable participant metadata and unverified headers cannot authorize leases.
    pub async fn claim(
        config: &Config,
        services: Services,
        headers: &HashMap<String, String>,
        room: &str,
        trace: Trace,
    ) -> Result<Option<Self>, String> {
        config.validate()?;
        if config.business_id.trim().is_empty()
            || config.business_id.trim() != services.scope().business_id().trim()
        {
            return Ok(None);
        }
        if services.scope().claims()["entry_surface"] != "aicc_inbound"
            || services.scope().claims()["sip_caller_verified"] != true
            || services.scope().room() != room
            || room.is_empty()
        {
            return Err("capacity requires verified SIP scope".into());
        }
        // Capacity authenticates this specific worker role, independently of
        // the configurable client name used for agent/knowledge requests.
        let mut api_config = services.config().clone();
        api_config.client_id = "sales-girl-voice-agent".into();
        let services = Services::new(api_config, services.scope().clone(), trace.clone())?;
        // The current AICC contract carries the verified SIP From number,
        // rather than requiring a custom header to survive the carrier.
        if headers
            .get("x-odion-capacity-reservation")
            .is_none_or(|id| id.trim().is_empty())
        {
            let caller = super::sip::phone(
                services.scope().claims()["caller_number"]
                    .as_str()
                    .unwrap_or(""),
            );
            if caller.chars().filter(char::is_ascii_digit).count() < 7 {
                return Err("capacity requires verified SIP caller number".into());
            }
            let (tx, rx) = tokio::sync::oneshot::channel();
            let services = services.clone();
            let room = room.to_string();
            let config = config.clone();
            let trace = trace.clone();
            // Finish an already submitted claim even if bootstrap is cancelled.
            // A late acquired lease is dropped/released if the owner has left.
            tokio::spawn(async move {
                let result = async {
                    let body = services
                        .request(
                            &services.config().conversation,
                            reqwest::Method::POST,
                            &[
                                "v1",
                                "internal",
                                "aicc",
                                "capacity",
                                "reservations",
                                "claim-by-caller",
                            ],
                            Some(json!({"caller_number":caller,"room_name":room})),
                        )
                        .await?;
                    if body["status"] != "active" {
                        return Err("capacity caller claim was not active".to_string());
                    }
                    let id = body["reservation_id"]
                        .as_str()
                        .filter(|s| {
                            !s.is_empty()
                                && s.len() <= 1024
                                && !matches!(*s, "." | "..")
                                && !s.chars().any(char::is_control)
                        })
                        .ok_or("capacity caller claim returned no valid reservation")?
                        .to_string();
                    let mut lease = Self::owned(services, id, room, trace);
                    lease.start_heartbeat(config.heartbeat_seconds);
                    lease.trace.emit(
                        "capacity_claimed",
                        0,
                        0,
                        json!({"source":"verified_sip_caller"}),
                    );
                    Ok(Some(lease))
                }
                .await;
                let _ = tx.send(result);
            });
            return rx.await.map_err(|_| "capacity claim task unavailable")?;
        }
        let id = config
            .reservation(services.scope().business_id(), headers)?
            .ok_or("capacity reservation missing")?;
        let mut lease = Self::owned(services.clone(), id.clone(), room.into(), trace.clone());
        post(&services, &id, "claim", room).await?;
        trace.emit("capacity_claimed", 0, 0, json!({"source":"sip_header"}));
        lease.start_heartbeat(config.heartbeat_seconds);
        Ok(Some(lease))
    }
    fn owned(services: Services, id: String, room: String, trace: Trace) -> Self {
        let (stop, _) = watch::channel(false);
        Self {
            services,
            reservation: Mutex::new(Some(id)),
            room,
            stop,
            task: None,
            trace,
        }
    }
    fn start_heartbeat(&mut self, seconds: f64) {
        let services = self.services.clone();
        let id = self
            .reservation
            .lock()
            .ok()
            .and_then(|id| id.clone())
            .unwrap_or_default();
        let room = self.room.clone();
        let trace = self.trace.clone();
        let mut rx = self.stop.subscribe();
        let interval = Duration::from_secs_f64(seconds);
        self.task = Some(tokio::spawn(async move {
            let mut timer = tokio::time::interval(interval);
            timer.tick().await;
            loop {
                tokio::select! {biased;_=rx.changed()=>return,_=timer.tick()=>{let result=post(&services,&id,"heartbeat",&room).await;trace.emit(if result.is_ok(){"capacity_heartbeat"}else{"capacity_heartbeat_failed"},0,0,json!({}));}}
            }
        }));
    }
    pub async fn release(&mut self) {
        let _ = self.stop.send(true);
        if let Some(task) = self.task.take() {
            task.abort();
            let _ = task.await;
        }
        let id = self.reservation.lock().ok().and_then(|mut id| id.take());
        if let Some(id) = id {
            // Root the release request before awaiting it, so cancellation of
            // the adapter's shutdown cannot cancel the only release attempt.
            let services = self.services.clone();
            let room = self.room.clone();
            let trace = self.trace.clone();
            let task = tokio::spawn(async move {
                let result = post(&services, &id, "release", &room).await;
                trace.emit(
                    if result.is_ok() {
                        "capacity_released"
                    } else {
                        "capacity_release_failed"
                    },
                    0,
                    0,
                    json!({}),
                );
            });
            let _ = task.await;
        }
    }
}
impl Drop for Lease {
    fn drop(&mut self) {
        let _ = self.stop.send(true);
        if let Some(task) = self.task.take() {
            task.abort();
        }
        let id = self.reservation.get_mut().ok().and_then(Option::take);
        if let (Some(id), Ok(runtime)) = (id, tokio::runtime::Handle::try_current()) {
            let services = self.services.clone();
            let room = self.room.clone();
            let trace = self.trace.clone();
            runtime.spawn(async move {
                let result = post(&services, &id, "release", &room).await;
                trace.emit(
                    if result.is_ok() {
                        "capacity_released"
                    } else {
                        "capacity_release_failed"
                    },
                    0,
                    0,
                    json!({"reason":"owner_dropped"}),
                );
            });
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        extract::State,
        http::{HeaderMap, StatusCode, Uri},
        response::{IntoResponse, Response},
        Json, Router,
    };
    use serde_json::Value;
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    };
    #[test]
    fn scoped_capacity_gate_matches_independent_python() {
        let cases: Value =
            serde_json::from_str(include_str!("../../fixtures/python-parity-reference.json"))
                .unwrap();
        for case in cases["capacity_gates"].as_array().unwrap() {
            let config = Config {
                business_id: case["configured"].as_str().unwrap().into(),
                ..Default::default()
            };
            let headers = HashMap::from([(
                "x-odion-capacity-reservation".into(),
                case["reservation"].as_str().unwrap().into(),
            )]);
            let result = config.reservation(case["business"].as_str().unwrap(), &headers);
            assert_eq!(result.is_err(), case["error"]);
            if let Ok(value) = result {
                assert_eq!(value.unwrap_or_default(), case["expected"]);
            }
        }
    }
    #[derive(Default)]
    struct Fake {
        requests: Mutex<Vec<String>>,
        hold: AtomicBool,
        entered: tokio::sync::Notify,
        release: tokio::sync::Notify,
        caller_denied: AtomicBool,
    }
    async fn api(
        State(fake): State<Arc<Fake>>,
        headers: HeaderMap,
        uri: Uri,
        Json(body): Json<Value>,
    ) -> Response {
        assert_eq!(headers["x-business-id"], "business-a");
        assert_eq!(headers["x-service-name"], "sales-girl-voice-agent");
        assert_eq!(headers["x-service-token"], "public-capacity-fixture");
        if uri.path().ends_with("/claim-by-caller") {
            assert_eq!(
                body,
                json!({"room_name":"room-a","caller_number":"+2347012345678"})
            );
            fake.requests.lock().unwrap().push("claim-by-caller".into());
            fake.entered.notify_one();
            if fake.hold.load(Ordering::SeqCst) {
                fake.release.notified().await;
            }
            if fake.caller_denied.load(Ordering::SeqCst) {
                return StatusCode::NOT_FOUND.into_response();
            }
            return Json(json!({"reservation_id":"opaque/token","status":"active"}))
                .into_response();
        }
        assert_eq!(body, json!({"room_name":"room-a"}));
        assert!(uri
            .path()
            .starts_with("/v1/internal/aicc/capacity/reservations/opaque%2Ftoken/"));
        let action = uri.path().rsplit('/').next().unwrap().to_string();
        fake.requests.lock().unwrap().push(action.clone());
        if action == "claim" {
            fake.entered.notify_one();
            if fake.hold.load(Ordering::SeqCst) {
                fake.release.notified().await;
                return StatusCode::SERVICE_UNAVAILABLE.into_response();
            }
        }
        Json(json!({"status":"active"})).into_response()
    }
    async fn setup() -> (Services, Arc<Fake>, JoinHandle<()>) {
        let fake = Arc::new(Fake::default());
        let socket = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let services = Services::new(
            super::super::platform::PlatformConfig {
                client_id: "custom-agent-config-client".into(),
                conversation: super::super::platform::Endpoint {
                    base_url: format!("http://{}", socket.local_addr().unwrap()),
                    token: "public-capacity-fixture".into(),
                    timeout_seconds: 1.0,
                },
                ..Default::default()
            },
            super::super::identity::test_session(
                json!({"entry_surface":"aicc_inbound","sip_caller_verified":true}),
            ),
            Trace::default(),
        )
        .unwrap();
        let app = Router::new().fallback(api).with_state(fake.clone());
        let server = tokio::spawn(async move {
            axum::serve(socket, app).await.unwrap();
        });
        (services, fake, server)
    }
    fn config() -> Config {
        Config {
            business_id: "business-a".into(),
            heartbeat_seconds: 10.0,
        }
    }
    fn headers() -> HashMap<String, String> {
        HashMap::from([("x-odion-capacity-reservation".into(), "opaque/token".into())])
    }
    async fn wait_for(fake: &Fake, action: &str) {
        tokio::time::timeout(Duration::from_secs(2), async {
            while !fake
                .requests
                .lock()
                .unwrap()
                .iter()
                .any(|item| item == action)
            {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
    }
    #[tokio::test]
    async fn lease_heartbeats_then_releases_once_without_model_or_audio_work() {
        let (services, fake, server) = setup().await;
        let mut lease = Lease::claim(&config(), services, &headers(), "room-a", Trace::default())
            .await
            .unwrap()
            .unwrap();
        tokio::task::yield_now().await;
        tokio::time::pause();
        tokio::time::advance(Duration::from_secs(10)).await;
        tokio::time::resume();
        wait_for(&fake, "heartbeat").await;
        lease.release().await;
        lease.release().await;
        drop(lease);
        assert_eq!(
            *fake.requests.lock().unwrap(),
            vec!["claim", "heartbeat", "release"]
        );
        server.abort();
    }
    #[tokio::test]
    async fn cancelled_or_failed_claim_keeps_release_owned() {
        for cancel in [false, true] {
            let (services, fake, server) = setup().await;
            fake.hold.store(true, Ordering::SeqCst);
            let task = tokio::spawn(async move {
                Lease::claim(&config(), services, &headers(), "room-a", Trace::default()).await
            });
            fake.entered.notified().await;
            if cancel {
                task.abort();
                let _ = task.await;
            } else {
                fake.release.notify_one();
                assert!(task.await.unwrap().is_err());
            }
            wait_for(&fake, "release").await;
            assert_eq!(*fake.requests.lock().unwrap(), vec!["claim", "release"]);
            server.abort();
        }
    }
    #[tokio::test]
    async fn browser_or_wrong_room_never_claims_a_sip_capacity_reservation() {
        let (services, fake, server) = setup().await;
        assert!(Lease::claim(
            &config(),
            services.clone(),
            &headers(),
            "other-room",
            Trace::default()
        )
        .await
        .is_err());
        let browser = Services::new(
            services.config().clone(),
            super::super::identity::test_session(json!({})),
            Trace::default(),
        )
        .unwrap();
        assert!(
            Lease::claim(&config(), browser, &headers(), "room-a", Trace::default())
                .await
                .is_err()
        );
        assert!(fake.requests.lock().unwrap().is_empty());
        server.abort();
    }
    fn with_caller(services: &Services) -> Services {
        Services::new(services.config().clone(),super::super::identity::test_session(json!({"entry_surface":"aicc_inbound","sip_caller_verified":true,"caller_number":"+2347012345678"})),Trace::default()).unwrap()
    }
    #[tokio::test]
    async fn current_aicc_contract_claims_by_verified_caller_and_releases_once() {
        for header in [
            HashMap::new(),
            HashMap::from([("x-odion-capacity-reservation".into(), " ".into())]),
        ] {
            let (services, fake, server) = setup().await;
            let mut lease = Lease::claim(
                &config(),
                with_caller(&services),
                &header,
                "room-a",
                Trace::default(),
            )
            .await
            .unwrap()
            .unwrap();
            lease.release().await;
            lease.release().await;
            drop(lease);
            assert_eq!(
                *fake.requests.lock().unwrap(),
                vec!["claim-by-caller", "release"]
            );
            server.abort();
        }
    }
    #[tokio::test]
    async fn late_caller_claim_releases_after_bootstrap_cancellation() {
        let (services, fake, server) = setup().await;
        fake.hold.store(true, Ordering::SeqCst);
        let services = with_caller(&services);
        let task = tokio::spawn(async move {
            Lease::claim(
                &config(),
                services,
                &HashMap::new(),
                "room-a",
                Trace::default(),
            )
            .await
        });
        fake.entered.notified().await;
        task.abort();
        let _ = task.await;
        fake.release.notify_one();
        wait_for(&fake, "release").await;
        assert_eq!(
            *fake.requests.lock().unwrap(),
            vec!["claim-by-caller", "release"]
        );
        server.abort();
    }
    #[tokio::test]
    async fn missing_or_unavailable_caller_reservation_never_admits_a_call() {
        let (services, fake, server) = setup().await;
        assert!(Lease::claim(
            &config(),
            services.clone(),
            &HashMap::new(),
            "room-a",
            Trace::default()
        )
        .await
        .is_err());
        assert!(fake.requests.lock().unwrap().is_empty());
        fake.caller_denied.store(true, Ordering::SeqCst);
        assert!(Lease::claim(
            &config(),
            with_caller(&services),
            &HashMap::new(),
            "room-a",
            Trace::default()
        )
        .await
        .is_err());
        assert_eq!(*fake.requests.lock().unwrap(), vec!["claim-by-caller"]);
        server.abort();
    }
}
