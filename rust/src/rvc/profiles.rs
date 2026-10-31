//! Call-scoped policy selection. These values come from trusted runtime config,
//! not editable participant metadata or a model's tool arguments.
use super::Config;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
pub fn clone_seed(voice: &str) -> String {
    let hash = Sha256::digest(voice.as_bytes());
    u32::from_be_bytes([hash[0], hash[1], hash[2], hash[3]]).to_string()
}
pub fn default_wema_seed() -> String {
    clone_seed("helen-mavino-0030")
}
pub fn resolve_tts_seed(config: &Config) -> String {
    if !config.tts_seed.trim().is_empty() {
        return config.tts_seed.trim().into();
    }
    let voice = config.tts_voice.trim();
    if default_voice_allowlist()
        .iter()
        .any(|v| v.starts_with("wema-en-") && v.eq_ignore_ascii_case(voice))
    {
        return config.tts_wema_seed.clone();
    }
    if voice.is_empty() {
        String::new()
    } else {
        clone_seed(voice)
    }
}
pub fn opening_text(config: &Config, context: &AgentContext) -> String {
    let language = super::languages::code(&config.tts_language);
    if language == "pcm" {
        return if context.name.trim().is_empty() {
            "How you dey? How I fit help you?".into()
        } else {
            format!(
                "How you dey? Na {} dey here. How I fit help you?",
                context.name.trim()
            )
        };
    }
    if let Some(opening) = super::languages::data()["profiles"][language]["opening"].as_str() {
        return opening.into();
    }
    for line in context.instructions.lines() {
        if let Some((label, text)) = line.split_once(':') {
            let text = text.trim();
            if label.trim().eq_ignore_ascii_case("SPOKEN_OPENING")
                && !text.is_empty()
                && text.chars().count() <= 500
            {
                return text.into();
            }
        }
    }
    if !config.opening_greeting_text.trim().is_empty() {
        return config.opening_greeting_text.trim().into();
    }
    let name = context.name.trim();
    if matches!(
        config.tts_language.to_lowercase().as_str(),
        "fr" | "french" | "français"
    ) {
        if name.is_empty() {
            "Bonjour ! Comment puis-je vous aider aujourd'hui ?".into()
        } else {
            format!("Bonjour ! Je suis {name}. Comment puis-je vous aider aujourd'hui ?")
        }
    } else if name.is_empty() {
        "Hello! How can I help you today?".into()
    } else {
        format!("Hello! This is {name}. How can I help you today?")
    }
}

