use super::coordinator::{Input, Output};
use super::*;
use crate::playout::{AudioSink, Capture, Playout};
use axum::{
    body::Body,
    extract::State,
    response::{IntoResponse, Response},
    routing::post,
    Json, Router,
};
use futures_util::StreamExt;
use serde_json::json;
use std::{
    convert::Infallible,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::net::TcpListener;
#[derive(Default)]
struct Fixture {
    requests: Mutex<Vec<Value>>,
    hold_idle: std::sync::atomic::AtomicBool,
    idle_started: tokio::sync::Notify,
    idle_release: tokio::sync::Notify,
    idle_silent: std::sync::atomic::AtomicBool,
    idle_invalid: std::sync::atomic::AtomicBool,
    tts_fail: std::sync::atomic::AtomicBool,
    hold_knowledge: std::sync::atomic::AtomicBool,
    knowledge_started: tokio::sync::Notify,
    knowledge_release: tokio::sync::Notify,
    hold_ticket: std::sync::atomic::AtomicBool,
    ticket_started: tokio::sync::Notify,
    ticket_release: tokio::sync::Notify,
    fail_ticket: std::sync::atomic::AtomicBool,
    hold_bank: std::sync::atomic::AtomicBool,
    bank_started: tokio::sync::Notify,
    bank_release: tokio::sync::Notify,
    private_packets: Mutex<Vec<Value>>,
    directory_reads: std::sync::atomic::AtomicUsize,
    directory_started: tokio::sync::Notify,
    directory_release: tokio::sync::Notify,
    hold_directory: std::sync::atomic::AtomicBool,
    directory_fail: std::sync::atomic::AtomicBool,
    directory_mails: Mutex<Vec<Value>>,
    mail_started: tokio::sync::Notify,
    mail_release: tokio::sync::Notify,
    hold_mail: std::sync::atomic::AtomicBool,
    mail_unknown: std::sync::atomic::AtomicBool,
}
async fn llm(State(s): State<Arc<Fixture>>, Json(body): Json<Value>) -> Response {
    s.requests.lock().unwrap().push(body.clone());
    let text = body["messages"].as_array().unwrap().last().unwrap()["content"]
        .as_str()
        .unwrap()
        .to_string();
    let system = body["messages"][0]["content"].as_str().unwrap_or("");
    let directory_request = body["messages"]
        .as_array()
        .unwrap()
        .iter()
        .rev()
        .filter(|m| m["role"] == "user")
        .filter_map(|m| m["content"].as_str())
        .find(|s| s.starts_with("directory "))
        .unwrap_or(&text);
    let background = if system.starts_with("You decide whether")
        || system.contains("You decide whether a voice assistant")
    {
        s.idle_started.notify_one();
        if s.hold_idle.load(std::sync::atomic::Ordering::SeqCst) {
            s.idle_release.notified().await;
        }
        if s.idle_invalid.load(std::sync::atomic::Ordering::SeqCst) {
            Some("invalid JSON".into())
        } else if s.idle_silent.load(std::sync::atomic::Ordering::SeqCst) {
            Some(json!({"speak":false,"text":"","quiet":"none"}).to_string())
        } else {
            let payload: Value = serde_json::from_str(&text).unwrap();
            let text = match payload["reason"].as_str() {
                Some("tool_started" | "tool_wait") => "I'm checking that request for you.",
                Some("tool_completed") => {
                    if payload["operation"]["tool"]
                        .as_str()
                        .is_some_and(|s| s.starts_with("wema_"))
                    {
                        "Your balance is 100 naira."
                    } else {
                        "The support ticket has been created."
                    }
                }
                _ => "Still here if you need me.",
            };
            Some(json!({"speak":true,"text":text,"quiet":"none"}).to_string())
        }
    } else if system.starts_with("Decide what the caller's CURRENT reply means")
        || system.starts_with("Interpret a caller's current response")
    {
        let payload: Value = serde_json::from_str(&text).unwrap();
        let reply = payload["current_caller_reply"].as_str().unwrap();
        Some(
            if reply.contains("reject request") {
                "rejected"
            } else if reply.contains("revise request") {
                "revised"
            } else if reply.contains("unclear request") {
                "unclear"
            } else {
                "confirmed"
            }
            .into(),
        )
    } else if system.contains("State the supplied application message faithfully") {
        Some("Je vous mets en relation maintenant.".into())
    } else if system.starts_with("You are speaking as the caller's assistant") {
        Some("Would you like me to create that support ticket?".into())
    } else if system.starts_with("Summarize older conversation") {
        Some(json!({"topics":["Older context"],"facts":["No transaction confirmed"],"corrections":[],"open_questions":[]}).to_string())
    } else {
        None
    };
    let item = if let Some(content) = background {
        json!({"choices":[{"delta":{"content":content}}]})
    } else if system.contains("Fresh provider result") {
        if directory_request.contains("email") {
            json!({"choices":[{"delta":{"tool_calls":[{"index":0,"id":"directory-email","function":{"name":"email_provider_results","arguments":if directory_request.contains("alternate"){json!({"recipient_email":"other@example.invalid"}).to_string()}else{"{}".into()}}}]}}]})
        } else {
            json!({"choices":[{"delta":{"content":"Alpha Clinic on Allen Avenue, Ikeja is listed for Plus. This is a listing, not coverage approval."}}]})
        }
    } else if system.contains("caller requested a hospital email before a search")
        || text.contains("directory")
    {
        let (name, args) = if text.contains("email")
            && !system.contains("caller requested a hospital email before a search")
        {
            (
                "email_provider_results",
                if text.contains("alternate") {
                    json!({"recipient_email":"other@example.invalid"})
                } else {
                    json!({})
                },
            )
        } else if text.contains("misheard") {
            (
                "lookup_provider_directory",
                json!({"intent":"lookup","plan":"PLOS","provider":"Kedja"}),
            )
        } else if text.contains("missing") {
            ("lookup_provider_directory", json!({"intent":"lookup"}))
        } else {
            (
                "lookup_provider_directory",
                json!({"intent":"lookup","network":"PROVIDER","plan":"Plus","town":"Ikeja","state":"Lagos"}),
            )
        };
        json!({"choices":[{"delta":{"content":"FALSE PREAMBLE. I have checked everything.","tool_calls":[{"index":0,"id":"directory-call","function":{"name":name,"arguments":args.to_string()}}]}}]})
    } else if system.contains("Verified banking result") {
        json!({"choices":[{"delta":{"content":if system.contains("Device transfer finished."){"The bank confirmed the transaction."}else{"Your balance is 100 naira."}}}]})
    } else if text.contains("bank two accounts") {
        json!({"choices":[{"delta":{"content":"One moment while I check that for you.","tool_calls":[{"index":0,"id":"account-1","function":{"name":"wema_get_balance","arguments":"{\"source_account\":\"0000000001\"}"}},{"index":1,"id":"account-2","function":{"name":"wema_get_balance","arguments":"{\"source_account\":\"0000000002\"}"}}]}}]})
    } else if text.contains("bank prepare") {
        json!({"choices":[{"delta":{"tool_calls":[{"index":0,"id":"bank-prepare","function":{"name":"wema_prepare_data_purchase","arguments":"{}"}}]}}]})
    } else if text.contains("bank balance") {
        let account = if text.contains("0000000001") {
            "0000000001"
        } else {
            "0000000002"
        };
        json!({"choices":[{"delta":{"tool_calls":[{"index":0,"id":"bank-read","function":{"name":"wema_get_balance","arguments":json!({"source_account":account}).to_string()}}]}}]})
    } else if system.contains("Validated business knowledge for the caller") {
        json!({"choices":[{"delta":{"content":"Verified business answer."}}]})
    } else if text.contains("natural knowledge request") {
        json!({"choices":[{"delta":{"content":"Let me check that for you."}}]})
    } else if text.contains("knowledge request") {
        json!({"choices":[{"delta":{"tool_calls":[{"index":0,"id":"lookup","function":{"name":"knowledge_lookup","arguments":"{\"query\":\"bank policy\"}"}}]}}]})
    } else if text.contains("ticket request") {
        json!({"choices":[{"delta":{"tool_calls":[{"index":0,"id":"ticket-call","function":{"name":"create_ticket","arguments":"{\"title\":\"Customer issue\",\"description\":\"Please investigate.\",\"priority\":\"normal\"}"}}]}}]})
    } else if text.contains("human request refused") {
        json!({"choices":[{"delta":{"content":"Understood. I will keep helping you."}}]})
    } else if text.contains("human request")
        && !body["tools"].as_array().is_some_and(|tools| {
            tools
                .iter()
                .any(|tool| tool["function"]["name"] == "transfer_to_aicc")
        })
    {
        json!({"choices":[{"delta":{"content":"That connection hasn't been confirmed. I'm still here to help."}}]})
    } else if text.contains("human request") {
        json!({"choices":[{"delta":{"content":"The human has answered already. This false preamble must never be spoken.","tool_calls":[{"index":0,"id":"human-call","function":{"name":"transfer_to_aicc","arguments":"{\"reason_summary\":\"The caller requested a colleague.\"}"}}]}}]})
    } else if text.contains("goodbye request") {
        json!({"choices":[{"delta":{"tool_calls":[{"index":0,"id":"close","function":{"name":"end_call","arguments":"{}"}}]}}]})
    } else if text.contains("silence") {
        json!({"choices":[{"delta":{"tool_calls":[{"index":0,"id":"gate","function":{"name":"set_speech_enabled","arguments":"{\"enabled\":false,\"response\":\"\"}"}}]}}]})
    } else if text.contains("resume") {
        json!({"choices":[{"delta":{"tool_calls":[{"index":0,"id":"gate","function":{"name":"set_speech_enabled","arguments":"{\"enabled\":true,\"response\":\"I kept listening.\"}"}}]}}]})
    } else if text.contains("transfer") {
        json!({"choices":[{"delta":{"tool_calls":[{"index":0,"id":"unsafe","function":{"name":"transfer_money","arguments":"{}"}}]}}]})
    } else {
        json!({"choices":[{"delta":{"content":if text.contains("second"){ "Second answer." }else{"First answer."}}}]})
    };
    let usage = if text.contains("usage frame request") {
        format!(
            "data: {}\n\ndata: {}\n\n",
            json!({"choices":[],"usage":{"prompt_tokens":42,"completion_tokens":3}}),
            json!({"choices":[],"usage":{"prompt_tokens":42,"completion_tokens":3}})
        )
    } else {
        String::new()
    };
    let private_context = if body["thinking"]["type"] == "enabled" {
        format!(
            "data: {}\n\n",
            json!({"choices":[{"delta":{"reasoning_content":"private-continuation-fixture"}}]})
        )
    } else {
        String::new()
    };
    let raw = format!("{private_context}data: {item}\n\n{usage}data: [DONE]\n\n");
    (
        [(axum::http::header::CONTENT_TYPE, "text/event-stream")],
        raw,
    )
        .into_response()
}
async fn tts(State(s): State<Arc<Fixture>>, Json(body): Json<Value>) -> Response {
    s.requests.lock().unwrap().push(body.clone());
    if s.tts_fail.load(std::sync::atomic::Ordering::SeqCst) {
        return axum::http::StatusCode::SERVICE_UNAVAILABLE.into_response();
    }
    assert_eq!(body["x_vector_only_mode"], false);
    assert_eq!(body["voice"], "helen-mavino-0030");
    assert_eq!(body["seed"], "3536072912");
    let sample = if body["input"].as_str().unwrap().contains("Second") {
        222i16
    } else {
        111i16
    };
    let pcm: Vec<u8> = (0..24000).flat_map(|_| sample.to_le_bytes()).collect();
    // Odd boundaries deliberately exercise PCM byte carry.
    let chunks: Vec<Result<Vec<u8>, Infallible>> =
        pcm.chunks(999).map(|c| Ok(c.to_vec())).collect();
    (
        [("x-sample-rate", "24000")],
        Body::from_stream(futures_util::stream::iter(chunks)),
    )
        .into_response()
}
#[derive(Default)]
struct Sink {
    pcm: Mutex<Vec<i16>>,
    clear: Mutex<usize>,
}
impl AudioSink for Sink {
    fn capture(&self, pcm: Vec<i16>) -> Capture<'_> {
        Box::pin(async move {
            self.pcm.lock().unwrap().extend(pcm);
            tokio::time::sleep(Duration::from_millis(10)).await;
            Ok(())
        })
    }
    fn clear(&self) {
        *self.clear.lock().unwrap() += 1;
    }
}
struct Harness {
    session: coordinator::Session,
    out: tokio::sync::mpsc::Receiver<Output>,
    trace: Trace,
    sink: Arc<Sink>,
    fixture: Arc<Fixture>,
    server: tokio::task::JoinHandle<()>,
    worker: tokio::task::JoinHandle<()>,
}
impl Harness {
    async fn new(engagement: bool) -> Self {
        Self::configured(engagement, |_| {}).await
    }
    async fn configured(engagement: bool, configure: impl FnOnce(&mut Config)) -> Self {
        Self::configured_asr(engagement, configure, None).await
    }
    async fn configured_asr(
        engagement: bool,
        configure: impl FnOnce(&mut Config),
        asr: Option<Arc<AsrFixture>>,
    ) -> Self {
        Self::configured_platform(engagement, configure, asr, false).await
    }
    async fn configured_platform(
        engagement: bool,
        configure: impl FnOnce(&mut Config),
        asr: Option<Arc<AsrFixture>>,
        with_services: bool,
    ) -> Self {
        let fixture = Arc::new(Fixture::default());
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let mut app = Router::new()
            .route("/chat/completions", post(llm))
            .route("/tts", post(tts))
            .route("/v1/knowledge/search", post(knowledge_service))
            .route("/v1/tickets", post(ticket_service))
            .route("/bank", post(bank_service))
            .route("/directory", post(directory_source))
            .route("/directory-mail", post(directory_mail))
            .route("/bank/v1/fido2/begin", post(device_begin_service))
            .route(
                "/bank/v1/tools/wema_execute_prepared",
                post(device_execute_service),
            )
            .with_state(fixture.clone());
        if let Some(state) = &asr {
            app = app.merge(
                Router::new()
                    .route("/ws", axum::routing::get(asr_ws))
                    .route("/final", post(asr_final))
                    .with_state(state.clone()),
            );
        }
        let server = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        let mut c: Config =
            serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
        c.llm_base_url = format!("http://{address}");
        c.tts_url = format!("http://{address}/tts");
        c.engagement_enabled = engagement;
        c.persist_conversation = false;
        if asr.is_some() {
            c.stt_ws_url = format!("ws://{address}/ws");
            c.stt_chat_url = format!("http://{address}/final");
            c.stt_batch_url = format!("http://{address}/unavailable-batch");
            c.stt_warmup_ms = 0.0;
        }
        configure(&mut c);
        if directory::enabled(&c.agent_context, directory::LOOKUP) {
            c.directory.endpoint = format!("http://{address}/directory");
            c.directory.token = "local-directory-fixture".into();
            c.directory.allow_loopback = true;
            c.directory.mail_url = format!("http://{address}/directory-mail");
        }
        let has_bank = c
            .agent_context
            .tools
            .iter()
            .any(|t| t["name"].as_str().is_some_and(super::banking::is_bank));
        if has_bank {
            for tool in &mut c.agent_context.tools {
                tool["url"] = json!(format!("http://{address}/bank"));
            }
        }
        let sink = Arc::new(Sink::default());
        let p = Playout::spawn(sink.clone(), 24000, 1, 24000);
        let trace = Trace::default();
        let services = with_services.then(|| {
            let r: Value =
                serde_json::from_str(include_str!("../../fixtures/python-parity-reference.json"))
                    .unwrap();
            let scope =if directory::enabled(&c.agent_context,directory::EMAIL){directory_scope()}else if has_bank{super::identity::test_session(json!({"voice_auth_required":false,"wema_context":{"customer_id":"customer-a","account_number":"0123456789","phone_number":"08000000000"}}))}else{ super::identity::Verifier::default()
                .verify(
                    r["identity"]["cases"][0]["token"].as_str().unwrap(),
                    r["identity"]["key"].as_str().unwrap(),
                    1000.0,
                )
                .unwrap()};
            super::platform::Services::new(
                super::platform::PlatformConfig {
                    conversation: super::platform::Endpoint {
                        base_url: format!("http://{address}"),
                        token: "local-service-fixture".into(),
                        timeout_seconds: 2.0,
                    },
                    knowledge: super::platform::Endpoint {
                        base_url: format!("http://{address}"),
                        token: "local-service-fixture".into(),
                        timeout_seconds: 2.0,
                    },
                    ..Default::default()
                },
                scope,
                trace.clone(),
            )
            .unwrap()
        });
        let (session, out, worker) = coordinator::start_with_services(
            Arc::new(c),
            p,
            trace.clone(),
            asr.is_some(),
            services,
        );
        Self {
            session,
            out,
            trace,
            sink,
            fixture,
            server,
            worker,
        }
    }
    async fn voice(&self) {
        for _ in 0..12 {
            self.session.pcm(vec![1000; 320]).await.unwrap();
        }
    }
    async fn end(&self) {
        for _ in 0..15 {
            self.session.pcm(vec![0; 320]).await.unwrap();
        }
    }
    async fn until(&self, event: &str) {
        tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                if self.trace.events().iter().any(|e| e.event == event) {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
        })
        .await
        .unwrap_or_else(|error| panic!("waiting for {event}: {error}; {:?}", self.trace.events()));
    }
    async fn complete(&mut self, turn: u64) {
        tokio::time::timeout(Duration::from_secs(6), async {
            while let Some(o) = self.out.recv().await {
                match o {
                    Output::Complete { turn: t } if t == turn => break,
                    Output::Event(body) => {
                        self.fixture.private_packets.lock().unwrap().push(body);
                    }
                    Output::ReliableEvent { body, receipt } => {
                        self.fixture.private_packets.lock().unwrap().push(body);
                        let _ = receipt.send(Ok(()));
                    }
                    Output::Error(e) => panic!("{e}"),
                    _ => {}
                }
            }
        })
        .await
        .unwrap_or_else(|error| {
            panic!(
                "turn {turn} did not complete: {error}; {:?}",
                self.trace.events()
            )
        });
    }
    async fn close(self) {
        self.session.close().await;
        self.worker.await.unwrap();
        self.server.abort();
    }
}
#[tokio::test]
async fn speculative_tts_is_held_until_owned_final_and_wrong_final_is_rejected() {
    let mut h = Harness::new(false).await;
    h.voice().await;
    h.session
        .input
        .send(Input::Partial("first request".into()))
        .await
        .unwrap();
    h.until("tts_first_pcm").await;
    assert!(h.sink.pcm.lock().unwrap().is_empty());
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "first request".into(),
        })
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_millis(10)).await;
    assert!(h.sink.pcm.lock().unwrap().is_empty());
    h.end().await;
    h.until("user_speech_end").await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "first request".into(),
        })
        .await
        .unwrap();
    h.complete(1).await;
    assert!(h.sink.pcm.lock().unwrap().iter().all(|s| *s == 111));
    assert!(!h.sink.pcm.lock().unwrap().is_empty());
    {
        let requests = h.fixture.requests.lock().unwrap();
        let request = requests
            .iter()
            .find(|r| r.get("messages").is_some())
            .unwrap();
        assert_eq!(request["max_tokens"], 180);
        assert_eq!(request["temperature"], 0.35);
    }
    h.close().await;
}
#[tokio::test]
async fn changed_final_cancels_prepared_audio_and_generates_from_authoritative_text() {
    let mut h = Harness::new(false).await;
    h.voice().await;
    h.session
        .input
        .send(Input::Partial("first request".into()))
        .await
        .unwrap();
    h.until("tts_first_pcm").await;
    h.end().await;
    h.until("user_speech_end").await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "second topic entirely".into(),
        })
        .await
        .unwrap();
    h.complete(1).await;
    assert!(h.sink.pcm.lock().unwrap().iter().all(|s| *s == 222));
    assert!(h.trace.events().iter().any(|e| e.event == "attempt_cancel"));
    h.close().await;
}
#[tokio::test]
async fn model_speech_gate_waits_for_final_retains_input_and_can_resume() {
    let mut h = Harness::new(true).await;
    h.voice().await;
    h.session
        .input
        .send(Input::Partial("silence request".into()))
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_millis(250)).await;
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "speech_gate_changed"));
    h.end().await;
    h.until("user_speech_end").await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "silence request".into(),
        })
        .await
        .unwrap();
    h.complete(1).await;
    assert!(h.sink.pcm.lock().unwrap().is_empty());
    h.voice().await;
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 2,
            text: "Please resume".into(),
        })
        .await
        .unwrap();
    h.complete(2).await;
    assert!(!h.sink.pcm.lock().unwrap().is_empty());
    assert!(
        h.trace
            .events()
            .iter()
            .filter(|e| e.event == "input_pcm")
            .count()
            >= 54
    );
    h.close().await;
}
#[tokio::test]
async fn unsupported_tools_never_execute() {
    let mut h = Harness::new(false).await;
    h.voice().await;
    h.end().await;
    h.until("user_speech_end").await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "transfer request".into(),
        })
        .await
        .unwrap();
    let error = tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            if let Some(Output::Error(e)) = h.out.recv().await {
                break e;
            }
        }
    })
    .await
    .unwrap();
    assert!(error.contains("unsupported tool"));
    assert!(h.sink.pcm.lock().unwrap().is_empty());
    h.close().await;
}

