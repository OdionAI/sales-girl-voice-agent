//! Official LiveKit Egress integration. File readiness requires COMPLETE plus
//! file metadata; an acknowledged stop or a predicted URL is not a recording.
use super::{platform::Services, Trace};
use chrono::{DateTime, Utc};
use livekit_api::services::egress::{
    EgressClient, EgressListFilter, EgressListOptions, EgressOutput, RoomCompositeOptions,
};
use livekit_protocol::{self as proto, EgressStatus};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::task::JoinHandle;
#[derive(Clone, Deserialize, Serialize)]
#[serde(default)]
pub struct Config {
    pub enabled: bool,
    pub api_url: String,
    pub api_key: String,
    pub api_secret: String,
    pub storage: String,
    pub bucket: String,
    pub gcp_credentials: String,
    pub s3_access_key: String,
    pub s3_secret: String,
    pub s3_session_token: String,
    pub s3_region: String,
    pub s3_endpoint: String,
    pub s3_force_path_style: bool,
    pub prefix: String,
    pub local_directory: String,
    pub format: String,
    pub public_base_url: String,
    pub poll_timeout_seconds: f64,
    pub poll_interval_seconds: f64,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            enabled: false,
            api_url: String::new(),
            api_key: String::new(),
            api_secret: String::new(),
            storage: "gcs".into(),
            bucket: String::new(),
            gcp_credentials: String::new(),
            s3_access_key: String::new(),
            s3_secret: String::new(),
            s3_session_token: String::new(),
            s3_region: "af-south-1".into(),
            s3_endpoint: String::new(),
            s3_force_path_style: false,
            prefix: "livekit-recordings".into(),
            local_directory: String::new(),
            format: "mp3".into(),
            public_base_url: String::new(),
            poll_timeout_seconds: 45.0,
            poll_interval_seconds: 2.0,
        }
    }
}
impl Config {
    pub(crate) fn validate(&self) -> Result<(), String> {
        for n in [self.poll_timeout_seconds, self.poll_interval_seconds] {
            if !n.is_finite() || n <= 0.0 || n > 3600.0 {
                return Err("invalid recording deadline".into());
            }
        }
        Ok(())
    }
    fn configured(&self) -> bool {
        self.enabled
            && if self.storage == "local" {
                self.local_config().is_ok()
            } else if self.bucket.is_empty() {
                false
            } else if self.storage == "s3" {
                !self.s3_access_key.is_empty()
                    && !self.s3_secret.is_empty()
                    && !self.s3_endpoint.is_empty()
            } else {
                !self.gcp_credentials.is_empty()
            }
    }
    fn local_config(&self) -> Result<(String, String), String> {
        let directory = self.local_directory.trim_end_matches('/');
        let prefix = self.prefix.trim_end_matches('/');
        if !directory.starts_with('/') || !safe_relative(&directory[1..]) {
            return Err("invalid_local_recording_directory".into());
        }
        if !safe_relative(prefix) {
            return Err("invalid_local_recording_prefix".into());
        }
        if !["mp3", "mp4", "ogg"].contains(&self.format.as_str()) {
            return Err("invalid_local_recording_format".into());
        }
        Ok((directory.into(), prefix.into()))
    }
    fn path(
        &self,
        business: &str,
        session: &str,
        room: &str,
        started: DateTime<Utc>,
    ) -> Result<String, String> {
        let local = self.storage == "local";
        let (directory, prefix, business) = if local {
            let (dir, prefix) = self.local_config()?;
            let business = uuid::Uuid::parse_str(business.trim())
                .map_err(|_| "invalid_local_recording_business_id")?
                .to_string();
            (dir, prefix, business)
        } else {
            (
                String::new(),
                {
                    let prefix = self.prefix.trim_matches('/');
                    if prefix.is_empty() {
                        "livekit-recordings".into()
                    } else {
                        prefix.into()
                    }
                },
                slug(business, "unknown-business", false),
            )
        };
        let filename = format!(
            "{}-{}-{}.{}",
            started.format("%Y%m%dT%H%M%SZ"),
            slug(room, "room", local),
            slug(session, "session", local),
            self.format
        );
        let relative = format!("{prefix}/{business}/{filename}");
        Ok(if local {
            format!("{directory}/{relative}")
        } else {
            relative
        })
    }
    fn public_url(&self, path: &str) -> Result<String, String> {
        if self.storage == "local" {
            let (dir, _) = self.local_config()?;
            let relative = path
                .strip_prefix(&format!("{dir}/"))
                .ok_or("local_recording_outside_directory")?;
            return validated_local_uri(&format!("local-recording:///{relative}"))
                .ok_or_else(|| "invalid_local_recording_path".into());
        }
        let path = path.trim_start_matches('/');
        if !self.public_base_url.is_empty() {
            return Ok(format!(
                "{}/{path}",
                self.public_base_url.trim_end_matches('/')
            ));
        }
        if self.storage == "s3" {
            return Ok(self.s3_url(&self.bucket, path));
        }
        Ok(format!(
            "https://storage.googleapis.com/{}/{path}",
            self.bucket
        ))
    }
    fn s3_url(&self, bucket: &str, path: &str) -> String {
        let endpoint = self.s3_endpoint.trim_end_matches('/');
        if endpoint.is_empty() {
            return format!("s3://{bucket}/{path}");
        }
        if self.s3_force_path_style {
            return format!("{endpoint}/{bucket}/{path}");
        }
        if let Some((scheme, host)) = endpoint.split_once("://") {
            return format!("{scheme}://{bucket}.{host}/{path}");
        }
        format!("{bucket}.{endpoint}/{path}")
    }
    fn normalize_url(&self, location: &str, fallback: Option<&str>) -> Option<String> {
        if self.storage == "local" || fallback.is_some_and(|s| s.starts_with("local-recording:")) {
            return fallback.and_then(validated_local_uri);
        }
        let location = location.trim();
        if location.is_empty() {
            return fallback.map(str::to_owned);
        }
        if let Some(rest) = location.strip_prefix("gs://") {
            if let Some((bucket, path)) = rest
                .split_once('/')
                .filter(|(b, p)| !b.is_empty() && !p.is_empty())
            {
                return Some(format!("https://storage.googleapis.com/{bucket}/{path}"));
            }
        }
        if let Some(rest) = location.strip_prefix("s3://") {
            if let Some((bucket, path)) = rest
                .split_once('/')
                .filter(|(b, p)| !b.is_empty() && !p.is_empty())
            {
                return Some(if !self.public_base_url.is_empty() {
                    format!("{}/{path}", self.public_base_url.trim_end_matches('/'))
                } else {
                    self.s3_url(bucket, path)
                });
            }
        }
        if location.starts_with("https://") || location.starts_with("http://") {
            Some(location.into())
        } else {
            fallback.map(str::to_owned)
        }
    }
    fn output(&self, path: String) -> Result<proto::EncodedFileOutput, String> {
        let output = if self.storage == "local" {
            None
        } else if self.storage == "s3" {
            Some(proto::encoded_file_output::Output::S3(proto::S3Upload {
                access_key: self.s3_access_key.clone(),
                secret: self.s3_secret.clone(),
                session_token: self.s3_session_token.clone(),
                region: self.s3_region.clone(),
                endpoint: self.s3_endpoint.clone(),
                bucket: self.bucket.clone(),
                force_path_style: self.s3_force_path_style,
                ..Default::default()
            }))
        } else {
            Some(proto::encoded_file_output::Output::Gcp(proto::GcpUpload {
                credentials: credentials(&self.gcp_credentials).ok_or("invalid_gcp_credentials")?,
                bucket: self.bucket.clone(),
                ..Default::default()
            }))
        };
        Ok(proto::EncodedFileOutput {
            file_type: match self.format.as_str() {
                "mp3" => proto::EncodedFileType::Mp3 as i32,
                "mp4" => proto::EncodedFileType::Mp4 as i32,
                _ => proto::EncodedFileType::Ogg as i32,
            },
            filepath: path,
            output,
            ..Default::default()
        })
    }
    fn client(&self) -> Result<EgressClient, String> {
        let url = reqwest::Url::parse(&self.api_url).map_err(|_| "recording_api_unavailable")?;
        if !matches!(url.scheme(), "http" | "https" | "ws" | "wss")
            || url.host_str().is_none()
            || !url.username().is_empty()
            || url.password().is_some()
            || self.api_key.is_empty()
            || self.api_secret.is_empty()
        {
            return Err("recording_api_unavailable".into());
        }
        Ok(EgressClient::with_api_key(
            &self
                .api_url
                .replacen("wss://", "https://", 1)
                .replacen("ws://", "http://", 1),
            &self.api_key,
            &self.api_secret,
        )
        .with_failover(false)
        .with_request_timeout(Duration::from_secs(10)))
    }
}
fn safe_relative(path: &str) -> bool {
    path.split('/').all(|part| {
        !part.is_empty()
            && part != "."
            && part != ".."
            && part
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"_.-".contains(&c))
    })
}
fn slug(value: &str, fallback: &str, ascii: bool) -> String {
    let raw = value
        .trim()
        .chars()
        .flat_map(|c| {
            if c.is_alphanumeric() && (!ascii || c.is_ascii()) {
                c.to_lowercase().collect::<Vec<_>>()
            } else {
                vec!['-']
            }
        })
        .collect::<String>();
    let value = raw
        .split('-')
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join("-");
    if value.is_empty() {
        fallback.into()
    } else {
        value
    }
}
fn validated_local_uri(uri: &str) -> Option<String> {
    let path = uri.strip_prefix("local-recording:///")?;
    let parts = path.split('/').collect::<Vec<_>>();
    if parts.len() < 3 || !safe_relative(path) {
        return None;
    }
    let business = parts[parts.len() - 2];
    if uuid::Uuid::parse_str(business).ok()?.to_string() != business {
        return None;
    }
    if ![".mp3", ".mp4", ".ogg"]
        .iter()
        .any(|extension| parts.last().is_some_and(|file| file.ends_with(extension)))
    {
        return None;
    }
    Some(uri.into())
}
fn strip_layout(raw: &str) -> String {
    let mut result = String::new();
    let (mut inside, mut escape) = (false, false);
    let mut chars = raw.chars().peekable();
    while let Some(c) = chars.next() {
        if inside {
            result.push(c);
            if escape {
                escape = false;
            } else if c == '\\' {
                escape = true;
            } else if c == '"' {
                inside = false;
            }
        } else if c == '"' {
            inside = true;
            result.push(c);
        } else if c == '\\' && chars.peek().is_some_and(|c| matches!(c, 'n' | 'r' | 't')) {
            if chars.next() == Some('t') {
                result.push(' ');
            }
        } else {
            result.push(c);
        }
    }
    result
}
fn credentials(raw: &str) -> Option<String> {
    let mut candidate = raw.trim().to_string();
    if candidate.is_empty() {
        return None;
    }
    for _ in 0..3 {
        match serde_json::from_str::<Value>(&candidate) {
            Ok(value @ Value::Object(_)) => return Some(super::python_json(&value)),
            Ok(Value::String(value)) => candidate = value.trim().into(),
            _ => {
                let normalized = strip_layout(&candidate);
                if normalized != candidate {
                    candidate = normalized;
                    continue;
                }
                break;
            }
        }
    }
    use std::io::Read;
    let mut bytes = Vec::new();
    std::fs::File::open(&candidate)
        .ok()?
        .take(1024 * 1024 + 1)
        .read_to_end(&mut bytes)
        .ok()?;
    if bytes.len() > 1024 * 1024 {
        return None;
    }
    let value: Value = serde_json::from_slice(&bytes).ok()?;
    value.is_object().then(|| super::python_json(&value))
}
#[allow(deprecated)] // The Python worker also supports the legacy protobuf File result.
fn completed_file(info: &proto::EgressInfo) -> Option<&proto::FileInfo> {
    if info.status != EgressStatus::EgressComplete as i32 {
        return None;
    }
    info.file_results
        .iter()
        .chain(match info.result.as_ref() {
            Some(proto::egress_info::Result::File(file)) => Some(file),
            _ => None,
        })
        .find(|file| !file.location.trim().is_empty() || !file.filename.trim().is_empty())
}