#[derive(Clone, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct AgentContext {
    pub business_id: String,
    pub agent_id: String,
    pub name: String,
    pub end_user_id: String,
    pub instructions: String,
    pub knowledge_base_ids: Vec<String>,
    pub tools: Vec<Value>,
    pub loaded: bool,
    pub tts_language_hint: String,
    pub tts_voice_id: String,
    pub outbound_enabled: bool,
    pub outbound_instructions: String,
    pub outbound_context_fields: Vec<Value>,
}
pub fn default_voice_allowlist() -> Vec<String> {
    "helen-mavino-0030,wema-en-ng-female,wema-en-ng-male,wema-en-us-female,wema-en-us-male,wema-en-ng-female-short,wema-en-ng-male-short,wema-en-us-female-short,wema-en-us-male-short,access-ghana-cindy".split(',').map(str::to_owned).collect()
}
pub fn apply_agent_configured_voice(
    config: &mut Config,
    context: &AgentContext,
    overrides: &Value,
) {
    let selected = overrides["tts_voice_id"]
        .as_str()
        .filter(|s| !s.is_empty())
        .or_else(|| overrides["tts_voice"].as_str())
        .unwrap_or("")
        .trim();
    let voice = context.tts_voice_id.trim();
    if selected.is_empty()
        && !voice.is_empty()
        && config
            .agent_voice_allowlist
            .iter()
            .any(|v| v.trim().eq_ignore_ascii_case(voice))
    {
        config.tts_voice = voice.into();
    }
}
pub fn reply_grace_seconds(config: &Config) -> f64 {
    if !matches!(config.transport.as_str(), "livekit" | "browser" | "sip")
        || !config.engagement_enabled
        || !config
            .reply_grace_agent_ids
            .iter()
            .map(|id| id.trim())
            .any(|id| !id.is_empty() && id == config.agent_context.agent_id)
    {
        return 0.0;
    }
    config.reply_grace_ms.clamp(0.0, 1500.0) / 1000.0
}
pub fn end_call_enabled(context: &AgentContext) -> bool {
    context.loaded
        && context.tools.iter().any(|t| {
            t["name"] == "end_call" && t["url"] == "builtin://end_call" && t["is_active"] != false
        })
}
/// Python pipeline.py scopes the private hint to agent/business IDs. A generic
/// harness without an agent context retains the operator's original profile.
pub fn apply_stt_scope(config: &mut Config) {
    if super::languages::uses_sbpn(&config.tts_language) {
        config.stt_chat_url.clear();
        config.stt_context.clear();
        return;
    }
    let context = &config.agent_context;
    let has_context =
        context.loaded || !context.agent_id.is_empty() || !context.business_id.is_empty();
    if !config.stt_chat_url.is_empty()
        && !config.stt_chat_scope.is_empty()
        && has_context
        && !config
            .stt_chat_scope
            .iter()
            .any(|id| id == &context.agent_id || id == &context.business_id)
    {
        config.stt_chat_url.clear();
        config.stt_context.clear();
    }
}
pub fn end_call_definition() -> Value {
    serde_json::json!({"type":"function","function":{"name":"end_call","description":"End the live phone call only when the caller explicitly says goodbye, asks to end or hang up, says there is nothing else, or confirms the conversation is finished. The application speaks the final goodbye before sending SIP BYE. Do not use this for a mere thank-you while the conversation may continue.","parameters":{"type":"object","properties":{},"additionalProperties":false}}})
}

pub fn for_inactivity_pilot(config: &mut Config, agent_id: &str, browser: bool, sip: bool) {
    let selected = config
        .inactivity_pilot_agent_ids
        .iter()
        .map(|id| id.trim())
        .any(|id| !id.is_empty() && (id == "*" || id == agent_id));
    if (browser || sip) && selected {
        config.engagement_enabled = true;
        config.engagement_idle_seconds = 10.0;
        config.engagement_tool_seconds = 10.0;
        config.engagement_max_idle_updates = 1;
        config.inactivity_pilot_enabled = true;
    }
}