#[tokio::test]
async fn confirmed_barge_in_cancels_output_but_short_noise_does_not() {
    let mut h = Harness::new(false).await;
    h.voice().await;
    h.end().await;
    h.until("user_speech_end").await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "first request".into(),
        })
        .await
        .unwrap();
    h.until("audio_first_enqueued").await;
    h.session.pcm(vec![1000; 320]).await.unwrap();
    h.session.pcm(vec![0; 320]).await.unwrap();
    tokio::time::sleep(Duration::from_millis(5)).await;
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "attempt_cancel" && e.data["reason"] == "user_barge_in"));
    h.voice().await;
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            if h.trace
                .events()
                .iter()
                .any(|e| e.event == "attempt_cancel" && e.data["reason"] == "user_barge_in")
            {
                break;
            }
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    let captured = h.sink.pcm.lock().unwrap().len();
    tokio::time::sleep(Duration::from_millis(150)).await;
    assert_eq!(h.sink.pcm.lock().unwrap().len(), captured);
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "stale prior final".into(),
        })
        .await
        .unwrap();
    h.session
        .input
        .send(Input::Final {
            turn: 2,
            text: "second request".into(),
        })
        .await
        .unwrap();
    h.complete(2).await;
    assert!(h.sink.pcm.lock().unwrap()[captured..]
        .iter()
        .all(|s| *s == 222));
    h.close().await;
}

#[tokio::test]
async fn listening_only_text_is_retained_but_unstructured_model_reply_is_discarded() {
    let mut h = Harness::new(true).await;
    h.voice().await;
    h.end().await;
    h.until("user_speech_end").await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "silence request".into(),
        })
        .await
        .unwrap();
    h.complete(1).await;
    h.voice().await;
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 2,
            text: "unrelated first request".into(),
        })
        .await
        .unwrap();
    h.complete(2).await;
    assert!(h.sink.pcm.lock().unwrap().is_empty());
    {
        let requests = h.fixture.requests.lock().unwrap();
        let request = requests
            .iter()
            .rfind(|r| r.get("messages").is_some())
            .unwrap();
        assert_eq!(request["tool_choice"], "required");
        assert!(request["messages"]
            .to_string()
            .contains("Listening-only speech"));
    }
    h.close().await;
}
#[test]
fn python_serialization_keeps_punctuation_inside_strings() {
    assert_eq!(
        python_json(&json!({"a":"Hello, a:b 😀","b":false})),
        r#"{"a": "Hello, a:b \ud83d\ude00", "b": false}"#
    );
}

#[derive(Default)]
struct AsrFixture {
    fail: std::sync::atomic::AtomicBool,
    open: std::sync::atomic::AtomicBool,
    handoff: std::sync::atomic::AtomicBool,
    no_partials: std::sync::atomic::AtomicBool,
    reject_generation: std::sync::atomic::AtomicBool,
    connections: std::sync::atomic::AtomicUsize,
    appended_samples: std::sync::atomic::AtomicUsize,
    max_stream_samples: std::sync::atomic::AtomicUsize,
    final_samples: std::sync::atomic::AtomicUsize,
    flushed_text: Mutex<Option<String>>,
    ignore_flush: std::sync::atomic::AtomicBool,
    hold_final: std::sync::atomic::AtomicBool,
    final_release: tokio::sync::Notify,
}
async fn asr_ws(
    State(s): State<Arc<AsrFixture>>,
    upgrade: axum::extract::ws::WebSocketUpgrade,
) -> Response {
    use axum::extract::ws::Message;
    use futures_util::StreamExt;
    use std::sync::atomic::Ordering;
    upgrade
        .on_upgrade(move |mut socket| async move {
            s.connections.fetch_add(1, Ordering::SeqCst);
            let mut stream_samples = 0;
            s.open.store(true, Ordering::SeqCst);
            socket
                .send(Message::Text(
                    json!({"type":"session.created"}).to_string().into(),
                ))
                .await
                .unwrap();
            while let Some(Ok(message)) = socket.next().await {
                match message {
                    Message::Text(raw) => {
                        let value: Value = serde_json::from_str(&raw).unwrap();
                        if value["type"] == "input_audio_buffer.commit" && value["final"] == true {
                            let text = s.flushed_text.lock().unwrap().clone();
                            if let Some(text) = text.filter(|_| !s.ignore_flush.load(Ordering::SeqCst)) {
                                let _ = socket.send(Message::Text(json!({"type":"transcription.done","text":text}).to_string().into())).await;
                            }
                        }
                        if value["type"] == "input_audio_buffer.commit"
                            && s.reject_generation.load(Ordering::SeqCst)
                        {
                            let _ = socket.send(Message::Text(json!({"type":"error","error":{"message":"engine unavailable"}}).to_string().into())).await;
                            continue;
                        }
                        if value["type"] == "input_audio_buffer.append" {
                            use base64::{engine::general_purpose::STANDARD, Engine};
                            let bytes = STANDARD.decode(value["audio"].as_str().unwrap()).unwrap();
                            stream_samples += bytes.len() / 2;
                            s.appended_samples.fetch_add(bytes.len() / 2, Ordering::SeqCst);
                            s.max_stream_samples.fetch_max(stream_samples, Ordering::SeqCst);
                            if bytes.iter().any(|b| *b != 0) && !s.no_partials.load(Ordering::SeqCst) {
                                let requires_flush = s.flushed_text.lock().unwrap().is_some();
                                let partial = if requires_flush { "incomplete" } else { "first request" };
                                for part in ["language", "English", "<asr_text>", partial] {
                                    socket
                                        .send(Message::Text(
                                            json!({"type":"transcription.delta","delta":part})
                                                .to_string()
                                                .into(),
                                        ))
                                        .await
                                        .unwrap();
                                }
                                if !requires_flush { socket
                                    .send(Message::Text(
                                        json!({"type":"transcription.done","text":"first request"})
                                            .to_string()
                                            .into(),
                                    ))
                                    .await
                                    .unwrap(); }
                            }
                        }
                    }
                    Message::Close(_) => {
                        s.open.store(false, Ordering::SeqCst);
                        let _ = socket.send(Message::Close(None)).await;
                        break;
                    }
                    _ => {}
                }
            }
            s.open.store(false, Ordering::SeqCst);
        })
        .into_response()
}
async fn asr_final(State(s): State<Arc<AsrFixture>>, Json(body): Json<Value>) -> Response {
    use base64::{engine::general_purpose::STANDARD, Engine};
    use std::sync::atomic::Ordering;
    assert!(
        !s.open.load(Ordering::SeqCst),
        "realtime must be quiesced before shared-engine HTTP final"
    );
    s.handoff.store(true, Ordering::SeqCst);
    if s.fail.load(Ordering::SeqCst) {
        return axum::http::StatusCode::BAD_GATEWAY.into_response();
    }
    assert_eq!(
        body["messages"].as_array().unwrap().last().unwrap()["content"][0]["input_audio"]["format"],
        "wav"
    );
    let wav = STANDARD
        .decode(
            body["messages"].as_array().unwrap().last().unwrap()["content"][0]["input_audio"]
                ["data"]
                .as_str()
                .unwrap(),
        )
        .unwrap();
    s.final_samples
        .store((wav.len() - 44) / 2, Ordering::SeqCst);
    if s.hold_final.load(Ordering::SeqCst) {
        s.final_release.notified().await;
    }
    Json(json!({"choices":[{"message":{"content":"language English<asr_text>first request"}}]}))
        .into_response()
}

