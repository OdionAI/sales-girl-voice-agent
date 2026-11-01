//! Native LiveKit worker control protocol using official protobufs and grants.
//! Media remains in the official RTC SDK. Explicit operator configuration is
//! separate from the fixed-credential loopback harness; handlers retain scope checks.
use futures_util::{SinkExt, StreamExt};
use livekit_api::access_token::{AccessToken, VideoGrants};
use livekit_protocol as p;
use prost::Message;
use std::{
    collections::{HashMap, HashSet, VecDeque},
    future::Future,
    pin::Pin,
    sync::Arc,
    time::Duration,
};
use tokio::{
    sync::{mpsc, watch},
    task::JoinSet,
    time::Instant,
};
use tokio_tungstenite::{
    connect_async_with_config,
    tungstenite::{client::IntoClientRequest, http::HeaderValue, Message as Ws},
};
pub type Run<'a> = Pin<Box<dyn Future<Output = Result<(), String>> + Send + 'a>>;
pub struct AssignedJob {
    pub job: p::Job,
    pub url: String,
    pub token: String,
}
pub trait JobHandler: Send + Sync + 'static {
    /// Must fail closed on unsupported identity/features before joining a room.
    fn accepts(&self, job: &p::Job) -> bool;
    fn run(&self, job: AssignedJob, cancel: watch::Receiver<bool>) -> Run<'_>;
}
#[derive(Clone)]
pub struct LocalConfig {
    pub port: u16,
    pub agent_name: String,
    pub max_jobs: usize,
    pub assignment_timeout: Duration,
}
impl LocalConfig {
    pub fn new(port: u16, agent_name: String) -> Result<Self, String> {
        if port == 0 || !agent_name.starts_with("sg-rust-") || agent_name.len() > 100 {
            return Err("local worker requires sg-rust- name and valid local port".into());
        }
        Ok(Self {
            port,
            agent_name,
            max_jobs: 1,
            assignment_timeout: Duration::from_secs(5),
        })
    }
    fn url(&self) -> String {
        format!("ws://127.0.0.1:{}", self.port)
    }
}
#[derive(Clone, serde::Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ProductionConfig {
    pub server_url: String,
    pub api_key: String,
    pub api_secret: String,
    pub agent_name: String,
    pub max_jobs: usize,
    pub assignment_timeout_seconds: f64,
    pub shutdown_grace_seconds: f64,
    pub reconnect_limit: Option<u32>,
    pub allow_insecure: bool,
}
impl Default for ProductionConfig {
    fn default() -> Self {
        Self {
            server_url: String::new(),
            api_key: String::new(),
            api_secret: String::new(),
            agent_name: String::new(),
            max_jobs: 1,
            assignment_timeout_seconds: 5.0,
            shutdown_grace_seconds: 3600.0,
            reconnect_limit: Some(16),
            allow_insecure: false,
        }
    }
}
impl ProductionConfig {
    fn connection(&self) -> Result<ConnectionConfig, String> {
        let base = self
            .server_url
            .trim()
            .trim_end_matches('/')
            .replacen("https://", "wss://", 1)
            .replacen("http://", "ws://", 1);
        let uri: tokio_tungstenite::tungstenite::http::Uri =
            base.parse().map_err(|_| "invalid worker URL")?;
        let host = uri.host().ok_or("worker URL requires host")?;
        if !matches!(uri.scheme_str(), Some("ws" | "wss"))
            || uri.authority().is_none_or(|a| a.as_str().contains('@'))
            || uri.query().is_some()
            || base.contains('#')
            || uri.path() != "/"
            || uri.scheme_str() == Some("ws")
                && !self.allow_insecure
                && !matches!(host, "127.0.0.1" | "localhost" | "[::1]")
            || self.api_key.is_empty()
            || self.api_secret.is_empty()
            || self.agent_name.trim().is_empty()
            || self.agent_name.len() > 100
            || !(1..=1024).contains(&self.max_jobs)
            || !self.assignment_timeout_seconds.is_finite()
            || !(1.0..=120.0).contains(&self.assignment_timeout_seconds)
            || !self.shutdown_grace_seconds.is_finite()
            || !(0.0..=3600.0).contains(&self.shutdown_grace_seconds)
        {
            return Err("invalid production worker configuration".into());
        }
        Ok(ConnectionConfig {
            base,
            api_key: self.api_key.clone(),
            api_secret: self.api_secret.clone(),
            agent_name: self.agent_name.clone(),
            max_jobs: self.max_jobs,
            assignment_timeout: Duration::from_secs_f64(self.assignment_timeout_seconds),
            shutdown_grace: Duration::from_secs_f64(self.shutdown_grace_seconds),
            reconnect_limit: self.reconnect_limit,
        })
    }
    pub fn validate(&self) -> Result<(), String> {
        self.connection().map(|_| ())
    }
}
#[derive(Default)]
struct SeenJobs {
    set: HashSet<String>,
    order: VecDeque<String>,
}
impl SeenJobs {
    fn contains(&self, id: &str) -> bool {
        self.set.contains(id)
    }
    fn remember(&mut self, id: String, active: &HashMap<String, Slot>) {
        self.set.insert(id.clone());
        self.order.push_back(id);
        while self.order.len() > 4096 {
            let Some(old) = self.order.pop_front() else {
                break;
            };
            if active.contains_key(&old) {
                self.order.push_back(old);
            } else {
                self.set.remove(&old);
            }
        }
    }
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Event {
    Registered(String),
    Reserved(String),
    Rejected(String),
    Started(String),
    Ended(String, bool),
    Disconnected,
    Stopped,
}
enum Slot {
    Reserved { job: Box<p::Job>, expires: Instant },
    Running { cancel: watch::Sender<bool> },
}
struct Admission {
    seen: SeenJobs,
    slots: HashMap<String, Slot>,
    max: usize,
    draining: bool,
}
impl Admission {
    fn offer(&mut self, job: p::Job, allowed: bool, timeout: Duration) -> bool {
        if self.draining
            || !allowed
            || job.id.is_empty()
            || job.id.len() > 128
            || !job
                .id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
            || self.seen.contains(&job.id)
            || self.slots.contains_key(&job.id)
            || self.slots.len() >= self.max
        {
            return false;
        }
        self.seen.remember(job.id.clone(), &self.slots);
        self.slots.insert(
            job.id.clone(),
            Slot::Reserved {
                job: Box::new(job),
                expires: Instant::now() + timeout,
            },
        );
        true
    }
    fn assignment(&mut self, a: &p::JobAssignment, base: &str) -> Result<AssignedJob, String> {
        if self.draining {
            return Err("worker draining".into());
        }
        let job = a.job.as_ref().ok_or("missing assigned job")?;
        let Some(Slot::Reserved {
            job: offered,
            expires,
        }) = self.slots.get(&job.id)
        else {
            return Err("unreserved or duplicate assignment".into());
        };
        let url = a
            .url
            .clone()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| base.into())
            .trim_end_matches('/')
            .replacen("https://", "wss://", 1)
            .replacen("http://", "ws://", 1);
        if *expires <= Instant::now()
            || !same_job(offered, job)
            || a.token.is_empty()
            || url != base
        {
            return Err("assignment scope/token/url mismatch".into());
        }
        Ok(AssignedJob {
            job: job.clone(),
            url,
            token: a.token.clone(),
        })
    }
    fn expire(&mut self) {
        self.slots
            .retain(|_, s| !matches!(s,Slot::Reserved{expires,..}if *expires<=Instant::now()));
    }
    fn cancel_all(&mut self) {
        self.draining = true;
        for s in self.slots.values() {
            if let Slot::Running { cancel } = s {
                let _ = cancel.send(true);
            }
        }
    }
}
fn same_job(a: &p::Job, b: &p::Job) -> bool {
    a.id == b.id
        && a.dispatch_id == b.dispatch_id
        && a.agent_name == b.agent_name
        && a.metadata == b.metadata
        && a.room == b.room
        && a.participant == b.participant
        && a.r#type == b.r#type
        && a.deployment == b.deployment
        && a.enable_recording == b.enable_recording
        && a.enable_redaction == b.enable_redaction
        && a.attributes == b.attributes
}
fn message(m: p::worker_message::Message) -> Ws {
    Ws::Binary(p::WorkerMessage { message: Some(m) }.encode_to_vec().into())
}
fn status(a: &Admission, seq: u64) -> Ws {
    message(p::worker_message::Message::UpdateWorker(
        p::UpdateWorkerStatus {
            status: Some(if a.draining || a.slots.len() >= a.max {
                p::WorkerStatus::WsFull
            } else {
                p::WorkerStatus::WsAvailable
            } as i32),
            // Server-side affinity uses target_load - load, independently of
            // WS_AVAILABLE. A fractional slot ratio can strand free slots above
            // the server's target. Fixed-slot admission reports binary capacity;
            // job_count retains the measured concurrent-job count.
            load: if a.draining || a.slots.len() >= a.max {
                1.0
            } else {
                0.0
            },
            job_count: a
                .slots
                .values()
                .filter(|s| matches!(s, Slot::Running { .. }))
                .count() as u32,
            draining: a.draining,
            seq,
        },
    ))
}
fn report(id: String, result: &Result<(), String>) -> Ws {
    message(p::worker_message::Message::UpdateJob(p::UpdateJobStatus {
        job_id: id,
        status: if result.is_ok() {
            p::JobStatus::JsSuccess
        } else {
            p::JobStatus::JsFailed
        } as i32,
        error: if result.is_err() {
            "job failed or cancelled".into()
        } else {
            String::new()
        },
    }))
}