#[derive(Clone)]
struct State {
    session: String,
    status: String,
    url: Option<String>,
    duration: Option<u64>,
    detail: String,
    egress: Option<String>,
    expected: Option<String>,
    stop_ack: Option<bool>,
    disconnected: bool,
    dirty: bool,
    stop_info: Option<proto::EgressInfo>,
    stop_requested: bool,
}
pub(crate) struct Recorder {
    config: Config,
    services: Services,
    room: String,
    started: DateTime<Utc>,
    trace: Trace,
    state: Mutex<State>,
    start_task: Mutex<Option<JoinHandle<()>>>,
    stop_task: Mutex<Option<JoinHandle<()>>>,
}
impl Drop for Recorder {
    fn drop(&mut self) {
        if let Ok(task) = self.start_task.get_mut() {
            if let Some(task) = task.take() {
                task.abort();
            }
        }
        if let Ok(task) = self.stop_task.get_mut() {
            if let Some(task) = task.take() {
                task.abort();
            }
        }
    }
}
impl Recorder {
    pub(crate) fn new(
        config: Config,
        services: Services,
        room: String,
        started: DateTime<Utc>,
        trace: Trace,
    ) -> Arc<Self> {
        Arc::new(Self {
            config,
            services,
            room,
            started,
            trace,
            state: Mutex::new(State {
                session: String::new(),
                status: "disabled".into(),
                url: None,
                duration: None,
                detail: "recording_disabled".into(),
                egress: None,
                expected: None,
                stop_ack: None,
                disconnected: false,
                dirty: false,
                stop_info: None,
                stop_requested: false,
            }),
            start_task: Mutex::new(None),
            stop_task: Mutex::new(None),
        })
    }
    pub(crate) fn start(self: &Arc<Self>, session: String) {
        let Ok(mut task) = self.start_task.lock() else {
            return;
        };
        if task.is_some() {
            return;
        }
        if let Ok(mut state) = self.state.lock() {
            state.session = session;
        }
        let this = self.clone();
        *task = Some(tokio::spawn(async move {
            this.run_start().await;
        }));
    }
    async fn run_start(self: &Arc<Self>) {
        let outcome = if self.room.is_empty() {
            Err(("disabled", "not_a_livekit_room".to_string()))
        } else if !self.config.enabled {
            Err(("disabled", "recording_disabled".into()))
        } else if !self.config.configured() {
            Err(("failed", "recording_disabled".into()))
        } else {
            match self.start_api().await {
                Ok(info) => {
                    if let Ok(mut state) = self.state.lock() {
                        if info.egress_id.is_empty() {
                            state.status = "failed".into();
                            state.detail = "recording_start_failed".into();
                        } else {
                            state.status = "processing".into();
                            state.detail = "recording_started".into();
                            state.egress = Some(info.egress_id);
                        }
                    }
                    Ok(())
                }
                Err(detail) => Err(("failed", detail)),
            }
        };
        if let Err((status, detail)) = outcome {
            if let Ok(mut state) = self.state.lock() {
                state.status = status.into();
                state.detail = detail;
            }
        }
        self.maybe_stop();
        self.publish().await;
    }
    async fn start_api(&self) -> Result<proto::EgressInfo, String> {
        let session = self
            .state
            .lock()
            .ok()
            .map(|s| s.session.clone())
            .ok_or("recording_state_unavailable")?;
        let path = self.config.path(
            self.services.scope().business_id(),
            &session,
            &self.room,
            self.started,
        )?;
        let expected = self.config.public_url(&path)?;
        let output = self.config.output(path)?;
        if let Ok(mut state) = self.state.lock() {
            state.expected = Some(expected);
        }
        let client = self.config.client()?;
        tokio::time::timeout(
            Duration::from_secs(10),
            client.start_room_composite_egress(
                &self.room,
                vec![EgressOutput::File(output)],
                RoomCompositeOptions {
                    audio_only: true,
                    ..Default::default()
                },
            ),
        )
        .await
        .map_err(|_| "recording_start_timeout_reconcile_room".to_string())?
        .map_err(|_| "recording_start_failed".into())
    }
    pub(crate) fn mark_disconnected(self: &Arc<Self>) {
        if let Ok(mut state) = self.state.lock() {
            state.disconnected = true;
        }
        self.maybe_stop();
    }
    fn maybe_stop(self: &Arc<Self>) {
        let egress = self.state.lock().ok().and_then(|mut state| {
            if !state.disconnected || state.stop_requested {
                return None;
            }
            let egress = state.egress.clone()?;
            state.stop_requested = true;
            Some(egress)
        });
        let Some(egress) = egress else {
            return;
        };
        let Ok(mut slot) = self.stop_task.lock() else {
            return;
        };
        if slot.is_some() {
            return;
        }
        let this = self.clone();
        *slot = Some(tokio::spawn(async move {
            let result = if let Ok(client) = this.config.client() {
                tokio::time::timeout(Duration::from_secs(5), client.stop_egress(&egress))
                    .await
                    .ok()
                    .and_then(Result::ok)
            } else {
                None
            };
            let acknowledged = result.is_some();
            if let Ok(mut state) = this.state.lock() {
                state.stop_ack = Some(acknowledged);
                state.stop_info = result;
            }
            this.trace.emit(
                "conversation_recording_stop",
                0,
                0,
                json!({"egress_id":egress,"stop_acknowledged":acknowledged}),
            );
        }));
    }
    async fn publish(&self) {
        let Some(state) = self.state.lock().ok().map(|s| s.clone()) else {
            return;
        };
        if state.session.is_empty() {
            return;
        }
        let payload = json!({"recording_status":state.status,"recording_url":state.url,"recording_duration_seconds":state.duration});
        self.trace.emit("conversation_recording_status",0,0,json!({"session_id":state.session,"room_name":self.room,"recording_status":state.status,"detail":state.detail}));
        let updated = super::conversation::request(
            &self.services,
            &[
                "v1",
                "conversations",
                "sessions",
                &state.session,
                "recording",
            ],
            payload.clone(),
            true,
            &self.trace,
        )
        .await;
        let event=super::conversation::request(&self.services,&["v1","conversations","sessions",&state.session,"events"],json!({"event_type":if state.status=="available"{"recording_ready"}else{"recording_status"},"role":"system","title":format!("Recording {}",state.status),"body":state.detail,"payload":{"recording_status":state.status,"recording_url":state.url,"recording_duration_seconds":state.duration,"egress_id":state.egress,"room_name":self.room,"detail":state.detail,"expected_url":state.expected,"started_at":self.started.to_rfc3339_opts(chrono::SecondsFormat::Micros,false),"stop_acknowledged":state.stop_ack,"reconciliation_required":state.status=="processing"||state.detail.contains("reconcile_room")}}),true,&self.trace).await;
        if let Ok(mut state) = self.state.lock() {
            state.dirty = updated["status"] == "failed" || event["status"] == "failed";
        }
    }
    pub(crate) async fn close(self: &Arc<Self>, duration: u64) {
        self.mark_disconnected();
        let task = self.start_task.lock().ok().and_then(|mut t| t.take());
        if let Some(mut task) = task {
            if tokio::time::timeout(Duration::from_secs(27), &mut task)
                .await
                .is_err()
            {
                task.abort();
                let _ = task.await;
                if let Ok(mut state) = self.state.lock() {
                    state.status = "failed".into();
                    state.detail = "recording_start_timeout_reconcile_room".into();
                }
                self.publish().await;
            }
        }
        self.maybe_stop();
        let task = self.stop_task.lock().ok().and_then(|mut t| t.take());
        if let Some(mut task) = task {
            if tokio::time::timeout(Duration::from_secs(6), &mut task)
                .await
                .is_err()
            {
                task.abort();
                let _ = task.await;
                if let Ok(mut state) = self.state.lock() {
                    state.stop_ack = Some(false);
                }
            }
        }
        let egress = self.state.lock().ok().and_then(|s| s.egress.clone());
        if let Some(egress) = egress {
            self.finalize(&egress, duration).await;
        }
        if self.state.lock().ok().is_some_and(|s| s.dirty) {
            self.publish().await;
        }
    }
    async fn finalize(&self, egress: &str, duration: u64) {
        let result = tokio::time::timeout(
            Duration::from_secs_f64(self.config.poll_timeout_seconds),
            self.poll(egress, duration),
        )
        .await;
        let (status, url, file_duration, detail) = match result {
            Ok(value) => value,
            Err(_) => (
                "processing",
                None,
                duration,
                "egress_completion_timeout".into(),
            ),
        };
        if let Ok(mut state) = self.state.lock() {
            state.status = status.into();
            state.url = url;
            state.duration = Some(file_duration);
            state.detail = detail;
        }
        self.publish().await;
    }
    async fn poll(
        &self,
        egress: &str,
        duration: u64,
    ) -> (&'static str, Option<String>, u64, String) {
        let Ok(client) = self.config.client() else {
            return (
                "processing",
                None,
                duration,
                "recording_api_unavailable".into(),
            );
        };
        let mut latest = self.state.lock().ok().and_then(|mut s| s.stop_info.take());
        let expected = self.state.lock().ok().and_then(|s| s.expected.clone());
        let mut polled = false;
        loop {
            if let Some(info) = latest.as_ref().filter(|info| info.egress_id == egress) {
                if [
                    EgressStatus::EgressFailed as i32,
                    EgressStatus::EgressAborted as i32,
                    EgressStatus::EgressLimitReached as i32,
                ]
                .contains(&info.status)
                {
                    return (
                        "failed",
                        None,
                        duration,
                        format!("egress_status_{}", info.status),
                    );
                }
                if let Some(file) = completed_file(info) {
                    if let Some(url) = self
                        .config
                        .normalize_url(&file.location, expected.as_deref())
                    {
                        return (
                            "available",
                            Some(url),
                            if file.duration > 0 {
                                file.duration as u64 / 1_000_000_000
                            } else {
                                duration
                            },
                            "recording_available".into(),
                        );
                    }
                }
            }
            if polled {
                tokio::time::sleep(Duration::from_secs_f64(self.config.poll_interval_seconds))
                    .await;
            }
            match client
                .list_egress(EgressListOptions {
                    filter: EgressListFilter::Egress(egress.into()),
                    ..Default::default()
                })
                .await
            {
                Ok(items) => latest = items.into_iter().find(|item| item.egress_id == egress),
                Err(_) => {
                    return (
                        "processing",
                        None,
                        duration,
                        "egress_lookup_unavailable".into(),
                    )
                }
            }
            polled = true;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        body::Bytes,
        extract::State as HttpState,
        http::{HeaderMap, Uri},
        response::{IntoResponse, Response},
        Json, Router,
    };
    use prost::Message;
    use std::sync::atomic::{AtomicUsize, Ordering};
    const BUSINESS: &str = "a10c3e32-9834-4ab1-993e-75cd8f95ad67";
    fn started() -> DateTime<Utc> {
        DateTime::parse_from_rfc3339("2026-09-10T12:30:00Z")
            .unwrap()
            .with_timezone(&Utc)
    }
    #[test]
    fn independent_python_paths_urls_credentials_and_private_path_guards_match() {
        let reference: Value =
            serde_json::from_str(include_str!("../../fixtures/python-parity-reference.json"))
                .unwrap();
        for case in reference["recording_paths"].as_array().unwrap() {
            let config = Config {
                storage: case["storage"].as_str().unwrap().into(),
                local_directory: case["directory"].as_str().unwrap().into(),
                prefix: case["prefix"].as_str().unwrap().into(),
                format: case["format"].as_str().unwrap().into(),
                bucket: "fixture-bucket".into(),
                s3_endpoint: "https://s3.example.test".into(),
                ..Default::default()
            };
            let path = config.path(
                case["business"].as_str().unwrap(),
                case["session"].as_str().unwrap(),
                case["room"].as_str().unwrap(),
                started(),
            );
            if let Some(expected) = case["error"].as_str() {
                assert_eq!(path.unwrap_err(), expected);
            } else {
                let path = path.unwrap();
                assert_eq!(path, case["path"]);
                assert_eq!(config.public_url(&path).unwrap(), case["url"]);
            }
        }
        for case in reference["recording_credentials"].as_array().unwrap() {
            assert_eq!(
                credentials(case["raw"].as_str().unwrap()).unwrap_or_default(),
                case["expected"]
            );
        }
        for uri in [
            "file:///etc/passwd",
            "local-recording:///../secret.mp3",
            "local-recording:///calls/not-a-uuid/name.mp3",
            "local-recording:///calls/A10C3E32-9834-4AB1-993E-75CD8F95AD67/name.mp3",
        ] {
            assert!(validated_local_uri(uri).is_none());
        }
    }
    #[derive(Default)]
    struct Fake {
        starts: AtomicUsize,
        stops: AtomicUsize,
        lists: AtomicUsize,
        mode: AtomicUsize,
        records: Mutex<Vec<Value>>,
        start_requests: Mutex<Vec<proto::RoomCompositeEgressRequest>>,
        start_gate: tokio::sync::Notify,
    }
    fn wire<T: Message>(value: T) -> Response {
        (
            [("content-type", "application/protobuf")],
            value.encode_to_vec(),
        )
            .into_response()
    }
    fn info(status: EgressStatus, file: bool) -> proto::EgressInfo {
        proto::EgressInfo {
            egress_id: "egress-a".into(),
            room_name: "room-a".into(),
            status: status as i32,
            file_results: if file {
                vec![proto::FileInfo {
                    filename: "finished.mp3".into(),
                    location: "gs://fixture-bucket/finished.mp3".into(),
                    duration: 3_900_000_000,
                    ..Default::default()
                }]
            } else {
                vec![]
            },
            ..Default::default()
        }
    }
    async fn api(
        HttpState(fake): HttpState<Arc<Fake>>,
        headers: HeaderMap,
        uri: Uri,
        body: Bytes,
    ) -> Response {
        let path = uri.path();
        if path.starts_with("/twirp/") {
            assert!(headers["authorization"]
                .to_str()
                .unwrap()
                .starts_with("Bearer "));
        }
        if path.ends_with("StartRoomCompositeEgress") {
            let request = proto::RoomCompositeEgressRequest::decode(body).unwrap();
            assert_eq!(request.room_name, "room-a");
            assert!(request.audio_only);
            assert_eq!(request.file_outputs.len(), 1);
            fake.start_requests.lock().unwrap().push(request);
            fake.starts.fetch_add(1, Ordering::SeqCst);
            if fake.mode.load(Ordering::SeqCst) == 3 {
                fake.start_gate.notified().await;
            }
            wire(info(EgressStatus::EgressActive, true))
        } else if path.ends_with("StopEgress") {
            let request = proto::StopEgressRequest::decode(body).unwrap();
            assert_eq!(request.egress_id, "egress-a");
            fake.stops.fetch_add(1, Ordering::SeqCst);
            wire(info(
                if fake.mode.load(Ordering::SeqCst) == 2 {
                    EgressStatus::EgressFailed
                } else {
                    EgressStatus::EgressEnding
                },
                true,
            ))
        } else if path.ends_with("ListEgress") {
            let request = proto::ListEgressRequest::decode(body).unwrap();
            assert_eq!(request.egress_id, "egress-a");
            let index = fake.lists.fetch_add(1, Ordering::SeqCst);
            let mode = fake.mode.load(Ordering::SeqCst);
            if mode == 5 {
                return axum::http::StatusCode::BAD_GATEWAY.into_response();
            }
            let mut item = info(
                if mode == 1 || index == 0 {
                    EgressStatus::EgressActive
                } else {
                    EgressStatus::EgressComplete
                },
                true,
            );
            if mode == 4 {
                item.egress_id = "unrelated-egress".into();
            }
            wire(proto::ListEgressResponse {
                items: vec![item],
                ..Default::default()
            })
        } else {
            assert_eq!(headers["x-business-id"], BUSINESS);
            let body: Value = serde_json::from_slice(&body).unwrap();
            if path.ends_with("/recording") {
                fake.records.lock().unwrap().push(body);
            }
            Json(json!({"status":"ok"})).into_response()
        }
    }
    struct Harness {
        recorder: Arc<Recorder>,
        fake: Arc<Fake>,
        server: JoinHandle<()>,
    }
    impl Drop for Harness {
        fn drop(&mut self) {
            self.server.abort();
        }
    }
    async fn setup(mode: usize) -> Harness {
        let fake = Arc::new(Fake::default());
        fake.mode.store(mode, Ordering::SeqCst);
        let socket = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", socket.local_addr().unwrap());
        let app = Router::new().fallback(api).with_state(fake.clone());
        let server = tokio::spawn(async move {
            axum::serve(socket, app).await.unwrap();
        });
        let services = Services::new(
            super::super::platform::PlatformConfig {
                conversation: super::super::platform::Endpoint {
                    base_url: base.clone(),
                    token: "public-store-fixture".into(),
                    timeout_seconds: 1.0,
                },
                ..Default::default()
            },
            super::super::identity::test_session(json!({"business_id":BUSINESS})),
            Trace::default(),
        )
        .unwrap();
        let config = Config {
            enabled: true,
            api_url: base,
            api_key: "public-key-fixture".into(),
            api_secret: "public-secret-fixture".into(),
            bucket: "fixture-bucket".into(),
            gcp_credentials: "{\"type\":\"service_account\",\"project_id\":\"public-fixture\"}"
                .into(),
            poll_timeout_seconds: 0.2,
            poll_interval_seconds: 0.001,
            ..Default::default()
        };
        let recorder = Recorder::new(
            config,
            services,
            "room-a".into(),
            started(),
            Trace::default(),
        );
        recorder.start("canonical-session".into());
        if mode == 3 {
            tokio::time::timeout(Duration::from_secs(2), async {
                while fake.starts.load(Ordering::SeqCst) == 0 {
                    tokio::task::yield_now().await;
                }
            })
            .await
            .unwrap();
            recorder.mark_disconnected();
            fake.start_gate.notify_one();
        }
        tokio::time::timeout(Duration::from_secs(2), async {
            while recorder.state.lock().unwrap().egress.is_none() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        Harness {
            recorder,
            fake,
            server,
        }
    }
    #[tokio::test]
    async fn official_sdk_acknowledgement_and_early_location_wait_for_completed_file() {
        let h = setup(0).await;
        assert_eq!(h.recorder.state.lock().unwrap().status, "processing");
        assert!(h.recorder.state.lock().unwrap().url.is_none());
        h.recorder.mark_disconnected();
        h.recorder.mark_disconnected();
        h.recorder.close(77).await;
        let state = h.recorder.state.lock().unwrap();
        assert_eq!(state.status, "available");
        assert_eq!(
            state.url.as_deref(),
            Some("https://storage.googleapis.com/fixture-bucket/finished.mp3")
        );
        assert_eq!(state.duration, Some(3));
        assert_eq!(state.stop_ack, Some(true));
        assert_eq!(h.fake.starts.load(Ordering::SeqCst), 1);
        assert_eq!(h.fake.stops.load(Ordering::SeqCst), 1);
        assert_eq!(h.fake.lists.load(Ordering::SeqCst), 2);
        let records = h.fake.records.lock().unwrap();
        assert!(records
            .iter()
            .any(|r| r["recording_status"] == "processing" && r["recording_url"].is_null()));
        assert_eq!(records.last().unwrap()["recording_status"], "available");
    }
    #[tokio::test]
    async fn active_or_failed_egress_never_promotes_predicted_url_to_available() {
        for (mode, expected) in [
            (1, "processing"),
            (2, "failed"),
            (4, "processing"),
            (5, "processing"),
        ] {
            let h = setup(mode).await;
            h.recorder.close(77).await;
            let state = h.recorder.state.lock().unwrap();
            assert_eq!(state.status, expected);
            assert!(state.url.is_none());
            assert!(state.expected.is_some());
            if mode == 2 {
                assert_eq!(h.fake.lists.load(Ordering::SeqCst), 0);
            }
        }
    }
    #[tokio::test]
    async fn late_start_after_disconnect_is_stopped_exactly_once() {
        let h = setup(3).await;
        h.recorder.mark_disconnected();
        h.recorder.close(2).await;
        h.recorder.mark_disconnected();
        assert_eq!(h.fake.stops.load(Ordering::SeqCst), 1);
        assert_eq!(h.recorder.state.lock().unwrap().status, "available");
    }
    #[tokio::test]
    async fn disabled_recording_does_not_issue_egress_requests() {
        let h = setup(0).await;
        let recorder = Recorder::new(
            Config {
                enabled: false,
                ..h.recorder.config.clone()
            },
            h.recorder.services.clone(),
            "room-b".into(),
            started(),
            Trace::default(),
        );
        recorder.start("canonical-session".into());
        recorder.close(5).await;
        assert_eq!(recorder.state.lock().unwrap().status, "disabled");
        assert_eq!(h.fake.starts.load(Ordering::SeqCst), 1);
        assert_eq!(h.fake.stops.load(Ordering::SeqCst), 0);
        assert_eq!(h.fake.lists.load(Ordering::SeqCst), 0);
        h.recorder.close(5).await;
    }
    #[test]
    #[allow(deprecated)]
    fn completed_legacy_filename_and_duration_have_same_readiness_rules() {
        let mut value = info(EgressStatus::EgressActive, false);
        value.result = Some(proto::egress_info::Result::File(proto::FileInfo {
            filename: "/private/file.mp3".into(),
            ..Default::default()
        }));
        assert!(completed_file(&value).is_none());
        value.status = EgressStatus::EgressComplete as i32;
        assert!(completed_file(&value).is_some());
        let config = Config {
            storage: "local".into(),
            ..Default::default()
        };
        let uri = format!("local-recording:///calls/{BUSINESS}/file.mp3");
        assert_eq!(
            config.normalize_url("/private/file.mp3", Some(&uri)),
            Some(uri)
        );
    }
}