pub fn inactivity_phrase(language: &str, goodbye: bool) -> Option<&'static str> {
    let phrases = match language.trim().to_lowercase().as_str() {
        "" | "en" | "eng" | "english" => (
            "Are you still there? Is there anything else I can help you with?",
            "I haven't heard back, so I'll end the call for now. Goodbye.",
        ),
        "fr" | "fra" | "fre" | "french" | "français" | "francais" => (
            "Êtes-vous toujours là ? Puis-je encore vous aider ?",
            "Je n’ai pas eu de réponse. Je vais terminer l’appel pour le moment. Au revoir.",
        ),
        "pcm" | "pd" | "pidgin" | "nigerian pidgin" => (
            "You still dey there? You need any other help from me?",
            "I no hear from you, so I go end the call now. Bye.",
        ),
        "yo" | "yor" | "yoruba" | "yorùbá" => (
            "Ṣé ẹ ṣì wà níbẹ̀? Ṣé mo tún lè ràn yín lọ́wọ́?",
            "Mi ò gbọ́ ìdáhùn yín, nítorí náà màá parí ìpè yìí báyìí. Ó dàbọ̀.",
        ),
        "ha" | "hau" | "hausa" => (
            "Kuna nan kuwa? Akwai wani abu da zan taimaka muku?",
            "Ban ji amsarku ba, zan kawo ƙarshen kiran yanzu. Sai an jima.",
        ),
        "ig" | "ibo" | "igbo" => (
            "Ị ka nọ ebe ahụ? Ọ dị ihe ọzọ m nwere ike inyere gị aka?",
            "Anụghị m azịza gị, ya mere m ga-akwụsị oku a ugbu a. Ka ọ dị.",
        ),
        _ => return None,
    };
    Some(if goodbye { phrases.1 } else { phrases.0 })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};
    #[test]
    fn private_asr_hint_is_removed_for_another_tenant() {
        let mut c: Config =
            serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
        c.stt_chat_url = "http://127.0.0.1/hint".into();
        c.stt_context = "private agent vocabulary".into();
        c.stt_chat_scope = vec!["business-a".into()];
        c.agent_context.loaded = true;
        c.agent_context.business_id = "business-a".into();
        apply_stt_scope(&mut c);
        assert!(!c.stt_context.is_empty());
        c.agent_context.business_id = "business-b".into();
        apply_stt_scope(&mut c);
        assert!(c.stt_context.is_empty());
        assert!(c.stt_chat_url.is_empty());
    }
    #[test]
    fn current_python_inactivity_contracts_match() {
        let reference: Value =
            serde_json::from_str(include_str!("../../fixtures/python-parity-reference.json"))
                .unwrap();
        for case in reference["inactivity_scope"].as_array().unwrap() {
            let mut c: Config =
                serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
            c.engagement_enabled = false;
            c.engagement_idle_seconds = 18.0;
            c.engagement_tool_seconds = 8.0;
            c.engagement_max_idle_updates = 3;
            c.inactivity_pilot_agent_ids = case["ids"]
                .as_array()
                .unwrap()
                .iter()
                .map(|s| s.as_str().unwrap().into())
                .collect();
            for_inactivity_pilot(
                &mut c,
                case["agent"].as_str().unwrap(),
                case["browser"].as_bool().unwrap(),
                case["sip"].as_bool().unwrap(),
            );
            let actual = json!({"engagement_enabled":c.engagement_enabled,"engagement_idle_seconds":c.engagement_idle_seconds,"engagement_tool_seconds":c.engagement_tool_seconds,"engagement_max_idle_updates":c.engagement_max_idle_updates,"inactivity_pilot_enabled":c.inactivity_pilot_enabled,"inactivity_pilot_close_seconds":c.inactivity_pilot_close_seconds});
            for (key, expected) in case["expected"].as_object().unwrap() {
                if expected.is_number() {
                    assert_eq!(actual[key].as_f64(), expected.as_f64(), "{case}");
                } else {
                    assert_eq!(actual[key], *expected, "{case}");
                }
            }
        }
        for case in reference["inactivity_phrases"].as_array().unwrap() {
            assert_eq!(
                inactivity_phrase(case["language"].as_str().unwrap(), false),
                case["checkin"].as_str(),
                "{case}"
            );
            assert_eq!(
                inactivity_phrase(case["language"].as_str().unwrap(), true),
                case["goodbye"].as_str(),
                "{case}"
            );
        }
        assert_eq!(
            super::super::prompts::DECISION_PROMPT,
            reference["decision_prompt"].as_str().unwrap()
        );
        let mut allowlist = default_voice_allowlist();
        allowlist.sort();
        assert_eq!(serde_json::json!(allowlist), reference["voice_allowlist"]);
        for case in reference["configured_voices"].as_array().unwrap() {
            let mut c: Config =
                serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
            let context = AgentContext {
                tts_voice_id: case["voice"].as_str().unwrap().into(),
                ..Default::default()
            };
            apply_agent_configured_voice(&mut c, &context, &case["overrides"]);
            assert_eq!(c.tts_voice, case["expected"].as_str().unwrap(), "{case}");
        }
        for case in reference["reply_grace"].as_array().unwrap() {
            let mut c: Config =
                serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
            c.engagement_enabled = case["enabled"].as_bool().unwrap();
            c.transport = case["transport"].as_str().unwrap().into();
            c.agent_context.agent_id = case["agent"].as_str().unwrap().into();
            c.reply_grace_agent_ids = vec!["selected".into()];
            c.reply_grace_ms = case["delay_ms"].as_f64().unwrap();
            assert_eq!(
                reply_grace_seconds(&c),
                case["expected_seconds"].as_f64().unwrap(),
                "{case}"
            );
        }
        for case in reference["seed_cases"].as_array().unwrap() {
            let mut c: Config =
                serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
            c.tts_wema_seed = reference["wema_seed"].as_str().unwrap().into();
            c.tts_voice = case["voice"].as_str().unwrap().into();
            c.tts_seed = case["seed"].as_str().unwrap().into();
            assert_eq!(resolve_tts_seed(&c), case["expected"].as_str().unwrap());
        }
        for case in reference["asr_echo"].as_array().unwrap() {
            assert_eq!(
                super::super::text::is_asr_context_echo(
                    case["transcript"].as_str().unwrap(),
                    case["context"].as_str().unwrap()
                ),
                case["expected"].as_bool().unwrap(),
                "{case}"
            );
        }
        for case in reference["knowledge_routes"].as_array().unwrap() {
            assert_eq!(
                super::super::text::requests_knowledge_lookup(case["answer"].as_str().unwrap()),
                case["expected"].as_bool().unwrap(),
                "{case}"
            );
        }
        for case in reference["opening_cases"].as_array().unwrap() {
            let mut c: Config =
                serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
            c.tts_language = case["language"].as_str().unwrap().into();
            c.opening_greeting_text = case["override"].as_str().unwrap().into();
            let context = AgentContext {
                name: case["name"].as_str().unwrap().into(),
                instructions: case["instructions"].as_str().unwrap().into(),
                ..Default::default()
            };
            assert_eq!(
                opening_text(&c, &context),
                case["expected"].as_str().unwrap()
            );
        }
        let identity = &reference["identity"];
        for case in identity["cases"].as_array().unwrap() {
            assert_eq!(
                super::super::identity::Verifier::default()
                    .verify(
                        case["token"].as_str().unwrap(),
                        identity["key"].as_str().unwrap(),
                        identity["now"].as_f64().unwrap()
                    )
                    .is_ok(),
                case["accepted"].as_bool().unwrap()
            );
        }
    }
}