type Wire =
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;
async fn send_wire(ws: &mut Wire, m: Ws) -> Result<(), ()> {
    tokio::time::timeout(Duration::from_secs(2), ws.send(m))
        .await
        .map_err(|_| ())?
        .map_err(|_| ())
}

/// Bounded diagnostic events use try_send; never block job control on a logger.
fn event(tx: &mpsc::Sender<Event>, e: Event) {
    let _ = tx.try_send(e);
}
/// Loopback test harness. Production credentials cannot be supplied here.
pub async fn run(
    config: LocalConfig,
    handler: Arc<dyn JobHandler>,
    stop: watch::Receiver<bool>,
    events: mpsc::Sender<Event>,
) -> Result<(), String> {
    LocalConfig::new(config.port, config.agent_name.clone())?;
    if config.max_jobs == 0 || config.max_jobs > 16 {
        return Err("invalid local capacity".into());
    }
    let base = config.url();
    run_control(
        ConnectionConfig {
            base,
            api_key: "devkey".into(),
            api_secret: "secret".into(),
            agent_name: config.agent_name,
            max_jobs: config.max_jobs,
            assignment_timeout: config.assignment_timeout,
            shutdown_grace: Duration::ZERO,
            reconnect_limit: Some(5),
        },
        handler,
        stop,
        events,
    )
    .await
}
/// Explicit operator configuration; the local harness never selects this path.
pub async fn run_production(
    config: ProductionConfig,
    handler: Arc<dyn JobHandler>,
    stop: watch::Receiver<bool>,
    events: mpsc::Sender<Event>,
) -> Result<(), String> {
    let config = config.connection()?;
    run_control(config, handler, stop, events).await
}
struct ConnectionConfig {
    base: String,
    api_key: String,
    api_secret: String,
    agent_name: String,
    max_jobs: usize,
    assignment_timeout: Duration,
    shutdown_grace: Duration,
    reconnect_limit: Option<u32>,
}
#[allow(deprecated)] // Official agent compatibility permission, retained by Python.
async fn connect_registered(config: &ConnectionConfig) -> Result<(Wire, String), String> {
    let token = AccessToken::with_api_key(&config.api_key, &config.api_secret)
        .with_grants(VideoGrants {
            agent: true,
            ..Default::default()
        })
        .to_jwt()
        .map_err(|_| "worker token failure")?;
    let mut req = format!("{}/agent", config.base)
        .into_client_request()
        .map_err(|_| "worker URL failure")?;
    req.headers_mut().insert(
        "Authorization",
        HeaderValue::from_str(&format!("Bearer {token}")).map_err(|_| "worker header failure")?,
    );
    let (mut ws, _) = tokio::time::timeout(
        Duration::from_secs(5),
        connect_async_with_config(
            req,
            Some(
                tokio_tungstenite::tungstenite::protocol::WebSocketConfig::default()
                    .max_message_size(Some(65536))
                    .max_frame_size(Some(65536)),
            ),
            true,
        ),
    )
    .await
    .map_err(|_| "worker connection timeout")?
    .map_err(|_| "worker connection failed")?;
    send_wire(
        &mut ws,
        message(p::worker_message::Message::Register(
            p::RegisterWorkerRequest {
                r#type: p::JobType::JtRoom as i32,
                agent_name: config.agent_name.clone(),
                version: env!("CARGO_PKG_VERSION").into(),
                ping_interval: 2,
                allowed_permissions: Some(p::ParticipantPermission {
                    can_publish: true,
                    can_subscribe: true,
                    can_publish_data: true,
                    can_update_metadata: true,
                    agent: true,
                    ..Default::default()
                }),
                ..Default::default()
            },
        )),
    )
    .await
    .map_err(|_| "registration send failed")?;
    let id = tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            match ws.next().await {
                Some(Ok(Ws::Binary(bytes))) => match p::ServerMessage::decode(bytes)
                    .ok()
                    .and_then(|m| m.message)
                {
                    Some(p::server_message::Message::Register(r)) if !r.worker_id.is_empty() => {
                        return Ok(r.worker_id)
                    }
                    _ => return Err("invalid registration response"),
                },
                Some(Ok(Ws::Ping(bytes))) => send_wire(&mut ws, Ws::Pong(bytes))
                    .await
                    .map_err(|_| "registration ping failed")?,
                _ => return Err("registration closed"),
            }
        }
    })
    .await
    .map_err(|_| "registration timeout")??;
    Ok((ws, id))
}
type JobResult = (String, Result<(), String>);
type JobCompletion = Option<Result<JobResult, tokio::task::JoinError>>;
fn completed(
    admission: &mut Admission,
    events: &mpsc::Sender<Event>,
    done: JobCompletion,
) -> Option<(String, Result<(), String>)> {
    if let Some(Ok((id, result))) = done {
        admission.slots.remove(&id);
        event(events, Event::Ended(id.clone(), result.is_ok()));
        Some((id, result))
    } else {
        None
    }
}
async fn finish_jobs(
    admission: &mut Admission,
    jobs: &mut JoinSet<JobResult>,
    events: &mpsc::Sender<Event>,
    grace: Duration,
) {
    admission.draining = true;
    admission
        .slots
        .retain(|_, slot| matches!(slot, Slot::Running { .. }));
    let deadline = Instant::now() + grace;
    while !jobs.is_empty() {
        tokio::select! {done=jobs.join_next()=>{completed(admission,events,done);},_=tokio::time::sleep_until(deadline)=>break,}
    }
    admission.cancel_all();
    while let Some(done) = jobs.join_next().await {
        completed(admission, events, Some(done));
    }
}
async fn handle_server(
    incoming: p::server_message::Message,
    ws: &mut Wire,
    admission: &mut Admission,
    jobs: &mut JoinSet<JobResult>,
    config: &ConnectionConfig,
    handler: &Arc<dyn JobHandler>,
    events: &mpsc::Sender<Event>,
) -> Result<(), ()> {
    match incoming {
        p::server_message::Message::Availability(offer) => {
            let Some(job) = offer.job else {
                return Ok(());
            };
            let id = job.id.clone();
            let allowed = !offer.resuming
                && job.agent_name == config.agent_name
                && job.r#type == p::JobType::JtRoom as i32
                && handler.accepts(&job);
            let accepted = admission.offer(job, allowed, config.assignment_timeout);
            event(
                events,
                if accepted {
                    Event::Reserved(id.clone())
                } else {
                    Event::Rejected(id.clone())
                },
            );
            let response = p::AvailabilityResponse {
                job_id: id.clone(),
                available: accepted,
                supports_resume: false,
                participant_identity: format!("sg-rust-{id}"),
                participant_name: "RVC native worker".into(),
                ..Default::default()
            };
            send_wire(
                ws,
                message(p::worker_message::Message::Availability(response)),
            )
            .await?;
        }
        p::server_message::Message::Assignment(assignment) => {
            match admission.assignment(&assignment, &config.base) {
                Ok(assigned) => {
                    let id = assigned.job.id.clone();
                    let (cancel, rx) = watch::channel(false);
                    admission.slots.insert(id.clone(), Slot::Running { cancel });
                    let handler = handler.clone();
                    event(events, Event::Started(id.clone()));
                    let running = id.clone();
                    jobs.spawn(async move {
                        use futures_util::FutureExt;
                        let result =
                            std::panic::AssertUnwindSafe(run_owned_job(handler, assigned, rx))
                                .catch_unwind()
                                .await
                                .unwrap_or_else(|_| Err("job task panicked".into()));
                        (id, result)
                    });
                    send_wire(
                        ws,
                        message(p::worker_message::Message::UpdateJob(p::UpdateJobStatus {
                            job_id: running,
                            status: p::JobStatus::JsRunning as i32,
                            error: String::new(),
                        })),
                    )
                    .await?;
                }
                Err(_) => {
                    if let Some(job) = assignment.job {
                        if !matches!(admission.slots.get(&job.id), Some(Slot::Running { .. })) {
                            admission.slots.remove(&job.id);
                            send_wire(ws, report(job.id, &Err("invalid assignment".into())))
                                .await?;
                        }
                    }
                }
            }
        }
        p::server_message::Message::Termination(termination) => {
            if let Some(slot) = admission.slots.get(&termination.job_id) {
                if let Slot::Running { cancel } = slot {
                    let _ = cancel.send(true);
                } else {
                    admission.slots.remove(&termination.job_id);
                }
            }
        }
        p::server_message::Message::GoAway(_) => return Err(()),
        p::server_message::Message::Pong(_) | p::server_message::Message::Register(_) => {}
    }
    Ok(())
}

