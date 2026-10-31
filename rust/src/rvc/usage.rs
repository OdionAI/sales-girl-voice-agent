//! Cumulative provider observations. No inference estimates and no network I/O
//! in audio callbacks. A repeated cumulative usage frame updates one request.
use super::{platform::Services, Trace};
use serde_json::{json, Value};
use std::{
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{sync::watch, task::JoinHandle};
#[derive(Default)]
struct Tokens {
    input: Option<u64>,
    output: Option<u64>,
    cached: Option<u64>,
}
#[derive(Default)]
struct State {
    revision: u64,
    requests: Vec<Tokens>,
    stt_seconds: f64,
    tts_characters: u64,
}
#[derive(Clone, Default)]
pub struct Collector(Arc<Mutex<State>>);
impl Collector {
    pub(crate) fn llm_observation(&self, request: usize) -> Value {
        let mut result = json!({});
        if let Ok(state) = self.0.lock() {
            if let Some(tokens) = request.checked_sub(1).and_then(|i| state.requests.get(i)) {
                for (name, value) in [
                    ("input_tokens", tokens.input),
                    ("output_tokens", tokens.output),
                    ("cached_tokens", tokens.cached),
                ] {
                    if let Some(value) = value {
                        result[name] = json!(value);
                    }
                }
            }
        }
        result
    }
    pub(crate) fn llm_started(&self) -> usize {
        let Ok(mut state) = self.0.lock() else {
            return 0;
        };
        state.requests.push(Tokens::default());
        state.revision += 1;
        state.requests.len()
    }
    pub(crate) fn llm_usage(&self, request: usize, usage: &Value) {
        if !usage.is_object() || request == 0 {
            return;
        }
        let Ok(mut state) = self.0.lock() else {
            return;
        };
        let Some(tokens) = state.requests.get_mut(request - 1) else {
            return;
        };
        if let Some(v) = usage["prompt_tokens"].as_u64() {
            tokens.input = Some(v);
        }
        if let Some(v) = usage["completion_tokens"].as_u64() {
            tokens.output = Some(v);
        }
        if let Some(v) = usage["prompt_tokens_details"]["cached_tokens"].as_u64() {
            tokens.cached = Some(v);
        }
        state.revision += 1;
    }
    pub(crate) fn stt_submitted(&self, pcm_bytes: usize) {
        if let Ok(mut state) = self.0.lock() {
            state.stt_seconds += pcm_bytes as f64 / 32000.0;
            state.revision += 1;
        }
    }
    pub(crate) fn tts_accepted(&self, text: &str) {
        if let Ok(mut state) = self.0.lock() {
            state.tts_characters += text.chars().count() as u64;
            state.revision += 1;
        }
    }
    pub fn snapshot(&self, final_report: bool) -> Value {
        let Ok(state) = self.0.lock() else {
            return json!({"measurement_status":{"llm_input_tokens":"unavailable","llm_output_tokens":"unavailable","llm_cached_tokens":"unavailable"}});
        };
        let mut output = json!({"revision":state.revision+u64::from(final_report),"final":final_report,"stt_seconds":format!("{:.6}",state.stt_seconds).parse::<f64>().unwrap_or(state.stt_seconds),"tts_characters":state.tts_characters,"llm_requests":state.requests.len(),"measurement_status":{"stt_seconds":"recorded","tts_characters":"recorded"},"measurement_basis":{"llm":"provider_reported_tokens_including_tools_speculation_and_compaction","stt":"pcm_audio_submitted_including_warmup_realtime_and_batch","tts":"characters_accepted_including_interrupted_and_speculative_requests"}});
        for (name, which) in [
            ("llm_input_tokens", 0),
            ("llm_output_tokens", 1),
            ("llm_cached_tokens", 2),
        ] {
            let observed: Vec<u64> = state
                .requests
                .iter()
                .filter_map(|tokens| match which {
                    0 => tokens.input,
                    1 => tokens.output,
                    _ => tokens.cached,
                })
                .collect();
            output[name] = if observed.is_empty() && !state.requests.is_empty() {
                Value::Null
            } else {
                observed
                    .iter()
                    .try_fold(0u64, |total, value| total.checked_add(*value))
                    .map_or(Value::Null, |total| json!(total))
            };
            output["measurement_status"][name] =
                json!(if output[name].is_null() && !observed.is_empty() {
                    "unavailable"
                } else if observed.len() == state.requests.len() {
                    "recorded"
                } else if observed.is_empty() {
                    "unavailable"
                } else {
                    "partial"
                });
        }
        output
    }
}
#[derive(Clone, Default)]
pub(crate) struct SessionIds(Arc<Mutex<(String, String)>>);
impl SessionIds {
    pub(crate) fn set(&self, session: String, conversation: String) {
        if let Ok(mut ids) = self.0.lock() {
            *ids = (session, conversation);
        }
    }
    pub(crate) fn get(&self) -> Option<(String, String)> {
        self.0
            .lock()
            .ok()
            .map(|ids| ids.clone())
            .filter(|(session, _)| !session.is_empty())
    }
}
struct ReporterInner {
    collector: Collector,
    ids: SessionIds,
    services: Services,
    trace: Trace,
    last_revision: Mutex<Option<u64>>,
}
pub(crate) struct Reporter {
    inner: Arc<ReporterInner>,
    stop: watch::Sender<bool>,
    task: Option<JoinHandle<()>>,
}
impl Drop for Reporter {
    fn drop(&mut self) {
        if let Some(task) = self.task.take() {
            task.abort();
        }
    }
}
impl Reporter {
    pub(crate) fn start(
        collector: Collector,
        ids: SessionIds,
        services: Services,
        trace: Trace,
    ) -> Self {
        let configured = !services.config().billing.base_url.is_empty()
            && !services.config().billing.token.is_empty();
        let (stop, mut rx) = watch::channel(false);
        let inner = Arc::new(ReporterInner {
            collector,
            ids,
            services,
            trace,
            last_revision: Mutex::new(None),
        });
        let task = if configured {
            let inner = inner.clone();
            Some(tokio::spawn(async move {
                let mut interval = tokio::time::interval(Duration::from_secs(10));
                interval.tick().await;
                loop {
                    tokio::select! {biased;_=rx.changed()=>return,_=interval.tick()=>{flush(&inner,false).await;}}
                }
            }))
        } else {
            inner.trace.emit(
                "usage_reporting_unavailable",
                0,
                0,
                json!({"reason":"billing_service_not_configured"}),
            );
            None
        };
        Self { inner, stop, task }
    }
    pub(crate) async fn close(mut self) {
        let _ = self.stop.send(true);
        if let Some(task) = self.task.take() {
            task.abort();
            let _ = task.await;
        }
        for _ in 0..2 {
            flush(&self.inner, true).await;
            let revision = self.inner.collector.snapshot(true)["revision"].as_u64();
            if self
                .inner
                .last_revision
                .lock()
                .ok()
                .is_some_and(|last| *last == revision)
            {
                break;
            }
        }
    }
}
async fn flush(inner: &ReporterInner, final_report: bool) {
    let Some((session, conversation)) = inner.ids.get() else {
        return;
    };
    if inner.services.config().billing.base_url.is_empty()
        || inner.services.config().billing.token.is_empty()
    {
        return;
    }
    let snapshot = inner.collector.snapshot(final_report);
    let revision = snapshot["revision"].as_u64().unwrap_or_default();
    if !final_report
        && inner
            .last_revision
            .lock()
            .ok()
            .is_some_and(|last| *last == Some(revision))
    {
        return;
    }
    let body = json!({"session_id":session,"conversation_id":conversation,"usage":snapshot});
    match inner.services.observed_usage(body).await {
        Ok(()) => {
            if let Ok(mut last) = inner.last_revision.lock() {
                *last = Some(revision);
            }
            inner.trace.emit(
                "usage_persisted",
                0,
                0,
                json!({"canonical_session_id":session,"final":final_report,"usage":snapshot}),
            );
        }
        Err(_) => inner.trace.emit(
            "usage_persist_failed",
            0,
            0,
            json!({"canonical_session_id":session,"final":final_report,"usage":snapshot}),
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        extract::State as HttpState,
        http::{HeaderMap, StatusCode},
        response::{IntoResponse, Response},
        routing::post,
        Json, Router,
    };
    use std::sync::atomic::{AtomicUsize, Ordering};
    #[test]
    fn snapshots_match_independent_python_counts_missing_fields_unicode_and_rounding() {
        let reference: Value =
            serde_json::from_str(include_str!("../../fixtures/python-parity-reference.json"))
                .unwrap();
        for case in reference["usage_sequences"].as_array().unwrap() {
            let collector = Collector::default();
            for (index, op) in case["operations"].as_array().unwrap().iter().enumerate() {
                match op["op"].as_str().unwrap() {
                    "start" => {
                        collector.llm_started();
                    }
                    "usage" => {
                        collector.llm_usage(op["request"].as_u64().unwrap() as usize, &op["value"])
                    }
                    "stt" => collector.stt_submitted(op["bytes"].as_u64().unwrap() as usize),
                    "tts" => collector.tts_accepted(op["text"].as_str().unwrap()),
                    _ => panic!(),
                };
                assert_eq!(collector.snapshot(false), case["snapshots"][index]);
            }
            assert_eq!(collector.snapshot(true), case["final"]);
        }
    }
    #[derive(Default)]
    struct Fake {
        bodies: Mutex<Vec<Value>>,
        failures: AtomicUsize,
    }
    async fn observed(
        HttpState(fake): HttpState<Arc<Fake>>,
        headers: HeaderMap,
        Json(body): Json<Value>,
    ) -> Response {
        assert_eq!(headers["x-business-id"], "business-a");
        assert_eq!(headers["x-service-token"], "local-billing-fixture");
        assert_eq!(body["session_id"], "canonical-session");
        fake.bodies.lock().unwrap().push(body);
        if fake
            .failures
            .try_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_sub(1))
            .is_ok()
        {
            StatusCode::SERVICE_UNAVAILABLE.into_response()
        } else {
            StatusCode::NO_CONTENT.into_response()
        }
    }
    #[tokio::test]
    async fn canonical_usage_reports_coalesce_accept_204_and_retry_final_without_double_count() {
        let fake = Arc::new(Fake::default());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let app = Router::new()
            .route("/v1/internal/credits/observed-usage", post(observed))
            .with_state(fake.clone());
        let server = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        let services = Services::new(
            super::super::platform::PlatformConfig {
                billing: super::super::platform::Endpoint {
                    base_url: format!("http://{address}"),
                    token: "local-billing-fixture".into(),
                    timeout_seconds: 1.0,
                },
                ..Default::default()
            },
            super::super::identity::test_session(json!({})),
            Trace::default(),
        )
        .unwrap();
        let collector = Collector::default();
        collector.tts_accepted("Hello");
        let ids = SessionIds::default();
        let reporter = Reporter::start(collector.clone(), ids.clone(), services, Trace::default());
        flush(&reporter.inner, false).await;
        assert!(fake.bodies.lock().unwrap().is_empty());
        ids.set("canonical-session".into(), "conversation-a".into());
        flush(&reporter.inner, false).await;
        flush(&reporter.inner, false).await;
        assert_eq!(fake.bodies.lock().unwrap().len(), 1);
        fake.failures.store(1, Ordering::SeqCst);
        reporter.close().await;
        let bodies = fake.bodies.lock().unwrap();
        assert_eq!(bodies.len(), 3);
        assert_eq!(bodies[1]["usage"], bodies[2]["usage"]);
        assert_eq!(bodies[2]["usage"]["tts_characters"], 5);
        assert_eq!(bodies[2]["usage"]["final"], true);
        server.abort();
    }
}