#[cfg(test)]
mod resolved_surface_tests {
    use super::*;
    fn config() -> Config {
        serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap()
    }
    #[test]
    fn resolved_browser_and_sip_profiles_retain_sdk_reply_grace_and_scope_private_hints() {
        let mut c = config();
        c.engagement_enabled = true;
        c.agent_context.agent_id = "selected".into();
        c.reply_grace_agent_ids = vec![" selected ".into()];
        c.reply_grace_ms = 500.0;
        for transport in ["livekit", "browser", "sip"] {
            c.transport = transport.into();
            assert_eq!(reply_grace_seconds(&c), 0.5);
        }
        c.transport = "websocket".into();
        assert_eq!(reply_grace_seconds(&c), 0.0);
        c.tts_language = "Yoruba".into();
        c.stt_chat_url = "http://127.0.0.1:1/unused-English-hint-model".into();
        c.stt_context = "Private English vocabulary hint.".into();
        apply_stt_scope(&mut c);
        assert!(c.stt_chat_url.is_empty() && c.stt_context.is_empty());
        c.inactivity_pilot_enabled = false;
        c.inactivity_pilot_agent_ids = vec!["".into()];
        for_inactivity_pilot(&mut c, "", true, false);
        assert!(!c.inactivity_pilot_enabled);
    }
}