#[tokio::test]
async fn long_idle_and_muted_audio_never_reach_asr_but_speech_still_speculates() {
    use std::sync::atomic::Ordering;
    let state = Arc::new(AsrFixture::default());
    let mut h = Harness::configured_asr(false, |_| {}, Some(state.clone())).await;
    h.until("session_ready").await;
    // Ninety seconds of input PCM, accelerated without any provider inference.
    for _ in 0..900 {
        h.session.pcm(vec![0; 1600]).await.unwrap();
    }
    tokio::time::timeout(Duration::from_secs(3), async {
        while h
            .trace
            .events()
            .iter()
            .filter(|e| e.event == "input_pcm")
            .count()
            < 900
        {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(state.appended_samples.load(Ordering::SeqCst), 0);
    h.voice().await;
    h.until("tts_first_pcm").await;
    assert!(
        h.sink.pcm.lock().unwrap().is_empty(),
        "speculation stays held before final"
    );
    h.end().await;
    h.complete(1).await;
    assert_eq!(state.final_samples.load(Ordering::SeqCst), 27 * 320);
    // The final handoff may discard queued realtime frames; it must keep all
    // owned PCM while forwarding no idle audio.
    assert!((1..=27 * 320).contains(&state.appended_samples.load(Ordering::SeqCst)));
    let before = state.appended_samples.load(Ordering::SeqCst);
    for _ in 0..900 {
        h.session.pcm(vec![0; 1600]).await.unwrap();
    }
    tokio::time::timeout(Duration::from_secs(3), async {
        while h
            .trace
            .events()
            .iter()
            .filter(|e| e.event == "input_pcm")
            .count()
            < 1827
        {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(state.appended_samples.load(Ordering::SeqCst), before);
    // Confirm the same ASR-enabled path can still cut off the next reply.
    h.voice().await;
    h.end().await;
    tokio::time::timeout(Duration::from_secs(3), async {
        while !h
            .trace
            .events()
            .iter()
            .any(|e| e.event == "audio_first_enqueued" && e.turn == 2)
        {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    h.voice().await;
    tokio::time::timeout(Duration::from_secs(3), async {
        while !h.trace.events().iter().any(|e| {
            e.event == "attempt_cancel" && e.turn == 2 && e.data["reason"] == "user_barge_in"
        }) {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    assert!(*h.sink.clear.lock().unwrap() > 0);
    h.end().await;
    h.complete(3).await;
    h.close().await;
}

fn flushing_asr(text: &str) -> Arc<AsrFixture> {
    use std::sync::atomic::Ordering;
    let state = Arc::new(AsrFixture::default());
    *state.flushed_text.lock().unwrap() = Some(text.into());
    state.hold_final.store(true, Ordering::SeqCst);
    state
}

#[test]
fn speculative_flush_is_opt_in_and_has_a_finite_upper_bound() {
    let mut c: Config =
        serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
    assert_eq!(c.stt_speculative_flush_ms, 0.0);
    for invalid in [-1.0, 501.0, f64::NAN, f64::INFINITY] {
        c.stt_speculative_flush_ms = invalid;
        assert!(c.validate().is_err());
    }
    c.stt_speculative_flush_ms = 350.0;
    assert!(c.validate().is_ok());
}

#[tokio::test]
async fn resumed_prefix_cannot_bypass_realtime_audio_limit() {
    use super::providers::{start_asr, AsrEvent, Providers, MAX_REALTIME_SAMPLES};
    use std::sync::atomic::Ordering;
    let state = Arc::new(AsrFixture::default());
    state.no_partials.store(true, Ordering::SeqCst);
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let app = Router::new()
        .route("/ws", axum::routing::get(asr_ws))
        .route("/final", post(asr_final))
        .with_state(state.clone());
    let server = tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    let mut c: Config =
        serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
    c.stt_ws_url = format!("ws://{addr}/ws");
    c.stt_chat_url = format!("http://{addr}/final");
    c.stt_batch_url = format!("http://{addr}/unavailable-batch");
    c.stt_warmup_ms = 0.0;
    let mut asr = start_asr(Providers::new(Arc::new(c), Trace::default()).unwrap());
    assert!(matches!(asr.events.recv().await, Some(AsrEvent::Ready)));
    let full = vec![1000; MAX_REALTIME_SAMPLES + 1600];
    asr.resume(full.clone()).await;
    asr.commit(42, full.clone()).await.unwrap();
    assert!(matches!(
        tokio::time::timeout(Duration::from_secs(3), asr.events.recv())
            .await
            .unwrap(),
        Some(AsrEvent::Final { turn: 42, .. })
    ));
    assert_eq!(state.final_samples.load(Ordering::SeqCst), full.len());
    assert!(state.max_stream_samples.load(Ordering::SeqCst) <= MAX_REALTIME_SAMPLES);
    asr.task.abort();
    let _ = asr.task.await;
    server.abort();
}

#[tokio::test]
async fn completed_hypothesis_prepares_immediately_but_waits_for_independent_final() {
    let state = flushing_asr("first request");
    let mut h = Harness::configured_asr(
        false,
        |c| {
            c.stt_speculative_flush_ms = 350.0;
            c.stable_partial_ms = 1000.0;
        },
        Some(state.clone()),
    )
    .await;
    h.until("session_ready").await;
    h.voice().await;
    h.end().await;
    h.until("tts_first_pcm").await;
    assert!(h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "stt_complete_hypothesis"));
    assert!(!h.trace.events().iter().any(|e| e.event == "stt_final"));
    assert!(h.sink.pcm.lock().unwrap().is_empty());
    state.final_release.notify_one();
    h.complete(1).await;
    assert!(h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "speculation_authorized"));
    h.close().await;
}

#[tokio::test]
async fn completed_hypothesis_mismatch_never_releases_prepared_wrong_audio() {
    let state = flushing_asr("second request");
    let mut h = Harness::configured_asr(
        false,
        |c| c.stt_speculative_flush_ms = 350.0,
        Some(state.clone()),
    )
    .await;
    h.until("session_ready").await;
    h.voice().await;
    h.end().await;
    h.until("tts_first_pcm").await;
    assert!(h.sink.pcm.lock().unwrap().is_empty());
    state.final_release.notify_one();
    h.complete(1).await;
    assert!(h.sink.pcm.lock().unwrap().iter().all(|s| *s == 111));
    assert!(h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "attempt_cancel" && e.data["reason"] == "final_transcript_changed"));
    h.close().await;
}

#[tokio::test]
async fn speech_resumption_invalidates_completion_and_replays_owned_prefix() {
    use std::sync::atomic::Ordering;
    let state = flushing_asr("first request");
    let mut h = Harness::configured_asr(
        false,
        |c| c.stt_speculative_flush_ms = 350.0,
        Some(state.clone()),
    )
    .await;
    h.until("session_ready").await;
    h.voice().await;
    h.session.pcm(vec![0; 320]).await.unwrap();
    h.until("stt_speculative_flush_start").await;
    h.voice().await;
    h.session
        .input
        .send(Input::Hypothesis {
            turn: 1,
            revision: 1,
            text: "second request".into(),
        })
        .await
        .unwrap();
    h.until("stt_hypothesis_rejected").await;
    tokio::time::timeout(Duration::from_secs(3), async {
        while state.connections.load(Ordering::SeqCst) < 2
            || state.max_stream_samples.load(Ordering::SeqCst) < 25 * 320
        {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    h.end().await;
    h.until("asr_final_request").await;
    assert!(h.sink.pcm.lock().unwrap().is_empty());
    state.final_release.notify_one();
    h.complete(1).await;
    assert_eq!(state.final_samples.load(Ordering::SeqCst), 40 * 320);
    assert_eq!(
        h.trace
            .events()
            .iter()
            .filter(|e| e.event == "stt_speculative_flush_start")
            .count(),
        1
    );
    h.close().await;
}

#[tokio::test]
async fn missing_flush_completion_has_bounded_wait_and_keeps_final_fallback() {
    use std::sync::atomic::Ordering;
    let state = flushing_asr("first request");
    state.ignore_flush.store(true, Ordering::SeqCst);
    state.hold_final.store(false, Ordering::SeqCst);
    let mut h =
        Harness::configured_asr(false, |c| c.stt_speculative_flush_ms = 40.0, Some(state)).await;
    h.until("session_ready").await;
    h.voice().await;
    h.end().await;
    h.complete(1).await;
    let events = h.trace.events();
    let start = events
        .iter()
        .find(|e| e.event == "stt_speculative_flush_start")
        .unwrap();
    let final_request = events
        .iter()
        .find(|e| e.event == "asr_final_request")
        .unwrap();
    assert!(final_request.at_us - start.at_us < 500_000);
    assert!(events
        .iter()
        .any(|e| e.event == "stt_speculative_flush_timeout"));
    assert!(!events.iter().any(|e| e.event == "stt_complete_hypothesis"));
    h.close().await;
}

#[tokio::test]
async fn final_handoff_does_not_wait_out_the_entire_optional_flush_budget() {
    use std::sync::atomic::Ordering;
    let state = flushing_asr("first request");
    state.ignore_flush.store(true, Ordering::SeqCst);
    state.hold_final.store(false, Ordering::SeqCst);
    let mut h =
        Harness::configured_asr(false, |c| c.stt_speculative_flush_ms = 500.0, Some(state)).await;
    h.until("session_ready").await;
    h.voice().await;
    h.end().await;
    h.complete(1).await;
    let events = h.trace.events();
    let start = events
        .iter()
        .find(|e| e.event == "stt_speculative_flush_start")
        .unwrap();
    let final_request = events
        .iter()
        .find(|e| e.event == "asr_final_request")
        .unwrap();
    assert!(
        final_request.at_us - start.at_us < 250_000,
        "final recognition must not wait for the full 500ms budget"
    );
    assert!(events
        .iter()
        .any(|e| e.event == "stt_speculative_flush_timeout"));
    h.close().await;
}

#[tokio::test]
async fn realtime_audio_limit_pauses_speculation_until_full_owned_final_then_recovers() {
    use super::providers::{start_asr, AsrEvent, Providers, MAX_REALTIME_SAMPLES};
    use std::sync::atomic::Ordering;
    let state = Arc::new(AsrFixture::default());
    state.no_partials.store(true, Ordering::SeqCst);
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let app = Router::new()
        .route("/ws", axum::routing::get(asr_ws))
        .route("/final", post(asr_final))
        .with_state(state.clone());
    let server = tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    let mut c: Config =
        serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
    c.stt_ws_url = format!("ws://{addr}/ws");
    c.stt_chat_url = format!("http://{addr}/final");
    c.stt_batch_url = format!("http://{addr}/unavailable-batch");
    c.stt_warmup_ms = 0.0;
    c.stt_recovery_buffer_seconds = 30.0;
    let trace = Trace::default();
    let mut asr = start_asr(Providers::new(Arc::new(c), trace.clone()).unwrap());
    assert!(matches!(asr.events.recv().await, Some(AsrEvent::Ready)));
    for _ in 0..250 {
        asr.append(vec![1000; 1600]);
    }
    tokio::time::timeout(Duration::from_secs(3), async {
        while !trace
            .events()
            .iter()
            .any(|e| e.event == "stt_realtime_limit_reached")
        {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(
        state.max_stream_samples.load(Ordering::SeqCst),
        MAX_REALTIME_SAMPLES
    );
    assert_eq!(state.connections.load(Ordering::SeqCst), 1);
    assert!(
        tokio::time::timeout(Duration::from_millis(30), asr.events.recv())
            .await
            .is_err(),
        "the limit must not manufacture a final"
    );
    asr.commit(42, vec![1000; 25 * 16000]).await.unwrap();
    assert!(matches!(
        tokio::time::timeout(Duration::from_secs(3), asr.events.recv())
            .await
            .unwrap(),
        Some(AsrEvent::Final { turn: 42, .. })
    ));
    assert_eq!(state.final_samples.load(Ordering::SeqCst), 25 * 16000);
    asr.append(vec![1000; 1600]);
    tokio::time::timeout(Duration::from_secs(3), async {
        while state.appended_samples.load(Ordering::SeqCst) < MAX_REALTIME_SAMPLES + 1600 {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(state.connections.load(Ordering::SeqCst), 2);
    asr.task.abort();
    let _ = asr.task.await;
    server.abort();
}

#[tokio::test]
async fn websocket_handshakes_do_not_reset_backoff_for_a_dead_inference_engine() {
    use super::providers::{start_asr, Providers};
    use std::sync::atomic::Ordering;
    let state = Arc::new(AsrFixture::default());
    state.reject_generation.store(true, Ordering::SeqCst);
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let app = Router::new()
        .route("/ws", axum::routing::get(asr_ws))
        .with_state(state.clone());
    let server = tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    let mut c: Config =
        serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
    c.stt_ws_url = format!("ws://{addr}/ws");
    c.stt_warmup_ms = 0.0;
    let mut asr = start_asr(Providers::new(Arc::new(c), Trace::default()).unwrap());
    let drain = tokio::spawn(async move { while asr.events.recv().await.is_some() {} });
    tokio::time::sleep(Duration::from_millis(3500)).await;
    assert_eq!(
        state.connections.load(Ordering::SeqCst),
        3,
        "engine errors should back off for 1, 2, then 4 seconds"
    );
    asr.task.abort();
    let _ = asr.task.await;
    drain.abort();
    server.abort();
}
#[tokio::test]
async fn native_asr_adapter_keeps_realtime_non_authoritative_and_serializes_final_handoff() {
    use super::providers::{start_asr, AsrEvent, Providers};
    use std::sync::atomic::Ordering;
    let state = Arc::new(AsrFixture::default());
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let app = Router::new()
        .route("/ws", axum::routing::get(asr_ws))
        .route("/final", post(asr_final))
        .with_state(state.clone());
    let server = tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    let mut c: Config =
        serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
    c.stt_ws_url = format!("ws://{addr}/ws");
    c.stt_chat_url = format!("http://{addr}/final");
    c.stt_batch_url = format!("http://{addr}/unavailable-batch");
    let mut asr = start_asr(Providers::new(Arc::new(c), Trace::default()).unwrap());
    assert!(matches!(
        tokio::time::timeout(Duration::from_secs(3), asr.events.recv())
            .await
            .unwrap(),
        Some(AsrEvent::Ready)
    ));
    asr.append(vec![1000; 1600]);
    assert!(
        matches!(tokio::time::timeout(Duration::from_secs(3),asr.events.recv()).await.unwrap(),Some(AsrEvent::Partial(t))if t=="first request")
    );
    assert!(
        tokio::time::timeout(Duration::from_millis(20), asr.events.recv())
            .await
            .is_err(),
        "realtime done must not authorize"
    );
    asr.commit(42, vec![1000; 1600]).await.unwrap();
    assert!(
        matches!(tokio::time::timeout(Duration::from_secs(3),asr.events.recv()).await.unwrap(),Some(AsrEvent::Final{turn:42,text})if text=="first request")
    );
    assert!(state.handoff.load(Ordering::SeqCst));
    asr.task.abort();
    let _ = asr.task.await;
    server.abort();
}

#[tokio::test]
async fn unavailable_authoritative_final_discards_speculation_and_rejects_late_final() {
    let mut h = Harness::new(false).await;
    h.voice().await;
    h.session
        .input
        .send(Input::Partial("first request".into()))
        .await
        .unwrap();
    h.until("tts_first_pcm").await;
    h.end().await;
    h.until("user_speech_end").await;
    h.session.input.send(Input::Unavailable(1)).await.unwrap();
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            if let Some(Output::Status(_)) = h.out.recv().await {
                break;
            }
        }
    })
    .await
    .unwrap();
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "first request".into(),
        })
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_millis(20)).await;
    assert!(h.sink.pcm.lock().unwrap().is_empty());
    h.close().await;
}
#[tokio::test]
async fn empty_authoritative_final_returns_to_listening() {
    // Regression for the 21 s "silent turn": a noise blip creates a turn whose
    // ASR final is empty. The coordinator must return to listening immediately
    // (emit stt_final_empty_recovered + listening status) instead of sitting in
    // AwaitingFinal until the safety timer fires.
    let mut h = Harness::new(false).await;
    h.voice().await;
    h.end().await;
    h.until("user_speech_end").await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: String::new(),
        })
        .await
        .unwrap();
    h.until("stt_final_empty_recovered").await;
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            if let Some(Output::Status(_)) = h.out.recv().await {
                break;
            }
        }
    })
    .await
    .unwrap();
    // No response was produced for the empty turn.
    assert!(h.sink.pcm.lock().unwrap().is_empty());
    h.close().await;
}
#[tokio::test]
async fn below_minimum_utterance_is_discarded_before_asr() {
    // Safety net: a committed buffer shorter than min_utterance_ms is dropped
    // before ASR and the turn returns to idle (no user_speech_end / commit).
    let h = Harness::configured(false, |c| {
        c.min_utterance_ms = 100_000.0;
    })
    .await;
    h.voice().await;
    h.end().await;
    h.until("utterance_discarded_below_minimum").await;
    assert!(
        !h.trace
            .events()
            .iter()
            .any(|e| e.event == "user_speech_end"),
        "discarded utterance must not commit to ASR"
    );
    h.close().await;
}
#[test]
fn unsupported_realtime_final_configuration_is_refused() {
    let mut c: Config =
        serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
    assert!(c.validate().is_ok());
    c.stt_ws_url = "ws://127.0.0.1/whisper-rt".into();
    assert!(c.validate().is_err());
}

#[tokio::test]
async fn http_final_failure_never_promotes_realtime_text() {
    use super::providers::{start_asr, AsrEvent, Providers};
    use std::sync::atomic::Ordering;
    let state = Arc::new(AsrFixture::default());
    state.fail.store(true, Ordering::SeqCst);
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let app = Router::new()
        .route("/ws", axum::routing::get(asr_ws))
        .route("/final", post(asr_final))
        .with_state(state.clone());
    let server = tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    let mut c: Config =
        serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
    c.stt_ws_url = format!("ws://{addr}/ws");
    c.stt_chat_url = format!("http://{addr}/final");
    c.stt_batch_url = format!("http://{addr}/unavailable-batch");
    let mut asr = start_asr(Providers::new(Arc::new(c), Trace::default()).unwrap());
    assert!(matches!(asr.events.recv().await, Some(AsrEvent::Ready)));
    asr.append(vec![1000; 1600]);
    assert!(matches!(
        asr.events.recv().await,
        Some(AsrEvent::Partial(_))
    ));
    asr.commit(42, vec![1000; 1600]).await.unwrap();
    assert!(matches!(
        tokio::time::timeout(Duration::from_secs(3), asr.events.recv())
            .await
            .unwrap(),
        Some(AsrEvent::Unavailable(42))
    ));
    assert!(
        tokio::time::timeout(Duration::from_millis(30), asr.events.recv())
            .await
            .is_err()
    );
    asr.task.abort();
    let _ = asr.task.await;
    server.abort();
}

#[tokio::test]
async fn idle_speech_has_no_manufactured_caller_turn_and_cannot_reopen_muted_output() {
    let mut h = Harness::configured(true, |c| {
        c.engagement_idle_seconds = 0.05;
        c.memory_compaction_enabled = false;
    })
    .await;
    h.voice().await;
    h.end().await;
    h.until("user_speech_end").await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "first request".into(),
        })
        .await
        .unwrap();
    h.complete(1).await;
    h.until("engagement_spoken").await;
    h.complete((1u64 << 63) | 2).await;
    assert_eq!(
        h.trace
            .events()
            .iter()
            .filter(|e| e.event == "user_speech_start")
            .count(),
        1
    );
    h.voice().await;
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 2,
            text: "silence request".into(),
        })
        .await
        .unwrap();
    h.complete(2).await;
    let before = h.sink.pcm.lock().unwrap().len();
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert_eq!(h.sink.pcm.lock().unwrap().len(), before);
    assert_eq!(
        h.trace
            .events()
            .iter()
            .filter(|e| e.event == "engagement_spoken")
            .count(),
        1
    );
    h.close().await;
}
#[tokio::test]
async fn caller_speech_discards_an_inflight_idle_decision() {
    use std::sync::atomic::Ordering;
    let mut h = Harness::configured(true, |c| {
        c.engagement_idle_seconds = 0.05;
        c.memory_compaction_enabled = false;
    })
    .await;
    h.fixture.hold_idle.store(true, Ordering::SeqCst);
    h.voice().await;
    h.end().await;
    h.until("user_speech_end").await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "first request".into(),
        })
        .await
        .unwrap();
    h.complete(1).await;
    tokio::time::timeout(Duration::from_secs(2), h.fixture.idle_started.notified())
        .await
        .unwrap();
    h.voice().await;
    h.fixture.idle_release.notify_waiters();
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "engagement_spoken"));
    h.close().await;
}
#[tokio::test]
async fn actor_compacts_closed_history_without_pausing_input() {
    let mut h = Harness::configured(false, |c| {
        c.memory_compaction_enabled = true;
        c.memory_compaction_interval_seconds = 1.0;
        c.memory_recent_exchanges = 2;
    })
    .await;
    for turn in 1..=4 {
        h.voice().await;
        h.end().await;
        h.session
            .input
            .send(Input::Final {
                turn,
                text: format!("first request {turn}"),
            })
            .await
            .unwrap();
        h.complete(turn).await;
    }
    h.until("memory_compaction_complete").await;
    {
        let requests = h.fixture.requests.lock().unwrap();
        let summary = requests.iter().find(|r| r["max_tokens"] == 768).unwrap();
        assert!(summary.get("tools").is_none());
    }
    h.close().await;
}
#[tokio::test]
async fn realtime_failure_at_start_does_not_disable_owned_http_final() {
    use super::providers::{start_asr, AsrEvent, Providers};
    let state = Arc::new(AsrFixture::default());
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let app = Router::new()
        .route("/final", post(asr_final))
        .with_state(state.clone());
    let server = tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    let mut c: Config =
        serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
    c.stt_ws_url = format!("ws://{addr}/missing");
    c.stt_chat_url = format!("http://{addr}/final");
    c.stt_batch_url = format!("http://{addr}/unavailable-batch");
    let mut asr = start_asr(Providers::new(Arc::new(c), Trace::default()).unwrap());
    assert!(matches!(
        asr.events.recv().await,
        Some(AsrEvent::Degraded(_))
    ));
    assert!(matches!(asr.events.recv().await, Some(AsrEvent::Ready)));
    for _ in 0..1000 {
        asr.append(vec![1000; 1600]);
    }
    asr.commit(42, vec![1000; 3200]).await.unwrap();
    assert!(matches!(
        tokio::time::timeout(Duration::from_secs(3), asr.events.recv())
            .await
            .unwrap(),
        Some(AsrEvent::Final { turn: 42, .. })
    ));
    asr.task.abort();
    let _ = asr.task.await;
    server.abort();
}

