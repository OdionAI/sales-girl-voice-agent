//! Direct service clients. Every client is bound to a verified dashboard scope;
//! credentials and caller scope are never selected by a model or participant.
use super::{identity::VerifiedSession, profiles::AgentContext, Trace};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
    time::Duration,
};

#[derive(Clone, Deserialize, Serialize)]
#[serde(default)]
pub struct Endpoint {
    pub base_url: String,
    pub token: String,
    pub timeout_seconds: f64,
}
impl Default for Endpoint {
    fn default() -> Self {
        Self {
            base_url: String::new(),
            token: String::new(),
            timeout_seconds: 8.0,
        }
    }
}
#[derive(Clone, Deserialize, Serialize)]
#[serde(default)]
pub struct PlatformConfig {
    pub agent: Endpoint,
    pub knowledge: Endpoint,
    pub conversation: Endpoint,
    pub billing: Endpoint,
    pub client_id: String,
    pub knowledge_top_k: usize,
    pub action_timeout_seconds: f64,
}
impl Default for PlatformConfig {
    fn default() -> Self {
        Self {
            agent: Endpoint::default(),
            knowledge: Endpoint::default(),
            conversation: Endpoint::default(),
            billing: Endpoint::default(),
            client_id: "sales-girl-voice-agent".into(),
            knowledge_top_k: 4,
            action_timeout_seconds: 12.0,
        }
    }
}
impl PlatformConfig {
    pub fn validate(&self) -> Result<(), String> {
        if !self.action_timeout_seconds.is_finite()
            || !(0.0..=600.0).contains(&self.action_timeout_seconds)
            || self.action_timeout_seconds == 0.0
        {
            return Err("invalid action deadline".into());
        }
        for endpoint in [
            &self.agent,
            &self.knowledge,
            &self.conversation,
            &self.billing,
        ] {
            if !endpoint.timeout_seconds.is_finite()
                || !(0.0..=120.0).contains(&endpoint.timeout_seconds)
                || endpoint.timeout_seconds == 0.0
            {
                return Err("invalid service deadline".into());
            }
            if !endpoint.base_url.is_empty() {
                let u =
                    reqwest::Url::parse(&endpoint.base_url).map_err(|_| "invalid service URL")?;
                if !matches!(u.scheme(), "http" | "https")
                    || u.host_str().is_none()
                    || !u.username().is_empty()
                    || u.password().is_some()
                {
                    return Err("unsupported service URL".into());
                }
            }
        }
        Ok(())
    }
}
#[derive(Clone)]
pub struct Services {
    config: Arc<PlatformConfig>,
    scope: VerifiedSession,
    http: reqwest::Client,
    trace: Trace,
    knowledge_cache: Arc<Mutex<HashMap<String, KnowledgeResult>>>,
}
pub struct PreparedCall {
    pub config: super::Config,
    pub services: Services,
    pub caller: String,
}
/// Resolve a signed call. Unsupported capabilities are rejected before RTC join.
pub async fn prepare_call(
    config: super::Config,
    metadata: &str,
    room: &str,
    verifier: &super::identity::Verifier,
    now: f64,
    trace: Trace,
) -> Result<PreparedCall, String> {
    let platform = config
        .platform
        .clone()
        .ok_or("platform services are not configured")?;
    let scope = verifier.dispatch(metadata, room, &platform.agent.token, now)?;
    let services = Services::new(platform, scope, trace)?;
    let context = services.load_agent().await?;
    resolve_call(config, services, context, room)
}
/// Resolve only a context loaded through an already verified service scope.
pub(crate) fn resolve_call(
    mut config: super::Config,
    services: Services,
    context: AgentContext,
    room: &str,
) -> Result<PreparedCall, String> {
    validate_capabilities(&context)?;
    let caller = services.scope().caller().to_string();
    let claims = services.scope().claims();
    let overrides = &claims["runtime_overrides"];
    // The operator's DeepSeek endpoint, key and model stay authoritative.
    if config.llm_provider != "deepseek" {
        for (key, target) in [
            ("llm_model", &mut config.llm_model),
            ("llm_base_url", &mut config.llm_base_url),
            ("llm_api_key", &mut config.llm_api_key),
        ] {
            if let Some(value) = overrides[key].as_str().filter(|s| !s.trim().is_empty()) {
                *target = value.trim().into();
            }
        }
        config.llm_base_url = config
            .llm_base_url
            .trim_end_matches('/')
            .trim_end_matches("/chat/completions")
            .into();
        match overrides["llm_disable_thinking"]
            .as_str()
            .unwrap_or("")
            .trim()
            .to_ascii_lowercase()
            .as_str()
        {
            "true" | "1" | "yes" | "on" => config.llm_enable_thinking = false,
            "false" | "0" | "no" | "off" => config.llm_enable_thinking = true,
            _ => {}
        }
    }
    for (key, target) in [
        ("stt_model", &mut config.stt_model),
        ("stt_context", &mut config.stt_context),
        ("tts_model", &mut config.tts_model),
        ("tts_base_url", &mut config.tts_url),
        ("tts_seed", &mut config.tts_seed),
    ] {
        if let Some(value) = overrides[key].as_str().filter(|v| !v.trim().is_empty()) {
            *target = value.trim().into();
        }
    }
    if let Some(value) = overrides["stt_base_url"]
        .as_str()
        .filter(|v| !v.trim().is_empty())
    {
        let value = value.trim();
        let mut url = if let Some(rest) = value.strip_prefix("https://") {
            format!("wss://{rest}")
        } else if let Some(rest) = value.strip_prefix("http://") {
            format!("ws://{rest}")
        } else {
            value.to_string()
        };
        if url.trim_end_matches('/').ends_with("/v1") {
            url = format!("{}/realtime", url.trim_end_matches('/'));
        }
        config.stt_ws_url = url;
        let http = config
            .stt_ws_url
            .replacen("wss://", "https://", 1)
            .replacen("ws://", "http://", 1)
            .trim_end_matches('/')
            .to_string();
        config.stt_batch_url = format!(
            "{}/audio/transcriptions",
            http.strip_suffix("/realtime").unwrap_or(&http)
        );
    }
    if super::providers::realtime_final(&config) {
        config.stt_batch_url.clear();
    }
    if let Some(frames) = overrides["tts_initial_codec_chunk_frames"].as_str() {
        if !frames.is_empty() && frames.bytes().all(|b| b.is_ascii_digit()) {
            config.tts_initial_codec_chunk_frames =
                frames.parse().map_err(|_| "invalid codec frame count")?;
        }
    } else if let Some(frames) = overrides["tts_initial_codec_chunk_frames"].as_u64() {
        config.tts_initial_codec_chunk_frames =
            usize::try_from(frames).map_err(|_| "invalid codec frame count")?;
    }
    if config.llm_provider != "deepseek" && overrides["llm_disable_thinking"] == true {
        config.llm_enable_thinking = false;
    }
    if overrides["engagement_enabled"] == false || overrides["engagement_enabled"] == "false" {
        config.engagement_enabled = false;
    }
    if let Some(voice) = overrides["tts_voice_id"]
        .as_str()
        .filter(|s| !s.trim().is_empty())
        .or_else(|| {
            overrides["tts_voice"]
                .as_str()
                .filter(|s| !s.trim().is_empty())
        })
    {
        config.tts_voice = voice.trim().into();
    }
    super::profiles::apply_agent_configured_voice(&mut config, &context, overrides);
    let language = [
        overrides["tts_language_hint"].as_str().unwrap_or(""),
        config.speech_language_override.as_str(),
        context.tts_language_hint.as_str(),
        claims["language"].as_str().unwrap_or(""),
        config.tts_language.as_str(),
    ]
    .into_iter()
    .find(|value| !value.is_empty())
    .unwrap_or("English");
    config.tts_language = super::languages::normalize(language);
    super::languages::apply(&mut config, &context, overrides)?;
    config.tts_resolved_seed = super::profiles::resolve_tts_seed(&config);
    if !context.instructions.trim().is_empty() {
        config.base_system_prompt = format!(
            "{}{}",
            context.instructions,
            super::prompts::CONVERSATION_RULES
        );
    }
    config
        .base_system_prompt
        .push_str(&super::languages::rules(&config.tts_language));
    config.resolved_opening = super::profiles::opening_text(&config, &context);
    let sip = claims["entry_surface"] == "aicc_inbound" && claims["sip_caller_verified"] == true;
    let browser = !sip && (claims["identity_type"] == "web" || room.starts_with("rvc-livekit-"));
    config.explicitly_routed_agent = sip;
    config.transport = if sip {
        "sip"
    } else if browser {
        "browser"
    } else {
        "livekit"
    }
    .into();
    super::profiles::for_inactivity_pilot(&mut config, &context.agent_id, browser, sip);
    config.agent_context = context;
    super::profiles::apply_stt_scope(&mut config);
    config.validate()?;
    Ok(PreparedCall {
        config,
        services,
        caller,
    })
}
fn validate_capabilities(context: &AgentContext) -> Result<(), String> {
    let bank = super::banking::configured_names(context);
    for tool in context
        .tools
        .iter()
        .filter(|tool| tool["is_active"] != false)
    {
        let name = tool["name"].as_str().unwrap_or("").trim();
        let supported = super::actions::enabled(context, name)
            || (name == "end_call" && tool["url"] == "builtin://end_call")
            || (name == super::transfer::TOOL && super::transfer::enabled(context))
            || (name == super::passkey::TOOL && super::passkey::configured(context))
            || super::directory::enabled(context, name)
            || bank.iter().any(|item| item == name);
        if !supported {
            return Err("configured action capability has not been ported or is invalid".into());
        }
    }
    Ok(())
}
#[derive(Clone, Serialize, Deserialize)]
pub struct KnowledgeMatch {
    pub source_name: String,
    pub text: String,
    pub score: f64,
}
#[derive(Clone, Serialize, Deserialize)]
pub struct KnowledgeResult {
    pub query: String,
    pub status: String,
    pub matches: Vec<KnowledgeMatch>,
    pub cache_hit: bool,
    pub error: String,
}
impl KnowledgeResult {
    pub fn prompt_context(&self) -> String {
        if self.status == "success" && !self.matches.is_empty() {
            let snippets = self
                .matches
                .iter()
                .take(3)
                .map(|m| {
                    format!(
                        "- {}: {}",
                        m.source_name,
                        m.text.chars().take(900).collect::<String>()
                    )
                })
                .collect::<Vec<_>>()
                .join("\n");
            return format!("Validated business knowledge for the caller's current request:\nUse only relevant facts from these snippets. If they do not answer the question, say that the information was not found.\n{snippets}");
        }
        if self.status == "success" {
            "The scoped business knowledge search returned no matching information. Do not invent a business-specific answer.".into()
        } else {
            "The scoped business knowledge search was unavailable. Be transparent that the information could not be verified and do not invent it.".into()
        }
    }
}
impl Services {
    pub fn new(
        config: PlatformConfig,
        scope: VerifiedSession,
        trace: Trace,
    ) -> Result<Self, String> {
        config.validate()?;
        let http = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|_| "service client unavailable")?;
        Ok(Self {
            config: Arc::new(config),
            scope,
            http,
            trace,
            knowledge_cache: Default::default(),
        })
    }
    pub fn scope(&self) -> &VerifiedSession {
        &self.scope
    }
    pub fn config(&self) -> &PlatformConfig {
        &self.config
    }
    pub(crate) async fn request(
        &self,
        endpoint: &Endpoint,
        method: reqwest::Method,
        path: &[&str],
        body: Option<Value>,
    ) -> Result<Value, String> {
        if endpoint.base_url.is_empty() || endpoint.token.is_empty() {
            return Err("service is not configured".into());
        }
        let mut url = reqwest::Url::parse(&endpoint.base_url).map_err(|_| "invalid service URL")?;
        url.path_segments_mut()
            .map_err(|_| "invalid service URL")?
            .pop_if_empty()
            .extend(path);
        let mut request = self
            .http
            .request(method, url)
            .header("X-Service-Token", &endpoint.token)
            .header("X-Service-Name", &self.config.client_id)
            .header("X-Business-ID", self.scope.business_id())
            .header("X-Agent-ID", self.scope.agent_id())
            .timeout(Duration::from_secs_f64(endpoint.timeout_seconds));
        if let Some(body) = body {
            request = request.json(&body);
        }
        let mut response = request
            .send()
            .await
            .map_err(|_| "service transport unavailable")?;
        if !response.status().is_success() {
            return Err(format!("service HTTP {}", response.status().as_u16()));
        }
        let mut bytes = Vec::new();
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|_| "service body interrupted")?
        {
            if bytes.len() + chunk.len() > 2 * 1024 * 1024 {
                return Err("service response exceeds bound".into());
            }
            bytes.extend_from_slice(&chunk);
        }
        serde_json::from_slice(&bytes).map_err(|_| "invalid service response".into())
    }
    pub async fn load_agent(&self) -> Result<AgentContext, String> {
        self.trace.emit("agent_config_load_start", 0, 0, json!({}));
        let body = self
            .request(
                &self.config.agent,
                reqwest::Method::GET,
                &[
                    "v1",
                    "agents",
                    "internal",
                    "agents",
                    self.scope.agent_id(),
                    "runtime-config",
                ],
                None,
            )
            .await?;
        if !body.is_object() || body["status"] == "failed" || body["status"] == "disabled" {
            return Err("configured agent could not be loaded".into());
        }
        if body["business_id"]
            .as_str()
            .is_some_and(|id| id != self.scope.business_id())
            || body["agent_id"]
                .as_str()
                .is_some_and(|id| id != self.scope.agent_id())
        {
            return Err("configured agent scope mismatch".into());
        }
        let text = |key: &str| body[key].as_str().unwrap_or("").trim().to_string();
        let context = AgentContext {
            business_id: self.scope.business_id().into(),
            agent_id: self.scope.agent_id().into(),
            end_user_id: self.scope.claims()["end_user_email"]
                .as_str()
                .filter(|s| !s.trim().is_empty())
                .unwrap_or(self.scope.end_user_id())
                .trim()
                .into(),
            name: text("name"),
            instructions: text("instructions"),
            tts_language_hint: text("tts_language_hint"),
            tts_voice_id: text("tts_voice_id"),
            outbound_enabled: body["outbound_enabled"] == true,
            outbound_instructions: text("outbound_instructions"),
            outbound_context_fields: body["outbound_context_fields"]
                .as_array()
                .map(|items| items.iter().filter(|v| v.is_object()).cloned().collect())
                .unwrap_or_default(),
            knowledge_base_ids: body["knowledge_base_ids"]
                .as_array()
                .map(|items| {
                    items
                        .iter()
                        .filter_map(Value::as_str)
                        .map(str::trim)
                        .filter(|s| !s.is_empty())
                        .map(str::to_owned)
                        .collect()
                })
                .unwrap_or_default(),
            tools: body["tools"]
                .as_array()
                .map(|items| items.iter().filter(|v| v.is_object()).cloned().collect())
                .unwrap_or_default(),
            loaded: true,
        };
        self.trace.emit("agent_config_load_complete",0,0,json!({"knowledge_base_count":context.knowledge_base_ids.len(),"configured_tool_count":context.tools.len()}));
        Ok(context)
    }
    pub async fn knowledge(&self, context: &AgentContext, query: &str) -> KnowledgeResult {
        let normalized = query
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
            .to_lowercase();
        let fail = |reason: &str| KnowledgeResult {
            query: query.into(),
            status: "failed".into(),
            matches: vec![],
            cache_hit: false,
            error: reason.into(),
        };
        if context.business_id != self.scope.business_id()
            || context.agent_id != self.scope.agent_id()
            || !context.loaded
        {
            return fail("agent scope mismatch");
        }
        if context.knowledge_base_ids.is_empty() {
            return fail("agent_has_no_attached_knowledge_bases");
        }
        if let Some(mut result) = self
            .knowledge_cache
            .lock()
            .ok()
            .and_then(|cache| cache.get(&normalized).cloned())
        {
            result.query = query.into();
            result.cache_hit = true;
            return result;
        }
        let result=self.request(&self.config.knowledge,reqwest::Method::POST,&["v1","knowledge","search"],Some(json!({"query":query.trim(),"top_k":self.config.knowledge_top_k,"knowledge_base_ids":context.knowledge_base_ids}))).await;
        let body = match result {
            Ok(body) => body,
            Err(error) => return fail(&error),
        };
        let matches = body["matches"]
            .as_array()
            .into_iter()
            .flatten()
            .take(self.config.knowledge_top_k)
            .filter_map(|m| {
                let text = m["text"]
                    .as_str()?
                    .split_whitespace()
                    .collect::<Vec<_>>()
                    .join(" ");
                if text.is_empty() {
                    return None;
                }
                Some(KnowledgeMatch {
                    source_name: m["source_name"]
                        .as_str()
                        .unwrap_or("Knowledge")
                        .trim()
                        .into(),
                    text,
                    score: m["score"].as_f64().unwrap_or(0.0),
                })
            })
            .collect();
        let result = KnowledgeResult {
            query: query.into(),
            status: "success".into(),
            matches,
            cache_hit: false,
            error: String::new(),
        };
        if let Ok(mut cache) = self.knowledge_cache.lock() {
            if cache.len() < 256 {
                cache.insert(normalized, result.clone());
            }
        }
        result
    }
    pub async fn conversation_request(
        &self,
        method: reqwest::Method,
        path: &[&str],
        body: Value,
    ) -> Result<Value, String> {
        self.request(&self.config.conversation, method, path, Some(body))
            .await
    }
    pub(crate) async fn action_request(&self, path: &[&str], body: Value) -> Result<Value, String> {
        let mut endpoint = self.config.conversation.clone();
        endpoint.timeout_seconds = self.config.action_timeout_seconds;
        self.request(&endpoint, reqwest::Method::POST, path, Some(body))
            .await
    }
    pub(crate) async fn persistence_request(
        &self,
        path: &[&str],
        body: Value,
    ) -> Result<Value, String> {
        let mut endpoint = self.config.conversation.clone();
        endpoint.timeout_seconds = 4.0;
        self.request(&endpoint, reqwest::Method::POST, path, Some(body))
            .await
    }
    pub(crate) async fn observed_usage(&self, body: Value) -> Result<(), String> {
        let endpoint = &self.config.billing;
        if endpoint.base_url.is_empty() || endpoint.token.is_empty() {
            return Err("usage service unavailable".into());
        }
        let mut url =
            reqwest::Url::parse(&endpoint.base_url).map_err(|_| "invalid usage service")?;
        url.path_segments_mut()
            .map_err(|_| "invalid usage service")?
            .pop_if_empty()
            .extend(["v1", "internal", "credits", "observed-usage"]);
        let mut response = self
            .http
            .post(url)
            .header("X-Service-Token", &endpoint.token)
            .header("X-Service-Name", &self.config.client_id)
            .header("X-Business-ID", self.scope.business_id())
            .json(&body)
            .timeout(Duration::from_secs(4))
            .send()
            .await
            .map_err(|_| "usage transport unavailable")?
            .error_for_status()
            .map_err(|_| "usage HTTP failure")?;
        let mut size = 0;
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|_| "usage body unavailable")?
        {
            size += chunk.len();
            if size > 65536 {
                return Err("usage response bound".into());
            }
        }
        Ok(())
    }
    pub async fn billing_request(&self, path: &[&str], body: Value) -> Result<Value, String> {
        self.request(
            &self.config.billing,
            reqwest::Method::POST,
            path,
            Some(body),
        )
        .await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        extract::State,
        http::{HeaderMap, StatusCode},
        routing::{get, post},
        Json, Router,
    };
    struct Fake {
        requests: Mutex<Vec<(String, Value)>>,
        wrong_tenant: std::sync::atomic::AtomicBool,
        token: Mutex<String>,
    }
    impl Default for Fake {
        fn default() -> Self {
            Self {
                requests: Default::default(),
                wrong_tenant: Default::default(),
                token: Mutex::new("local-service-fixture".into()),
            }
        }
    }
    async fn agent(State(state): State<Arc<Fake>>, headers: HeaderMap) -> Json<Value> {
        assert_eq!(headers["x-business-id"], "business-a");
        assert_eq!(
            headers["x-service-token"].to_str().unwrap(),
            state.token.lock().unwrap().as_str()
        );
        state
            .requests
            .lock()
            .unwrap()
            .push(("agent".into(), Value::Null));
        Json(
            json!({"business_id":if state.wrong_tenant.load(std::sync::atomic::Ordering::SeqCst){"business-b"}else{"business-a"},"agent_id":"agent-a","name":"Configured agent","instructions":"Use attached facts only.","tts_voice_id":"access-ghana-cindy","knowledge_base_ids":["kb-a"],"tools":[],"outbound_enabled":false}),
        )
    }
    async fn knowledge(
        State(state): State<Arc<Fake>>,
        headers: HeaderMap,
        Json(body): Json<Value>,
    ) -> Json<Value> {
        assert_eq!(headers["x-business-id"], "business-a");
        assert_eq!(headers["x-agent-id"], "agent-a");
        assert_eq!(body["knowledge_base_ids"], json!(["kb-a"]));
        state
            .requests
            .lock()
            .unwrap()
            .push(("knowledge".into(), body));
        Json(
            json!({"matches":[{"source_name":"Agent document","text":"  scoped  fact \n only ","score":0.9}]}),
        )
    }
    fn scope() -> VerifiedSession {
        let r: Value =
            serde_json::from_str(include_str!("../../fixtures/python-parity-reference.json"))
                .unwrap();
        super::super::identity::Verifier::default()
            .verify(
                r["identity"]["cases"][0]["token"].as_str().unwrap(),
                r["identity"]["key"].as_str().unwrap(),
                1000.0,
            )
            .unwrap()
    }
    #[tokio::test]
    async fn configured_agent_and_knowledge_are_tenant_scoped_and_cached_per_call() {
        let state = Arc::new(Fake::default());
        let app = Router::new()
            .route(
                "/v1/agents/internal/agents/agent-a/runtime-config",
                get(agent),
            )
            .route("/v1/knowledge/search", post(knowledge))
            .with_state(state.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async { axum::serve(listener, app).await.unwrap() });
        let endpoint = Endpoint {
            base_url: url,
            token: "local-service-fixture".into(),
            timeout_seconds: 1.0,
        };
        let service = Services::new(
            PlatformConfig {
                agent: endpoint.clone(),
                knowledge: endpoint,
                ..Default::default()
            },
            scope(),
            Trace::default(),
        )
        .unwrap();
        let context = service.load_agent().await.unwrap();
        assert!(context.loaded);
        assert_eq!(context.tts_voice_id, "access-ghana-cindy");
        assert_eq!(context.end_user_id, "caller-a");
        let first = service.knowledge(&context, "bank policy").await;
        assert_eq!(first.status, "success");
        assert!(!first.cache_hit);
        assert_eq!(first.matches[0].text, "scoped fact only");
        let cached = service.knowledge(&context, "  BANK   policy ").await;
        assert!(cached.cache_hit);
        let mut wrong = context.clone();
        wrong.business_id = "business-b".into();
        assert_eq!(
            service.knowledge(&wrong, "bank policy").await.status,
            "failed"
        );
        assert_eq!(state.requests.lock().unwrap().len(), 2);
        state
            .wrong_tenant
            .store(true, std::sync::atomic::Ordering::SeqCst);
        assert!(service.load_agent().await.is_err());
        server.abort();
    }
    #[tokio::test]
    async fn service_failures_and_redirects_never_become_loaded_context() {
        let app = Router::new().fallback(|| async {
            (
                StatusCode::TEMPORARY_REDIRECT,
                [("Location", "http://127.0.0.1:1/should-not-be-called")],
            )
        });
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async { axum::serve(listener, app).await.unwrap() });
        let service = Services::new(
            PlatformConfig {
                agent: Endpoint {
                    base_url: url,
                    token: "local-service-fixture".into(),
                    timeout_seconds: 1.0,
                },
                ..Default::default()
            },
            scope(),
            Trace::default(),
        )
        .unwrap();
        assert!(matches!(service.load_agent().await,Err(e) if e=="service HTTP 307"));
        server.abort();
    }
    #[tokio::test]
    async fn signed_review_bootstrap_resolves_scoped_config_before_rtc_join() {
        let reference: Value =
            serde_json::from_str(include_str!("../../fixtures/python-parity-reference.json"))
                .unwrap();
        let key = reference["identity"]["key"].as_str().unwrap();
        let metadata =
            json!({"session_token":reference["identity"]["cases"][0]["token"]}).to_string();
        let state = Arc::new(Fake::default());
        *state.token.lock().unwrap() = key.into();
        let app = Router::new()
            .route(
                "/v1/agents/internal/agents/agent-a/runtime-config",
                get(agent),
            )
            .with_state(state.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async { axum::serve(listener, app).await.unwrap() });
        let mut config: super::super::Config =
            serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
        config.platform = Some(PlatformConfig {
            agent: Endpoint {
                base_url: url,
                token: key.into(),
                timeout_seconds: 1.0,
            },
            ..Default::default()
        });
        let verifier = super::super::identity::Verifier::default();
        let prepared = prepare_call(
            config.clone(),
            &metadata,
            "room-a",
            &verifier,
            1000.0,
            Trace::default(),
        )
        .await
        .unwrap();
        assert_eq!(prepared.caller, "caller");
        assert!(prepared
            .config
            .base_system_prompt
            .starts_with("Use attached facts only."));
        assert_eq!(prepared.config.tts_voice, "access-ghana-cindy");
        assert_eq!(
            prepared.config.tts_resolved_seed,
            super::super::profiles::clone_seed("access-ghana-cindy")
        );
        assert_eq!(prepared.config.agent_context.agent_id, "agent-a");
        assert!(prepare_call(
            config,
            &metadata,
            "room-a",
            &verifier,
            1000.0,
            Trace::default()
        )
        .await
        .is_err());
        assert_eq!(state.requests.lock().unwrap().len(), 1);
        server.abort();
    }
}
