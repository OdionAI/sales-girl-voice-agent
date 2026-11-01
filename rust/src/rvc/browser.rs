//! Caller-scoped SDK publications and bounded telemetry. This task owns all
//! data-channel I/O; neither audio ingestion nor the coordinator awaits it.
use super::{Trace, TraceEvent};
use futures_util::{Stream, StreamExt};
use livekit::{data_stream::api::SendTextOptions, prelude::*, Transcription, TranscriptionSegment};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, VecDeque},
    future::Future,
    pin::Pin,
    sync::Arc,
    time::Duration,
};
use tokio::{
    sync::{mpsc, oneshot, watch},
    task::JoinHandle,
    time::Instant,
};

pub const METRICS_TOPIC: &str = "odion.voice_lab.metrics";
pub enum Publication {
    Data {
        topic: String,
        body: Value,
    },
    State(&'static str),
    Transcript {
        user: bool,
        final_: bool,
        text: String,
    },
}
pub type SendFuture<'a> = Pin<Box<dyn Future<Output = Result<(), String>> + Send + 'a>>;
pub trait Transport: Send + Sync + 'static {
    fn send(&self, item: Publication) -> SendFuture<'_>;
}
pub struct SdkTransport {
    participant: LocalParticipant,
    caller: String,
    output_track: String,
    input_track: watch::Receiver<String>,
    segments: std::sync::Mutex<HashMap<bool, String>>,
}
impl SdkTransport {
    pub fn new(
        participant: LocalParticipant,
        caller: String,
        output_track: String,
        input_track: watch::Receiver<String>,
    ) -> Result<Self, String> {
        if caller.is_empty() {
            return Err("browser publications require a caller destination".into());
        }
        Ok(Self {
            participant,
            caller,
            output_track,
            input_track,
            segments: Default::default(),
        })
    }
}
impl Transport for SdkTransport {
    fn send(&self, item: Publication) -> SendFuture<'_> {
        Box::pin(async move {
            match item {
                Publication::Data { topic, body } => self
                    .participant
                    .publish_data(DataPacket {
                        payload: body.to_string().into_bytes(),
                        reliable: true,
                        topic: Some(topic),
                        destination_identities: vec![self.caller.clone().into()],
                    })
                    .await
                    .map_err(|_| "data publication failed".into()),
                Publication::State(state) => self
                    .participant
                    .set_attributes(HashMap::from([("lk.agent.state".into(), state.into())]))
                    .await
                    .map_err(|_| "agent state publication failed".into()),
                Publication::Transcript { user, final_, text } => {
                    if text.is_empty() {
                        return Ok(());
                    }
                    let identity = if user {
                        self.caller.clone()
                    } else {
                        self.participant.identity().to_string()
                    };
                    let track = if user {
                        self.input_track.borrow().clone()
                    } else {
                        self.output_track.clone()
                    };
                    let id = {
                        let mut segments = self
                            .segments
                            .lock()
                            .map_err(|_| "transcription state unavailable")?;
                        let id = segments
                            .entry(user)
                            .or_insert_with(|| uuid::Uuid::new_v4().simple().to_string())
                            .clone();
                        if final_ {
                            segments.remove(&user);
                        }
                        id
                    };
                    if !track.is_empty() {
                        self.participant
                            .publish_transcription(Transcription {
                                participant_identity: identity.clone(),
                                track_id: track.clone(),
                                segments: vec![TranscriptionSegment {
                                    id: id.clone(),
                                    text: text.clone(),
                                    start_time: 0,
                                    end_time: 0,
                                    language: "en".into(),
                                    r#final: final_,
                                }],
                            })
                            .await
                            .map_err(|_| "transcription publication failed")?;
                    }
                    if final_ {
                        self.participant
                            .send_text(
                                &text,
                                SendTextOptions::new_with_topic("lk.transcription")
                                    .with_destination_identity(self.caller.clone())
                                    .with_sender_identity(identity)
                                    .with_attribute("lk.transcription_final", "true")
                                    .with_attribute("lk.segment_id", id)
                                    .with_attribute("lk.transcribed_track_id", track),
                            )
                            .await
                            .map_err(|_| "transcription stream failed")?;
                    }
                    Ok(())
                }
            }
        })
    }
}
struct Command {
    item: Publication,
    receipt: Option<oneshot::Sender<Result<(), String>>>,
    queued: Instant,
}
pub struct Publisher {
    tx: Option<mpsc::Sender<Command>>,
    task: Option<JoinHandle<()>>,
    trace: Trace,
}
impl Publisher {
    pub fn spawn(transport: Arc<dyn Transport>, trace: Trace, run_id: String) -> Self {
        let (tx, mut rx) = mpsc::channel::<Command>(128);
        let mut events = trace.subscribe();
        let observed = trace.clone();
        let task = tokio::spawn(async move {
            let mut mapper = MetricMapper::new(run_id);
            let mut state = "";
            let mut playback = PlaybackAcks::default();
            loop {
                let command = tokio::select! {biased;command=rx.recv()=>{let Some(command)=command else{return;};Some(command)},event=events.recv()=>match event{
                    Ok(event)=>{
                        if let Some((turn,attempt,data))=playback.observe(&event){observed.emit("browser_playback_confirmed",turn,attempt,data);}
                        if let Some(next)=state_for(&event){if state!=next{state=next;let _=tokio::time::timeout(Duration::from_secs(1),transport.send(Publication::State(next))).await;}}
                        mapper.map(&event).map(|body|Command{item:Publication::Data{topic:METRICS_TOPIC.into(),body},receipt:None,queued:Instant::now()})
                    },Err(tokio::sync::broadcast::error::RecvError::Lagged(count))=>{observed.emit("browser_telemetry_dropped",0,0,json!({"count":count}));None},Err(_)=>return
                }};
                if let Some(command) = command {
                    let result = if command.queued.elapsed() > Duration::from_secs(2) {
                        Err("publication deadline elapsed".into())
                    } else {
                        tokio::time::timeout(Duration::from_secs(1), transport.send(command.item))
                            .await
                            .unwrap_or_else(|_| Err("publication timed out".into()))
                    };
                    if result.is_err() {
                        observed.emit("browser_publication_failed", 0, 0, json!({}));
                    }
                    if let Some(receipt) = command.receipt {
                        let _ = receipt.send(result);
                    }
                }
            }
        });
        Self {
            tx: Some(tx),
            task: Some(task),
            trace,
        }
    }
    pub fn submit(&self, item: Publication, receipt: Option<oneshot::Sender<Result<(), String>>>) {
        if let Some(tx) = &self.tx {
            if let Err(error) = tx.try_send(Command {
                item,
                receipt,
                queued: Instant::now(),
            }) {
                if let Some(receipt) = error.into_inner().receipt {
                    let _ = receipt.send(Err("publication queue unavailable".into()));
                }
                self.trace
                    .emit("browser_publication_dropped", 0, 0, json!({}));
            }
        }
    }
    pub fn data(&self, topic: &str, body: Value) {
        self.submit(
            Publication::Data {
                topic: topic.into(),
                body,
            },
            None,
        );
    }
    pub async fn close(mut self) {
        self.tx.take();
        if let Some(mut task) = self.task.take() {
            if tokio::time::timeout(Duration::from_millis(250), &mut task)
                .await
                .is_err()
            {
                task.abort();
                let _ = task.await;
            }
        }
    }
}
// Drop must also cancel a publisher on setup failure or forced worker stop.
impl Drop for Publisher {
    fn drop(&mut self) {
        if let Some(task) = self.task.take() {
            task.abort();
        }
    }
}
fn state_for(event: &TraceEvent) -> Option<&'static str> {
    match event.event.as_str() {
        "session_ready" | "playout_complete" | "attempt_cancel" => Some("listening"),
        "audio_first_enqueued" => Some("speaking"),
        _ => None,
    }
}
#[derive(Default)]
struct Turn {
    speech_end: Option<u64>,
    final_at: Option<u64>,
    first: bool,
    llm: HashMap<u64, u64>,
    tts: HashMap<u64, (u64, u64)>,
    next_tts: u64,
    first_tts: HashMap<u64, (Value, String)>,
}
pub struct MetricMapper {
    run: String,
    turns: HashMap<u64, Turn>,
    order: VecDeque<u64>,
}
fn elapsed(end: u64, start: Option<u64>) -> Value {
    start
        .filter(|start| end >= *start)
        .map(|start| json!((end - start) as f64 / 1000.0))
        .unwrap_or(Value::Null)
}
impl MetricMapper {
    pub fn new(run: String) -> Self {
        Self {
            run,
            turns: Default::default(),
            order: Default::default(),
        }
    }
    pub fn map(&mut self, event: &TraceEvent) -> Option<Value> {
        let name = event.event.as_str();
        if event.turn == 0
            || !matches!(
                name,
                "user_speech_end"
                    | "stt_final"
                    | "llm_request_start"
                    | "llm_first_token"
                    | "llm_text_released"
                    | "tts_request_start"
                    | "tts_first_pcm"
                    | "tts_request_complete"
                    | "audio_first_enqueued"
                    | "speculation_start"
                    | "speculation_authorized"
                    | "attempt_cancel"
                    | "turn_complete"
                    | "llm_transfer_guard_decision"
                    | "llm_directory_guard_decision"
                    | "llm_first_tool"
                    | "tool_call_prepared"
                    | "llm_provider_request_complete"
            )
        {
            return None;
        }
        if !self.turns.contains_key(&event.turn) {
            self.order.push_back(event.turn);
            self.turns.insert(event.turn, Turn::default());
        }
        while self.order.len() > 50 {
            if let Some(id) = self.order.pop_front() {
                self.turns.remove(&id);
            }
        }
        let turn = self.turns.get_mut(&event.turn)?;
        let mut body = json!({"type":"odion.voice_lab.metric","event":name,"run_id":self.run,"turn_id":format!("turn-{}",event.turn),"attempt_id":format!("attempt-{}",event.attempt),"ts_ms":event.at_us as f64/1000.0,"transport":"livekit","measurement_source":"native_rust_worker","clock":"run_relative"});
        match name {
            "user_speech_end" => {
                turn.speech_end = Some(event.at_us);
                return None;
            }
            "stt_final" => {
                turn.final_at = Some(event.at_us);
                body["transcript_preview"] = json!(event.data["text"]
                    .as_str()
                    .unwrap_or("")
                    .chars()
                    .take(500)
                    .collect::<String>());
                body["transcript_delay_ms"] = elapsed(event.at_us, turn.speech_end);
                body["boundary"] = json!("server_vad_commit_to_final");
            }
            "llm_request_start" => {
                if turn.llm.len() >= 64 {
                    turn.llm.clear();
                }
                turn.llm.insert(event.attempt, event.at_us);
                body["event"] = json!("llm_request_started");
            }
            "llm_first_token" => {
                body["event"] = json!("llm_first_token");
                body["llm_ttft_ms"] = event
                    .data
                    .get("provider_ttft_ms")
                    .cloned()
                    .unwrap_or_else(|| elapsed(event.at_us, turn.llm.get(&event.attempt).copied()));
                if let Some(id) = event.data.get("request_id") {
                    body["request_id"] = id.clone();
                }
                body["boundary"] = json!("provider_request_to_first_usable_text_before_guard");
            }
            "llm_text_released" => {
                body["post_guard_ms"] = elapsed(event.at_us, turn.llm.get(&event.attempt).copied());
                body["boundary"] = json!("llm_dispatch_to_post_guard_text");
            }
            "tts_request_start" => {
                turn.next_tts += 1;
                if turn.tts.len() >= 64 {
                    turn.tts.clear();
                }
                turn.tts.insert(event.attempt, (event.at_us, turn.next_tts));
                body["event"] = json!("tts_request_started");
            }
            "tts_first_pcm" | "tts_request_complete" => {
                let start = turn.tts.get(&event.attempt);
                body["request_id"] = json!(format!(
                    "{}:{}:{}",
                    event.turn,
                    event.attempt,
                    start.map(|(_, n)| *n).unwrap_or(0)
                ));
                if name == "tts_first_pcm" {
                    body["event"] = json!("tts_first_audio");
                    body["ttfa_ms"] = elapsed(event.at_us, start.map(|(s, _)| *s));
                    if turn.first_tts.len() >= 16 {
                        turn.first_tts.clear();
                    }
                    turn.first_tts.insert(
                        event.attempt,
                        (
                            body["ttfa_ms"].clone(),
                            body["request_id"].as_str().unwrap_or("").into(),
                        ),
                    );
                } else {
                    body["event"] = json!("tts_done");
                    body["total_ms"] = elapsed(event.at_us, start.map(|(s, _)| *s));
                    for key in ["audio_seconds", "bytes", "chunks"] {
                        body[key] = event.data[key].clone();
                    }
                    body["pushes"] = event.data["chunks"].clone();
                    body["boundary"] = json!("tts_stream_wall_including_output_backpressure");
                    let total = body["total_ms"].as_f64();
                    let seconds = body["audio_seconds"].as_f64();
                    body["rtf"] = total
                        .zip(seconds)
                        .filter(|(_, s)| *s > 0.0)
                        .map(|(t, s)| json!(t / (s * 1000.0)))
                        .unwrap_or(Value::Null);
                }
            }
            "audio_first_enqueued" => {
                if turn.first || turn.final_at.is_none() {
                    return None;
                }
                turn.first = true;
                if let Some((ttfa, id)) = turn.first_tts.get(&event.attempt) {
                    body["ttfa_ms"] = ttfa.clone();
                    body["request_id"] = json!(id);
                }
                body["event"] = json!("tts_first_audio");
                body["system_since_user_final_ms"] = elapsed(event.at_us, turn.final_at);
                body["boundary"] = json!("server_final_to_first_livekit_enqueue");
            }
            "speculation_start" => body["event"] = json!("preemptive_start"),
            "speculation_authorized" => body["event"] = json!("preemptive_release"),
            "attempt_cancel" => {
                body["event"] = json!("generation_cancelled");
                body["reason"] = event.data["reason"].clone();
            }
            "turn_complete" => {
                body["event"] = json!("turn_closed");
                body["reason"] = json!(event.data["reason"].as_str().unwrap_or(""));
            }
            "llm_first_tool" if event.data["assembled"] == false => return None,
            "llm_first_tool" | "tool_call_prepared" => body["event"] = json!("llm_tool_response"),
            "llm_provider_request_complete" => {
                for key in [
                    "request_id",
                    "request_index",
                    "completed",
                    "first_text_observed",
                    "total_ms",
                    "input_tokens",
                    "output_tokens",
                    "cached_tokens",
                ] {
                    if let Some(v) = event.data.get(key) {
                        body[key] = v.clone();
                    }
                }
            }
            "llm_transfer_guard_decision" | "llm_directory_guard_decision" => {
                for key in ["suppressed", "buffered_events", "text_buffer_ms"] {
                    body[key] = event.data[key].clone();
                }
            }
            _ => return None,
        }
        Some(body)
    }
}
/// A bounded committed text stream never manufactures acoustic voice evidence.
pub async fn read_chat<S, E>(mut stream: S) -> Option<String>
where
    S: Stream<Item = Result<String, E>> + Unpin,
{
    tokio::time::timeout(Duration::from_secs(10), async move {
        let mut text = String::new();
        let mut count = 0;
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.ok()?;
            count += chunk.chars().count();
            if count > 4096 {
                return None;
            }
            text.push_str(&chunk);
        }
        (!text.trim().is_empty()).then_some(text)
    })
    .await
    .ok()
    .flatten()
}