#[tokio::test]
async fn punctuation_revision_replaces_and_cancels_previous_provider_work() {
    let mut h = Harness::new(false).await;
    h.voice().await;
    h.session
        .input
        .send(Input::Partial("first request".into()))
        .await
        .unwrap();
    h.until("tts_first_pcm").await;
    h.session
        .input
        .send(Input::Partial("first request!".into()))
        .await
        .unwrap();
    h.until("attempt_cancel").await;
    let events = h.trace.events();
    assert!(events.iter().any(
        |e| e.event == "attempt_cancel" && e.data["reason"] == "new_attempt_replaced_existing"
    ));
    assert!(h.sink.pcm.lock().unwrap().is_empty());
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "first request!".into(),
        })
        .await
        .unwrap();
    h.complete(1).await;
    h.close().await;
}

#[tokio::test]
async fn failed_final_recovers_for_a_subsequent_caller_turn() {
    let mut h = Harness::new(false).await;
    h.voice().await;
    h.end().await;
    h.session.input.send(Input::Unavailable(1)).await.unwrap();
    tokio::time::timeout(Duration::from_secs(2), async {
        while !matches!(h.out.recv().await, Some(Output::Status(_))) {}
    })
    .await
    .unwrap();
    h.voice().await;
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 2,
            text: "next request".into(),
        })
        .await
        .unwrap();
    h.complete(2).await;
    assert!(!h.sink.pcm.lock().unwrap().is_empty());
    h.close().await;
}

#[test]
fn empty_asr_markers_replace_hypotheses_without_deduplicating_caller_words() {
    let mut decoder = text::DeltaAccumulator::default();
    decoder.push("language English<asr_text>");
    assert_eq!(decoder.push("Oh."), Some("Oh.".into()));
    assert_eq!(decoder.push("language English<asr_text>"), None);
    assert_eq!(
        decoder.push("Please tell me."),
        Some("Please tell me.".into())
    );
    decoder.push("language English<asr_text>");
    assert_eq!(
        decoder.push("Please tell me whether you can hear me clearly."),
        Some("Please tell me whether you can hear me clearly.".into())
    );
    assert_eq!(
        decoder.finish(None),
        "Please tell me whether you can hear me clearly."
    );
    // Ordinary repeated words are actual transcript content, not reset signals.
    assert_eq!(decoder.push("No."), Some("No.".into()));
    assert_eq!(decoder.push("No."), Some("No.No.".into()));
    decoder.push("<asr_text>");
    assert_eq!(decoder.push("New request."), Some("New request.".into()));
    decoder.push("language French<asr_text>");
    assert_eq!(decoder.push("Bonjour."), None);
    decoder.push("language English<asr_text>");
    assert_eq!(decoder.push("Hello."), Some("Hello.".into()));
}

#[tokio::test]
async fn stable_partial_can_prepare_during_final_asr_without_authorizing_output() {
    let mut h = Harness::new(false).await;
    h.voice().await;
    h.session
        .input
        .send(Input::Partial("first request".into()))
        .await
        .unwrap();
    h.end().await;
    h.until("user_speech_end").await;
    h.until("tts_first_pcm").await;
    assert!(
        h.sink.pcm.lock().unwrap().is_empty(),
        "pending final cannot release prepared audio"
    );
    h.session
        .input
        .send(Input::Final {
            turn: 99,
            text: "first request".into(),
        })
        .await
        .unwrap();
    h.until("stt_final_rejected").await;
    assert!(h.sink.pcm.lock().unwrap().is_empty());
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "first request".into(),
        })
        .await
        .unwrap();
    h.complete(1).await;
    let events = h.trace.events();
    assert!(events.iter().any(|e| e.event == "speculation_authorized"));
    assert!(!events
        .iter()
        .any(|e| e.event == "attempt_cancel" && e.data["reason"] == "final_transcript_changed"));
    assert!(!h.sink.pcm.lock().unwrap().is_empty());
    h.close().await;
}

#[tokio::test]
async fn changed_final_discards_audio_prepared_during_final_asr() {
    let mut h = Harness::new(false).await;
    h.voice().await;
    h.session
        .input
        .send(Input::Partial("first request".into()))
        .await
        .unwrap();
    h.end().await;
    h.until("tts_first_pcm").await;
    assert!(h.sink.pcm.lock().unwrap().is_empty());
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "second request".into(),
        })
        .await
        .unwrap();
    h.complete(1).await;
    {
        let pcm = h.sink.pcm.lock().unwrap();
        assert!(!pcm.is_empty());
        assert!(
            pcm.iter().all(|sample| *sample == 222 || *sample == 0),
            "old prepared answer must never leak"
        );
    }
    h.close().await;
}

#[tokio::test]
async fn late_speculative_speech_gate_waits_for_owned_final() {
    let mut h = Harness::new(true).await;
    h.voice().await;
    h.session
        .input
        .send(Input::Partial("please silence".into()))
        .await
        .unwrap();
    h.end().await;
    h.until("llm_first_tool").await;
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "speech_gate_changed"));
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "please silence".into(),
        })
        .await
        .unwrap();
    h.complete(1).await;
    assert!(h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "speech_gate_changed" && e.data["enabled"] == false));
    assert!(h.sink.pcm.lock().unwrap().is_empty());
    h.close().await;
}

async fn inactivity_harness() -> Harness {
    Harness::configured(true, |c| {
        c.inactivity_pilot_enabled = true;
        c.engagement_idle_seconds = 0.05;
        c.inactivity_pilot_close_seconds = 0.15;
        c.memory_compaction_enabled = false;
    })
    .await
}
async fn finish_caller(h: &mut Harness, text: &str) {
    h.voice().await;
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: text.into(),
        })
        .await
        .unwrap();
    h.complete(1).await;
}
fn directory_scope() -> identity::VerifiedSession {
    identity::VerifiedSession::from_pinned_sip(json!({"entry_surface":"aicc_inbound","sip_caller_verified":true,"sip_trunk_id":"pinned-test-trunk","business_id":"business-a","config_agent_id":"agent-a","end_user_id":"caller-a","caller_phone":"+2348012345678","room_name":"room-a","participant_identity":"caller"})).unwrap()
}
async fn directory_source(State(s): State<Arc<Fixture>>, Json(body): Json<Value>) -> Response {
    assert_eq!(body["token"], "local-directory-fixture");
    assert_eq!(body["sheets"], json!(["PROVIDER"]));
    s.directory_reads
        .fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    s.directory_started.notify_one();
    if s.hold_directory.load(std::sync::atomic::Ordering::SeqCst) {
        s.directory_release.notified().await;
    }
    if s.directory_fail.load(std::sync::atomic::Ordering::SeqCst) {
        return Json(json!({"ok":false,"error":"UNAUTHORIZED"})).into_response();
    }
    Json(json!({"ok":true,"spreadsheetId":directory::data()["sheet_id"],"readCompletedAt":"2026-10-31T10:00:00Z","sheets":[{"name":"PROVIDER","values":[["CODE","ZONE","STATE","TOWN","PROVIDER","ADDRESS","CAT","PLAN","SPECIALTY"],["1","","Lagos","Ikeja","Alpha Clinic","Allen Avenue","D","PLUS & HIGHER","DENTAL"]]}]})).into_response()
}
async fn directory_mail(
    State(s): State<Arc<Fixture>>,
    headers: axum::http::HeaderMap,
    Json(body): Json<Value>,
) -> Response {
    assert_eq!(
        headers["authorization"],
        "Zoho-enczapikey local-mail-fixture"
    );
    s.directory_mails.lock().unwrap().push(body);
    s.mail_started.notify_one();
    if s.hold_mail.load(std::sync::atomic::Ordering::SeqCst) {
        s.mail_release.notified().await;
    }
    if s.mail_unknown.load(std::sync::atomic::Ordering::SeqCst) {
        return (axum::http::StatusCode::OK, "invalid JSON").into_response();
    }
    Json(json!({"data":[{"code":"EM_104"}],"request_id":"local-mail-request"})).into_response()
}
fn configure_directory(c: &mut Config, email: bool) {
    c.agent_context.loaded = true;
    c.agent_context.business_id = "business-a".into();
    c.agent_context.agent_id = "agent-a".into();
    c.agent_context.tools =
        vec![json!({"name":directory::LOOKUP,"url":"builtin://provider-directory"})];
    if email {
        c.agent_context
            .tools
            .push(json!({"name":directory::EMAIL,"url":"builtin://email-provider-results"}));
        c.directory.zepto_key = "local-mail-fixture".into();
        c.directory.from_address = "sender@example.invalid".into();
    }
}
fn contacts_db() -> std::path::PathBuf {
    let path =
        std::env::temp_dir().join(format!("sg-rust-directory-{}.sqlite", uuid::Uuid::new_v4()));
    let db = rusqlite::Connection::open(&path).unwrap();
    db.execute_batch("CREATE TABLE caller_contacts(business_id TEXT NOT NULL,phone_e164 TEXT NOT NULL,email TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,full_name TEXT NOT NULL DEFAULT '', PRIMARY KEY(business_id,phone_e164));INSERT INTO caller_contacts(business_id,phone_e164,email,full_name) VALUES('business-a','+2348012345678','saved@example.invalid','Ada Test');").unwrap();
    path
}
async fn directory_harness(email: bool, path: Option<&std::path::Path>) -> Harness {
    Harness::configured_platform(
        true,
        |c| {
            configure_directory(c, email);
            if let Some(path) = path {
                c.directory.contacts_db = path.to_string_lossy().into();
            }
        },
        None,
        true,
    )
    .await
}
async fn directory_outcome(h: &mut Harness) -> Value {
    tokio::time::timeout(Duration::from_secs(6), async {
        loop {
            if let Some(e) = h
                .trace
                .events()
                .iter()
                .rev()
                .find(|e| e.event == "tool_execution_complete")
            {
                return e.data.clone();
            }
            if let Some(Output::Error(e)) = h.out.recv().await {
                panic!("{e}");
            }
        }
    })
    .await
    .unwrap()
}
#[tokio::test]
async fn directory_clarification_does_not_fetch_or_speak_false_preamble() {
    let mut h = directory_harness(false, None).await;
    h.session
        .input
        .send(Input::Text("directory missing plan".into()))
        .await
        .unwrap();
    h.complete(1).await;
    assert_eq!(
        h.fixture
            .directory_reads
            .load(std::sync::atomic::Ordering::SeqCst),
        0
    );
    let r = h.fixture.requests.lock().unwrap().clone();
    assert!(r
        .iter()
        .any(|r| r["input"] == "Which health plan are you on?"));
    assert!(!r.iter().any(|r| r["input"]
        .as_str()
        .is_some_and(|t| t.contains("FALSE PREAMBLE") || t.contains("Let me check"))));
    drop(r);
    h.close().await;
}
#[tokio::test]
async fn directory_location_requires_fresh_final_then_reads_once() {
    let mut h = directory_harness(false, None).await;
    h.session
        .input
        .send(Input::Text("directory misheard".into()))
        .await
        .unwrap();
    h.complete(1).await;
    assert_eq!(
        h.fixture
            .directory_reads
            .load(std::sync::atomic::Ordering::SeqCst),
        0
    );
    h.session
        .input
        .send(Input::Text("confirmed request".into()))
        .await
        .unwrap();
    h.until("provider_lookup_started").await;
    h.until("provider_directory_result").await;
    tokio::time::timeout(Duration::from_secs(6), async {
        loop {
            if h.fixture.requests.lock().unwrap().iter().any(|r| {
                r["messages"][0]["content"]
                    .as_str()
                    .is_some_and(|s| s.contains("Fresh provider result"))
            }) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(
        h.fixture
            .directory_reads
            .load(std::sync::atomic::Ordering::SeqCst),
        1
    );
    let r = h.fixture.requests.lock().unwrap().clone();
    assert!(!r.iter().any(|r| r["input"]
        .as_str()
        .is_some_and(|t| t.contains("FALSE PREAMBLE"))));
    assert!(r.iter().any(|r| r["messages"][0]["content"]
        .as_str()
        .is_some_and(|s| s.contains("Fresh provider result") && s.contains("Alpha Clinic"))));
    drop(r);
    h.close().await;
}
#[tokio::test]
async fn directory_saved_email_searches_refreshes_and_sends_without_confirmation() {
    let path = contacts_db();
    let mut h = directory_harness(true, Some(&path)).await;
    h.session
        .input
        .send(Input::Text("directory email request".into()))
        .await
        .unwrap();
    let outcome = directory_outcome(&mut h).await;
    assert_eq!(outcome["status"], "ok");
    tokio::time::timeout(Duration::from_secs(6), h.fixture.mail_started.notified())
        .await
        .unwrap();
    h.until("tool_execution_complete").await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(
        h.fixture
            .directory_reads
            .load(std::sync::atomic::Ordering::SeqCst),
        2
    );
    let mails = h.fixture.directory_mails.lock().unwrap().clone();
    assert_eq!(mails.len(), 1);
    assert_eq!(
        mails[0]["to"][0]["email_address"]["address"],
        "saved@example.invalid"
    );
    assert!(mails[0]["textbody"]
        .as_str()
        .unwrap()
        .contains("Hello Ada Test,"));
    drop(mails);
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "tool_confirmation_decision"));
    h.close().await;
    std::fs::remove_file(path).unwrap();
}
#[tokio::test]
async fn directory_alternate_requires_separate_confirmation_and_sends_exact_address() {
    let path = contacts_db();
    let h = directory_harness(true, Some(&path)).await;
    h.session
        .input
        .send(Input::Text("directory email alternate request".into()))
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(6), async {
        loop {
            if h.fixture.requests.lock().unwrap().iter().any(|r| {
                r["input"]
                    .as_str()
                    .is_some_and(|s| s.contains("other at example dot invalid"))
            }) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .unwrap();
    assert!(h.fixture.directory_mails.lock().unwrap().is_empty());
    h.session
        .input
        .send(Input::Text("confirmed request".into()))
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(6), h.fixture.mail_started.notified())
        .await
        .unwrap();
    assert_eq!(
        h.fixture.directory_mails.lock().unwrap()[0]["to"][0]["email_address"]["address"],
        "other@example.invalid"
    );
    h.close().await;
    std::fs::remove_file(path).unwrap();
}
async fn wait_idle_end(h: &mut Harness) {
    tokio::time::timeout(Duration::from_secs(5), async {
        while let Some(output) = h.out.recv().await {
            if let Output::EndCall { reason } = output {
                assert_eq!(reason, "inactivity_pilot_caller_absent");
                return;
            }
        }
        panic!("coordinator closed before end-call request");
    })
    .await
    .unwrap();
}
async fn wait_directory_operations(h: &mut Harness, count: usize) {
    tokio::time::timeout(Duration::from_secs(8),async{loop{if h.trace.events().iter().filter(|e|e.event=="tool_execution_complete").count()>=count{return;}tokio::select!{packet=h.out.recv()=>if let Some(Output::Error(e))=packet{panic!("{e}");},_=tokio::time::sleep(Duration::from_millis(5))=>{}}}}).await.unwrap_or_else(|e|panic!("{e}: {:?}",h.trace.events()));
}
#[tokio::test]
async fn directory_speculation_waits_for_final_and_matching_plan_reuses_generation() {
    let mut h = directory_harness(false, None).await;
    h.voice().await;
    h.session
        .input
        .send(Input::Partial("directory request".into()))
        .await
        .unwrap();
    h.until("llm_directory_guard_decision").await;
    assert_eq!(
        h.fixture
            .directory_reads
            .load(std::sync::atomic::Ordering::SeqCst),
        0
    );
    assert!(h.sink.pcm.lock().unwrap().is_empty());
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "directory request".into(),
        })
        .await
        .unwrap();
    h.complete(1).await;
    assert_eq!(
        h.fixture
            .directory_reads
            .load(std::sync::atomic::Ordering::SeqCst),
        1
    );
    assert_eq!(
        h.fixture
            .requests
            .lock()
            .unwrap()
            .iter()
            .filter(|r| r["messages"].is_array())
            .count(),
        2
    );
    assert!(h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "speculation_authorized"));
    h.close().await;
}
#[tokio::test]
async fn directory_changed_final_discards_speculative_lookup() {
    let mut h = directory_harness(false, None).await;
    h.voice().await;
    h.session
        .input
        .send(Input::Partial("directory request".into()))
        .await
        .unwrap();
    h.until("llm_directory_guard_decision").await;
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "a different ordinary request".into(),
        })
        .await
        .unwrap();
    h.complete(1).await;
    assert_eq!(
        h.fixture
            .directory_reads
            .load(std::sync::atomic::Ordering::SeqCst),
        0
    );
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "provider_lookup_started"));
    h.close().await;
}
#[tokio::test]
async fn directory_email_unknown_survives_restart_without_duplicate() {
    let path = contacts_db();
    let mut h = directory_harness(true, Some(&path)).await;
    h.fixture
        .mail_unknown
        .store(true, std::sync::atomic::Ordering::SeqCst);
    h.session
        .input
        .send(Input::Text("directory email request".into()))
        .await
        .unwrap();
    wait_directory_operations(&mut h, 2).await;
    assert!(h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "tool_execution_complete" && e.data["status"] == "unknown"));
    assert_eq!(h.fixture.directory_mails.lock().unwrap().len(), 1);
    h.close().await;
    let mut h = directory_harness(true, Some(&path)).await;
    h.session
        .input
        .send(Input::Text("directory email request".into()))
        .await
        .unwrap();
    wait_directory_operations(&mut h, 2).await;
    assert!(h.fixture.directory_mails.lock().unwrap().is_empty());
    assert!(h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "tool_execution_complete" && e.data["status"] == "unknown"));
    h.close().await;
    std::fs::remove_file(path).unwrap();
}
#[tokio::test]
async fn directory_accepted_email_survives_restart_without_duplicate() {
    let path = contacts_db();
    let mut h = directory_harness(true, Some(&path)).await;
    h.session
        .input
        .send(Input::Text("directory email request".into()))
        .await
        .unwrap();
    wait_directory_operations(&mut h, 2).await;
    assert_eq!(h.fixture.directory_mails.lock().unwrap().len(), 1);
    h.close().await;
    let mut h = directory_harness(true, Some(&path)).await;
    h.session
        .input
        .send(Input::Text("directory email request".into()))
        .await
        .unwrap();
    wait_directory_operations(&mut h, 2).await;
    assert!(h.fixture.directory_mails.lock().unwrap().is_empty());
    assert!(h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "tool_execution_complete" && e.data["status"] == "success"));
    h.close().await;
    std::fs::remove_file(path).unwrap();
}
#[tokio::test]
async fn directory_inflight_email_survives_caller_barge_and_records_acceptance() {
    let path = contacts_db();
    let mut h = directory_harness(true, Some(&path)).await;
    h.fixture
        .hold_mail
        .store(true, std::sync::atomic::Ordering::SeqCst);
    h.session
        .input
        .send(Input::Text("directory email request".into()))
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(6), h.fixture.mail_started.notified())
        .await
        .unwrap();
    h.voice().await;
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 2,
            text: "a different ordinary request".into(),
        })
        .await
        .unwrap();
    h.complete(2).await;
    assert_eq!(h.fixture.directory_mails.lock().unwrap().len(), 1);
    h.fixture.mail_release.notify_one();
    wait_directory_operations(&mut h, 2).await;
    let db = rusqlite::Connection::open(&path).unwrap();
    let status: String = db
        .query_row("SELECT status FROM directory_email_outbox", [], |r| {
            r.get(0)
        })
        .unwrap();
    assert_eq!(status, "accepted");
    drop(db);
    h.close().await;
    std::fs::remove_file(path).unwrap();
}
#[tokio::test]
async fn directory_inflight_read_cancels_on_new_question_without_stale_answer() {
    let mut h = directory_harness(false, None).await;
    h.fixture
        .hold_directory
        .store(true, std::sync::atomic::Ordering::SeqCst);
    h.session
        .input
        .send(Input::Text("directory request".into()))
        .await
        .unwrap();
    tokio::time::timeout(
        Duration::from_secs(3),
        h.fixture.directory_started.notified(),
    )
    .await
    .unwrap();
    h.voice().await;
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 2,
            text: "a different ordinary request".into(),
        })
        .await
        .unwrap();
    h.fixture.directory_release.notify_one();
    h.complete(2).await;
    assert!(
        !h.fixture.requests.lock().unwrap().iter().any(|r| r["input"]
            .as_str()
            .is_some_and(|s| s.contains("Alpha Clinic")))
    );
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "tool_execution_complete"));
    h.close().await;
}
#[tokio::test]
async fn inactivity_checkin_goodbye_then_hangup_follows_audio_drain() {
    let mut h = inactivity_harness().await;
    finish_caller(&mut h, "first request").await;
    wait_idle_end(&mut h).await;
    let events = h.trace.events();
    let checkin = events
        .iter()
        .position(|e| e.event == "inactivity_pilot_checkin")
        .unwrap();
    let goodbye = events
        .iter()
        .position(|e| e.event == "inactivity_pilot_goodbye")
        .unwrap();
    let end = events
        .iter()
        .position(|e| e.event == "inactivity_pilot_hangup")
        .unwrap();
    assert!(checkin < goodbye && goodbye < end);
    assert!(events[goodbye..end]
        .iter()
        .any(|e| e.event == "playout_complete"));
    assert_eq!(
        events
            .iter()
            .filter(|e| e.event == "user_speech_start")
            .count(),
        1
    );
    assert!(events
        .iter()
        .filter(|e| e.event == "audio_first_enqueued")
        .any(|e| e.turn >= 1 << 63));
    h.close().await;
}
#[tokio::test]
async fn inactivity_pilot_falls_back_when_model_selects_silence() {
    let mut h = inactivity_harness().await;
    h.fixture
        .idle_silent
        .store(true, std::sync::atomic::Ordering::SeqCst);
    finish_caller(&mut h, "first request").await;
    wait_idle_end(&mut h).await;
    assert!(h.fixture.requests.lock().unwrap().iter().any(|r| r["input"]
        .as_str()
        .is_some_and(|s| s.starts_with("Are you still there?"))));
    h.close().await;
}
#[tokio::test]
async fn inactivity_pilot_falls_back_when_model_output_is_invalid() {
    let mut h = inactivity_harness().await;
    h.fixture
        .idle_invalid
        .store(true, std::sync::atomic::Ordering::SeqCst);
    finish_caller(&mut h, "first request").await;
    wait_idle_end(&mut h).await;
    assert!(h.fixture.requests.lock().unwrap().iter().any(|r| r["input"]
        .as_str()
        .is_some_and(|s| s.starts_with("Are you still there?"))));
    h.close().await;
}
#[tokio::test]
async fn confirmed_caller_return_cancels_inactivity_hangup() {
    let mut h = inactivity_harness().await;
    finish_caller(&mut h, "first request").await;
    h.until("inactivity_pilot_checkin").await;
    h.voice().await;
    tokio::time::sleep(Duration::from_millis(350)).await;
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "inactivity_pilot_hangup"));
    h.close().await;
}
#[tokio::test]
async fn caller_can_interrupt_goodbye_without_stale_hangup() {
    let mut h = inactivity_harness().await;
    finish_caller(&mut h, "first request").await;
    h.until("inactivity_pilot_goodbye").await;
    h.voice().await;
    tokio::time::sleep(Duration::from_millis(350)).await;
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "inactivity_pilot_hangup"));
    h.close().await;
}
#[tokio::test]
async fn inactivity_does_not_reopen_explicitly_muted_output() {
    let mut h = inactivity_harness().await;
    finish_caller(&mut h, "please silence").await;
    tokio::time::sleep(Duration::from_millis(350)).await;
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|e| e.event.starts_with("inactivity_pilot_")));
    h.close().await;
}
#[tokio::test]
async fn failed_goodbye_synthesis_still_allows_safe_hangup() {
    let mut h = inactivity_harness().await;
    finish_caller(&mut h, "first request").await;
    h.until("inactivity_pilot_checkin").await;
    h.fixture
        .tts_fail
        .store(true, std::sync::atomic::Ordering::SeqCst);
    wait_idle_end(&mut h).await;
    assert!(h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "engagement_failed"));
    h.close().await;
}

