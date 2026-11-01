//! Existing deployment environment -> native operator configuration. Secrets
//! remain in process memory; no JSON config or debug dump is written to disk.
use super::{
    bootstrap::OperatorConfig,
    platform::{Endpoint, PlatformConfig},
    Config,
};
use serde_json::{json, Value};

pub fn from_environment() -> Result<OperatorConfig, String> {
    let mut values: std::collections::HashMap<String, String> = std::env::vars().collect();
    for (file_key, keys) in [
        (
            "RVC_LAB_LIVEKIT_ENV_FILE",
            &["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"][..],
        ),
        (
            "RVC_LAB_DASHBOARD_ENV_FILE",
            &[
                "AGENT_CONFIG_API_BASE_URL",
                "AGENT_CONFIG_SERVICE_BASE_URL",
                "AGENT_CONFIG_SERVICE_TOKEN",
                "AGENT_CLIENT_ID",
                "CONVERSATION_API_BASE_URL",
                "CONVERSATION_SERVICE_BASE_URL",
                "CONVERSATION_SERVICE_TOKEN",
                "KNOWLEDGE_SERVICE_BASE_URL",
                "KNOWLEDGE_SERVICE_TOKEN",
            ][..],
        ),
    ] {
        let raw = values
            .get(file_key)
            .map(String::as_str)
            .unwrap_or("/sales-girl-dashboard/.env.local");
        let path = if let Some(rest) = raw.strip_prefix("~/") {
            std::path::PathBuf::from(
                std::env::var_os("HOME").ok_or("environment home unavailable")?,
            )
            .join(rest)
        } else {
            raw.into()
        };
        load_file(&mut values, &path, keys)?;
    }
    from_lookup(|name| values.get(name).cloned())
}
fn load_file(
    values: &mut std::collections::HashMap<String, String>,
    path: &std::path::Path,
    keys: &[&str],
) -> Result<(), String> {
    if !path.is_file() {
        return Ok(());
    }
    if std::fs::metadata(path)
        .map_err(|_| "cannot inspect trusted environment file")?
        .len()
        > 1024 * 1024
    {
        return Err("trusted environment file too large".into());
    }
    let entries =
        dotenvy::from_path_iter(path).map_err(|_| "cannot read trusted environment file")?;
    for entry in entries {
        let (key, value) = entry.map_err(|_| "invalid trusted environment file syntax")?;
        if keys.contains(&key.as_str())
            && !value.trim().is_empty()
            && values.get(&key).is_none_or(|v| v.trim().is_empty())
        {
            values.insert(key, value.trim().into());
        }
    }
    Ok(())
}
pub fn from_lookup(get: impl Fn(&str) -> Option<String>) -> Result<OperatorConfig, String> {
    let value = |names: &[&str], default: &str| {
        names
            .iter()
            .find_map(|n| get(n).filter(|s| !s.trim().is_empty()))
            .unwrap_or_else(|| default.into())
    };
    let mut voice: Value =
        serde_json::from_str(include_str!("../../fixtures/reference-config.json"))
            .map_err(|_| "invalid compiled voice defaults")?;
    // Parse by the declared config type. Bad booleans/numbers are configuration
    // errors rather than silently disabling an existing authentication feature.
    for (name, field) in voice.as_object_mut().ok_or("invalid voice defaults")? {
        if let Some(raw) = get(&format!("RVC_LAB_{}", name.to_uppercase())) {
            *field = match field {
                Value::Bool(_) => json!(boolean(&raw)?),
                Value::Number(_) => serde_json::from_str::<Value>(&raw)
                    .ok()
                    .filter(Value::is_number)
                    .ok_or_else(|| format!("invalid RVC_LAB_{} number", name.to_uppercase()))?,
                Value::Array(_) => json!(ids(&raw)),
                Value::String(_) => json!(raw),
                _ => {
                    return Err(format!(
                        "unsupported environment override: RVC_LAB_{}",
                        name.to_uppercase()
                    ))
                }
            };
        }
    }
    voice["transport"] = json!("livekit");
    voice["agent_context"] = json!({});
    voice["inactivity_pilot_enabled"] = json!(false);
    voice["hausa_twb_voices_enabled"] = json!(boolean(&value(
        &["RVC_LAB_ENABLE_HAUSA_TWB_VOICES"],
        "false"
    ))?);
    voice["input_noise_suppression"] = json!(value(&["RVC_INPUT_NOISE_SUPPRESSION"], "0") == "1");
    voice["input_noise_agent_ids"] = json!(ids(&value(&["RVC_INPUT_NOISE_AGENT_IDS"], "")));
    voice["input_noise_implementation"] =
        json!(value(&["RVC_INPUT_NOISE_IMPLEMENTATION"], "webrtc"));
    if voice["llm_provider"] == "deepseek" {
        for (field, env, default) in [
            (
                "llm_base_url",
                "RVC_LAB_DEEPSEEK_BASE_URL",
                "https://api.deepseek.com",
            ),
            ("llm_model", "RVC_LAB_DEEPSEEK_MODEL", "deepseek-flash"),
            ("llm_api_key", "RVC_LAB_DEEPSEEK_API_KEY", ""),
            (
                "llm_reasoning_effort",
                "RVC_LAB_DEEPSEEK_REASONING_EFFORT",
                "high",
            ),
        ] {
            voice[field] = json!(value(&[env], default));
        }
        if voice["llm_api_key"].as_str().is_none_or(str::is_empty) {
            return Err("RVC_LAB_DEEPSEEK_API_KEY is required".into());
        }
        voice["llm_enable_thinking"] =
            json!(boolean(&value(&["RVC_LAB_DEEPSEEK_THINKING"], "false"))?);
    }
    let agent_token = value(
        &["AGENT_CONFIG_SERVICE_TOKEN", "CONVERSATION_SERVICE_TOKEN"],
        "",
    );
    let endpoint = |urls: &[&str], tokens: &[&str]| Endpoint {
        base_url: value(urls, ""),
        token: value(tokens, ""),
        ..Default::default()
    };
    voice["platform"] = serde_json::to_value(PlatformConfig {
        agent: endpoint(
            &["AGENT_CONFIG_API_BASE_URL", "AGENT_CONFIG_SERVICE_BASE_URL"],
            &["AGENT_CONFIG_SERVICE_TOKEN", "CONVERSATION_SERVICE_TOKEN"],
        ),
        knowledge: endpoint(
            &["KNOWLEDGE_SERVICE_BASE_URL"],
            &["KNOWLEDGE_SERVICE_TOKEN", "CONVERSATION_SERVICE_TOKEN"],
        ),
        conversation: endpoint(
            &["CONVERSATION_SERVICE_BASE_URL", "CONVERSATION_API_BASE_URL"],
            &["CONVERSATION_SERVICE_TOKEN", "AGENT_CONFIG_SERVICE_TOKEN"],
        ),
        billing: endpoint(
            &["BILLING_SERVICE_BASE_URL", "BILLING_API_BASE_URL"],
            &["BILLING_SERVICE_TOKEN", "CONVERSATION_SERVICE_TOKEN"],
        ),
        client_id: value(&["AGENT_CLIENT_ID"], "sales-girl-voice-agent"),
        ..Default::default()
    })
    .map_err(|_| "invalid platform config")?;
    if agent_token.is_empty() {
        return Err("Agent config signing/service token is required".into());
    }
    for (field, env) in [
        ("agent", "AGENT_CONFIG_API_TIMEOUT_SECONDS"),
        ("knowledge", "KNOWLEDGE_SERVICE_TIMEOUT_SECONDS"),
        ("conversation", "CONVERSATION_API_TIMEOUT_SECONDS"),
        ("billing", "BILLING_API_TIMEOUT_SECONDS"),
    ] {
        voice["platform"][field]["timeout_seconds"] = json!(number(&value(&[env], "8"))?);
    }
    voice["platform"]["knowledge_top_k"] = voice["knowledge_top_k"].clone();
    voice["platform"]["action_timeout_seconds"] = voice["action_tool_timeout_seconds"].clone();
    voice["banking"] = json!({"voice_auth_sidecar_url":value(&["VOICE_AUTH_SIDECAR_URL"],""),"voice_auth_disabled":boolean(&value(&["VOICE_AUTH_DISABLED"],"false"))?,"voice_auth_interval_seconds":number(&value(&["VOICE_AUTH_INTERVAL_SECONDS"],"12"))?});
    for (section, prefix) in [
        ("passkey", "ATTENTIVE_PASSKEY_REAL"),
        ("passkey_demo", "ATTENTIVE_PASSKEY_DEMO"),
    ] {
        voice[section] = json!({"business_ids":ids(&value(&[&format!("{prefix}_BUSINESS_IDS")],"")),"agent_ids":ids(&value(&[&format!("{prefix}_AGENT_IDS")],""))});
    }
    voice["passkey"]["trim_options"] = json!(boolean(&value(
        &["ATTENTIVE_PASSKEY_TRIM_OPTIONS"],
        "true"
    ))?);
    voice["passkey_demo"]["subjects"] =
        json!(ids(&value(&["ATTENTIVE_PASSKEY_DEMO_SUBJECTS"], "")));
    voice["directory"] = json!({"endpoint":value(&["PROVIDER_DIRECTORY_ENDPOINT"],""),"token":value(&["PROVIDER_DIRECTORY_TOKEN"],""),"contacts_db":value(&["DIRECTORY_CONTACTS_DB"],""),"zepto_key":value(&["ZEPTO_API_KEY"],""),"from_address":value(&["ZEPTO_FROM_ADDRESS"],"")});
    let mut recording = serde_json::to_value(super::recording::Config::default())
        .map_err(|_| "invalid recording defaults")?;
    for (field, env) in [
        ("storage", "STORAGE_PROVIDER"),
        ("bucket", "BUCKET"),
        ("gcp_credentials", "GCP_CREDENTIALS_JSON"),
        ("s3_access_key", "S3_ACCESS_KEY"),
        ("s3_secret", "S3_SECRET_KEY"),
        ("s3_session_token", "S3_SESSION_TOKEN"),
        ("s3_region", "S3_REGION"),
        ("s3_endpoint", "S3_ENDPOINT"),
        ("prefix", "FILE_PREFIX"),
        ("local_directory", "LOCAL_DIRECTORY"),
        ("format", "FORMAT"),
        ("public_base_url", "PUBLIC_BASE_URL"),
    ] {
        if let Some(raw) = get(&format!("LIVEKIT_RECORDING_{env}")) {
            recording[field] = json!(raw);
        }
    }
    recording["enabled"] = json!(boolean(&value(&["LIVEKIT_RECORDING_ENABLED"], "false"))?);
    recording["s3_force_path_style"] = json!(boolean(&value(
        &["LIVEKIT_RECORDING_S3_FORCE_PATH_STYLE"],
        "false"
    ))?);
    recording["bucket"] = json!(value(
        &["LIVEKIT_RECORDING_BUCKET", "LIVEKIT_RECORDING_GCS_BUCKET"],
        ""
    ));
    for (field, env, default) in [
        (
            "poll_timeout_seconds",
            "LIVEKIT_RECORDING_POLL_TIMEOUT_SECONDS",
            "45",
        ),
        (
            "poll_interval_seconds",
            "LIVEKIT_RECORDING_POLL_INTERVAL_SECONDS",
            "2",
        ),
    ] {
        recording[field] = json!(number(&value(&[env], default))?);
    }
    voice["recording"] = recording;
    voice["transfer"] = json!({"outbound_trunk_id":value(&["AICC_OUTBOUND_TRUNK_ID"],""),"target_number":value(&["AICC_TRANSFER_TARGET_NUMBER"],""),"from_number":value(&["AICC_TRANSFER_FROM_NUMBER"],""),"preserve_caller_id":boolean(&value(&["AICC_TRANSFER_PRESERVE_CALLER_ID"],"false"))?});
    if let Some(path) = get("RVC_LAB_REPORT_DIR") {
        voice["report_dir"] = json!(path);
    }

    let voice: Config =
        serde_json::from_value(voice).map_err(|_| "invalid native voice environment types")?;
    voice.validate()?;
    let sip = match get("RVC_AICC_SIP_ROUTE").filter(|s| !s.trim().is_empty()) {
        Some(raw) => Some(serde_json::from_str(&raw).map_err(|_| "invalid RVC_AICC_SIP_ROUTE")?),
        None => None,
    };
    let capacity_business = value(&["MOMO_CALL_CAPACITY_BUSINESS_ID"], "");
    if !capacity_business.trim().is_empty()
        && (value(&["CONVERSATION_SERVICE_TOKEN"], "").trim().is_empty()
            || value(
                &["CONVERSATION_SERVICE_BASE_URL", "CONVERSATION_API_BASE_URL"],
                "",
            )
            .trim()
            .is_empty())
    {
        return Err(
            "Enabled capacity admission requires its Conversation service URL and token".into(),
        );
    }
    let config = OperatorConfig {
        worker: crate::worker::ProductionConfig {
            server_url: value(&["LIVEKIT_URL"], ""),
            api_key: value(&["LIVEKIT_API_KEY"], ""),
            api_secret: value(&["LIVEKIT_API_SECRET"], ""),
            agent_name: value(&["RVC_LAB_LIVEKIT_AGENT_NAME"], "rvc-livekit-comparison"),
            max_jobs: value(&["RVC_LAB_MAX_JOBS"], "8")
                .parse()
                .map_err(|_| "invalid RVC_LAB_MAX_JOBS")?,
            reconnect_limit: None,
            allow_insecure: boolean(&value(&["RVC_LAB_ALLOW_INSECURE_LIVEKIT"], "false"))?,
            ..Default::default()
        },
        voice,
        sip,
        capacity: super::capacity::Config {
            business_id: capacity_business,
            ..Default::default()
        },
    };
    config.validate()?;
    Ok(config)
}
fn ids(raw: &str) -> Vec<String> {
    raw.split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect()
}
fn boolean(raw: &str) -> Result<bool, String> {
    match raw.trim().to_lowercase().as_str() {
        "true" | "1" | "yes" | "on" => Ok(true),
        "false" | "0" | "no" | "off" | "" => Ok(false),
        _ => Err("invalid boolean environment setting".into()),
    }
}
fn number(raw: &str) -> Result<f64, String> {
    raw.parse::<f64>()
        .ok()
        .filter(|n| n.is_finite())
        .ok_or("invalid numeric environment setting".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    #[test]
    fn existing_env_file_is_allowlisted_and_never_overrides_process_settings() {
        let path = std::env::temp_dir().join(format!("native-env-{}", uuid::Uuid::new_v4()));
        std::fs::write(&path,"LIVEKIT_API_KEY=fixture-file-key\nAGENT_CONFIG_SERVICE_TOKEN=fixture-file-token\nRVC_LAB_LLM_API_KEY=must-not-import\n").unwrap();
        let mut values = HashMap::from([(
            "AGENT_CONFIG_SERVICE_TOKEN".into(),
            "fixture-process-token".into(),
        )]);
        load_file(
            &mut values,
            &path,
            &["LIVEKIT_API_KEY", "AGENT_CONFIG_SERVICE_TOKEN"],
        )
        .unwrap();
        assert_eq!(values.len(), 2);
        assert_eq!(values["LIVEKIT_API_KEY"], "fixture-file-key");
        assert_eq!(
            values["AGENT_CONFIG_SERVICE_TOKEN"],
            "fixture-process-token"
        );
        std::fs::write(&path, "PRIVATE_FIXTURE=\"unterminated-private-value").unwrap();
        let error = load_file(&mut values, &path, &["PRIVATE_FIXTURE"]).unwrap_err();
        assert!(!error.contains("unterminated-private-value"));
        std::fs::remove_file(path).unwrap();
    }
    fn fixture() -> HashMap<String, String> {
        [
            ("LIVEKIT_URL", "wss://livekit.example.test"),
            ("LIVEKIT_API_KEY", "public-test-key"),
            ("LIVEKIT_API_SECRET", "test-secret"),
            ("AGENT_CONFIG_API_BASE_URL", "https://agent.example.test"),
            ("AGENT_CONFIG_SERVICE_TOKEN", "fixture-signing-key"),
        ]
        .into_iter()
        .map(|(k, v)| (k.into(), v.into()))
        .collect()
    }
    #[test]
    fn existing_deployment_services_credentials_and_flags_survive() {
        let mut env = fixture();
        for (k, v) in [
            ("RVC_LAB_LLM_PROVIDER", "deepseek"),
            ("RVC_LAB_DEEPSEEK_API_KEY", "fixture-model-key"),
            ("RVC_INPUT_NOISE_SUPPRESSION", "1"),
            ("RVC_INPUT_NOISE_AGENT_IDS", "*"),
            ("RVC_INPUT_NOISE_IMPLEMENTATION", "rnnoise"),
            ("VOICE_AUTH_SIDECAR_URL", "http://voice-auth:8080"),
            (
                "CONVERSATION_SERVICE_BASE_URL",
                "https://conversation.example.test",
            ),
            ("CONVERSATION_SERVICE_TOKEN", "fixture-storage-key"),
            ("ATTENTIVE_PASSKEY_REAL_AGENT_IDS", "selected-agent"),
            ("RVC_LAB_INACTIVITY_PILOT_AGENT_IDS", "selected-agent"),
            ("RVC_LAB_REPLY_GRACE_MS", "500"),
            (
                "PROVIDER_DIRECTORY_ENDPOINT",
                "https://script.google.com/macros/s/fixture/exec",
            ),
            ("PROVIDER_DIRECTORY_TOKEN", "fixture-directory-key"),
            ("AICC_OUTBOUND_TRUNK_ID", "ST_fixture"),
            ("AICC_TRANSFER_TARGET_NUMBER", "+2347012345678"),
            (
                "LIVEKIT_RECORDING_GCP_CREDENTIALS_JSON",
                "{\n\"private_key\":\"fixture\"\n}",
            ),
        ] {
            env.insert(k.into(), v.into());
        }
        let config = from_lookup(|k| env.get(k).cloned()).unwrap();
        assert_eq!(config.worker.agent_name, "rvc-livekit-comparison");
        assert!(config.worker.reconnect_limit.is_none());
        assert_eq!(config.voice.llm_api_key, "fixture-model-key");
        assert_eq!(config.voice.llm_model, "deepseek-flash");
        assert!(!config.voice.llm_enable_thinking);
        assert!(config.voice.input_noise_suppression);
        assert_eq!(config.voice.input_noise_implementation, "rnnoise");
        assert_eq!(config.voice.passkey.agent_ids, vec!["selected-agent"]);
        assert!(!config.voice.inactivity_pilot_enabled);
        assert_eq!(
            config.voice.inactivity_pilot_agent_ids,
            vec!["selected-agent"]
        );
        assert_eq!(config.voice.reply_grace_ms, 500.0);
        assert_eq!(config.voice.transfer.outbound_trunk_id, "ST_fixture");
        assert!(config.voice.recording.gcp_credentials.contains('\n'));
        let platform = config.voice.platform.unwrap();
        assert_eq!(platform.agent.token, "fixture-signing-key");
        assert_eq!(platform.conversation.token, "fixture-storage-key");
    }
    #[test]
    fn enabled_capacity_requires_its_existing_service_credentials() {
        let mut env = fixture();
        env.insert("MOMO_CALL_CAPACITY_BUSINESS_ID".into(), "business-a".into());
        assert!(from_lookup(|key| env.get(key).cloned()).is_err());
        env.insert(
            "CONVERSATION_SERVICE_BASE_URL".into(),
            "https://capacity.example.test".into(),
        );
        assert!(from_lookup(|key| env.get(key).cloned()).is_err());
        env.insert(
            "CONVERSATION_SERVICE_TOKEN".into(),
            "fixture-capacity-token".into(),
        );
        let config = from_lookup(|key| env.get(key).cloned()).unwrap();
        assert_eq!(config.capacity.business_id, "business-a");
    }
    #[test]
    fn bad_or_incomplete_settings_fail_without_disclosing_values() {
        let mut env = fixture();
        env.insert("VOICE_AUTH_DISABLED".into(), "secret-not-a-bool".into());
        let error = from_lookup(|k| env.get(k).cloned()).err().unwrap();
        assert!(!error.contains("secret-not-a-bool"));
        env.remove("VOICE_AUTH_DISABLED");
        env.insert("RVC_LAB_LLM_PROVIDER".into(), "deepseek".into());
        assert!(from_lookup(|k| env.get(k).cloned()).is_err());
        env.insert(
            "RVC_LAB_DEEPSEEK_API_KEY".into(),
            "private-test-model-key".into(),
        );
        env.insert("RVC_AICC_SIP_ROUTE".into(), "{}".into());
        assert!(from_lookup(|k| env.get(k).cloned()).is_err());
    }
}
