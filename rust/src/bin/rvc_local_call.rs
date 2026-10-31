//! Loopback-only browser caller. Holds no production/provider credentials.
use axum::{
    extract::{DefaultBodyLimit, State},
    http::{header, HeaderMap, StatusCode},
    response::{Html, IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use livekit_api::{
    access_token::{AccessToken, VideoGrants},
    services::{
        agent_dispatch::AgentDispatchClient,
        room::{CreateRoomOptions, RoomClient},
    },
};
use serde::Deserialize;
use serde_json::json;
use std::{
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::sync::Mutex;
const API: &str = "http://127.0.0.1:17880";
const PORT: u16 = 17900;
struct Call {
    room: String,
    dispatch: Option<String>,
}
struct App {
    rooms: RoomClient,
    dispatch: AgentDispatchClient,
    agent: String,
    sdk: Vec<u8>,
    sequence: AtomicU64,
    active: Mutex<Option<Call>>,
    port: u16,
}
fn local_origin(headers: &HeaderMap, port: u16) -> bool {
    let host = headers.get(header::HOST).and_then(|h| h.to_str().ok());
    let origin = headers.get(header::ORIGIN).and_then(|h| h.to_str().ok());
    [format!("localhost:{port}"), format!("127.0.0.1:{port}")]
        .iter()
        .any(|v| Some(v.as_str()) == host)
        && [
            format!("http://localhost:{port}"),
            format!("http://127.0.0.1:{port}"),
        ]
        .iter()
        .any(|v| Some(v.as_str()) == origin)
}
fn error(code: StatusCode, message: &str) -> Response {
    (code, Json(json!({"error": message}))).into_response()
}
async fn retire(app: &App, call: Call) {
    if let Some(id) = call.dispatch {
        let _ = tokio::time::timeout(
            Duration::from_secs(5),
            app.dispatch.delete_dispatch(&id, &call.room),
        )
        .await;
    }
    let _ = tokio::time::timeout(Duration::from_secs(5), app.rooms.delete_room(&call.room)).await;
}
async fn session(State(app): State<Arc<App>>, headers: HeaderMap) -> Response {
    if !local_origin(&headers, app.port) {
        return error(StatusCode::FORBIDDEN, "Local browser origin required");
    }
    let mut active = app.active.lock().await;
    if let Some(call) = active.take() {
        retire(&app, call).await;
    }
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis();
    let name = format!(
        "sg-rvc-mac-{stamp}-{}",
        app.sequence.fetch_add(1, Ordering::Relaxed)
    );
    if !matches!(
        tokio::time::timeout(
            Duration::from_secs(8),
            app.rooms.create_room(
                &name,
                CreateRoomOptions {
                    empty_timeout: 60,
                    max_participants: 2,
                    ..Default::default()
                }
            )
        )
        .await,
        Ok(Ok(_))
    ) {
        return error(
            StatusCode::SERVICE_UNAVAILABLE,
            "Local call server is not ready",
        );
    }
    let token = AccessToken::with_api_key("devkey", "secret")
        .with_identity("sg-rvc-caller")
        .with_ttl(Duration::from_secs(900))
        .with_grants(VideoGrants {
            room_join: true,
            room: name.clone(),
            can_publish: Some(true),
            can_subscribe: Some(true),
            can_publish_data: Some(true),
            can_publish_sources: vec!["microphone".into()],
            ..Default::default()
        })
        .to_jwt();
    let Ok(token) = token else {
        let _ = app.rooms.delete_room(&name).await;
        return error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "Could not create local call token",
        );
    };
    *active = Some(Call {
        room: name.clone(),
        dispatch: None,
    });
    Json(json!({"room": name, "token": token, "url": "ws://127.0.0.1:17880"})).into_response()
}
#[derive(Deserialize)]
struct Request {
    room: String,
}
async fn dispatch(
    State(app): State<Arc<App>>,
    headers: HeaderMap,
    Json(request): Json<Request>,
) -> Response {
    if !local_origin(&headers, app.port) {
        return error(StatusCode::FORBIDDEN, "Local browser origin required");
    }
    let mut active = app.active.lock().await;
    let Some(call) = active.as_mut().filter(|c| c.room == request.room) else {
        return error(StatusCode::NOT_FOUND, "Local call no longer exists");
    };
    if call.dispatch.is_none() {
        let result = tokio::time::timeout(
            Duration::from_secs(8),
            app.dispatch
                .create_dispatch(livekit_protocol::CreateAgentDispatchRequest {
                    agent_name: app.agent.clone(),
                    room: call.room.clone(),
                    metadata: "native-rvc-one-call".into(),
                    ..Default::default()
                }),
        )
        .await;
        match result {
            Ok(Ok(value)) => call.dispatch = Some(value.id),
            _ => {
                return error(
                    StatusCode::SERVICE_UNAVAILABLE,
                    "Local Rust worker is not ready; try again",
                )
            }
        }
    }
    Json(json!({"ok": true})).into_response()
}
async fn end(
    State(app): State<Arc<App>>,
    headers: HeaderMap,
    Json(request): Json<Request>,
) -> Response {
    if !local_origin(&headers, app.port) {
        return error(StatusCode::FORBIDDEN, "Local browser origin required");
    }
    let mut active = app.active.lock().await;
    if active.as_ref().is_some_and(|c| c.room == request.room) {
        if let Some(call) = active.take() {
            retire(&app, call).await;
        }
    }
    Json(json!({"ok": true})).into_response()
}
async fn sdk(State(app): State<Arc<App>>) -> impl IntoResponse {
    (
        [
            (header::CONTENT_TYPE, "application/javascript"),
            (header::CACHE_CONTROL, "public, max-age=3600"),
        ],
        app.sdk.clone(),
    )
}
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    if !(3..=4).contains(&args.len()) || !args[2].starts_with("sg-rust-") {
        return Err("usage: rvc-local-call OFFICIAL_SDK_UMD_PATH sg-rust-AGENT_NAME [PORT]".into());
    }
    let port = args
        .get(3)
        .map(|s| s.parse::<u16>())
        .transpose()?
        .unwrap_or(PORT);
    if port == 0 {
        return Err("local review port must be nonzero".into());
    }
    let app = Arc::new(App {
        rooms: RoomClient::with_api_key(API, "devkey", "secret"),
        dispatch: AgentDispatchClient::with_api_key(API, "devkey", "secret"),
        sdk: std::fs::read(&args[1])?,
        agent: args[2].clone(),
        sequence: AtomicU64::new(0),
        active: Mutex::new(None),
        port,
    });
    let router = Router::new()
        .route(
            "/",
            get(|| async { Html(include_str!("../local_call/index.html")) }),
        )
        .route("/sdk.js", get(sdk))
        .route("/health", get(|| async { Json(json!({"local": true})) }))
        .route("/api/session", post(session))
        .route("/api/dispatch", post(dispatch))
        .route("/api/end", post(end))
        .layer(DefaultBodyLimit::max(1024))
        .with_state(app.clone());
    let listener = tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, port)).await?;
    println!("Local call ready: http://localhost:{port}");
    axum::serve(listener, router)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    if let Some(call) = app.active.lock().await.take() {
        retire(&app, call).await;
    }
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn browser_mutations_require_loopback_host_and_origin() {
        let mut h = HeaderMap::new();
        h.insert(header::HOST, "localhost:17900".parse().unwrap());
        assert!(!local_origin(&h, PORT));
        h.insert(header::ORIGIN, "https://example.com".parse().unwrap());
        assert!(!local_origin(&h, PORT));
        h.insert(header::ORIGIN, "http://localhost:17900".parse().unwrap());
        assert!(local_origin(&h, PORT));
        h.insert(header::HOST, "example.com:17900".parse().unwrap());
        assert!(!local_origin(&h, PORT));
        h.insert(header::HOST, "localhost:17901".parse().unwrap());
        assert!(!local_origin(&h, 17901));
        h.insert(header::ORIGIN, "http://localhost:17901".parse().unwrap());
        assert!(local_origin(&h, 17901));
        assert!(!local_origin(&h, 17900));
    }
}
