//! Local-only cross-service acceptance driver. The embedded worker entrypoint
//! is the exact production implementation; it has no test admission bypass.
#[path = "rvc_worker.rs"]
mod production;
use livekit::prelude::*;
use livekit_api::{
    access_token::{AccessToken, VideoGrants},
    services::{
        agent_dispatch::AgentDispatchClient,
        room::{CreateRoomOptions, RoomClient},
    },
};
use livekit_protocol as proto;
use serde_json::{json, Value};
use std::{
    path::PathBuf,
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};
const BASE: &str = "http://127.0.0.1:17930";
const LK: &str = "http://127.0.0.1:17880";
const BUSINESS: &str = "11111111-2222-3333-4444-555555555555";
const AGENT: &str = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
struct ChildGuard(Child);
impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
fn main() -> Result<(), Box<dyn std::error::Error>> {
    if std::env::args().nth(1).as_deref() == Some("--platform") {
        return production::main();
    }
    tokio::runtime::Runtime::new()?
        .block_on(async { tokio::time::timeout(Duration::from_secs(180), run()).await? })
}
async fn state(http: &reqwest::Client) -> Result<Value, Box<dyn std::error::Error>> {
    Ok(http
        .get(format!("{BASE}/fixture/state"))
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?)
}
async fn reserve(http: &reqwest::Client, i: usize) -> Result<Value, Box<dyn std::error::Error>> {
    Ok(http.post(format!("{BASE}/v1/aicc/capacity/reservations")).header("X-AICC-API-Key","aicc-local-fixture").json(&json!({"agent_id":"agt_capacity_fixture","attempt_id":format!("capacity-{i}"),"caller_number":format!("+23470123{i:05}")})).send().await?.error_for_status()?.json().await?)
}
async fn wait_count(
    http: &reqwest::Client,
    status: &str,
    n: usize,
) -> Result<(), Box<dyn std::error::Error>> {
    tokio::time::timeout(Duration::from_secs(25), async {
        loop {
            let s = state(http).await?;
            if s["rows"]
                .as_array()
                .ok_or("missing rows")?
                .iter()
                .filter(|r| r["status"] == status)
                .count()
                == n
            {
                return Ok::<_, Box<dyn std::error::Error>>(());
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    })
    .await??;
    Ok(())
}
async fn join(
    api: &RoomClient,
    dispatch: &AgentDispatchClient,
    agent: &str,
    i: usize,
) -> Result<Room, Box<dyn std::error::Error>> {
    let name = format!("sg-capacity-local-{}-{i}", std::process::id());
    api.create_room(
        &name,
        CreateRoomOptions {
            empty_timeout: 30,
            ..Default::default()
        },
    )
    .await?;
    let token = AccessToken::with_api_key("devkey", "secret")
        .with_identity(&format!("sip-fixture-{i}"))
        .with_kind("sip")
        .with_attributes([
            (
                "sip.trunkID".to_string(),
                "ST_local_capacity_fixture".to_string(),
            ),
            ("sip.phoneNumber".into(), format!("+23470123{i:05}")),
            ("sip.to".into(), "0301051".into()),
        ])
        .with_grants(VideoGrants {
            room_join: true,
            room: name.clone(),
            ..Default::default()
        })
        .to_jwt()?;
    let (room, mut events) =
        Room::connect("ws://127.0.0.1:17880", &token, RoomOptions::default()).await?;
    room.local_participant()
        .register_rpc_method("lk.sip.GetRemoteHeaders".into(), |_| {
            Box::pin(async { Ok(json!({"headers":{"To":"0301051"}}).to_string()) })
        });
    tokio::spawn(async move { while events.recv().await.is_some() {} });
    dispatch
        .create_dispatch(proto::CreateAgentDispatchRequest {
            room: name,
            agent_name: agent.into(),
            metadata:
                json!({"entry_surface":"aicc_inbound","sip_trunk_id":"ST_local_capacity_fixture"})
                    .to_string(),
            ..Default::default()
        })
        .await?;
    Ok(room)
}
async fn run() -> Result<(), Box<dyn std::error::Error>> {
    let http = reqwest::Client::new();
    let initial = state(&http).await?;
    if !initial["rows"].as_array().ok_or("missing rows")?.is_empty() {
        return Err("fixture database must be fresh".into());
    }
    let limit = initial["limit"].as_u64().ok_or("missing limit")? as usize;
    if !(2..=16).contains(&limit) {
        return Err("local review limit must be 2..16".into());
    }
    let dir = PathBuf::from(
        std::env::args()
            .nth(1)
            .ok_or("usage: rvc-capacity-smoke OUTPUT_DIRECTORY")?,
    );
    std::fs::create_dir_all(&dir)?;
    let agent = "sg-capacity-review-local".to_string();
    let mut voice: Value =
        serde_json::from_str(include_str!("../../fixtures/reference-config.json"))?;
    for (key, value) in voice.as_object_mut().ok_or("voice config")? {
        if value.as_str().is_some_and(|s| {
            s.starts_with("http:")
                || s.starts_with("https:")
                || s.starts_with("ws:")
                || s.starts_with("wss:")
        }) {
            *value = json!(if key == "stt_ws_url" {
                "ws://127.0.0.1:17930/asr".to_string()
            } else {
                BASE.to_string()
            });
        }
    }
    voice["opening_greeting_enabled"] = json!(false);
    voice["persist_conversation"] = json!(false);
    voice["platform"] = json!({"agent":{"base_url":BASE,"token":"local-internal-service-token"},"conversation":{"base_url":BASE,"token":"local-internal-service-token"}});
    voice["agent_context"] = json!({});
    let config = json!({"worker":{"server_url":"ws://127.0.0.1:17880","api_key":"devkey","api_secret":"secret","agent_name":agent,"max_jobs":limit,"allow_insecure":true},"voice":voice,"sip":{"trunk_ids":["ST_local_capacity_fixture"],"room_prefix":"sg-capacity-local-","business_id":BUSINESS,"config_agent_id":AGENT,"configured_agent_name":"Capacity Review","voice_auth_required":false},"capacity":{"business_id":BUSINESS,"heartbeat_seconds":10.0}});
    let path = dir.join("operator.json");
    std::fs::write(&path, serde_json::to_vec_pretty(&config)?)?;
    let log = std::fs::File::create(dir.join("worker.log"))?;
    let _worker = ChildGuard(
        Command::new(std::env::current_exe()?)
            .env_clear()
            .args([
                "--platform",
                path.to_str().ok_or("path")?,
                dir.to_str().ok_or("path")?,
            ])
            .stdout(Stdio::from(log.try_clone()?))
            .stderr(Stdio::from(log))
            .spawn()?,
    );
    tokio::time::timeout(Duration::from_secs(15), async {
        loop {
            if std::fs::read_to_string(dir.join("worker.log"))
                .unwrap_or_default()
                .contains("worker_ready")
            {
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    })
    .await?;
    // Contend for all slots using the unchanged capacity API's real SQL locking.
    let requests = (0..limit * 3).map(|i| {
        let http = http.clone();
        async move { Ok::<_, Box<dyn std::error::Error>>((i, reserve(&http, i).await?)) }
    });
    let results = futures_util::future::join_all(requests).await;
    let mut admitted = Vec::new();
    let mut fallback = [0, 0];
    for result in results {
        let (i, r) = result?;
        if r["available"] == true {
            admitted.push(i);
        } else {
            assert_eq!(r["reason"], "at_capacity");
            let target = if i % 2 == 0 { "human" } else { "ivr" };
            http.post(format!("{BASE}/fixture/fallback/{target}"))
                .json(&json!({"attempt_id":format!("capacity-{i}")}))
                .send()
                .await?
                .error_for_status()?;
            fallback[i % 2] += 1;
        }
    }
    assert_eq!(admitted.len(), limit);
    assert_eq!(fallback.iter().sum::<usize>(), limit * 2);
    let api = RoomClient::with_api_key(LK, "devkey", "secret");
    let dispatch = AgentDispatchClient::with_api_key(LK, "devkey", "secret");
    let mut rooms = Vec::new();
    for i in admitted {
        rooms.push(join(&api, &dispatch, &agent, i).await?);
    }
    wait_count(&http, "active", limit).await?;
    // Require the actual production worker to publish a track and reach ASR ready.
    tokio::time::timeout(Duration::from_secs(20), async {
        loop {
            let count = std::fs::read_dir(&dir)?
                .filter_map(Result::ok)
                .filter(|e| e.path().extension().is_some_and(|x| x == "jsonl"))
                .filter(|e| {
                    std::fs::read_to_string(e.path())
                        .unwrap_or_default()
                        .contains("session_ready")
                })
                .count();
            if count == limit {
                return Ok::<_, std::io::Error>(());
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    })
    .await??;
    tokio::time::sleep(Duration::from_secs(11)).await;
    assert!(
        state(&http).await?["events"]
            .as_array()
            .ok_or("events")?
            .iter()
            .filter(|e| e["path"]
                .as_str()
                .is_some_and(|p| p.ends_with("/heartbeat"))
                && e["status"] == 200)
            .count()
            >= limit
    );
    let first = rooms.remove(0);
    let first_name = first.name();
    let start = Instant::now();
    first.close().await?;
    wait_count(&http, "released", 1).await?;
    let hangup_ms = start.elapsed().as_millis();
    wait_count(&http, "active", limit - 1).await?;
    // Race multiple fresh callers against exactly one newly freed slot.
    let results = futures_util::future::join_all((100..112).map(|i| {
        let http = http.clone();
        async move { (i, reserve(&http, i).await) }
    }))
    .await;
    let mut replacement = None;
    for (i, result) in results {
        let r = result?;
        if r["available"] == true {
            assert!(replacement.replace(i).is_none());
        } else {
            assert_eq!(r["reason"], "at_capacity");
        }
    }
    rooms.push(
        join(
            &api,
            &dispatch,
            &agent,
            replacement.ok_or("freed slot unavailable")?,
        )
        .await?,
    );
    wait_count(&http, "active", limit).await?;
    api.delete_room(&first_name).await?;
    let mut finished_rooms = Vec::new();
    for room in rooms {
        let name = room.name();
        room.close().await?;
        finished_rooms.push(name);
    }
    wait_count(&http, "released", limit + 1).await?;
    wait_count(&http, "active", 0).await?;
    for name in finished_rooms {
        api.delete_room(&name).await?;
    }
    let end = state(&http).await?;
    let release_requests = end["events"]
        .as_array()
        .ok_or("events")?
        .iter()
        .filter(|e| {
            e["path"].as_str().is_some_and(|p| p.ends_with("/release")) && e["status"] == 200
        })
        .count();
    assert_eq!(release_requests, limit + 1);
    // Real HTTP through both Next servers, then real RTC join and native bootstrap.
    let mut web_verified = Vec::new();
    for (surface, url, body, cookie) in [
        (
            "dashboard-public",
            "http://127.0.0.1:17931/api/public-agent/connection-details",
            json!({"businessSlug":"capacity-review","agentPublicId":"agt_capacity_fixture","endUserContact":"caller@example.test","businessId":"spoofed"}),
            false,
        ),
        (
            "dashboard-owner",
            "http://127.0.0.1:17931/api/agent-test/connection-details",
            json!({"businessId":"spoofed","configAgentId":"spoofed"}),
            true,
        ),
        (
            "frontend-slug",
            "http://127.0.0.1:17932/api/connection-details",
            json!({"businessSlug":"capacity-review","agentPublicId":"agt_capacity_fixture","endUserEmail":"caller@example.test","businessId":"spoofed","configAgentId":"spoofed","runtimeAgentName":"spoofed"}),
            false,
        ),
        (
            "frontend-domain",
            "http://127.0.0.1:17932/api/connection-details",
            json!({"endUserEmail":"caller@example.test"}),
            false,
        ),
    ] {
        let mut req = http.post(url).json(&body);
        if cookie {
            req = req.header("Cookie", "sales_girl_access_token=local-owner-fixture");
        }
        let response = req.send().await?;
        let code = response.status();
        let connection: Value = response.json().await?;
        if !code.is_success() {
            return Err(format!("{surface}: {code}: {connection}").into());
        }
        assert_eq!(connection["serverUrl"], "ws://127.0.0.1:17880");
        let name = connection["roomName"].as_str().ok_or("room name")?;
        let (caller, mut events) = Room::connect(
            "ws://127.0.0.1:17880",
            connection["participantToken"].as_str().ok_or("token")?,
            RoomOptions::default(),
        )
        .await?;
        // Observe the actual worker's session status delivered over RTC, not an HTTP-only mock.
        tokio::time::timeout(Duration::from_secs(20), async {
            loop {
                match events.recv().await {
                    Some(RoomEvent::DataReceived { payload, .. }) => {
                        let value: Value = serde_json::from_slice(&payload).unwrap_or(Value::Null);
                        if value["state"] == "listening" || value["type"] == "ready" {
                            return Ok(());
                        }
                    }
                    None => {
                        return Err::<(), Box<dyn std::error::Error>>(
                            "RTC event stream closed before ready".into(),
                        )
                    }
                    _ => {}
                }
            }
        })
        .await??;
        // Require authoritative native traces for this specific room's SDK job.
        let participants = api.list_participants(name).await?;
        assert!(
            participants
                .iter()
                .any(|p| p.identity.starts_with("sg-rust-")),
            "native worker did not join {surface}"
        );
        caller.close().await?;
        api.delete_room(name).await?;
        web_verified.push(surface);
    }
    let refused=http.post("http://127.0.0.1:17932/api/connection-details").json(&json!({"businessSlug":"capacity-review","agentPublicId":"agt_capacity_fixture","endUserEmail":"no-airtime@example.test"})).send().await?;
    assert_eq!(refused.status(), reqwest::StatusCode::PAYMENT_REQUIRED);
    let summary = json!({"capacity": {"limit":limit,"initial_contenders":limit*3,"admitted":limit,"simulated_human_fallback":fallback[0],"simulated_ivr_fallback":fallback[1],"sdk_sip_calls_ready":limit,"first_hangup_release_ms":hangup_ms,"replacement_race_contenders":12,"replacement_admitted":1,"released":limit+1,"release_requests":release_requests,"active_after_hangup":0,"actual_external_aicc_handoff_tested":false},"web_surfaces":web_verified,"frontend_billing_refusal":402});
    std::fs::write(
        dir.join("summary.json"),
        serde_json::to_vec_pretty(&summary)?,
    )?;
    println!("{summary}");
    Ok(())
}
