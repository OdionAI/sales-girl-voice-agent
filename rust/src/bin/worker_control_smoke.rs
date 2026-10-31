//! Real local LiveKit server control-plane smoke, no Python or media bridge.
use livekit_api::services::{
    agent_dispatch::AgentDispatchClient,
    room::{CreateRoomOptions, RoomClient},
};
use livekit_protocol as p;
use sg_voice_prototype::worker::{self, AssignedJob, Event, JobHandler, LocalConfig, Run};
use std::{sync::Arc, time::Duration};
use tokio::sync::{mpsc, watch};
struct Handler;
impl JobHandler for Handler {
    fn accepts(&self, j: &p::Job) -> bool {
        j.metadata == "native-local-synthetic"
            && j.room
                .as_ref()
                .is_some_and(|r| r.name.starts_with("sg-rust-"))
            && !j.enable_recording
    }
    fn run(&self, j: AssignedJob, mut cancel: watch::Receiver<bool>) -> Run<'_> {
        Box::pin(async move {
            assert_eq!(j.job.metadata, "native-local-synthetic");
            let _ = cancel.changed().await;
            Err("cancelled".into())
        })
    }
}
async fn event(
    rx: &mut mpsc::Receiver<Event>,
    predicate: impl Fn(&Event) -> bool,
) -> Result<Event, String> {
    tokio::time::timeout(Duration::from_secs(10), async {
        while let Some(e) = rx.recv().await {
            println!("{e:?}");
            if predicate(&e) {
                return Ok(e);
            }
        }
        Err("closed".into())
    })
    .await
    .map_err(|_| "event timeout")?
}
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let room = format!("sg-rust-control-{}", std::process::id());
    let agent = format!("sg-rust-control-worker-{}", std::process::id());
    let api = RoomClient::with_api_key("http://127.0.0.1:17880", "devkey", "secret");
    let dispatch = AgentDispatchClient::with_api_key("http://127.0.0.1:17880", "devkey", "secret");
    api.create_room(
        &room,
        CreateRoomOptions {
            empty_timeout: 30,
            ..Default::default()
        },
    )
    .await?;
    let (tx, rx) = watch::channel(false);
    let (ev, mut events) = mpsc::channel(64);
    let w = tokio::spawn(worker::run(
        LocalConfig::new(17880, agent.clone())?,
        Arc::new(Handler),
        rx,
        ev,
    ));
    event(&mut events, |e| matches!(e, Event::Registered(_))).await?;
    let d = dispatch
        .create_dispatch(p::CreateAgentDispatchRequest {
            agent_name: agent.clone(),
            room: room.clone(),
            metadata: "native-local-synthetic".into(),
            ..Default::default()
        })
        .await?;
    event(&mut events, |e| matches!(e, Event::Started(_))).await?;
    dispatch.delete_dispatch(&d.id, &room).await?;
    event(&mut events, |e| matches!(e, Event::Ended(_, false))).await?;
    dispatch
        .create_dispatch(p::CreateAgentDispatchRequest {
            agent_name: agent,
            room: room.clone(),
            metadata: "native-local-synthetic".into(),
            ..Default::default()
        })
        .await?;
    event(&mut events, |e| matches!(e, Event::Started(_))).await?;
    tx.send(true)?;
    tokio::time::timeout(Duration::from_secs(5), w)
        .await??
        .map_err(|e| format!("worker {e}"))?;
    api.delete_room(&room).await?;
    println!("{{\"official_server_dispatch\":true,\"termination\":true,\"active_job_shutdown\":true,\"python_bridge\":false}}");
    Ok(())
}