#[tokio::test]
async fn reply_grace_retains_prior_words_and_cancels_response_on_continuation() {
    let mut h = Harness::configured(true, |c| {
        c.agent_context.agent_id = "selected".into();
        c.reply_grace_agent_ids = vec!["selected".into()];
        c.reply_grace_ms = 500.0;
    })
    .await;
    h.voice().await;
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "first request".into(),
        })
        .await
        .unwrap();
    h.until("reply_grace_start").await;
    h.voice().await;
    tokio::time::sleep(Duration::from_millis(600)).await;
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "generation_start" && e.turn == 1));
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 2,
            text: "second request".into(),
        })
        .await
        .unwrap();
    h.complete(2).await;
    assert!(h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "reply_grace_elapsed" && e.turn == 2));
    let requests = h.fixture.requests.lock().unwrap().clone();
    let last = requests
        .iter()
        .find(|r| r.get("messages").is_some())
        .unwrap();
    assert!(last["messages"]
        .as_array()
        .unwrap()
        .iter()
        .any(|m| m["content"] == "first request"));
    h.close().await;
}
#[tokio::test]
async fn text_uses_committed_path_without_manufacturing_voice_evidence() {
    let mut h = Harness::new(true).await;
    h.session
        .input
        .send(Input::Text("  first request  ".into()))
        .await
        .unwrap();
    h.complete(1).await;
    let events = h.trace.events();
    assert!(events
        .iter()
        .any(|e| e.event == "stt_final" && e.data["text"] == "first request"));
    assert!(!events
        .iter()
        .any(|e| e.event == "user_speech_start" || e.event == "input_pcm"));
    h.close().await;
}
#[tokio::test]
async fn private_asr_hint_echo_never_reaches_conversation_or_model() {
    let mut h = Harness::configured(true, |c| {
        c.stt_context =
            "Listen on this call. The bank is Wema Bank; network names include MTN and Glo.".into();
    })
    .await;
    h.voice().await;
    h.session
        .input
        .send(Input::Partial("Listen on this call.".into()))
        .await
        .unwrap();
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "Listen on this call.".into(),
        })
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(!h.trace.events().iter().any(|e| e.event == "stt_final"
        || e.event == "generation_start"
        || e.event == "speculation_start"));
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "Please check my Wema Bank balance.".into(),
        })
        .await
        .unwrap();
    h.complete(1).await;
    h.close().await;
}

fn configured_ending(c: &mut Config) {
    c.agent_context.loaded = true;
    c.agent_context.tools = vec![json!({"name":"end_call","url":"builtin://end_call"})];
}
#[tokio::test]
async fn configured_end_call_speaks_goodbye_and_drains_before_deleting_room() {
    let mut h = Harness::configured(true, configured_ending).await;
    h.session
        .input
        .send(Input::Text("goodbye request".into()))
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(4), async {
        loop {
            if let Some(Output::EndCall { reason }) = h.out.recv().await {
                assert_eq!(reason, "configured_end_call_after_goodbye");
                break;
            }
        }
    })
    .await
    .unwrap();
    let events = h.trace.events();
    let authorized = events
        .iter()
        .position(|e| e.event == "end_call_authorized")
        .unwrap();
    let close = events
        .iter()
        .position(|e| e.event == "end_call_playout_complete")
        .unwrap();
    assert!(events[authorized..close]
        .iter()
        .any(|e| e.event == "playout_complete"));
    assert!(h
        .fixture
        .requests
        .lock()
        .unwrap()
        .iter()
        .filter_map(|r| r["tools"].as_array())
        .flatten()
        .any(|t| t["function"]["name"] == "end_call"));
    h.close().await;
}
#[tokio::test]
async fn end_call_is_unavailable_when_not_configured() {
    let h = Harness::new(true).await;
    h.session
        .input
        .send(Input::Text("goodbye request".into()))
        .await
        .unwrap();
    h.until("error").await;
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "end_call_authorized"));
    h.close().await;
}
#[tokio::test]
async fn speculative_end_call_never_overrides_changed_authoritative_final() {
    let mut h = Harness::configured(true, configured_ending).await;
    h.voice().await;
    h.session
        .input
        .send(Input::Partial("goodbye request".into()))
        .await
        .unwrap();
    h.until("speculation_start").await;
    tokio::time::sleep(Duration::from_millis(40)).await;
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "end_call_authorized"));
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "first request instead".into(),
        })
        .await
        .unwrap();
    h.complete(1).await;
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "end_call_authorized"));
    h.close().await;
}

#[tokio::test]
async fn whisper_final_fallback_receives_full_owned_audio_and_never_uses_partial() {
    use super::providers::{start_asr, AsrEvent, Providers};
    let fixture = Arc::new(AsrFixture::default());
    *fixture.flushed_text.lock().unwrap() = Some("owned complete request".into());
    fixture
        .fail
        .store(true, std::sync::atomic::Ordering::SeqCst);
    let app = Router::new()
        .route("/ws", axum::routing::get(asr_ws))
        .route("/final", post(asr_final))
        .with_state(fixture.clone());
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let mut c: Config =
        serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
    c.stt_model = "whisper-large-v3-turbo".into();
    c.stt_warmup_ms = 0.0;
    c.stt_ws_url = format!("ws://{addr}/ws");
    c.stt_batch_url.clear();
    c.stt_chat_url = format!("http://{addr}/final");
    c.stt_batch_timeout_seconds = 0.5;
    assert!(c.validate().is_ok());
    let trace = Trace::default();
    let mut asr = start_asr(Providers::new(Arc::new(c), trace.clone()).unwrap());
    assert!(matches!(asr.events.recv().await, Some(AsrEvent::Ready)));
    asr.append(vec![1000; 1600]);
    assert!(matches!(
        asr.events.recv().await,
        Some(AsrEvent::Partial(_))
    ));
    asr.commit(91, vec![1000; 3200]).await.unwrap();
    let text = tokio::time::timeout(Duration::from_secs(4), async {
        loop {
            if let Some(AsrEvent::Final { turn, text }) = asr.events.recv().await {
                assert_eq!(turn, 91);
                break text;
            }
        }
    })
    .await
    .unwrap();
    assert_eq!(text, "owned complete request");
    assert_eq!(
        fixture
            .max_stream_samples
            .load(std::sync::atomic::Ordering::SeqCst),
        3200
    );
    assert!(trace
        .events()
        .iter()
        .any(|e| e.event == "stt_chat_final_fallback"));
    asr.task.abort();
    let _ = asr.task.await;
    server.abort();
}

#[tokio::test]
async fn configured_ending_preserves_fast_speculative_validation_for_normal_replies() {
    let mut h = Harness::configured(true, configured_ending).await;
    h.voice().await;
    h.session
        .input
        .send(Input::Partial("first request.".into()))
        .await
        .unwrap();
    h.until("speculation_start").await;
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "first request!".into(),
        })
        .await
        .unwrap();
    h.complete(1).await;
    let requests = h.fixture.requests.lock().unwrap().clone();
    assert_eq!(
        requests
            .iter()
            .filter(|r| r.get("messages").is_some())
            .count(),
        1
    );
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "end_call_authorized"));
    h.close().await;
}

async fn knowledge_service(
    State(fixture): State<Arc<Fixture>>,
    headers: axum::http::HeaderMap,
    Json(body): Json<Value>,
) -> Json<Value> {
    assert_eq!(headers["x-business-id"], "business-a");
    assert_eq!(headers["x-agent-id"], "agent-a");
    fixture.requests.lock().unwrap().push(body);
    fixture.knowledge_started.notify_one();
    if fixture
        .hold_knowledge
        .load(std::sync::atomic::Ordering::SeqCst)
    {
        fixture.knowledge_release.notified().await;
    }
    Json(
        json!({"matches":[{"source_name":"Agent policy","text":"Use only this verified policy.","score":0.9}]}),
    )
}
async fn knowledge_harness() -> Harness {
    Harness::configured_platform(
        true,
        |c| {
            c.agent_context.loaded = true;
            c.agent_context.business_id = "business-a".into();
            c.agent_context.agent_id = "agent-a".into();
            c.agent_context.end_user_id = "caller-a".into();
            c.agent_context.knowledge_base_ids = vec!["kb-a".into()];
        },
        None,
        true,
    )
    .await
}
#[tokio::test]
async fn knowledge_is_deferred_until_final_then_followup_uses_scoped_facts() {
    let mut h = knowledge_harness().await;
    h.voice().await;
    h.session
        .input
        .send(Input::Partial("knowledge request".into()))
        .await
        .unwrap();
    h.until("speculation_start").await;
    tokio::time::sleep(Duration::from_millis(40)).await;
    assert!(!h
        .fixture
        .requests
        .lock()
        .unwrap()
        .iter()
        .any(|r| r.get("knowledge_base_ids").is_some()));
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "knowledge request".into(),
        })
        .await
        .unwrap();
    h.complete(1).await;
    assert!(h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "knowledge_lookup_complete"));
    let requests = h.fixture.requests.lock().unwrap().clone();
    let followup = requests
        .iter()
        .rfind(|r| r.get("messages").is_some())
        .unwrap();
    assert!(followup["messages"][0]["content"]
        .as_str()
        .unwrap()
        .contains("Use only this verified policy."));
    assert_eq!(
        requests
            .iter()
            .filter(|r| r.get("knowledge_base_ids").is_some())
            .count(),
        1
    );
    h.close().await;
}
#[tokio::test]
async fn mismatched_final_cancels_speculative_knowledge_without_service_effect() {
    let mut h = knowledge_harness().await;
    h.voice().await;
    h.session
        .input
        .send(Input::Partial("knowledge request".into()))
        .await
        .unwrap();
    h.until("speculation_start").await;
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "first request instead".into(),
        })
        .await
        .unwrap();
    h.complete(1).await;
    assert!(!h
        .fixture
        .requests
        .lock()
        .unwrap()
        .iter()
        .any(|r| r.get("knowledge_base_ids").is_some()));
    h.close().await;
}
#[tokio::test]
async fn slow_knowledge_request_never_blocks_caller_interruption_or_releases_stale_answer() {
    let mut h = knowledge_harness().await;
    h.fixture
        .hold_knowledge
        .store(true, std::sync::atomic::Ordering::SeqCst);
    h.session
        .input
        .send(Input::Text("natural knowledge request".into()))
        .await
        .unwrap();
    tokio::time::timeout(
        Duration::from_secs(2),
        h.fixture.knowledge_started.notified(),
    )
    .await
    .unwrap();
    h.voice().await;
    tokio::time::timeout(Duration::from_secs(2), async {
        while !h
            .trace
            .events()
            .iter()
            .any(|e| e.event == "user_speech_start" && e.turn == 2)
        {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    h.fixture.knowledge_release.notify_one();
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 2,
            text: "second request".into(),
        })
        .await
        .unwrap();
    h.complete(2).await;
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "knowledge_lookup_complete" && e.turn == 1));
    let requests = h.fixture.requests.lock().unwrap().clone();
    let last = requests
        .iter()
        .rfind(|r| r.get("messages").is_some())
        .unwrap();
    assert!(last["messages"]
        .as_array()
        .unwrap()
        .iter()
        .any(|m| m["role"] == "assistant"
            && m["content"].as_str().is_some_and(
                |s| s.starts_with("Let me check that for you.") && s.contains("interrupted")
            )));
    h.close().await;
}

