//! Native RVC worker. Loopback review and explicit operator modes are separate.
use futures_util::StreamExt;
use livekit::webrtc::{
    audio_source::native::NativeAudioSource, audio_stream::native::NativeAudioStream, prelude::*,
};
use livekit::{options::TrackPublishOptions, prelude::*};
use sg_voice_prototype::{
    playout::{LiveKitSink, Playout},
    rvc::{
        browser::{self, Publication, Publisher, SdkTransport},
        coordinator::{self, Input, Output},
        Config, Trace,
    },
    worker::{self, AssignedJob, Event, JobHandler, LocalConfig, Run},
};
use std::{path::PathBuf, sync::Arc, time::Duration};
use tokio::{
    sync::{mpsc, watch},
    task::JoinSet,
};
struct AbortTask<T>(tokio::task::JoinHandle<T>);
impl<T> Drop for AbortTask<T> {
    fn drop(&mut self) {
        self.0.abort();
    }
}
async fn shutdown_signal() -> std::io::Result<()> {
    #[cfg(unix)]
    {
        let mut terminate =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
        tokio::select! { result = tokio::signal::ctrl_c() => result, _ = terminate.recv() => Ok(()) }
    }
    #[cfg(not(unix))]
    tokio::signal::ctrl_c().await
}
struct Handler {
    operator: Option<Arc<sg_voice_prototype::rvc::bootstrap::OperatorConfig>>,
    config: Arc<Config>,
    directory: PathBuf,
    verifier: sg_voice_prototype::rvc::identity::Verifier,
}
impl JobHandler for Handler {
    fn accepts(&self, j: &livekit_protocol::Job) -> bool {
        let room = j.room.as_ref().map(|r| r.name.as_str()).unwrap_or("");
        let scoped = if let Some(operator) = &self.operator {
            operator.accepts(&j.metadata, room)
        } else {
            room.starts_with("sg-rvc-")
                && (j.metadata == "native-rvc-one-call"
                    || self.config.platform.is_some()
                        && j.metadata.len() <= 20 * 1024
                        && serde_json::from_str::<serde_json::Value>(&j.metadata)
                            .ok()
                            .is_some_and(|v| v["session_token"].is_string()))
        };
        scoped
            && !j.id.is_empty()
            && j.id.len() <= 128
            && j.id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
            && !j.enable_recording
            && j.participant.is_none()
    }
    fn run(&self, j: AssignedJob, mut cancel: watch::Receiver<bool>) -> Run<'_> {
        Box::pin(async move {
            if !self.accepts(&j.job) {
                return Err("unsupported dispatch scope".into());
            }
            if *cancel.borrow() {
                return Ok(());
            }
            let trace = Trace::synchronous(self.directory.join(format!("{}.jsonl", j.job.id)))
                .map_err(|_| "trace open failed")?;
            let _report = sg_voice_prototype::rvc::report::OnClose::new(
                trace.clone(),
                self.config
                    .report_dir
                    .clone()
                    .unwrap_or_else(|| self.directory.join("reports"))
                    .join(format!("{}.md", j.job.id)),
            );
            let room_name = &j.job.room.as_ref().ok_or("missing room")?.name;
            let sip_dispatch = self
                .operator
                .as_ref()
                .map(|op| op.sip_dispatch(&j.job.metadata, room_name))
                .transpose()?
                .flatten();
            let (mut config, mut services, mut caller) = if sip_dispatch.is_some()
                || (self.operator.is_none() && j.job.metadata == "native-rvc-one-call")
            {
                (self.config.clone(), None, "sg-rvc-caller".to_string())
            } else {
                let now = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map_err(|_| "clock unavailable")?
                    .as_secs_f64();
                let prepared = tokio::select! {biased;_=cancel.changed()=>return Ok(()),result=sg_voice_prototype::rvc::platform::prepare_call((*self.config).clone(),&j.job.metadata,room_name,&self.verifier,now,trace.clone())=>result?};
                (
                    Arc::new(prepared.config),
                    Some(prepared.services),
                    prepared.caller,
                )
            };
            let (room, mut events) = tokio::select! { biased;
                _ = cancel.changed() => return Ok(()),
                result = Room::connect(&j.url, &j.token, RoomOptions::default()) => result.map_err(|_| "RTC connection failed")?,
            };
            let room = Arc::new(room);
            let mut room_guard = sg_voice_prototype::rvc::bootstrap::RoomGuard::new(room.clone());
            if room.name() != *room_name
                || room.local_participant().identity().to_string()
                    != format!("sg-rust-{}", j.job.id)
            {
                return Err("assigned room mismatch".into());
            }
            let mut lease = None;
            let mut outbound = None;
            if let (Some(operator), Some(dispatch)) = (&self.operator, &sip_dispatch) {
                let participant =
                    wait_caller(&room, &mut events, &mut cancel, Some(dispatch), None).await?;
                let call = tokio::select! {biased;_=cancel.changed()=>return Ok(()),result=operator.prepare_sip(dispatch,&room,&participant,trace.clone())=>result?};
                config = Arc::new(call.prepared.config);
                services = Some(call.prepared.services);
                caller = call.prepared.caller;
                lease = call.capacity;
                outbound = call.outbound;
            } else if self.operator.is_some() {
                // A valid signed identity still needs its actual SDK participant.
                wait_caller(&room, &mut events, &mut cancel, None, Some(&caller)).await?;
            }
            let capacity = Arc::new(tokio::sync::Mutex::new(lease));
            let mut resolved_config = (*config).clone();
            resolved_config.client_session_id = j.job.id.clone();
            let (api_url, api_key, api_secret) = self
                .operator
                .as_ref()
                .map(|op| {
                    (
                        op.worker.server_url.as_str(),
                        op.worker.api_key.as_str(),
                        op.worker.api_secret.as_str(),
                    )
                })
                .unwrap_or(("http://127.0.0.1:17880", "devkey", "secret"));
            if self.operator.is_some() {
                resolved_config.recording.api_url = api_url.into();
                resolved_config.recording.api_key = api_key.into();
                resolved_config.recording.api_secret = api_secret.into();
            }
            let api_root = api_url
                .trim_end_matches('/')
                .replacen("wss://", "https://", 1)
                .replacen("ws://", "http://", 1);
            let config = Arc::new(resolved_config);
            let source = NativeAudioSource::new(AudioSourceOptions::default(), 24000, 1, 240);
            let track = LocalAudioTrack::create_audio_track(
                "rvc-native-output",
                RtcAudioSource::Native(source.clone()),
            );
            let publication = room
                .local_participant()
                .publish_track(
                    LocalTrack::Audio(track),
                    TrackPublishOptions {
                        source: TrackSource::Microphone,
                        ..Default::default()
                    },
                )
                .await
                .map_err(|_| "output publication failed")?;
            let (input_track_tx, input_track_rx) = watch::channel(String::new());
            let publisher = Publisher::spawn(
                Arc::new(SdkTransport::new(
                    room.local_participant(),
                    caller.clone(),
                    publication.sid().to_string(),
                    input_track_rx,
                )?),
                trace.clone(),
                j.job.id.clone(),
            );
            publisher.submit(Publication::State("initializing"), None);
            let playout = Playout::spawn(Arc::new(LiveKitSink(source)), 24000, 1, 24000);
            let bridge = sg_voice_prototype::rvc::transfer::Client::new(
                config.transfer.clone(),
                api_url,
                api_key,
                api_secret,
                trace.clone(),
            )?;
            let scope = services.as_ref().map(|services| services.scope().clone());
            let (session, mut output, task) =
                coordinator::start_with_services(config, playout, trace.clone(), true, services);
            let mut controls = JoinSet::new();
            let mut established = None;
            let mut worker_departing = false;
            let mut task = AbortTask(task);
            let mut input: Option<AbortTask<()>> = None;
            let mut input_track = String::new();
            let (ready_tx, ready_rx) = watch::channel(false);
            // A microphone may already be subscribed while SIP/profile startup ran.
            if let Some(participant) = room
                .remote_participants()
                .values()
                .find(|p| p.identity().as_str() == caller)
            {
                for publication in participant.track_publications().values() {
                    if publication.source() == TrackSource::Microphone {
                        if let Some(RemoteTrack::Audio(track)) = publication.track() {
                            input_track = publication.sid().to_string();
                            input = Some(input_task(track, session.clone()));
                            break;
                        }
                    }
                }
            }
            if self.operator.is_some() {
                let _ = session.input.send(Input::ClientReady).await;
            }
            let _ = input_track_tx.send(input_track.clone());
            let result = loop {
                tokio::select! {biased;
                 _=cancel.changed()=>{worker_departing=true;break Ok(());},
                 _=controls.join_next(),if !controls.is_empty()=>{},
                 message=output.recv()=>match message{
                  Some(Output::Transfer{reason,mut reply,cleanup})=>{
                   let bridge=bridge.clone();let session=session.clone();let scope=scope.clone();let room=room.name();let id=j.job.id.clone();
                   controls.spawn(async move {
                    tokio::select! {biased;_=reply.closed()=>{let _=cleanup.send(true);return;},_=session.handoff_snapshot(&reason,serde_json::json!({"status":"connecting"}))=>{}}
                    let id=session.canonical_ids().map(|(session,_)|session).unwrap_or(id);
                    let result=tokio::select! {biased;_=reply.closed()=>{let _=bridge.drain_cleanup().await;return;},result=bridge.connect_notified(&room,&id,scope.as_ref(),cleanup)=>result};
                    let failed=result.as_ref().err().cloned();
                    let _=reply.send(result);
                    if let Some(failure)=failed{session.handoff_snapshot(&reason,failure).await;}
                    let _=bridge.drain_cleanup().await;
                   });
                  },
                  Some(Output::Handoff{reason,connection})=>{
                   let receipt=connection.receipt().clone();
                   if established.is_none(){established=Some(connection);}else{drop(connection);}
                   let session=session.clone();let capacity=capacity.clone();controls.spawn(async move{
                    let lease=capacity.lock().await.take();if let Some(mut lease)=lease{lease.release().await;}
                    session.handoff_snapshot(&reason,receipt).await;
                   });
                  },
                  Some(Output::EndCall{reason})=>{
                   let client=livekit_api::services::room::RoomClient::with_api_key(&api_root,api_key,api_secret);
                   trace.emit("livekit_room_delete_requested",0,0,serde_json::json!({"reason":reason}));
                   let room=room.name();let session=session.clone();
                   controls.spawn(async move{if !matches!(tokio::time::timeout(Duration::from_secs(5),client.delete_room(&room)).await,Ok(Ok(_))){let _=session.input.send(Input::EndCallFailed{reason}).await;}});
                  },
                  Some(Output::ReliableEvent{body,receipt})=>{
                   if let Some(topic)=body["type"].as_str(){publisher.submit(Publication::Data{topic:topic.into(),body:body["payload"].clone()},Some(receipt));}else{let _=receipt.send(Err("missing private topic".into()));}
                  },
                  Some(Output::Event(body))=>{if let Some(topic)=body["type"].as_str(){publisher.data(topic,body["payload"].clone());}},
                  Some(message)=>{
                   match &message{
                    Output::Ready=>{let _=ready_tx.send(true);publisher.submit(Publication::State("listening"),None);},
                    Output::Partial(text)=>publisher.submit(Publication::Transcript{user:true,final_:false,text:text.clone()},None),
                    Output::Final(text)=>publisher.submit(Publication::Transcript{user:true,final_:true,text:text.clone()},None),
                    Output::AnswerPartial(text)=>publisher.submit(Publication::Transcript{user:false,final_:false,text:text.clone()},None),
                    Output::Answer(text)=>publisher.submit(Publication::Transcript{user:false,final_:true,text:text.clone()},None),
                    _=>{}
                   }
                   let body=match message{Output::Ready=>serde_json::json!({"type":"ready"}),Output::Partial(t)=>serde_json::json!({"type":"partial","text":t}),Output::Final(t)=>serde_json::json!({"type":"final","text":t}),Output::AnswerPartial(t)=>serde_json::json!({"type":"answer_partial","text":t}),Output::Answer(t)=>serde_json::json!({"type":"answer","text":t}),Output::Complete{turn}=>serde_json::json!({"type":"complete","turn":turn}),Output::Status(e)=>serde_json::json!({"type":"status","content":e}),Output::Error(e)=>serde_json::json!({"type":"error","error":e}),_=>continue};
                   publisher.data("sg-rvc.status",body);
                 },None=>break Err("coordinator closed".into())
                 },
                 event=events.recv()=>match event{
                  Some(RoomEvent::TrackSubscribed{track:RemoteTrack::Audio(track),participant,publication,..})if participant.identity().as_str()==caller&&publication.source()==TrackSource::Microphone=>{
                   if input_track!=publication.sid().as_str(){
                    if let Some(mut old)=input.take(){old.0.abort();let _=(&mut old.0).await;}
                    input_track=publication.sid().to_string();let _=input_track_tx.send(input_track.clone());input=Some(input_task(track,session.clone()));
                   }
                  },
                  Some(RoomEvent::TrackUnsubscribed{participant,publication,..})if participant.identity().as_str()==caller&&publication.sid().as_str()==input_track=>{
                   if let Some(mut old)=input.take(){old.0.abort();let _=(&mut old.0).await;}input_track.clear();let _=input_track_tx.send(String::new());
                  },
                  Some(RoomEvent::DataReceived{payload,participant,topic,..})if topic.as_deref()==Some("rvc_latency_ack")&&participant.as_ref().is_some_and(|p|p.identity().as_str()==caller)=>{browser::record_playback_ack(&trace,&payload);},
                  Some(RoomEvent::DataReceived{payload,participant,topic,..})if topic.as_deref()==Some("attentive.fido2.v1")&&participant.as_ref().is_some_and(|p|p.identity().as_str()==caller)=>{if payload.len()<=14000{let _=session.input.send(Input::DeviceFrame(payload.to_vec())).await;}},
                  Some(RoomEvent::DataReceived{payload,participant,topic,..})if topic.as_deref()==Some("sg-rvc.control")&&participant.as_ref().is_some_and(|p|p.identity().as_str()==caller)=>{
                   if let Ok(v)=serde_json::from_slice::<serde_json::Value>(&payload){
                    if v["type"]=="client_ready"{let _=session.input.send(Input::ClientReady).await;}
                    else if v["type"]=="text"{if let Some(text)=v["content"].as_str().or_else(||v["text"].as_str()){let _=session.input.send(Input::Text(text.into())).await;}}
                   }
                  },
                  Some(RoomEvent::TextStreamOpened{reader,topic,participant_identity}) if topic=="lk.chat" && participant_identity.as_str()==caller && controls.len()<32=>{
                   if let Some(reader)=reader.take(){let session=session.clone();let mut ready=ready_rx.clone();controls.spawn(async move{if let Some(text)=browser::read_chat(reader).await{if ready.wait_for(|ready|*ready).await.is_ok(){let _=session.input.send(Input::Text(text)).await;}}});}
                  },
                  Some(RoomEvent::ParticipantDisconnected(p))if p.identity().as_str()==caller=>break Ok(()),
                  Some(RoomEvent::Disconnected{..})|None=>break Ok(()),_=>{}
                 }
                }
            };
            if let Some(mut input) = input {
                input.0.abort();
                let _ = (&mut input.0).await;
            }
            // Closing the actor must not block behind an unread output channel.
            let mut drain = AbortTask(tokio::spawn(async move {
                while let Some(message) = output.recv().await {
                    match message {
                        Output::ReliableEvent { receipt, .. } => {
                            let _ = receipt.send(Err("call closed".into()));
                        }
                        Output::Transfer { reply, cleanup, .. } => {
                            let _ = cleanup.send(true);
                            let _ = reply.send(Err(
                                serde_json::json!({"status":"failed","error":"call_closed"}),
                            ));
                        }
                        Output::Handoff { connection, .. } if worker_departing => {
                            connection.detach();
                        }
                        _ => {}
                    }
                }
            }));
            let _ = tokio::time::timeout(Duration::from_secs(120), session.close()).await;
            let _ = tokio::time::timeout(Duration::from_secs(5), &mut task.0).await;
            if tokio::time::timeout(Duration::from_secs(12), async {
                while controls.join_next().await.is_some() {}
            })
            .await
            .is_err()
            {
                controls.abort_all();
                while controls.join_next().await.is_some() {}
            }
            if let Some(connection) = established.take() {
                if worker_departing {
                    connection.detach();
                } else {
                    drop(connection);
                }
            }
            let _ = bridge.drain_cleanup().await;
            let lease = capacity.lock().await.take();
            if let Some(mut lease) = lease {
                lease.release().await;
            }
            if let Some(bound) = outbound.as_mut() {
                let conversation = session
                    .canonical_ids()
                    .map(|(_, conversation)| conversation);
                bound
                    .finish(
                        if result.is_ok() {
                            "completed"
                        } else {
                            "failed"
                        },
                        result.as_ref().err().map(String::as_str),
                        conversation.as_deref(),
                    )
                    .await;
            }
            let _ = tokio::time::timeout(Duration::from_secs(2), &mut drain.0).await;
            publisher.close().await;
            trace
                .save(&self.directory.join(format!("{}.jsonl", j.job.id)))
                .map_err(|_| "trace save failed")?;
            room_guard.close().await;
            result
        })
    }
}
async fn wait_caller(
    room: &Room,
    events: &mut mpsc::UnboundedReceiver<RoomEvent>,
    cancel: &mut watch::Receiver<bool>,
    sip: Option<&sg_voice_prototype::rvc::sip::Dispatch>,
    caller: Option<&str>,
) -> Result<RemoteParticipant, String> {
    let wait = async {
        loop {
            let candidates = room
                .remote_participants()
                .into_values()
                .filter(|p| {
                    sip.map(|s| s.accepts(p))
                        .unwrap_or_else(|| caller == Some(p.identity().as_str()))
                })
                .collect::<Vec<_>>();
            if candidates.len() > 1 {
                return Err("ambiguous verified caller".into());
            }
            if let Some(participant) = candidates.into_iter().next() {
                return Ok(participant);
            }
            tokio::select! {biased;_=cancel.changed()=>return Err("caller bootstrap cancelled".into()),event=events.recv()=>match event{Some(RoomEvent::Disconnected{..})|None=>return Err("room closed before caller".into()),_=>{}}}
        }
    };
    tokio::time::timeout(Duration::from_secs(30), wait)
        .await
        .map_err(|_| "verified caller did not join")?
}
fn input_task(track: RemoteAudioTrack, session: coordinator::Session) -> AbortTask<()> {
    AbortTask(tokio::spawn(async move {
        let mut stream = NativeAudioStream::new(track.rtc_track(), 16000, 1);
        let mut pending = Vec::new();
        while let Some(frame) = stream.next().await {
            pending.extend_from_slice(&frame.data);
            while pending.len() >= 1600 {
                let tail = pending.split_off(1600);
                let block = std::mem::replace(&mut pending, tail);
                if session.pcm(block).await.is_err() {
                    return;
                }
            }
        }
        if !pending.is_empty() {
            let _ = session.pcm(pending).await;
        }
    }))
}
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    if args.get(1).is_some_and(|s| s == "--check-env") {
        sg_voice_prototype::rvc::environment::from_environment()?;
        println!("native_environment_valid");
        return Ok(());
    }
    if args.get(1).is_some_and(|s| s == "--healthcheck") {
        use std::io::{Read, Write};
        let port = std::env::var("RVC_LAB_LIVEKIT_WORKER_PORT")
            .unwrap_or_else(|_| "8084".into())
            .parse::<u16>()?;
        let mut stream = std::net::TcpStream::connect_timeout(
            &std::net::SocketAddr::from(([127, 0, 0, 1], port)),
            Duration::from_secs(2),
        )?;
        stream.set_read_timeout(Some(Duration::from_secs(2)))?;
        stream
            .write_all(b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")?;
        let mut bytes = [0; 128];
        let n = stream.read(&mut bytes)?;
        if !bytes[..n].starts_with(b"HTTP/1.1 200 ") {
            return Err("worker is not registered".into());
        }
        return Ok(());
    }
    let environment = args.get(1).is_some_and(|s| s == "--env");
    if if environment {
        !(2..=3).contains(&args.len())
    } else {
        args.len() != 4
    } {
        return Err("usage: rvc-worker CONFIG_JSON TRACE_DIRECTORY sg-rust-AGENT_NAME | rvc-worker --platform OPERATOR_JSON TRACE_DIRECTORY".into());
    }
    let production = environment || args[1] == "--platform";
    let (operator, config, directory, local_name) = if production {
        let op: sg_voice_prototype::rvc::bootstrap::OperatorConfig = if environment {
            sg_voice_prototype::rvc::environment::from_environment()?
        } else {
            serde_json::from_slice(&std::fs::read(&args[2])?)
                .map_err(|_| "invalid operator configuration schema")?
        };
        op.validate()?;
        let config = Arc::new(op.voice.clone());
        (
            Some(Arc::new(op)),
            config,
            if environment {
                args.get(2).map(PathBuf::from).unwrap_or_else(|| {
                    PathBuf::from(
                        std::env::var("RVC_LAB_TRACE_DIR")
                            .unwrap_or_else(|_| "/app/latency_lab/artifacts/traces".into()),
                    )
                })
            } else {
                PathBuf::from(&args[3])
            },
            None,
        )
    } else {
        (
            None,
            Arc::new(Config::load(std::path::Path::new(&args[1]))?),
            PathBuf::from(&args[2]),
            Some(args[3].clone()),
        )
    };
    std::fs::create_dir_all(&directory)?;
    let handler = Arc::new(Handler {
        operator: operator.clone(),
        config,
        directory,
        verifier: Default::default(),
    });
    let (stop, rx) = watch::channel(false);
    let registered = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let _health = if production {
        let port = std::env::var("RVC_LAB_LIVEKIT_WORKER_PORT")
            .unwrap_or_else(|_| "8084".into())
            .parse::<u16>()?;
        let listener =
            tokio::net::TcpListener::bind((std::net::Ipv4Addr::UNSPECIFIED, port)).await?;
        let registered = registered.clone();
        Some(AbortTask(tokio::spawn(async move {
            use tokio::io::{AsyncReadExt, AsyncWriteExt};
            while let Ok((mut socket, _)) = listener.accept().await {
                let registered = registered.clone();
                tokio::spawn(async move {
                    let mut data = [0; 1024];
                    let ok = matches!(tokio::time::timeout(Duration::from_secs(2),socket.read(&mut data)).await,Ok(Ok(n)) if n>0 && (data[..n].starts_with(b"GET /health ") || data[..n].starts_with(b"GET / ")));
                    let status = if !ok {
                        "404 Not Found"
                    } else if registered.load(std::sync::atomic::Ordering::SeqCst) {
                        "200 OK"
                    } else {
                        "503 Service Unavailable"
                    };
                    let reply = format!(
                        "HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    );
                    let _ = tokio::time::timeout(
                        Duration::from_secs(2),
                        socket.write_all(reply.as_bytes()),
                    )
                    .await;
                });
            }
        })))
    } else {
        None
    };
    let (tx, mut events) = mpsc::channel(64);
    let mut run = if let Some(operator) = operator {
        tokio::spawn(worker::run_production(
            operator.worker.clone(),
            handler,
            rx,
            tx,
        ))
    } else {
        tokio::spawn(worker::run(
            LocalConfig::new(17880, local_name.ok_or("missing local agent name")?)?,
            handler,
            rx,
            tx,
        ))
    };
    let signal = shutdown_signal();
    tokio::pin!(signal);
    loop {
        tokio::select! { biased;
            result = &mut signal => { result?; break; },
            result = &mut run => { return result?.map_err(|e|format!("worker: {e}").into()); },
            event = events.recv() => match event {
                Some(Event::Registered(_)) => {registered.store(true,std::sync::atomic::Ordering::SeqCst);println!("worker_ready");},
                Some(Event::Disconnected|Event::Stopped)=>{registered.store(false,std::sync::atomic::Ordering::SeqCst);},
                Some(Event::Ended(_, _)) if !production => break,
                None => break,
                _ => {},
            }
        }
    }
    let _ = stop.send(true);
    registered.store(false, std::sync::atomic::Ordering::SeqCst);
    run.await?.map_err(|e| format!("worker: {e}"))?;
    Ok(())
}