/// Called only after the SDK adapter verifies the packet's caller destination.
pub fn record_playback_ack(trace: &Trace, bytes: &[u8]) {
    if bytes.len() > 4096 {
        return;
    }
    let Ok(value) = serde_json::from_slice::<Value>(bytes) else {
        return;
    };
    if value["type"] != "rvc_browser_playback_start" {
        return;
    }
    let mut body = json!({});
    for key in ["browser_turn_id", "worker_turn_id", "detection_source"] {
        if let Some(text) = value[key].as_str() {
            body[key] = json!(text
                .chars()
                .filter(|c| !c.is_control())
                .take(128)
                .collect::<String>());
        }
    }
    for key in [
        "browser_wall_ms",
        "browser_mono_ms",
        "user_final_wall_ms",
        "user_final_to_heard_ms",
    ] {
        if let Some(number) = value[key]
            .as_f64()
            .filter(|number| number.is_finite() && *number >= 0.0 && *number <= 1e15)
        {
            body[key] = json!(number);
        }
    }
    trace.emit("browser_playback_ack_received", 0, 0, body);
}
#[derive(Default)]
struct PlaybackAcks {
    active: Option<(u64, u64, u64)>,
    acknowledged: VecDeque<u64>,
}
impl PlaybackAcks {
    fn observe(&mut self, event: &TraceEvent) -> Option<(u64, u64, Value)> {
        if event.event == "audio_first_enqueued" && event.turn != 0 {
            self.active = Some((event.turn, event.attempt, event.at_us));
        }
        if event.event == "attempt_cancel"
            && self
                .active
                .is_some_and(|(_, attempt, _)| attempt == event.attempt)
        {
            self.active = None;
        }
        if event.event != "browser_playback_ack_received" {
            return None;
        }
        let (turn, attempt, started) = self.active?;
        if self.acknowledged.contains(&turn)
            || event.at_us < started
            || event.at_us - started > 15_000_000
        {
            return None;
        }
        if event.data["worker_turn_id"]
            .as_str()
            .is_some_and(|id| id != format!("turn-{turn}"))
        {
            return None;
        }
        self.acknowledged.push_back(turn);
        if self.acknowledged.len() > 50 {
            self.acknowledged.pop_front();
        }
        let mut data = event.data.clone();
        data["livekit_enqueue_to_ack_ms"] = elapsed(event.at_us, Some(started));
        data["measurement_source"] = json!("caller_browser_ack");
        Some((turn, attempt, data))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    };
    fn item(name: &str, us: u64, turn: u64, attempt: u64, data: Value) -> TraceEvent {
        TraceEvent {
            event: name.into(),
            at_us: us,
            turn,
            attempt,
            data,
        }
    }
    #[test]
    fn metrics_keep_real_boundaries_unknown_values_and_private_fields_out() {
        let mut mapper = MetricMapper::new("native-test".into());
        assert!(mapper
            .map(&item(
                "private_device_proof",
                1,
                1,
                1,
                json!({"credential":"never forward"})
            ))
            .is_none());
        assert!(mapper
            .map(&item("user_speech_end", 1000, 1, 0, json!({})))
            .is_none());
        let final_ = mapper
            .map(&item(
                "stt_final",
                3000,
                1,
                0,
                json!({"text":"hello","service_token":"do not forward"}),
            ))
            .unwrap();
        assert_eq!(final_["transcript_delay_ms"], 2.0);
        assert!(!final_.to_string().contains("service_token"));
        assert_eq!(
            mapper
                .map(&item("llm_first_token", 3500, 1, 2, json!({})))
                .unwrap()["llm_ttft_ms"],
            Value::Null
        );
        mapper.map(&item(
            "llm_request_start",
            4000,
            1,
            2,
            json!({"prompt":"private system context"}),
        ));
        assert_eq!(
            mapper
                .map(&item("llm_first_token", 9000, 1, 2, json!({})))
                .unwrap()["llm_ttft_ms"],
            5.0
        );
        let output = mapper
            .map(&item("audio_first_enqueued", 12000, 1, 2, json!({})))
            .unwrap();
        assert_eq!(output["system_since_user_final_ms"], 9.0);
        assert_eq!(output["boundary"], "server_final_to_first_livekit_enqueue");
        assert!(output.get("audible_latency_ms").is_none());
        assert!(mapper
            .map(&item("audio_first_enqueued", 13000, 1, 2, json!({})))
            .is_none());
        mapper.map(&item(
            "tts_request_start",
            14000,
            1,
            2,
            json!({"text":"private result"}),
        ));
        assert_eq!(
            mapper
                .map(&item("tts_first_pcm", 16000, 1, 2, json!({})))
                .unwrap()["ttfa_ms"],
            2.0
        );
        let done = mapper
            .map(&item(
                "tts_request_complete",
                24000,
                1,
                2,
                json!({"audio_seconds":0.02,"bytes":960,"chunks":2}),
            ))
            .unwrap();
        assert_eq!(done["rtf"], 0.5);
        assert!(mapper.map(&item("stt_final", 1, 0, 0, json!({}))).is_none());
        for turn in 2..200 {
            mapper.map(&item("stt_final", 20000, turn, 0, json!({"text":"x"})));
        }
        assert_eq!(mapper.turns.len(), 50);
    }
    #[test]
    fn metrics_match_independent_python_contracts() {
        let fixture: Value =
            serde_json::from_str(include_str!("../../fixtures/python-parity-reference.json"))
                .unwrap();
        let mut mapper = MetricMapper::new("run".into());
        for case in fixture["browser_metrics"].as_array().unwrap() {
            let output = mapper.map(&item(
                case["native"].as_str().unwrap(),
                case["at_us"].as_u64().unwrap(),
                1,
                2,
                case["data"].clone(),
            ));
            if case["expected"].is_null() {
                assert!(output.is_none());
            } else {
                let output = output.unwrap();
                for (key, value) in case["expected"].as_object().unwrap() {
                    assert_eq!(&output[key], value, "{} {key}", case["native"]);
                }
            }
        }
    }
    #[test]
    fn browser_acknowledgements_require_actual_output_are_one_use_and_keep_missing_values_unknown()
    {
        let trace = Trace::default();
        record_playback_ack(&trace,br#"{"type":"rvc_browser_playback_start","worker_turn_id":"turn-1","browser_turn_id":"browser-1","proof":"not copied"}"#);
        let mut receipt = trace.events().pop().unwrap();
        assert!(receipt.data.get("proof").is_none());
        assert!(receipt.data.get("user_final_to_heard_ms").is_none());
        let mut acks = PlaybackAcks::default();
        assert!(acks.observe(&receipt).is_none());
        acks.observe(&item("audio_first_enqueued", 1000, 1, 2, json!({})));
        receipt.at_us = 3000;
        let (_, _, data) = acks.observe(&receipt).unwrap();
        assert_eq!(data["livekit_enqueue_to_ack_ms"], 2.0);
        assert!(data.get("user_final_to_heard_ms").is_none());
        assert!(acks.observe(&receipt).is_none());
        acks.observe(&item("audio_first_enqueued", 4000, 2, 3, json!({})));
        assert!(acks.observe(&receipt).is_none());
        receipt.data["worker_turn_id"] = json!("turn-2");
        receipt.at_us = 5000;
        acks.observe(&item("attempt_cancel", 4500, 2, 3, json!({})));
        assert!(acks.observe(&receipt).is_none());
    }
    #[test]
    fn provider_metrics_use_actual_request_boundary_and_never_copy_private_data() {
        let mut map = MetricMapper::new("metric-fixture".into());
        map.map(&item("llm_request_start", 1000, 1, 2, json!({})));
        let first = map
            .map(&item(
                "llm_first_token",
                9000,
                1,
                2,
                json!({"provider_ttft_ms":3.0,"request_id":"llm-2","reasoning_content":"private"}),
            ))
            .unwrap();
        assert_eq!(first["llm_ttft_ms"], 3.0);
        assert_eq!(first["request_id"], "llm-2");
        assert!(first.get("reasoning_content").is_none());
        let complete=map.map(&item("llm_provider_request_complete",10000,1,2,json!({"request_id":"llm-2","request_index":2,"completed":false,"first_text_observed":true,"total_ms":4.0,"input_tokens":42,"cached_tokens":0,"proof":"private"}))).unwrap();
        assert_eq!(complete["input_tokens"], 42);
        assert_eq!(complete["cached_tokens"], 0);
        assert!(complete.get("output_tokens").is_none());
        assert!(complete.get("proof").is_none());
        assert!(map
            .map(&item(
                "llm_first_tool",
                11000,
                1,
                2,
                json!({"assembled":false})
            ))
            .is_none());
        assert_eq!(
            map.map(&item(
                "tool_call_prepared",
                12000,
                1,
                2,
                json!({"arguments":"private"})
            ))
            .unwrap()["event"],
            "llm_tool_response"
        );
    }
    #[tokio::test]
    async fn committed_text_is_bounded_by_unicode_characters_and_deadline() {
        assert_eq!(
            read_chat(futures_util::stream::iter([
                Ok::<_, ()>("A ".into()),
                Ok("voice-free chat".into())
            ]))
            .await
            .as_deref(),
            Some("A voice-free chat")
        );
        assert!(
            read_chat(futures_util::stream::iter([Ok::<_, ()>(" ".into())]))
                .await
                .is_none()
        );
        assert!(
            read_chat(futures_util::stream::iter([Ok::<_, ()>("é".repeat(4097))]))
                .await
                .is_none()
        );
        assert_eq!(
            read_chat(futures_util::stream::iter([Ok::<_, ()>("é".repeat(4096))]))
                .await
                .unwrap()
                .chars()
                .count(),
            4096
        );
        assert!(
            read_chat(futures_util::stream::iter([Err::<String, _>(())]))
                .await
                .is_none()
        );
    }
    #[tokio::test(start_paused = true)]
    async fn unfinished_chat_stream_times_out() {
        let task = tokio::spawn(read_chat(
            futures_util::stream::pending::<Result<String, ()>>(),
        ));
        tokio::task::yield_now().await;
        tokio::time::advance(Duration::from_secs(11)).await;
        assert!(task.await.unwrap().is_none());
    }
    #[derive(Default)]
    struct Slow {
        hold: AtomicBool,
        sent: Mutex<Vec<String>>,
        entered: tokio::sync::Notify,
    }
    impl Transport for Slow {
        fn send(&self, item: Publication) -> SendFuture<'_> {
            Box::pin(async move {
                self.entered.notify_one();
                if self.hold.load(Ordering::SeqCst) {
                    std::future::pending::<()>().await;
                }
                if let Publication::Data { topic, .. } = item {
                    self.sent.lock().unwrap().push(topic);
                }
                Ok(())
            })
        }
    }
    #[tokio::test(start_paused = true)]
    async fn stalled_data_never_blocks_submit_and_private_receipts_report_timeout_or_overflow() {
        let slow = Arc::new(Slow::default());
        slow.hold.store(true, Ordering::SeqCst);
        let trace = Trace::default();
        let publisher = Publisher::spawn(slow.clone(), trace.clone(), "run".into());
        let (tx, rx) = oneshot::channel();
        publisher.submit(
            Publication::Data {
                topic: "attentive.fido2.v1".into(),
                body: json!({"private":"proof"}),
            },
            Some(tx),
        );
        slow.entered.notified().await;
        for i in 0..128 {
            publisher.data("sg-rvc.status", json!({"i":i}));
        }
        let (tx, overflow) = oneshot::channel();
        publisher.submit(
            Publication::Data {
                topic: "attentive.fido2.v1".into(),
                body: json!({}),
            },
            Some(tx),
        );
        assert!(overflow.await.unwrap().is_err());
        tokio::time::advance(Duration::from_secs(1)).await;
        assert!(rx.await.unwrap().is_err());
        publisher.close().await;
        assert!(!trace
            .events()
            .iter()
            .any(|event| event.data.to_string().contains("proof")));
    }
    #[tokio::test]
    #[ignore = "requires the existing loopback-only LiveKit server on port 17880"]
    async fn official_sdk_delivers_standard_transcripts_chat_state_and_private_data() {
        use livekit::webrtc::{audio_source::native::NativeAudioSource, prelude::*};
        use livekit::{data_stream::api::StreamReader, options::TrackPublishOptions};
        use livekit_api::access_token::{AccessToken, VideoGrants};
        let name = format!("sg-rust-wire-{}", uuid::Uuid::new_v4());
        let token = |identity: &str, agent: bool| {
            AccessToken::with_api_key("devkey", "secret")
                .with_identity(identity)
                .with_grants(VideoGrants {
                    room_join: true,
                    room: name.clone(),
                    agent,
                    can_update_own_metadata: Some(true),
                    ..Default::default()
                })
                .to_jwt()
                .unwrap()
        };
        let (caller, mut caller_events) = Room::connect(
            "ws://127.0.0.1:17880",
            &token("wire-caller", false),
            RoomOptions::default(),
        )
        .await
        .unwrap();
        let caller = Arc::new(caller);
        let mut caller_guard = super::super::bootstrap::RoomGuard::new(caller.clone());
        let (agent, mut agent_events) = Room::connect(
            "ws://127.0.0.1:17880",
            &token("wire-agent", true),
            RoomOptions::default(),
        )
        .await
        .unwrap();
        let agent = Arc::new(agent);
        let mut agent_guard = super::super::bootstrap::RoomGuard::new(agent.clone());
        let (observer, mut observer_events) = Room::connect(
            "ws://127.0.0.1:17880",
            &token("wire-observer", false),
            RoomOptions::default(),
        )
        .await
        .unwrap();
        let observer = Arc::new(observer);
        let mut observer_guard = super::super::bootstrap::RoomGuard::new(observer.clone());
        let source = NativeAudioSource::new(AudioSourceOptions::default(), 24000, 1, 240);
        let publication = agent
            .local_participant()
            .publish_track(
                LocalTrack::Audio(LocalAudioTrack::create_audio_track(
                    "wire-output",
                    RtcAudioSource::Native(source),
                )),
                TrackPublishOptions {
                    source: TrackSource::Microphone,
                    ..Default::default()
                },
            )
            .await
            .unwrap();
        tokio::time::timeout(Duration::from_secs(10), async {
            loop {
                if let Some(RoomEvent::TrackSubscribed { participant, .. }) =
                    caller_events.recv().await
                {
                    if participant.identity().as_str() == "wire-agent" {
                        break;
                    }
                }
            }
        })
        .await
        .unwrap();
        let (_, input) = watch::channel(String::new());
        let transport = SdkTransport::new(
            agent.local_participant(),
            "wire-caller".into(),
            publication.sid().to_string(),
            input,
        )
        .unwrap();
        transport
            .send(Publication::State("listening"))
            .await
            .unwrap();
        transport
            .send(Publication::Transcript {
                user: false,
                final_: false,
                text: "Hello".into(),
            })
            .await
            .unwrap();
        transport
            .send(Publication::Transcript {
                user: false,
                final_: true,
                text: "Hello caller.".into(),
            })
            .await
            .unwrap();
        transport
            .send(Publication::Data {
                topic: "attentive.fido2.v1".into(),
                body: json!({"type":"public-fixture-only"}),
            })
            .await
            .unwrap();
        let mut ids = vec![];
        let mut stream = false;
        let mut private = false;
        let mut state = false;
        let mut chat = false;
        let outcome = tokio::time::timeout(Duration::from_secs(10), async {
            while ids.len() < 2 || !stream || !private || !state {
                match caller_events.recv().await.unwrap() {
                    RoomEvent::TranscriptionReceived { segments, .. } => {
                        for segment in segments {
                            if segment.text == "Hello" || segment.text == "Hello caller." {
                                ids.push(segment.id);
                            }
                        }
                    }
                    RoomEvent::TextStreamOpened { reader, topic, .. }
                        if topic == "lk.transcription" =>
                    {
                        let reader = reader.take().unwrap();
                        assert_eq!(reader.info().attributes()["lk.transcription_final"], "true");
                        let id = reader.info().attributes()["lk.segment_id"].clone();
                        assert_eq!(reader.read_all().await.unwrap(), "Hello caller.");
                        if let Some(first) = ids.first() {
                            assert_eq!(first, &id);
                        }
                        stream = true;
                    }
                    RoomEvent::DataReceived { topic, .. }
                        if topic.as_deref() == Some("attentive.fido2.v1") =>
                    {
                        private = true
                    }
                    RoomEvent::ParticipantAttributesChanged { participant, .. }
                        if participant.identity().as_str() == "wire-agent" =>
                    {
                        state = true
                    }
                    _ => {}
                }
            }
            assert_eq!(ids[0], ids[1]);
            caller
                .local_participant()
                .send_text(
                    "A committed chat",
                    SendTextOptions::new_with_topic("lk.chat")
                        .with_destination_identity("wire-agent"),
                )
                .await
                .unwrap();
            loop {
                if let Some(RoomEvent::TextStreamOpened {
                    reader,
                    topic,
                    participant_identity,
                }) = agent_events.recv().await
                {
                    if topic == "lk.chat" {
                        assert_eq!(participant_identity.as_str(), "wire-caller");
                        assert_eq!(
                            read_chat(reader.take().unwrap()).await.as_deref(),
                            Some("A committed chat")
                        );
                        chat = true;
                        break;
                    }
                }
            }
        })
        .await;
        let private_leak = tokio::time::timeout(Duration::from_millis(150), async {
            loop {
                if let Some(RoomEvent::DataReceived { topic, .. }) = observer_events.recv().await {
                    if topic.as_deref() == Some("attentive.fido2.v1") {
                        break;
                    }
                }
            }
        })
        .await;
        agent_guard.close().await;
        caller_guard.close().await;
        observer_guard.close().await;
        assert!(outcome.is_ok(), "standard SDK wire contracts timed out: legacy={ids:?} stream={stream} private={private} state={state} chat={chat}");
        assert!(
            private_leak.is_err(),
            "private packet reached another participant"
        );
    }
}