#[tokio::test]
async fn unoffered_closing_tool_cannot_reopen_muted_output_or_loop_requests() {
    let mut h = Harness::configured(true, configured_ending).await;
    h.session
        .input
        .send(Input::Text("please silence".into()))
        .await
        .unwrap();
    h.complete(1).await;
    h.session
        .input
        .send(Input::Text("goodbye request".into()))
        .await
        .unwrap();
    h.until("error").await;
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "end_call_authorized"));
    assert_eq!(
        h.fixture
            .requests
            .lock()
            .unwrap()
            .iter()
            .filter(|r| r.get("messages").is_some())
            .count(),
        2
    );
    h.close().await;
}

#[tokio::test]
async fn natural_lookup_acknowledgement_routes_to_knowledge_and_retains_final_answer() {
    let mut h = knowledge_harness().await;
    h.session
        .input
        .send(Input::Text("natural knowledge request".into()))
        .await
        .unwrap();
    h.complete(1).await;
    assert!(h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "knowledge_acknowledgement_routed"));
    assert!(h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "knowledge_lookup_complete"));
    h.close().await;
}

#[tokio::test]
async fn failed_configured_room_close_leaves_the_caller_able_to_continue() {
    let mut h = Harness::configured(true, configured_ending).await;
    h.session
        .input
        .send(Input::Text("goodbye request".into()))
        .await
        .unwrap();
    let reason = tokio::time::timeout(Duration::from_secs(4), async {
        while let Some(out) = h.out.recv().await {
            if let Output::EndCall { reason } = out {
                return reason;
            }
        }
        panic!("closed before room-close request")
    })
    .await
    .unwrap();
    h.session
        .input
        .send(Input::EndCallFailed { reason })
        .await
        .unwrap();
    h.until("configured_end_call_failed").await;
    h.session
        .input
        .send(Input::Text("second request".into()))
        .await
        .unwrap();
    h.complete(2).await;
    h.close().await;
}

async fn ticket_service(
    State(fixture): State<Arc<Fixture>>,
    headers: axum::http::HeaderMap,
    Json(body): Json<Value>,
) -> Response {
    assert_eq!(headers["x-business-id"], "business-a");
    assert_eq!(body["agent_id"], "agent-a");
    assert_eq!(body["customer_contact"], "caller-a");
    fixture.requests.lock().unwrap().push(body);
    fixture.ticket_started.notify_one();
    if fixture
        .hold_ticket
        .load(std::sync::atomic::Ordering::SeqCst)
    {
        fixture.ticket_release.notified().await;
    }
    if fixture
        .fail_ticket
        .load(std::sync::atomic::Ordering::SeqCst)
    {
        return axum::http::StatusCode::BAD_GATEWAY.into_response();
    }
    Json(json!({"ticket_code":"TKT-001"})).into_response()
}
async fn action_harness() -> Harness {
    Harness::configured_platform(
        true,
        |c| {
            c.agent_context.loaded = true;
            c.agent_context.business_id = "business-a".into();
            c.agent_context.agent_id = "agent-a".into();
            c.agent_context.end_user_id = "caller-a".into();
            c.agent_context.tools =
                vec![json!({"name":"create_ticket","url":"internal://create-ticket"})];
        },
        None,
        true,
    )
    .await
}
fn ticket_posts(h: &Harness) -> usize {
    h.fixture
        .requests
        .lock()
        .unwrap()
        .iter()
        .filter(|r| r.get("customer_contact").is_some())
        .count()
}
#[tokio::test]
async fn ticket_requires_separate_committed_confirmation_without_magic_words() {
    let mut h = action_harness().await;
    h.session
        .input
        .send(Input::Text("ticket request".into()))
        .await
        .unwrap();
    h.complete(1).await;
    assert_eq!(ticket_posts(&h), 0);
    h.voice().await;
    h.session
        .input
        .send(Input::Partial("That would be very helpful.".into()))
        .await
        .unwrap();
    h.until("speculation_start").await;
    tokio::time::sleep(Duration::from_millis(40)).await;
    assert_eq!(ticket_posts(&h), 0);
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 2,
            text: "That would be very helpful.".into(),
        })
        .await
        .unwrap();
    h.complete(2).await;
    assert_eq!(ticket_posts(&h), 1);
    assert!(h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "tool_confirmation_decision" && e.data["decision"] == "confirmed"));
    h.close().await;
}
#[tokio::test]
async fn confirmation_uses_changed_final_and_never_executes_speculative_agreement() {
    let mut h = action_harness().await;
    h.session
        .input
        .send(Input::Text("ticket request".into()))
        .await
        .unwrap();
    h.complete(1).await;
    h.voice().await;
    h.session
        .input
        .send(Input::Partial("Please go ahead.".into()))
        .await
        .unwrap();
    h.until("speculation_start").await;
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 2,
            text: "reject request please".into(),
        })
        .await
        .unwrap();
    h.complete(2).await;
    assert_eq!(ticket_posts(&h), 0);
    assert!(h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "tool_confirmation_decision" && e.data["decision"] == "rejected"));
    h.close().await;
}
#[tokio::test]
async fn committed_action_survives_interruption_and_is_never_sent_twice() {
    let mut h = action_harness().await;
    h.fixture
        .hold_ticket
        .store(true, std::sync::atomic::Ordering::SeqCst);
    h.session
        .input
        .send(Input::Text("ticket request".into()))
        .await
        .unwrap();
    h.complete(1).await;
    h.session
        .input
        .send(Input::Text("That would help, thanks.".into()))
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(2), h.fixture.ticket_started.notified())
        .await
        .unwrap();
    h.voice().await;
    tokio::time::timeout(Duration::from_secs(2), async {
        while !h
            .trace
            .events()
            .iter()
            .any(|e| e.event == "user_speech_start" && e.turn == 3)
        {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(ticket_posts(&h), 1);
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 3,
            text: "second request".into(),
        })
        .await
        .unwrap();
    h.complete(3).await;
    h.fixture.ticket_release.notify_one();
    h.until("tool_execution_complete").await;
    assert_eq!(ticket_posts(&h), 1);
    assert!(h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "tool_execution_complete" && e.data["status"] == "success"));
    h.close().await;
}

#[tokio::test]
async fn pending_operation_updates_are_interruptible_and_never_trigger_idle_hangup() {
    let mut h = Harness::configured_platform(
        true,
        |c| {
            c.agent_context.loaded = true;
            c.agent_context.business_id = "business-a".into();
            c.agent_context.agent_id = "agent-a".into();
            c.agent_context.end_user_id = "caller-a".into();
            c.agent_context.tools =
                vec![json!({"name":"create_ticket","url":"internal://create-ticket"})];
            c.engagement_ack_seconds = 0.03;
            c.engagement_tool_seconds = 0.1;
            c.inactivity_pilot_enabled = true;
            c.engagement_idle_seconds = 0.05;
            c.inactivity_pilot_close_seconds = 0.05;
        },
        None,
        true,
    )
    .await;
    h.fixture
        .hold_ticket
        .store(true, std::sync::atomic::Ordering::SeqCst);
    h.session
        .input
        .send(Input::Text("ticket request".into()))
        .await
        .unwrap();
    h.complete(1).await;
    h.session
        .input
        .send(Input::Text("That would help.".into()))
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(2), h.fixture.ticket_started.notified())
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(2), async {
        while !h
            .trace
            .events()
            .iter()
            .any(|e| e.event == "engagement_spoken" && e.data["reason"] == "tool_started")
        {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "inactivity_pilot_hangup"));
    h.voice().await;
    tokio::time::timeout(Duration::from_secs(2), async {
        while !h
            .trace
            .events()
            .iter()
            .any(|e| e.event == "user_speech_start" && e.turn == 3)
        {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    h.fixture.ticket_release.notify_one();
    h.until("tool_execution_complete").await;
    assert_eq!(ticket_posts(&h), 1);
    h.close().await;
}
#[tokio::test]
async fn unknown_outcome_is_retained_and_never_retried_automatically() {
    let mut h = action_harness().await;
    h.fixture
        .fail_ticket
        .store(true, std::sync::atomic::Ordering::SeqCst);
    h.session
        .input
        .send(Input::Text("ticket request".into()))
        .await
        .unwrap();
    h.complete(1).await;
    h.session
        .input
        .send(Input::Text("That would help.".into()))
        .await
        .unwrap();
    h.complete(2).await;
    assert!(h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "tool_execution_complete" && e.data["status"] == "unknown"));
    h.session
        .input
        .send(Input::Text("ticket request again".into()))
        .await
        .unwrap();
    h.until("error").await;
    assert_eq!(ticket_posts(&h), 1);
    h.close().await;
}
#[tokio::test]
async fn unclear_confirmation_has_a_bounded_reask_without_any_side_effect() {
    let mut h = action_harness().await;
    h.session
        .input
        .send(Input::Text("ticket request".into()))
        .await
        .unwrap();
    h.complete(1).await;
    for turn in 2..=4 {
        h.session
            .input
            .send(Input::Text("unclear request".into()))
            .await
            .unwrap();
        h.complete(turn).await;
    }
    h.session
        .input
        .send(Input::Text("That would be helpful.".into()))
        .await
        .unwrap();
    h.complete(5).await;
    assert_eq!(ticket_posts(&h), 0);
    h.close().await;
}

#[tokio::test]
async fn muted_caller_keeps_submitted_result_without_optional_speech_or_repeat() {
    let mut h = action_harness().await;
    h.fixture
        .hold_ticket
        .store(true, std::sync::atomic::Ordering::SeqCst);
    h.session
        .input
        .send(Input::Text("ticket request".into()))
        .await
        .unwrap();
    h.complete(1).await;
    h.session
        .input
        .send(Input::Text("That would help.".into()))
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(2), h.fixture.ticket_started.notified())
        .await
        .unwrap();
    h.session
        .input
        .send(Input::Text("silence please".into()))
        .await
        .unwrap();
    h.complete(3).await;
    let before = h.sink.pcm.lock().unwrap().len();
    h.fixture.ticket_release.notify_one();
    h.until("tool_execution_complete").await;
    tokio::time::sleep(Duration::from_millis(400)).await;
    assert_eq!(h.sink.pcm.lock().unwrap().len(), before);
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "engagement_spoken"));
    h.session
        .input
        .send(Input::Text("resume please".into()))
        .await
        .unwrap();
    h.complete(4).await;
    tokio::time::timeout(Duration::from_secs(2), async {
        while !h
            .trace
            .events()
            .iter()
            .any(|e| e.event == "engagement_spoken" && e.data["reason"] == "tool_completed")
        {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(ticket_posts(&h), 1);
    h.close().await;
}

async fn bank_service(
    State(fixture): State<Arc<Fixture>>,
    headers: axum::http::HeaderMap,
    Json(body): Json<Value>,
) -> Json<Value> {
    assert_eq!(headers["x-business-id"], "business-a");
    assert_eq!(headers["x-wema-customer-id"], "customer-a");
    let tool = headers["x-tool-name"].to_str().unwrap();
    fixture
        .requests
        .lock()
        .unwrap()
        .push(json!({"bank_request":tool,"arguments":body}));
    fixture.bank_started.notify_one();
    if fixture.hold_bank.load(std::sync::atomic::Ordering::SeqCst) {
        fixture.bank_release.notified().await;
    }
    if tool == "wema_prepare_data_purchase" {
        Json(
            json!({"status":"prepared","message":"Preview ready","data":{"operation_id":"op-latest","preview":{"package_name":"1 GB","network":"MTN","phone_number":"08000000000"}}}),
        )
    } else {
        Json(
            json!({"status":"ok","message":"Balance retrieved","data":{"availableBalance":"100.00"}}),
        )
    }
}
async fn bank_harness() -> Harness {
    Harness::configured_platform(true,|c|{
 c.agent_context.loaded=true;c.agent_context.agent_id="agent-a".into();c.agent_context.business_id="business-a".into();c.agent_context.end_user_id="caller-a".into();
 for name in ["wema_get_balance","wema_prepare_data_purchase","wema_execute_prepared"]{let (props,required)=if name=="wema_execute_prepared"{(json!({"operation_id":{"type":"string"}}),json!(["operation_id"]))}else{(json!({"source_account":{"type":"string","pattern":"^[0-9]{10}$"},"phone_number":{"type":"string","pattern":"^0[0-9]{10}$"}}),json!([]))};c.agent_context.tools.push(json!({"name":name,"url":"http://127.0.0.1:1/bank","method":"POST","request_schema":{"type":"object","properties":props,"required":required,"additionalProperties":false}}));}
},None,true).await
}
fn bank_posts(h: &Harness) -> Vec<Value> {
    h.fixture
        .requests
        .lock()
        .unwrap()
        .iter()
        .filter(|v| v["bank_request"].is_string())
        .cloned()
        .collect()
}
#[tokio::test]
async fn banking_preview_requires_separate_final_confirmation_and_executes_once() {
    let mut h = bank_harness().await;
    h.session
        .input
        .send(Input::Text("bank prepare a data package".into()))
        .await
        .unwrap();
    h.complete(1).await;
    assert_eq!(bank_posts(&h).len(), 1);
    assert_eq!(
        bank_posts(&h)[0]["bank_request"],
        "wema_prepare_data_purchase"
    );
    h.voice().await;
    h.session
        .input
        .send(Input::Partial("That would be helpful.".into()))
        .await
        .unwrap();
    h.until("speculation_start").await;
    assert_eq!(bank_posts(&h).len(), 1);
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 2,
            text: "That would be helpful.".into(),
        })
        .await
        .unwrap();
    h.complete(2).await;
    assert_eq!(bank_posts(&h).len(), 2);
    assert_eq!(bank_posts(&h)[1]["arguments"]["operation_id"], "op-latest");
    h.close().await;
}
#[tokio::test]
async fn interrupting_banking_wait_cue_prevents_submission() {
    let h = bank_harness().await;
    h.session
        .input
        .send(Input::Text("bank balance 0000000001".into()))
        .await
        .unwrap();
    h.until("audio_first_enqueued").await;
    h.voice().await;
    tokio::time::timeout(Duration::from_secs(2), async {
        while !h
            .trace
            .events()
            .iter()
            .any(|e| e.event == "user_speech_start" && e.turn == 2)
        {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    tokio::time::sleep(Duration::from_millis(1200)).await;
    assert!(bank_posts(&h).is_empty());
    h.close().await;
}
#[tokio::test]
async fn banking_speculation_requires_exact_final_for_account_numbers() {
    let mut h = bank_harness().await;
    h.voice().await;
    h.session
        .input
        .send(Input::Partial("bank balance for account 0000000001".into()))
        .await
        .unwrap();
    h.until("speculation_start").await;
    h.end().await;
    assert!(bank_posts(&h).is_empty());
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "bank balance for account 0000000002".into(),
        })
        .await
        .unwrap();
    h.complete(1).await;
    assert!(h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "attempt_cancel" && e.data["reason"] == "final_transcript_changed"));
    assert_eq!(bank_posts(&h).len(), 1);
    assert_eq!(
        bank_posts(&h)[0]["arguments"]["source_account"],
        "0000000002"
    );
    h.close().await;
}
#[tokio::test]
async fn submitted_banking_read_survives_interruption_without_repetition() {
    let mut h = bank_harness().await;
    h.fixture
        .hold_bank
        .store(true, std::sync::atomic::Ordering::SeqCst);
    h.session
        .input
        .send(Input::Text("bank balance 0000000001".into()))
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(3), h.fixture.bank_started.notified())
        .await
        .unwrap();
    h.voice().await;
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 2,
            text: "second question".into(),
        })
        .await
        .unwrap();
    h.complete(2).await;
    h.fixture.bank_release.notify_one();
    h.until("tool_execution_complete").await;
    assert_eq!(bank_posts(&h).len(), 1);
    h.close().await;
}
#[tokio::test]
async fn multi_account_tools_run_in_order_after_acknowledgement_and_retain_all_results() {
    let mut h = bank_harness().await;
    h.session
        .input
        .send(Input::Text("bank two accounts please".into()))
        .await
        .unwrap();
    h.complete(1).await;
    let posts = bank_posts(&h);
    assert_eq!(posts.len(), 2);
    assert_eq!(posts[0]["arguments"]["source_account"], "0000000001");
    assert_eq!(posts[1]["arguments"]["source_account"], "0000000002");
    let events = h.trace.events();
    let drain = events
        .iter()
        .position(|e| e.event == "playout_complete")
        .unwrap();
    let start = events
        .iter()
        .position(|e| e.event == "tool_execution_start")
        .unwrap();
    assert!(drain < start);
    {
        let requests = h.fixture.requests.lock().unwrap();
        let followup = requests
            .iter()
            .find(|r| {
                r["messages"][0]["content"]
                    .as_str()
                    .is_some_and(|s| s.contains("Verified banking result"))
            })
            .unwrap();
        assert_eq!(
            followup["messages"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|m| m["role"] == "tool")
                .count(),
            2
        );
    }
    h.session
        .input
        .send(Input::Text("second question".into()))
        .await
        .unwrap();
    h.complete(2).await;
    {
        let requests = h.fixture.requests.lock().unwrap();
        let later = requests
            .iter()
            .rev()
            .find(|r| r["messages"].is_array())
            .unwrap();
        assert!(later.to_string().contains("Your balance is 100 naira."));
    }
    h.close().await;
}