async fn run_control(
    config: ConnectionConfig,
    handler: Arc<dyn JobHandler>,
    mut stop: watch::Receiver<bool>,
    events: mpsc::Sender<Event>,
) -> Result<(), String> {
    let mut admission = Admission {
        seen: SeenJobs::default(),
        slots: HashMap::new(),
        max: config.max_jobs,
        draining: false,
    };
    let mut jobs: JoinSet<JobResult> = JoinSet::new();
    let mut reports = std::collections::VecDeque::new();
    let mut attempts = 0u32;
    let mut drain_deadline: Option<Instant> = None;
    loop {
        if *stop.borrow() || stop.has_changed().is_err() {
            let grace = drain_deadline
                .map(|deadline| deadline.saturating_duration_since(Instant::now()))
                .unwrap_or(config.shutdown_grace);
            finish_jobs(&mut admission, &mut jobs, &events, grace).await;
            event(&events, Event::Stopped);
            return Ok(());
        }
        let connection = tokio::select! {biased;_=stop.changed()=>continue,done=jobs.join_next(),if !jobs.is_empty()=>{if let Some(done)=completed(&mut admission,&events,done){reports.push_back(done);}continue;},result=connect_registered(&config)=>result};
        let (mut ws, id) = match connection {
            Ok(connected) => connected,
            Err(_) => {
                attempts = attempts.saturating_add(1);
                if config.reconnect_limit.is_some_and(|limit| attempts > limit) {
                    finish_jobs(&mut admission, &mut jobs, &events, Duration::ZERO).await;
                    return Err("worker reconnect limit".into());
                }
                let delay =
                    Duration::from_millis((100u64.saturating_mul(attempts as u64)).min(10000));
                tokio::select! {_=stop.changed()=>{},done=jobs.join_next(),if !jobs.is_empty()=>{if let Some(done)=completed(&mut admission,&events,done){reports.push_back(done);}},_=tokio::time::sleep(delay)=>{}}
                continue;
            }
        };
        attempts = 0;
        event(&events, Event::Registered(id));
        let mut seq = 0;
        let mut ticks = 0u32;
        let mut last_message = Instant::now();
        let mut shutting_down = false;
        let mut forced = false;
        // Match official worker reconnect semantics: the RTC jobs keep their owned
        // state/audio, and the server re-associates them with the new control socket.
        let active = admission
            .slots
            .iter()
            .filter(|(_, s)| matches!(s, Slot::Running { .. }))
            .map(|(id, _)| id.clone())
            .collect::<Vec<_>>();
        let mut startup_failed = !active.is_empty()
            && send_wire(
                &mut ws,
                message(p::worker_message::Message::MigrateJob(
                    p::MigrateJobRequest { job_ids: active },
                )),
            )
            .await
            .is_err();
        if !startup_failed {
            startup_failed = send_wire(&mut ws, status(&admission, seq)).await.is_err();
        }
        while !startup_failed {
            let Some((id, result)) = reports.front() else {
                break;
            };
            if send_wire(&mut ws, report(id.clone(), result))
                .await
                .is_err()
            {
                startup_failed = true;
            } else {
                reports.pop_front();
            }
        }
        if startup_failed {
            event(&events, Event::Disconnected);
            tokio::select! { _=stop.changed()=>{},_=tokio::time::sleep(Duration::from_millis(100))=>{} }
            continue;
        }
        let mut tick = tokio::time::interval(Duration::from_millis(250));
        'connected: loop {
            tokio::select! {
                biased;
                _=stop.changed(),if !shutting_down=>{
                    shutting_down=true;
                    admission.draining=true;
                    admission.slots.retain(|_,slot|matches!(slot,Slot::Running{..}));
                    drain_deadline=Some(Instant::now()+config.shutdown_grace);
                    seq+=1;
                    let _=send_wire(&mut ws,status(&admission,seq)).await;
                }
                done=jobs.join_next(),if !jobs.is_empty()=>{
                    if let Some(done)=completed(&mut admission,&events,done) {
                        reports.push_back(done);
                        seq+=1;
                        if send_wire(&mut ws,status(&admission,seq)).await.is_err(){break 'connected;}
                        if let Some((id,result))=reports.front() {
                            if send_wire(&mut ws,report(id.clone(),result)).await.is_err(){break 'connected;}
                            reports.pop_front();
                        }
                    }else{break 'connected;}
                }
                _=tick.tick()=>{
                    if last_message.elapsed()>Duration::from_secs(10){break 'connected;}
                    admission.expire();
                    if shutting_down&&jobs.is_empty(){
                        let _=ws.close(None).await;
                        event(&events,Event::Stopped);
                        return Ok(());
                    }
                    if !forced&&drain_deadline.is_some_and(|deadline|deadline<=Instant::now()){
                        forced=true;
                        admission.cancel_all();
                    }
                    seq+=1;
                    if send_wire(&mut ws,status(&admission,seq)).await.is_err(){break 'connected;}
                    ticks=ticks.wrapping_add(1);
                    if ticks.is_multiple_of(8) {
                        let timestamp=std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_millis() as i64;
                        if send_wire(&mut ws,message(p::worker_message::Message::Ping(p::WorkerPing{timestamp}))).await.is_err(){break 'connected;}
                    }
                }
                incoming=ws.next()=>{
                    let bytes=match incoming {
                        Some(Ok(Ws::Binary(bytes)))=>bytes,
                        Some(Ok(Ws::Ping(bytes)))=>{
                            last_message=Instant::now();
                            if send_wire(&mut ws,Ws::Pong(bytes)).await.is_err(){break 'connected;}
                            continue;
                        }
                        Some(Ok(Ws::Close(_)))|Some(Err(_))|None=>break 'connected,
                        _=>continue,
                    };
                    last_message=Instant::now();
                    let msg=match p::ServerMessage::decode(bytes){Ok(msg)=>msg,Err(_)=>break 'connected};
                    if let Some(msg)=msg.message {
                        if handle_server(msg,&mut ws,&mut admission,&mut jobs,&config,&handler,&events).await.is_err(){break 'connected;}
                    }
                }
            }
        }
        // Only reservations belong to a control epoch. Running RTC calls survive it.
        admission
            .slots
            .retain(|_, slot| matches!(slot, Slot::Running { .. }));
        let _ = ws.close(None).await;
        event(&events, Event::Disconnected);
        tokio::select! {_=stop.changed()=>{},_=tokio::time::sleep(Duration::from_millis(100))=>{}}
    }
}

/// Recording finalization, queued transcripts and final usage must outlive the
/// stop signal. Dropping the job after two seconds lost that cleanup work.
async fn run_owned_job(
    handler: Arc<dyn JobHandler>,
    assigned: AssignedJob,
    cancel: watch::Receiver<bool>,
) -> Result<(), String> {
    let mut abort = cancel.clone();
    let future = handler.run(assigned, cancel);
    tokio::pin!(future);
    tokio::select! { result = &mut future => result,
        _ = abort.changed() => match tokio::time::timeout(Duration::from_secs(240), &mut future).await {
            Ok(result) => result,
            Err(_) => Err("job cancellation deadline".into()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn job(id: &str) -> p::Job {
        p::Job {
            id: id.into(),
            agent_name: "sg-rust-test".into(),
            metadata: "trusted-dispatch-metadata".into(),
            room: Some(p::Room {
                name: "sg-rust-room".into(),
                ..Default::default()
            }),
            ..Default::default()
        }
    }
    fn admission() -> Admission {
        Admission {
            seen: SeenJobs::default(),
            slots: HashMap::new(),
            max: 1,
            draining: false,
        }
    }
    #[tokio::test(start_paused = true)]
    async fn reservations_prevent_overcommit_and_expire() {
        let mut a = admission();
        assert!(a.offer(job("1"), true, Duration::from_secs(5)));
        assert!(!a.offer(job("2"), true, Duration::from_secs(5)));
        assert!(!a.offer(job("1"), true, Duration::from_secs(5)));
        tokio::time::advance(Duration::from_secs(5)).await;
        a.expire();
        assert!(a.slots.is_empty());
        assert!(a.offer(job("2"), true, Duration::from_secs(5)));
        a.cancel_all();
        a.slots.clear();
        assert!(!a.offer(job("3"), true, Duration::from_secs(5)));
    }
    #[tokio::test]
    async fn recent_id_cache_stays_bounded_without_filling_worker_forever() {
        let mut a = admission();
        a.max = 2;
        assert!(a.offer(job("long-running"), true, Duration::from_secs(5)));
        a.slots.insert(
            "long-running".into(),
            Slot::Running {
                cancel: watch::channel(false).0,
            },
        );
        for i in 0..4200 {
            let id = format!("completed-{i}");
            assert!(a.offer(job(&id), true, Duration::from_secs(5)));
            a.slots.remove(&id);
        }
        assert_eq!(a.seen.set.len(), 4096);
        assert_eq!(a.seen.order.len(), 4096);
        assert!(!a.offer(job("long-running"), true, Duration::from_secs(5)));
        assert!(!a.offer(job("completed-4199"), true, Duration::from_secs(5)));
        assert!(a.offer(job("next-job"), true, Duration::from_secs(5)));
    }
    #[test]
    fn operator_worker_configuration_is_explicit_and_validates_transport_and_limits() {
        let valid = ProductionConfig {
            server_url: "https://voice.example.test/".into(),
            api_key: "public-key".into(),
            api_secret: "public-secret".into(),
            agent_name: "rvc-livekit-comparison".into(),
            max_jobs: 24,
            ..Default::default()
        };
        assert_eq!(valid.connection().unwrap().base, "wss://voice.example.test");
        for url in [
            "ws://voice.example.test",
            "wss://name:secret@voice.example.test",
            "wss://voice.example.test/?token=bad",
            "file:///tmp/socket",
            "wss://voice.example.test/other",
        ] {
            assert!(ProductionConfig {
                server_url: url.into(),
                ..valid.clone()
            }
            .validate()
            .is_err());
        }
        assert!(ProductionConfig {
            max_jobs: 0,
            ..valid.clone()
        }
        .validate()
        .is_err());
        assert!(ProductionConfig {
            assignment_timeout_seconds: f64::NAN,
            ..valid.clone()
        }
        .validate()
        .is_err());
        assert!(ProductionConfig {
            server_url: "ws://127.0.0.1:1234".into(),
            ..valid
        }
        .validate()
        .is_ok());
    }
    #[tokio::test]
    async fn assignment_is_bound_to_offered_scope_and_local_server() {
        let mut a = admission();
        let j = job("1");
        a.offer(j.clone(), true, Duration::from_secs(5));
        let mut assignment = p::JobAssignment {
            job: Some(j),
            url: None,
            token: "synthetic-token".into(),
        };
        let result = a.assignment(&assignment, "ws://127.0.0.1:17880").unwrap();
        assert_eq!(result.job.metadata, "trusted-dispatch-metadata");
        assignment.job.as_mut().unwrap().metadata = "forged".into();
        assert!(a.assignment(&assignment, "ws://127.0.0.1:17880").is_err());
        assignment.job = Some(job("1"));
        assignment.url = Some("wss://production.invalid".into());
        assert!(a.assignment(&assignment, "ws://127.0.0.1:17880").is_err());
        assignment.url = None;
        assignment.token.clear();
        assert!(a.assignment(&assignment, "ws://127.0.0.1:17880").is_err());
        assignment.token = "synthetic-token".into();
        a.slots.insert(
            "1".into(),
            Slot::Running {
                cancel: watch::channel(false).0,
            },
        );
        assert!(a.assignment(&assignment, "ws://127.0.0.1:17880").is_err());
    }
    struct CleanupHandler(Duration);
    impl JobHandler for CleanupHandler {
        fn accepts(&self, _: &p::Job) -> bool {
            true
        }
        fn run(&self, _: AssignedJob, mut cancel: watch::Receiver<bool>) -> Run<'_> {
            Box::pin(async move {
                let _ = cancel.changed().await;
                tokio::time::sleep(self.0).await;
                Ok(())
            })
        }
    }
    #[tokio::test(start_paused = true)]
    async fn cancellation_keeps_cleanup_owned_and_bounded() {
        for (cleanup, success) in [(3, true), (241, false)] {
            let (stop, rx) = watch::channel(false);
            let task = tokio::spawn(run_owned_job(
                Arc::new(CleanupHandler(Duration::from_secs(cleanup))),
                AssignedJob {
                    job: job("cleanup"),
                    url: String::new(),
                    token: String::new(),
                },
                rx,
            ));
            tokio::task::yield_now().await;
            stop.send(true).unwrap();
            tokio::task::yield_now().await;
            assert!(!task.is_finished());
            tokio::time::advance(Duration::from_secs(if success { 3 } else { 240 })).await;
            assert_eq!(task.await.unwrap().is_ok(), success);
        }
    }
    #[test]
    fn free_slots_keep_positive_server_affinity_at_every_occupancy() {
        for occupied in 0..=8 {
            let mut a = admission();
            a.max = 8;
            for i in 0..occupied {
                let (tx, _) = watch::channel(false);
                a.slots.insert(i.to_string(), Slot::Running { cancel: tx });
            }
            let Ws::Binary(bytes) = status(&a, 1) else {
                panic!("binary worker update")
            };
            let Some(p::worker_message::Message::UpdateWorker(update)) =
                p::WorkerMessage::decode(bytes).unwrap().message
            else {
                panic!("worker status")
            };
            assert_eq!(update.job_count, occupied);
            if occupied < 8 {
                assert_eq!(update.status, Some(p::WorkerStatus::WsAvailable as i32));
                for target in [0.1_f32, 0.5, 0.65, 0.9] {
                    assert!(
                        (target - update.load).max(0.0) > 0.0,
                        "a free slot must remain discoverable by server affinity"
                    );
                }
            } else {
                assert_eq!(update.status, Some(p::WorkerStatus::WsFull as i32));
                assert!(update.load >= 1.0);
            }
        }
    }
    #[tokio::test]
    async fn drain_cancels_running_jobs() {
        let (tx, rx) = watch::channel(false);
        let mut a = admission();
        a.slots
            .insert("1".into(), Slot::Running { cancel: tx.clone() });
        a.cancel_all();
        assert!(*rx.borrow());
        tx.send_replace(false);
        assert!(!a.offer(job("2"), true, Duration::from_secs(5)));
    }
}

#[cfg(test)]
mod protocol_tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use tokio::net::TcpListener;
    use tokio_tungstenite::{accept_async, WebSocketStream};
    struct Handler(AtomicUsize);
    impl JobHandler for Handler {
        fn accepts(&self, j: &p::Job) -> bool {
            j.metadata == "local-scope"
        }
        fn run(&self, j: AssignedJob, mut cancel: watch::Receiver<bool>) -> Run<'_> {
            Box::pin(async move {
                assert_eq!(j.job.metadata, "local-scope");
                assert_eq!(j.token, "test-token");
                self.0.fetch_add(1, Ordering::SeqCst);
                let _ = cancel.changed().await;
                Err("cancelled".into())
            })
        }
    }
    async fn send(ws: &mut WebSocketStream<tokio::net::TcpStream>, m: p::server_message::Message) {
        ws.send(Ws::Binary(
            p::ServerMessage { message: Some(m) }.encode_to_vec().into(),
        ))
        .await
        .unwrap();
    }
    async fn receive(
        ws: &mut WebSocketStream<tokio::net::TcpStream>,
    ) -> p::worker_message::Message {
        loop {
            if let Some(Ok(Ws::Binary(b))) = ws.next().await {
                return p::WorkerMessage::decode(b).unwrap().message.unwrap();
            }
        }
    }
    async fn offer_and_assign(
        ws: &mut WebSocketStream<tokio::net::TcpStream>,
        id: &str,
        name: &str,
    ) {
        let job = p::Job {
            id: id.into(),
            agent_name: name.into(),
            metadata: "local-scope".into(),
            room: Some(p::Room {
                name: "sg-rust-room".into(),
                ..Default::default()
            }),
            ..Default::default()
        };
        send(
            ws,
            p::server_message::Message::Availability(p::AvailabilityRequest {
                job: Some(job.clone()),
                resuming: false,
            }),
        )
        .await;
        loop {
            if let p::worker_message::Message::Availability(a) = receive(ws).await {
                assert!(a.available);
                break;
            }
        }
        send(
            ws,
            p::server_message::Message::Assignment(p::JobAssignment {
                job: Some(job),
                url: None,
                token: "test-token".into(),
            }),
        )
        .await;
    }
    async fn register(ws: &mut WebSocketStream<tokio::net::TcpStream>, worker_id: &str) {
        assert!(matches!(
            receive(ws).await,
            p::worker_message::Message::Register(_)
        ));
        send(
            ws,
            p::server_message::Message::Register(p::RegisterWorkerResponse {
                worker_id: worker_id.into(),
                ..Default::default()
            }),
        )
        .await;
    }
    // This is the callback signature required by tungstenite's official API.
    #[allow(clippy::result_large_err, deprecated)]
    fn verify_operator_header(
        request: &tokio_tungstenite::tungstenite::handshake::server::Request,
        response: tokio_tungstenite::tungstenite::handshake::server::Response,
    ) -> Result<
        tokio_tungstenite::tungstenite::handshake::server::Response,
        tokio_tungstenite::tungstenite::handshake::server::ErrorResponse,
    > {
        let token = request.headers()["authorization"]
            .to_str()
            .unwrap()
            .strip_prefix("Bearer ")
            .unwrap();
        let claims = livekit_api::access_token::TokenVerifier::with_api_key(
            "operator-fixture-key",
            "operator-fixture-secret",
        )
        .verify(token)
        .unwrap();
        assert!(claims.video.agent);
        assert!(!claims.video.room_join);
        Ok(response)
    }
    #[tokio::test]
    #[allow(deprecated)]
    async fn operator_credentials_concurrent_calls_and_reconnect_keep_owned_jobs_alive() {
        tokio::time::timeout(Duration::from_secs(10), async {
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let port = listener.local_addr().unwrap().port();
            let (stop, rx) = watch::channel(false);
            let (events, mut ev) = mpsc::channel(64);
            let handler = Arc::new(Handler(AtomicUsize::new(0)));
            let config = ProductionConfig {
                server_url: format!("http://127.0.0.1:{port}"),
                api_key: "operator-fixture-key".into(),
                api_secret: "operator-fixture-secret".into(),
                agent_name: "rvc-livekit-comparison".into(),
                max_jobs: 2,
                shutdown_grace_seconds: 0.0,
                ..Default::default()
            };
            let task = tokio::spawn(run_production(config, handler.clone(), rx, events));
            let mut ws = tokio_tungstenite::accept_hdr_async(
                listener.accept().await.unwrap().0,
                verify_operator_header,
            )
            .await
            .unwrap();
            register(&mut ws, "worker-one").await;
            offer_and_assign(&mut ws, "first", "rvc-livekit-comparison").await;
            offer_and_assign(&mut ws, "second", "rvc-livekit-comparison").await;
            while ev.recv().await != Some(Event::Started("second".into())) {}
            while handler.0.load(Ordering::SeqCst) < 2 {
                tokio::task::yield_now().await;
            }
            ws.close(None).await.unwrap();
            let mut ws = accept_async(listener.accept().await.unwrap().0)
                .await
                .unwrap();
            register(&mut ws, "worker-two").await;
            loop {
                if let p::worker_message::Message::MigrateJob(mut migrated) = receive(&mut ws).await
                {
                    migrated.job_ids.sort();
                    assert_eq!(migrated.job_ids, vec!["first", "second"]);
                    break;
                }
            }
            assert_eq!(handler.0.load(Ordering::SeqCst), 2);
            loop {
                if let p::worker_message::Message::UpdateWorker(status) = receive(&mut ws).await {
                    assert_eq!(status.job_count, 2);
                    assert_eq!(status.status, Some(p::WorkerStatus::WsFull as i32));
                    break;
                }
            }
            stop.send(true).unwrap();
            assert!(task.await.unwrap().is_ok());
        })
        .await
        .unwrap();
    }
    struct NaturalFinish {
        started: tokio::sync::Notify,
        finish: tokio::sync::Notify,
        cancelled: AtomicUsize,
    }
    impl JobHandler for NaturalFinish {
        fn accepts(&self, _: &p::Job) -> bool {
            true
        }
        fn run(&self, _: AssignedJob, mut cancel: watch::Receiver<bool>) -> Run<'_> {
            Box::pin(async move {
                self.started.notify_one();
                tokio::select! {_=self.finish.notified()=>Ok(()),_=cancel.changed()=>{self.cancelled.fetch_add(1,Ordering::SeqCst);Err("cancelled".into())}}
            })
        }
    }
    #[tokio::test]
    async fn draining_declines_new_calls_and_allows_active_call_to_finish() {
        tokio::time::timeout(Duration::from_secs(10), async {
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let (stop, rx) = watch::channel(false);
            let (events, _ev) = mpsc::channel(64);
            let handler = Arc::new(NaturalFinish {
                started: Default::default(),
                finish: Default::default(),
                cancelled: AtomicUsize::new(0),
            });
            let config = ProductionConfig {
                server_url: format!("ws://127.0.0.1:{}", listener.local_addr().unwrap().port()),
                api_key: "fixture".into(),
                api_secret: "fixture".into(),
                agent_name: "platform-agent".into(),
                shutdown_grace_seconds: 60.0,
                ..Default::default()
            };
            let task = tokio::spawn(run_production(config, handler.clone(), rx, events));
            let mut ws = accept_async(listener.accept().await.unwrap().0)
                .await
                .unwrap();
            register(&mut ws, "draining-worker").await;
            offer_and_assign(&mut ws, "ongoing", "platform-agent").await;
            handler.started.notified().await;
            stop.send(true).unwrap();
            loop {
                if let p::worker_message::Message::UpdateWorker(status) = receive(&mut ws).await {
                    if status.draining {
                        assert_eq!(status.status, Some(p::WorkerStatus::WsFull as i32));
                        break;
                    }
                }
            }
            let job = p::Job {
                id: "new-call".into(),
                agent_name: "platform-agent".into(),
                ..Default::default()
            };
            send(
                &mut ws,
                p::server_message::Message::Availability(p::AvailabilityRequest {
                    job: Some(job),
                    resuming: false,
                }),
            )
            .await;
            loop {
                if let p::worker_message::Message::Availability(a) = receive(&mut ws).await {
                    assert!(!a.available);
                    break;
                }
            }
            assert_eq!(handler.cancelled.load(Ordering::SeqCst), 0);
            handler.finish.notify_one();
            assert!(task.await.unwrap().is_ok());
            assert_eq!(handler.cancelled.load(Ordering::SeqCst), 0);
        })
        .await
        .unwrap();
    }
    #[tokio::test]
    async fn official_wire_registration_assignment_termination_reconnect_and_shutdown() {
        tokio::time::timeout(Duration::from_secs(10),async{
            let listener=TcpListener::bind("127.0.0.1:0").await.unwrap();let port=listener.local_addr().unwrap().port();
            let (stop,rx)=watch::channel(false);let(events,mut ev)=mpsc::channel(64);let handler=Arc::new(Handler(AtomicUsize::new(0)));
            let task=tokio::spawn(run(LocalConfig::new(port,"sg-rust-wire".into()).unwrap(),handler.clone(),rx,events));
            let mut ws=accept_async(listener.accept().await.unwrap().0).await.unwrap();
            assert!(matches!(receive(&mut ws).await,p::worker_message::Message::Register(r)if r.agent_name=="sg-rust-wire"));
            send(&mut ws,p::server_message::Message::Register(p::RegisterWorkerResponse{worker_id:"worker-1".into(),..Default::default()})).await;
            let j=p::Job{id:"job-1".into(),agent_name:"sg-rust-wire".into(),metadata:"local-scope".into(),room:Some(p::Room{name:"sg-rust-room".into(),..Default::default()}),..Default::default()};
            send(&mut ws,p::server_message::Message::Availability(p::AvailabilityRequest{job:Some(j.clone()),resuming:false})).await;
            loop{if let p::worker_message::Message::Availability(a)=receive(&mut ws).await{assert!(a.available);break;}}
            // A reservation occupies capacity before the assigned session starts.
            let mut j2=j.clone();j2.id="job-2".into();send(&mut ws,p::server_message::Message::Availability(p::AvailabilityRequest{job:Some(j2),resuming:false})).await;
            loop{if let p::worker_message::Message::Availability(a)=receive(&mut ws).await{assert!(!a.available);break;}}
            let a=p::JobAssignment{job:Some(j.clone()),url:None,token:"test-token".into()};
            send(&mut ws,p::server_message::Message::Assignment(a.clone())).await;send(&mut ws,p::server_message::Message::Assignment(a)).await;
            while ev.recv().await!=Some(Event::Started("job-1".into())){}
            tokio::task::yield_now().await;
            send(&mut ws,p::server_message::Message::Termination(p::JobTermination{job_id:j.id.clone()})).await;
            let mut available_before_end = false;
            loop { match receive(&mut ws).await {
                p::worker_message::Message::UpdateWorker(u) => available_before_end = u.status == Some(p::WorkerStatus::WsAvailable as i32),
                p::worker_message::Message::UpdateJob(u) if u.status != p::JobStatus::JsRunning as i32 => {
                    assert_eq!(u.job_id,"job-1"); assert_eq!(u.status,p::JobStatus::JsFailed as i32);
                    assert!(available_before_end, "capacity must be restored before job completion can trigger another dispatch");
                    break;
                }, _ => {}
            }}
            assert_eq!(handler.0.load(Ordering::SeqCst),1);
            ws.close(None).await.unwrap();
            let mut ws=accept_async(listener.accept().await.unwrap().0).await.unwrap();
            assert!(matches!(receive(&mut ws).await,p::worker_message::Message::Register(_)));
            send(&mut ws,p::server_message::Message::Register(p::RegisterWorkerResponse{worker_id:"worker-2".into(),..Default::default()})).await;
            while ev.recv().await!=Some(Event::Registered("worker-2".into())){}
            stop.send(true).unwrap();assert_eq!(task.await.unwrap(),Ok(()));
        }).await.unwrap();
    }
}

#[cfg(test)]
mod boundary_tests {
    use super::*;
    #[tokio::test]
    async fn completed_job_ids_cannot_be_admitted_again() {
        let mut a = Admission {
            seen: SeenJobs::default(),
            slots: HashMap::new(),
            max: 1,
            draining: false,
        };
        let job = p::Job {
            id: "job".into(),
            ..Default::default()
        };
        assert!(a.offer(job.clone(), true, Duration::from_secs(5)));
        a.slots.remove("job");
        assert!(!a.offer(job.clone(), true, Duration::from_secs(5)));
        let mut next_epoch = Admission {
            seen: a.seen,
            slots: HashMap::new(),
            max: 1,
            draining: false,
        };
        assert!(!next_epoch.offer(job, true, Duration::from_secs(5)));
    }
    #[test]
    fn local_worker_cannot_be_named_as_production_agent() {
        assert!(LocalConfig::new(17880, "rvc-livekit-comparison".into()).is_err());
        assert!(LocalConfig::new(0, "sg-rust-test".into()).is_err());
    }
}
