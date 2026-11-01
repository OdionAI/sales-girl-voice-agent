//! Synthetic loopback-only native worker integration. No model/business services.
use futures_util::StreamExt;
use livekit::webrtc::{
    audio_source::native::NativeAudioSource, audio_stream::native::NativeAudioStream, prelude::*,
};
use livekit::{options::TrackPublishOptions, prelude::*};
use livekit_api::{
    access_token::{AccessToken, VideoGrants},
    services::{
        agent_dispatch::AgentDispatchClient,
        room::{CreateRoomOptions, RoomClient},
    },
};
use livekit_protocol as proto;
use sg_voice_prototype::{
    playout::{Completion, Error as AudioError, LiveKitSink, Playout},
    worker::{self, AssignedJob, Event, JobHandler, LocalConfig, Run},
};
use std::{
    borrow::Cow,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};
use tokio::sync::{mpsc, watch};
const URL: &str = "ws://127.0.0.1:17880";
struct Ready {
    id: String,
    playout: Playout,
}
struct Handler {
    ready: mpsc::Sender<Ready>,
    input: Arc<AtomicU64>,
    data: Arc<AtomicU64>,
}
impl JobHandler for Handler {
    fn accepts(&self, j: &proto::Job) -> bool {
        j.metadata == "{\"native_local_test\":true}"
            && j.room
                .as_ref()
                .is_some_and(|r| r.name.starts_with("sg-rust-"))
            && !j.enable_recording
            && j.participant.is_none()
    }
    fn run(&self, assigned: AssignedJob, mut cancel: watch::Receiver<bool>) -> Run<'_> {
        Box::pin(async move {
            if !self.accepts(&assigned.job) {
                return Err("unsupported job scope".into());
            }
            let (room, mut events) = tokio::select! {_=cancel.changed()=>return Err("cancelled before connect".into()),r=Room::connect(&assigned.url,&assigned.token,RoomOptions::default())=>r.map_err(|_|"RTC connection failed")?};
            if room.name() != assigned.job.room.as_ref().ok_or("missing room")?.name
                || room.local_participant().identity().to_string()
                    != format!("sg-rust-{}", assigned.job.id)
            {
                let _ = room.close().await;
                return Err("assigned room/identity mismatch".into());
            }
            let source = NativeAudioSource::new(AudioSourceOptions::default(), 24000, 1, 100);
            let playout = Playout::spawn(Arc::new(LiveKitSink(source.clone())), 24000, 1, 24000);
            let track = LocalAudioTrack::create_audio_track(
                "rvc-native-test-output",
                RtcAudioSource::Native(source),
            );
            if room
                .local_participant()
                .publish_track(LocalTrack::Audio(track), TrackPublishOptions::default())
                .await
                .is_err()
            {
                let _ = playout.shutdown().await;
                let _ = room.close().await;
                return Err("publish failed".into());
            }
            if self
                .ready
                .send(Ready {
                    id: assigned.job.id,
                    playout: playout.clone(),
                })
                .await
                .is_err()
            {
                let _ = playout.shutdown().await;
                let _ = room.close().await;
                return Err("harness closed".into());
            }
            let mut input_task = None;
            let result = loop {
                tokio::select! {biased;
                    _=cancel.changed()=>break Err("job terminated".into()),
                    e=events.recv()=>match e{
                        Some(RoomEvent::TrackSubscribed{track:RemoteTrack::Audio(t),..})=>{
                            if input_task.is_some(){break Err("unsupported second input track".into());}
                            let count=self.input.clone();
                            input_task=Some(tokio::spawn(async move{
                                let mut s=NativeAudioStream::new(t.rtc_track(),16000,1);
                                while let Some(f)=s.next().await{if f.data.iter().any(|v|i32::from(*v).abs()>100){count.fetch_add(f.data.len() as u64,Ordering::SeqCst);}}
                            }));
                        },
                        Some(RoomEvent::DataReceived{payload,topic,..})if topic.as_deref()==Some("sg-native-test") && payload.as_slice()==b"caller-data"=>{
                            self.data.fetch_add(1,Ordering::SeqCst);
                            let _=room.local_participant().publish_data(DataPacket{payload:b"worker-data".to_vec(),reliable:true,topic:Some("sg-native-test".into()),..Default::default()}).await;
                        },
                        Some(RoomEvent::Disconnected{..})|None=>break Err("room disconnected".into()),
                        _=>{},
                    }
                }
            };
            if let Some(t) = input_task {
                t.abort();
                let _ = t.await;
            }
            let _ = playout.shutdown().await;
            let _ = room.close().await;
            result
        })
    }
}
async fn wait_event(
    events: &mut mpsc::Receiver<Event>,
    predicate: impl Fn(&Event) -> bool,
) -> Result<Event, String> {
    tokio::time::timeout(Duration::from_secs(15), async {
        while let Some(e) = events.recv().await {
            println!("event {e:?}");
            if predicate(&e) {
                return Ok(e);
            }
        }
        Err("worker event stream closed".into())
    })
    .await
    .map_err(|_| "worker event timeout")?
}
fn tone(rate: u32, index: usize) -> Vec<i16> {
    (0..rate / 100)
        .map(|i| {
            (((index * rate as usize / 100 + i as usize) as f64 * 997.0 * std::f64::consts::TAU
                / rate as f64)
                .sin()
                * 8000.0) as i16
        })
        .collect()
}
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tokio::time::timeout(Duration::from_secs(60), test()).await??;
    Ok(())
}
async fn test() -> Result<(), Box<dyn std::error::Error>> {
    let name = format!("sg-rust-native-{}", std::process::id());
    let agent = format!("sg-rust-worker-{}", std::process::id());
    let api = RoomClient::with_api_key("http://127.0.0.1:17880", "devkey", "secret");
    let dispatch_api =
        AgentDispatchClient::with_api_key("http://127.0.0.1:17880", "devkey", "secret");
    api.create_room(
        &name,
        CreateRoomOptions {
            empty_timeout: 30,
            ..Default::default()
        },
    )
    .await?;
    let token = AccessToken::with_api_key("devkey", "secret")
        .with_identity("sg-rust-caller")
        .with_grants(VideoGrants {
            room_join: true,
            room: name.clone(),
            ..Default::default()
        })
        .to_jwt()?;
    let (caller, mut caller_events) = Room::connect(URL, &token, RoomOptions::default()).await?;
    let (stop, stop_rx) = watch::channel(false);
    let (events, mut worker_events) = mpsc::channel(64);
    let (ready, mut ready_rx) = mpsc::channel(4);
    let input = Arc::new(AtomicU64::new(0));
    let data = Arc::new(AtomicU64::new(0));
    let h = Arc::new(Handler {
        ready,
        input: input.clone(),
        data: data.clone(),
    });
    let worker = tokio::spawn(worker::run(
        LocalConfig::new(17880, agent.clone())?,
        h,
        stop_rx,
        events,
    ));
    wait_event(&mut worker_events, |e| matches!(e, Event::Registered(_))).await?;
    let dispatch = dispatch_api
        .create_dispatch(proto::CreateAgentDispatchRequest {
            agent_name: agent.clone(),
            room: name.clone(),
            metadata: "{\"native_local_test\":true}".into(),
            ..Default::default()
        })
        .await?;
    let ready = tokio::time::timeout(Duration::from_secs(15), ready_rx.recv())
        .await?
        .ok_or("job never joined")?;
    let output = tokio::time::timeout(Duration::from_secs(10), async {
        loop {
            if let Some(RoomEvent::TrackSubscribed {
                track: RemoteTrack::Audio(t),
                ..
            }) = caller_events.recv().await
            {
                break t;
            }
        }
    })
    .await?;
    let received = Arc::new(AtomicU64::new(0));
    let receiver = {
        let count = received.clone();
        tokio::spawn(async move {
            let mut s = NativeAudioStream::new(output.rtc_track(), 24000, 1);
            while let Some(f) = s.next().await {
                if f.data.iter().any(|v| i32::from(*v).abs() > 100) {
                    count.fetch_add(f.data.len() as u64, Ordering::SeqCst);
                }
            }
        })
    };
    let mic = NativeAudioSource::new(AudioSourceOptions::default(), 16000, 1, 100);
    caller
        .local_participant()
        .publish_track(
            LocalTrack::Audio(LocalAudioTrack::create_audio_track(
                "synthetic-microphone",
                RtcAudioSource::Native(mic.clone()),
            )),
            TrackPublishOptions::default(),
        )
        .await?;
    let microphone = tokio::spawn(async move {
        for i in 0..500 {
            let p = tone(16000, i);
            mic.capture_frame(&AudioFrame {
                data: Cow::Owned(p),
                sample_rate: 16000,
                num_channels: 1,
                samples_per_channel: 160,
            })
            .await
            .unwrap();
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    });
    caller
        .local_participant()
        .publish_data(DataPacket {
            payload: b"caller-data".to_vec(),
            reliable: true,
            topic: Some("sg-native-test".into()),
            ..Default::default()
        })
        .await?;
    tokio::time::timeout(Duration::from_secs(10), async {
        loop {
            if let Some(RoomEvent::DataReceived { payload, .. }) = caller_events.recv().await {
                if payload.as_slice() == b"worker-data" {
                    break;
                }
            }
        }
    })
    .await?;
    let p = ready.playout;
    p.begin(1).await.map_err(|e| format!("begin {e:?}"))?;
    assert_eq!(
        p.push(1, tone(24000, 0)).await,
        Err(AudioError::Unauthorized)
    );
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(received.load(Ordering::SeqCst), 0);
    p.authorize(1)
        .await
        .map_err(|e| format!("authorize {e:?}"))?;
    for i in 0..30 {
        p.push(1, tone(24000, i))
            .await
            .map_err(|e| format!("PCM {e:?}"))?;
    }
    let finish = Instant::now();
    assert_eq!(p.finish(1).await, Ok(Completion::LocalQueueDrained));
    let queue_wait_ms = finish.elapsed().as_secs_f64() * 1000.0;
    tokio::time::timeout(Duration::from_secs(5), async {
        while received.load(Ordering::SeqCst) == 0 {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await?;
    p.begin(2).await.map_err(|e| format!("begin2 {e:?}"))?;
    p.authorize(2).await.map_err(|e| format!("auth2 {e:?}"))?;
    let send = tokio::spawn({
        let p = p.clone();
        async move { p.push(2, vec![8000; 24000]).await }
    });
    tokio::time::sleep(Duration::from_millis(30)).await;
    let cancel = Instant::now();
    p.cancel(2).await.map_err(|e| format!("cancel {e:?}"))?;
    let cancel_ms = cancel.elapsed().as_secs_f64() * 1000.0;
    let _ = send.await?;
    assert_eq!(p.push(2, tone(24000, 1)).await, Err(AudioError::Stale));
    p.begin(3).await.map_err(|e| format!("begin3 {e:?}"))?;
    p.authorize(3).await.map_err(|e| format!("auth3 {e:?}"))?;
    p.speech_decision(3, false)
        .await
        .map_err(|e| format!("quiet {e:?}"))?;
    let before = input.load(Ordering::SeqCst);
    tokio::time::timeout(Duration::from_secs(3), async {
        while input.load(Ordering::SeqCst) <= before {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await?;
    assert_eq!(
        p.push(3, tone(24000, 1)).await,
        Err(AudioError::Unauthorized)
    );
    dispatch_api.delete_dispatch(&dispatch.id, &name).await?;
    wait_event(
        &mut worker_events,
        |e| matches!(e,Event::Ended(id,_)if id==&ready.id),
    )
    .await?;
    assert_eq!(p.push(3, tone(24000, 1)).await, Err(AudioError::Closed));
    // Verify graceful worker shutdown closes an active native RTC job too.
    dispatch_api
        .create_dispatch(proto::CreateAgentDispatchRequest {
            agent_name: agent,
            room: name.clone(),
            metadata: "{\"native_local_test\":true}".into(),
            ..Default::default()
        })
        .await?;
    let second = tokio::time::timeout(Duration::from_secs(10), ready_rx.recv())
        .await?
        .ok_or("second assignment failed")?;
    stop.send(true)?;
    tokio::time::timeout(Duration::from_secs(5), worker)
        .await??
        .map_err(|e| format!("worker {e}"))?;
    assert_eq!(
        second.playout.push(1, tone(24000, 1)).await,
        Err(AudioError::Closed)
    );
    microphone.abort();
    let _ = microphone.await;
    receiver.abort();
    let _ = receiver.await;
    caller.close().await?;
    api.delete_room(&name).await?;
    println!(
        "{}",
        serde_json::json!({"native_worker_dispatch":true,"termination":true,"active_job_shutdown":true,"room_connection":true,"microphone_pcm_samples":input.load(Ordering::SeqCst),"published_pcm_received_samples":received.load(Ordering::SeqCst),"data_events":data.load(Ordering::SeqCst),"local_queue_finish_wait_ms":queue_wait_ms,"cancel_control_ms":cancel_ms,"stale_audio_rejected":true,"speculative_audio_blocked":true,"listening_retains_input":true,"remote_audible_completion":null,"python_bridge":false})
    );
    Ok(())
}