async fn device_begin_service(
    State(fixture): State<Arc<Fixture>>,
    headers: axum::http::HeaderMap,
    Json(body): Json<Value>,
) -> Json<Value> {
    assert_eq!(headers["x-business-id"], "business-a");
    assert_eq!(headers["x-wema-customer-id"], "customer-a");
    assert_eq!(body["feature"], "Transfer");
    fixture
        .requests
        .lock()
        .unwrap()
        .push(json!({"device_request":"begin"}));
    Json(
        json!({"status":"ok","data":{"authentication_id":"private-authentication","action":1,"options":{"challenge":"private-challenge","rpId":"bank.example"}}}),
    )
}
async fn device_execute_service(
    State(fixture): State<Arc<Fixture>>,
    headers: axum::http::HeaderMap,
    Json(body): Json<Value>,
) -> Json<Value> {
    assert_eq!(headers["x-wema-customer-id"], "customer-a");
    assert_eq!(body["operation_id"], "op-latest");
    assert_eq!(body["authentication_id"], "private-authentication");
    assert_eq!(body["fido2_credential_proof"]["id"], "private-credential");
    fixture
        .requests
        .lock()
        .unwrap()
        .push(json!({"device_request":"execute","operation_id":body["operation_id"]}));
    if fixture.hold_bank.load(std::sync::atomic::Ordering::SeqCst) {
        fixture.bank_release.notified().await;
    }
    Json(
        json!({"status":"ok","message":"Device transfer finished.","data":{"transaction_reference":"TXN-001","fido2_credential_proof":body["fido2_credential_proof"]}}),
    )
}
async fn device_harness() -> Harness {
    Harness::configured_platform(true,|c|{
 c.agent_context.loaded=true;c.agent_context.agent_id="agent-a".into();c.agent_context.business_id="business-a".into();c.agent_context.end_user_id="caller-a".into();c.passkey.business_ids=vec!["business-a".into()];c.passkey.agent_ids=vec!["agent-a".into()];c.passkey.connector.timeout_seconds=5.0;
 for name in ["wema_prepare_data_purchase","wema_execute_prepared"]{let (properties,required)=if name=="wema_execute_prepared"{(json!({"operation_id":{"type":"string"}}),json!(["operation_id"]))}else{(json!({}),json!([]))};c.agent_context.tools.push(json!({"name":name,"url":"http://127.0.0.1:1/bank","method":"POST","request_schema":{"type":"object","properties":properties,"required":required,"additionalProperties":false}}));}
},None,true).await
}
fn device_posts(h: &Harness) -> usize {
    h.fixture
        .requests
        .lock()
        .unwrap()
        .iter()
        .filter(|v| v["device_request"] == "execute")
        .count()
}
fn device_proof(h: &Harness) -> Vec<u8> {
    let packets = h.fixture.private_packets.lock().unwrap();
    let ceremony = packets
        .iter()
        .rev()
        .find(|p| p["type"] == "attentive.fido2.v1" && p["payload"]["type"] == "fido2_ceremony")
        .unwrap();
    serde_json::to_vec(&json!({"type":"fido2_proof","authenticationId":ceremony["payload"]["authenticationId"],"correlationId":ceremony["payload"]["correlationId"],"proof":{"id":"private-credential","response":{"signature":"private-signature"}}})).unwrap()
}
#[tokio::test]
async fn passkey_policy_starts_after_final_but_spoken_agreement_never_submits_money() {
    let mut h = device_harness().await;
    h.session
        .input
        .send(Input::Text("bank prepare please".into()))
        .await
        .unwrap();
    h.complete(1).await;
    h.session
        .input
        .send(Input::Text("That would be helpful.".into()))
        .await
        .unwrap();
    h.complete(2).await;
    assert_eq!(bank_posts(&h).len(), 1);
    assert_eq!(device_posts(&h), 0);
    let proof = device_proof(&h);
    h.session
        .input
        .send(Input::DeviceFrame(proof.clone()))
        .await
        .unwrap();
    h.session
        .input
        .send(Input::DeviceFrame(proof))
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(2), async {
        while h
            .trace
            .events()
            .iter()
            .filter(|e| e.event == "tool_execution_complete")
            .count()
            < 2
        {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    h.complete(2).await;
    assert_eq!(device_posts(&h), 1);
    {
        let requests = h.fixture.requests.lock().unwrap();
        for request in requests.iter().filter(|r| r["messages"].is_array()) {
            let text = request.to_string();
            for secret in [
                "private-credential",
                "private-signature",
                "private-authentication",
                "private-challenge",
            ] {
                assert!(!text.contains(secret));
            }
        }
    }
    let trace = serde_json::to_string(&h.trace.events()).unwrap();
    assert!(!trace.contains("private-credential"));
    assert!(!trace.contains("private-signature"));
    h.close().await;
}
#[tokio::test]
async fn passkey_submission_survives_interruption_without_replay() {
    let mut h = device_harness().await;
    h.session
        .input
        .send(Input::Text("bank prepare please".into()))
        .await
        .unwrap();
    h.complete(1).await;
    h.session
        .input
        .send(Input::Text("That would be helpful.".into()))
        .await
        .unwrap();
    h.complete(2).await;
    h.fixture
        .hold_bank
        .store(true, std::sync::atomic::Ordering::SeqCst);
    h.session
        .input
        .send(Input::DeviceFrame(device_proof(&h)))
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(2), async {
        while device_posts(&h) == 0 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    h.session
        .input
        .send(Input::Text("second question".into()))
        .await
        .unwrap();
    h.complete(3).await;
    h.fixture.bank_release.notify_one();
    tokio::time::timeout(Duration::from_secs(2), async {
        while h
            .trace
            .events()
            .iter()
            .filter(|e| e.event == "tool_execution_complete")
            .count()
            < 2
        {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert_eq!(device_posts(&h), 1);
    h.close().await;
}
#[tokio::test]
async fn native_provider_usage_handles_usage_only_frames_unicode_and_rejected_tts() {
    let fixture = Arc::new(Fixture::default());
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let app = Router::new()
        .route("/chat/completions", post(llm))
        .route("/tts", post(tts))
        .with_state(fixture.clone());
    let server = tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    let mut config: Config =
        serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
    config.llm_base_url = base.clone();
    config.tts_url = format!("{base}/tts");
    let p = providers::Providers::new(Arc::new(config), Trace::default()).unwrap();
    assert_eq!(
        p.collect(
            vec![
                json!({"role":"system","content":"Follow the fixture."}),
                json!({"role":"user","content":"usage frame request"})
            ],
            32,
            0.0
        )
        .await
        .unwrap(),
        "First answer."
    );
    let snapshot = p.usage.snapshot(false);
    assert_eq!(snapshot["llm_input_tokens"], 42);
    assert_eq!(snapshot["llm_output_tokens"], 3);
    assert_eq!(snapshot["llm_cached_tokens"], Value::Null);
    assert_eq!(snapshot["llm_requests"], 1);
    let (tx, mut rx) = tokio::sync::mpsc::channel(8);
    let consumer = tokio::spawn(async move { while rx.recv().await.is_some() {} });
    let text = "Ẹ káàrọ̀ 👋";
    p.tts(text, 1, 1, &tx).await.unwrap();
    assert_eq!(
        p.usage.snapshot(false)["tts_characters"],
        text.chars().count()
    );
    fixture
        .tts_fail
        .store(true, std::sync::atomic::Ordering::SeqCst);
    assert!(p.tts("Rejected text", 1, 2, &tx).await.is_err());
    assert_eq!(
        p.usage.snapshot(false)["tts_characters"],
        text.chars().count()
    );
    drop(tx);
    consumer.await.unwrap();
    server.abort();
}
fn configured_human(c: &mut Config) {
    c.agent_context.loaded = true;
    c.agent_context.tools =
        vec![json!({"name":"transfer_to_aicc","url":"builtin://transfer_to_aicc"})];
}
#[test]
fn native_llm_payloads_match_unchanged_python_across_provider_modes() {
    let f: Value =
        serde_json::from_str(include_str!("../../fixtures/python-parity-reference.json")).unwrap();
    for case in f["llm_payloads"].as_array().unwrap() {
        let mut config: Config =
            serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
        config.llm_provider = case["provider"].as_str().unwrap().into();
        config.llm_base_url = case["base_url"].as_str().unwrap().into();
        config.llm_model = "local-fixture-model".into();
        config.llm_enable_thinking = case["thinking"].as_bool().unwrap();
        config.llm_reasoning_effort = case["effort"].as_str().unwrap().into();
        let actual = providers::llm_body(
            &config,
            case["messages"].as_array().unwrap().clone(),
            case["tools"].as_array().unwrap().clone(),
            providers::LlmOptions {
                required: true,
                ..Default::default()
            },
        );
        assert_eq!(actual, case["expected"], "{case}");
    }
}
#[tokio::test]
async fn cancelled_llm_stream_reports_incomplete_request_without_invented_token_counts() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let app = Router::new().route(
        "/chat/completions",
        post(|| async {
            let first: Result<Vec<u8>, Infallible> = Ok(format!(
                "data: {}\n\n",
                json!({"choices":[{"delta":{"content":"Partial answer."}}]})
            )
            .into_bytes());
            let stream = futures_util::stream::iter([first])
                .chain(futures_util::stream::pending::<Result<Vec<u8>, Infallible>>());
            (
                [(axum::http::header::CONTENT_TYPE, "text/event-stream")],
                Body::from_stream(stream),
            )
        }),
    );
    let server = tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    let mut config: Config =
        serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
    config.llm_base_url = format!("http://{address}");
    let trace = Trace::default();
    let p = providers::Providers::new(Arc::new(config), trace.clone()).unwrap();
    let (tx, mut rx) = tokio::sync::mpsc::channel(4);
    let producer = p.clone();
    let task = tokio::spawn(async move {
        producer
            .llm(
                vec![json!({"role":"user","content":"Hello"})],
                vec![],
                false,
                1,
                1,
                tx,
            )
            .await
    });
    assert!(matches!(
        rx.recv().await,
        Some(providers::LlmEvent::Text(_))
    ));
    task.abort();
    let _ = task.await;
    let event = trace
        .events()
        .into_iter()
        .find(|e| e.event == "llm_provider_request_complete")
        .unwrap();
    assert_eq!(event.data["completed"], false);
    assert_eq!(event.data["first_text_observed"], true);
    assert!(event.data.get("input_tokens").is_none());
    assert!(event.data.get("cached_tokens").is_none());
    assert_eq!(p.usage.snapshot(false)["llm_input_tokens"], Value::Null);
    server.abort();
}
#[test]
fn continuation_metadata_is_redacted_from_debug_serialization_and_default_memory() {
    let context = actions::RequestContext {
        reasoning: "private-continuation".into(),
        content: Some("A permitted preamble".into()),
    };
    assert!(!format!("{context:?}").contains("private-continuation"));
    let call = actions::Call {
        id: "call-1".into(),
        name: "knowledge_lookup".into(),
        arguments: json!({"query":"policy"}),
        context: Some(context),
    };
    assert!(serde_json::to_value(&call)
        .unwrap()
        .get("context")
        .is_none());
    let mut memory = memory::Memory::default();
    memory.user(1, "Question", false);
    memory.tool(
        1,
        call.request_message(),
        json!({"role":"tool","tool_call_id":"call-1","content":"Answer"}),
    );
    assert!(
        !serde_json::to_string(&memory.context(None, 4096, &Trace::default()))
            .unwrap()
            .contains("private-continuation")
    );
    assert!(serde_json::to_string(&memory.context_with_reasoning(
        None,
        4096,
        &Trace::default(),
        true
    ))
    .unwrap()
    .contains("private-continuation"));
}
#[tokio::test]
async fn native_deepseek_tool_continuation_is_retained_privately_after_knowledge_result() {
    let mut h = Harness::configured_platform(
        true,
        |c| {
            c.agent_context.loaded = true;
            c.agent_context.business_id = "business-a".into();
            c.agent_context.agent_id = "agent-a".into();
            c.agent_context.knowledge_base_ids = vec!["kb-a".into()];
            c.llm_provider = "deepseek".into();
            c.llm_enable_thinking = true;
        },
        None,
        true,
    )
    .await;
    h.session
        .input
        .send(Input::Text("knowledge request".into()))
        .await
        .unwrap();
    h.complete(1).await;
    let requests = h.fixture.requests.lock().unwrap().clone();
    let followup = requests
        .iter()
        .find(|r| {
            r["messages"][0]["content"]
                .as_str()
                .is_some_and(|s| s.contains("Validated business knowledge"))
        })
        .unwrap();
    assert!(followup["messages"]
        .as_array()
        .unwrap()
        .iter()
        .any(|m| m["role"] == "assistant"
            && m["tool_calls"][0]["function"]["name"] == "knowledge_lookup"
            && m["reasoning_content"] == "private-continuation-fixture"));
    assert!(!requests.iter().any(|r| r["input"]
        .as_str()
        .is_some_and(|s| s.contains("private-continuation"))));
    assert!(!serde_json::to_string(&h.trace.events())
        .unwrap()
        .contains("private-continuation-fixture"));
    let completions: Vec<_> = h
        .trace
        .events()
        .into_iter()
        .filter(|e| e.event == "llm_provider_request_complete")
        .collect();
    assert_eq!(completions.len(), 2);
    assert!(completions
        .iter()
        .all(|e| e.data["completed"] == true && e.data["request_index"] == 1));
    h.close().await;
}
fn configured_demo(c: &mut Config) {
    c.agent_context.loaded = true;
    c.passkey_demo.business_ids = vec!["business-a".into()];
    c.passkey_demo.agent_ids = vec!["agent-a".into()];
    c.passkey_demo.subjects = vec!["caller-a".into()];
}
async fn demo_harness() -> Harness {
    let h = Harness::configured_platform(true, configured_demo, None, true).await;
    h.session
        .input
        .send(Input::DeviceFrame(
            serde_json::to_vec(
                &json!({"type":"session_context","correlationId":"local-demo-context"}),
            )
            .unwrap(),
        ))
        .await
        .unwrap();
    h
}
async fn demo_packet(h: &mut Harness, kind: &str) -> Value {
    tokio::time::timeout(Duration::from_secs(7), async {
        while let Some(out) = h.out.recv().await {
            match out {
                Output::ReliableEvent { body, receipt } => {
                    let _ = receipt.send(Ok(()));
                    h.fixture.private_packets.lock().unwrap().push(body.clone());
                    if body["type"] == "attentive.fido2.v1" && body["payload"]["type"] == kind {
                        return body["payload"].clone();
                    }
                }
                Output::Error(e) => panic!("{e}"),
                _ => {}
            }
        }
        panic!("closed before demo packet")
    })
    .await
    .unwrap_or_else(|e| panic!("{e}: {:?}", h.trace.events()))
}
async fn demo_prove(h: &Harness, packet: &Value, proof: Value) {
    h.session.input.send(Input::DeviceFrame(serde_json::to_vec(&json!({"type":"fido2_proof","authenticationId":packet["authenticationId"],"correlationId":packet["correlationId"],"proof":proof})).unwrap())).await.unwrap();
}
#[tokio::test]
async fn demo_speculation_waits_for_final_and_never_calls_a_bank_or_model() {
    let mut h = demo_harness().await;
    h.voice().await;
    h.session
        .input
        .send(Input::Partial("test my passkey".into()))
        .await
        .unwrap();
    h.until("speculation_start").await;
    assert!(h.fixture.requests.lock().unwrap().is_empty());
    assert!(h.fixture.private_packets.lock().unwrap().is_empty());
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "test my passkey".into(),
        })
        .await
        .unwrap();
    let mut device = passkey_demo::tests::Device::new();
    let create = demo_packet(&mut h, "fido2_ceremony").await;
    assert_eq!(create["feature"], "AttentiveDemoOnly");
    assert!(h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "playout_complete"));
    demo_prove(
        &h,
        &create,
        device.proof(&create, true, "https://docs.odion.ai"),
    )
    .await;
    assert_eq!(demo_packet(&mut h, "fido2_result").await["accepted"], true);
    let auth = demo_packet(&mut h, "fido2_ceremony").await;
    demo_prove(
        &h,
        &auth,
        device.proof(&auth, true, "https://docs.odion.ai"),
    )
    .await;
    assert_eq!(demo_packet(&mut h, "fido2_result").await["accepted"], true);
    h.complete(1).await;
    assert!(h
        .fixture
        .requests
        .lock()
        .unwrap()
        .iter()
        .all(|r| r["messages"].is_null()));
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "tool_execution_start"));
    assert!(!serde_json::to_string(&h.trace.events())
        .unwrap()
        .contains("attestationObject"));
    assert!(h.fixture.requests.lock().unwrap().iter().any(|r| r["input"]
        .as_str()
        .is_some_and(|s| s.contains("No bank transaction was authorized"))));
    h.close().await;
}
#[tokio::test]
async fn demo_changed_final_vetoes_the_speculative_trigger() {
    let mut h = demo_harness().await;
    h.voice().await;
    h.session
        .input
        .send(Input::Partial("test my passkey".into()))
        .await
        .unwrap();
    h.until("speculation_start").await;
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "second request instead".into(),
        })
        .await
        .unwrap();
    h.complete(1).await;
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|e| e.event == "passkey_demo_requested"));
    assert!(h.fixture.private_packets.lock().unwrap().is_empty());
    h.close().await;
}
#[tokio::test]
async fn demo_prompt_cancels_on_barge_and_old_proof_cannot_finish_a_new_request() {
    let mut h = demo_harness().await;
    h.session
        .input
        .send(Input::Text("test my passkey".into()))
        .await
        .unwrap();
    let old = demo_packet(&mut h, "fido2_ceremony").await;
    h.voice().await;
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 2,
            text: "second request instead".into(),
        })
        .await
        .unwrap();
    h.complete(2).await;
    assert!(h
        .fixture
        .private_packets
        .lock()
        .unwrap()
        .iter()
        .any(|p| p["payload"]["type"] == "fido2_abort"
            && p["payload"]["authenticationId"] == old["authenticationId"]));
    h.session
        .input
        .send(Input::Text("test my passkey".into()))
        .await
        .unwrap();
    let new = demo_packet(&mut h, "fido2_ceremony").await;
    assert_ne!(new["authenticationId"], old["authenticationId"]);
    let mut device = passkey_demo::tests::Device::new();
    demo_prove(&h, &old, device.proof(&old, true, "https://docs.odion.ai")).await;
    tokio::time::sleep(Duration::from_millis(10)).await;
    h.session.input.send(Input::DeviceFrame(serde_json::to_vec(&json!({"type":"fido2_cancelled","authenticationId":new["authenticationId"],"correlationId":new["correlationId"]})).unwrap())).await.unwrap();
    h.complete(3).await;
    assert!(!h
        .fixture
        .private_packets
        .lock()
        .unwrap()
        .iter()
        .any(|p| p["payload"]["accepted"] == true));
    h.close().await;
}
async fn transfer_request(
    h: &mut Harness,
) -> (
    String,
    tokio::sync::oneshot::Sender<Result<super::transfer::Connection, Value>>,
    tokio::sync::oneshot::Sender<bool>,
) {
    tokio::time::timeout(Duration::from_secs(5), async {
        while let Some(output) = h.out.recv().await {
            match output {
                Output::Transfer {
                    reason,
                    reply,
                    cleanup,
                } => return (reason, reply, cleanup),
                Output::Error(error) => panic!("{error}"),
                _ => {}
            }
        }
        panic!("no transfer")
    })
    .await
    .unwrap()
}
#[tokio::test]
async fn human_transfer_suppresses_model_preamble_drains_cue_and_requires_no_second_confirmation() {
    let mut h = Harness::configured(true, configured_human).await;
    h.session
        .input
        .send(Input::Text("human request".into()))
        .await
        .unwrap();
    let (_, reply, cleanup) = transfer_request(&mut h).await;
    assert!(h
        .trace
        .events()
        .iter()
        .any(|event| event.event == "human_transfer_requested"
            && event.data["after_playout"] == true));
    assert!(h
        .trace
        .events()
        .iter()
        .any(|event| event.event == "llm_transfer_guard_decision"
            && event.data["suppressed"] == true));
    let requests = h.fixture.requests.lock().unwrap().clone();
    assert_eq!(
        requests
            .iter()
            .filter(|request| request["messages"].is_array())
            .count(),
        1
    );
    assert!(requests
        .iter()
        .any(|request| request["input"] == "All right, I'll connect you now."));
    assert!(!requests
        .iter()
        .any(|request| request["input"].as_str().is_some_and(|text| text
            .contains("false preamble")
            || text.contains("human has answered"))));
    let (client, fake, server) = super::transfer::tests::setup().await;
    let connection = client
        .connect_notified("room-a", "session-a", None, cleanup)
        .await
        .unwrap();
    assert!(reply.send(Ok(connection)).is_ok());
    h.until("human_transfer_result").await;
    let established = tokio::time::timeout(Duration::from_secs(2), async {
        while let Some(output) = h.out.recv().await {
            if let Output::Handoff { connection, .. } = output {
                return connection;
            }
        }
        panic!("missing accepted connection")
    })
    .await
    .unwrap();
    assert_eq!(fake.requests.lock().unwrap().len(), 1);
    let before = h
        .fixture
        .requests
        .lock()
        .unwrap()
        .iter()
        .filter(|request| request["messages"].is_array())
        .count();
    h.session
        .input
        .send(Input::Text("second request".into()))
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_millis(30)).await;
    assert_eq!(
        h.fixture
            .requests
            .lock()
            .unwrap()
            .iter()
            .filter(|request| request["messages"].is_array())
            .count(),
        before
    );
    h.close().await;
    drop(established);
    assert!(client.drain_cleanup().await);
    assert_eq!(fake.removed.lock().unwrap().len(), 1);
    server.abort();
}
#[tokio::test]
async fn interruption_during_connection_cue_never_requests_a_bridge() {
    let mut h = Harness::configured(true, configured_human).await;
    h.session
        .input
        .send(Input::Text("human request".into()))
        .await
        .unwrap();
    h.until("tts_request_start").await;
    h.voice().await;
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 2,
            text: "second request".into(),
        })
        .await
        .unwrap();
    h.complete(2).await;
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|event| event.event == "human_transfer_requested"));
    h.close().await;
}
#[tokio::test]
async fn speculative_human_tool_waits_for_final_and_changed_final_prevents_transfer() {
    assert!(super::text::valid_final("human request please after checking all of my questions about your current account fees and support opening hours today","human request refused after checking all of my questions about your current account fees and support opening hours today"));
    let mut h = Harness::configured(true, configured_human).await;
    h.voice().await;
    h.session
        .input
        .send(Input::Partial("human request please after checking all of my questions about your current account fees and support opening hours today".into()))
        .await
        .unwrap();
    h.until("speculation_start").await;
    tokio::time::sleep(Duration::from_millis(40)).await;
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|event| event.event == "human_transfer_requested"));
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "human request refused after checking all of my questions about your current account fees and support opening hours today".into(),
        })
        .await
        .unwrap();
    h.complete(1).await;
    assert!(!h
        .trace
        .events()
        .iter()
        .any(|event| event.event == "human_transfer_requested"));
    assert!(h
        .trace
        .events()
        .iter()
        .any(|event| event.event == "attempt_cancel"
            && event.data["reason"] == "final_transcript_changed"));
    h.close().await;
}
#[tokio::test]
async fn caller_interruption_cancels_pending_sdk_dial_and_retains_cleanup_acknowledgement() {
    use std::sync::atomic::Ordering;
    let mut h = Harness::configured(true, configured_human).await;
    h.session
        .input
        .send(Input::Text("human request".into()))
        .await
        .unwrap();
    let (_, mut reply, cleanup) = transfer_request(&mut h).await;
    let (client, fake, server) = super::transfer::tests::setup().await;
    fake.mode.store(1, Ordering::SeqCst);
    let dial = client.clone();
    let task = tokio::spawn(async move {
        tokio::select! {biased;_=reply.closed()=>{},result=dial.connect_notified("room-a","session-a",None,cleanup)=>{let _=reply.send(result);}}
        dial.drain_cleanup().await
    });
    fake.entered.notified().await;
    h.voice().await;
    h.until("human_transfer_cancelled").await;
    assert!(task.await.unwrap());
    h.until("human_transfer_cleanup").await;
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 2,
            text: "second request".into(),
        })
        .await
        .unwrap();
    h.complete(2).await;
    assert_eq!(fake.requests.lock().unwrap().len(), 1);
    assert_eq!(fake.removed.lock().unwrap().len(), 1);
    assert!(
        !h.trace
            .events()
            .iter()
            .any(|event| event.event == "human_transfer_result"
                && event.data["status"] == "connected")
    );
    h.session
        .input
        .send(Input::Text("human request again".into()))
        .await
        .unwrap();
    let (_, reply, cleanup) = transfer_request(&mut h).await;
    let _ = cleanup.send(true);
    let _ = reply.send(Err(json!({"status":"failed","cleanup_confirmed":true})));
    h.until("human_transfer_result").await;
    h.complete(3).await;
    h.close().await;
    server.abort();
}
#[tokio::test]
async fn late_answered_connection_is_removed_after_its_caller_turn_is_interrupted() {
    let mut h = Harness::configured(true, configured_human).await;
    h.session
        .input
        .send(Input::Text("human request".into()))
        .await
        .unwrap();
    let (_, reply, cleanup) = transfer_request(&mut h).await;
    let (client, fake, server) = super::transfer::tests::setup().await;
    let connection = client
        .connect_notified("room-a", "session-a", None, cleanup)
        .await
        .unwrap();
    h.voice().await;
    h.until("human_transfer_cancelled").await;
    assert!(reply.send(Ok(connection)).is_err());
    assert!(client.drain_cleanup().await);
    h.until("human_transfer_cleanup").await;
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 2,
            text: "second request".into(),
        })
        .await
        .unwrap();
    h.complete(2).await;
    assert_eq!(fake.removed.lock().unwrap().len(), 1);
    assert!(
        !h.trace
            .events()
            .iter()
            .any(|event| event.event == "human_transfer_result"
                && event.data["status"] == "connected")
    );
    h.close().await;
    server.abort();
}
#[tokio::test]
async fn unchanged_speculative_human_request_reuses_its_model_plan_after_final() {
    let mut h = Harness::configured(true, configured_human).await;
    h.voice().await;
    h.session
        .input
        .send(Input::Partial("human request please".into()))
        .await
        .unwrap();
    h.until("speculation_start").await;
    tokio::time::sleep(Duration::from_millis(40)).await;
    assert!(h.sink.pcm.lock().unwrap().is_empty());
    h.end().await;
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "Human request please.".into(),
        })
        .await
        .unwrap();
    let (_, reply, cleanup) = transfer_request(&mut h).await;
    assert_eq!(
        h.fixture
            .requests
            .lock()
            .unwrap()
            .iter()
            .filter(|request| request["messages"].is_array())
            .count(),
        1
    );
    let _ = cleanup.send(true);
    let _ = reply.send(Err(json!({"status":"failed","cleanup_confirmed":true})));
    h.until("human_transfer_result").await;
    h.complete(1).await;
    h.close().await;
}
#[tokio::test]
async fn ordinary_speech_with_configured_transfer_is_retained_when_no_transfer_is_selected() {
    let mut h = Harness::configured(true, configured_human).await;
    h.session
        .input
        .send(Input::Text("first request".into()))
        .await
        .unwrap();
    h.complete(1).await;
    assert!(h
        .fixture
        .requests
        .lock()
        .unwrap()
        .iter()
        .any(|request| request["input"] == "First answer."));
    assert!(h
        .trace
        .events()
        .iter()
        .any(|event| event.event == "llm_transfer_guard_decision"
            && event.data["suppressed"] == false));
    h.close().await;
}
#[tokio::test]
async fn unknown_human_connection_cleanup_is_honest_and_not_redialed() {
    use std::sync::atomic::Ordering;
    let mut h = Harness::configured(true, configured_human).await;
    h.session
        .input
        .send(Input::Text("human request".into()))
        .await
        .unwrap();
    let (_, reply, cleanup) = transfer_request(&mut h).await;
    let (client, fake, server) = super::transfer::tests::setup().await;
    fake.mode.store(4, Ordering::SeqCst);
    let result = client
        .connect_notified("room-a", "session-a", None, cleanup)
        .await;
    assert!(reply.send(result).is_ok());
    h.until("human_transfer_result").await;
    h.complete(1).await;
    assert!(h
        .fixture
        .requests
        .lock()
        .unwrap()
        .iter()
        .any(|request| request["input"]
            .as_str()
            .is_some_and(|text| text.contains("couldn't confirm"))));
    h.session
        .input
        .send(Input::Text("human request again".into()))
        .await
        .unwrap();
    h.complete(2).await;
    assert_eq!(
        h.trace
            .events()
            .iter()
            .filter(|event| event.event == "human_transfer_requested")
            .count(),
        1
    );
    assert_eq!(fake.requests.lock().unwrap().len(), 1);
    {
        let requests = h.fixture.requests.lock().unwrap();
        let last = requests
            .iter()
            .rev()
            .find(|request| request["messages"].is_array())
            .unwrap();
        assert!(!last["tools"]
            .as_array()
            .unwrap()
            .iter()
            .any(|tool| tool["function"]["name"] == "transfer_to_aicc"));
    }
    h.close().await;
    server.abort();
}

#[tokio::test]
async fn french_human_transfer_synthesizes_one_localized_cue_without_an_extra_permission_question()
{
    let mut h = Harness::configured(true, |c| {
        configured_human(c);
        c.tts_language = "French".into();
        c.agent_context.tts_language_hint = "French".into();
    })
    .await;
    h.session
        .input
        .send(Input::Text("human request".into()))
        .await
        .unwrap();
    let (_, reply, cleanup) = transfer_request(&mut h).await;
    let requests = h.fixture.requests.lock().unwrap().clone();
    assert_eq!(
        requests.iter().filter(|r| r["messages"].is_array()).count(),
        2
    );
    assert!(requests
        .iter()
        .any(|r| r["input"] == "Je vous mets en relation maintenant."));
    assert!(!requests
        .iter()
        .any(|r| r["input"] == "All right, I'll connect you now."));
    let localized = requests
        .iter()
        .find(|r| {
            r["messages"][0]["content"]
                .as_str()
                .is_some_and(|s| s.contains("State the supplied application message faithfully"))
        })
        .unwrap();
    assert!(localized["messages"][0]["content"]
        .as_str()
        .unwrap()
        .contains("Speak only French"));
    assert_eq!(localized["tools"], Value::Null);
    cleanup.send(true).unwrap();
    reply
        .send(Err(
            json!({"status":"failed","cleanup_confirmed":true,"error":"public_fixture"}),
        ))
        .unwrap();
    h.until("human_transfer_result").await;
    h.close().await;
}

#[tokio::test]
async fn owned_utterances_are_not_cut_at_the_old_sixty_second_prototype_limit() {
    let h = Harness::configured(false, |c| c.max_utterance_seconds = 1800.0).await;
    for _ in 0..610 {
        h.session.pcm(vec![1000; 1600]).await.unwrap();
    }
    for _ in 0..3 {
        h.session.pcm(vec![0; 1600]).await.unwrap();
    }
    h.session
        .input
        .send(Input::Final {
            turn: 1,
            text: "A complete long caller turn.".into(),
        })
        .await
        .unwrap();
    h.until("turn_complete").await;
    assert!(!h.trace.events().iter().any(|event| event.event == "error"));
    assert!(h
        .trace
        .events()
        .iter()
        .any(|event| event.event == "stt_final"
            && event.data["text"] == "A complete long caller turn."));
    h.close().await;
}
