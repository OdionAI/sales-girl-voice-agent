//! Pinned language/provider contracts. Routing is operator/verified-profile
//! configuration; model output can never choose endpoints or voice identities.
use super::{profiles::AgentContext, Config};
use serde_json::Value;
use std::sync::OnceLock;
pub const SBPN_MODEL: &str = "ogunlao/SBPN_multilingual_base";
pub fn data() -> &'static Value {
    static DATA: OnceLock<Value> = OnceLock::new();
    DATA.get_or_init(|| {
        serde_json::from_str(include_str!("speech_language_data.json")).unwrap_or(Value::Null)
    })
}
pub fn code(language: &str) -> &'static str {
    match language.trim().to_lowercase().as_str() {
        "pcm" | "pd" | "pidgin" | "nigerian pidgin" => "pcm",
        "yo" | "yor" | "yoruba" | "yorùbá" => "yo",
        "ha" | "hau" | "hausa" => "ha",
        "ig" | "ibo" | "igbo" => "ig",
        _ => "",
    }
}
pub fn french(language: &str) -> bool {
    matches!(
        language.trim().to_lowercase().as_str(),
        "fr" | "fra" | "fre" | "french" | "français" | "francais"
    )
}
pub fn normalize(language: &str) -> String {
    let code = code(language);
    if !code.is_empty() {
        return match code {
            "pcm" => "Nigerian Pidgin",
            "yo" => "Yoruba",
            "ha" => "Hausa",
            _ => "Igbo",
        }
        .into();
    }
    if french(language) {
        "French".into()
    } else {
        match language.trim().to_lowercase().as_str() {
            "" | "en" | "eng" | "english" => "English".into(),
            _ => language.trim().into(),
        }
    }
}
pub fn uses_sbpn(language: &str) -> bool {
    !code(language).is_empty()
}
fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    value[key].as_str().unwrap_or("")
}
fn override_voice(overrides: &Value) -> &str {
    if text(overrides, "tts_voice_id").trim().is_empty() {
        text(overrides, "tts_voice")
    } else {
        text(overrides, "tts_voice_id")
    }
}
fn voice(selected: &str, voices: &Value, legacy: &Value, default: &str) -> Result<String, String> {
    let selected = selected.trim().to_lowercase();
    if legacy
        .as_array()
        .is_some_and(|items| items.iter().any(|v| v == &selected))
    {
        return Ok(default.into());
    }
    voices
        .as_object()
        .and_then(|voices| {
            voices
                .iter()
                .find(|(name, speaker)| {
                    name.as_str() == selected || speaker.as_str() == Some(&selected)
                })
                .map(|(name, _)| name.clone())
        })
        .ok_or("unknown configured speech voice".into())
}
fn gateway(config: &Config, url: &str) -> Result<String, String> {
    if config.speech_gateway_url.is_empty() {
        return Ok(url.into());
    }
    let source = reqwest::Url::parse(url).map_err(|_| "invalid compiled speech endpoint")?;
    let mut base =
        reqwest::Url::parse(&config.speech_gateway_url).map_err(|_| "invalid speech gateway")?;
    if !matches!(base.scheme(), "http" | "https")
        || base.host_str().is_none()
        || !base.username().is_empty()
        || base.password().is_some()
        || base.query().is_some()
        || base.fragment().is_some()
    {
        return Err("invalid speech gateway".into());
    }
    let path = format!("{}{}", base.path().trim_end_matches('/'), source.path());
    base.set_path(&path);
    if source.scheme() == "ws" {
        let secure = base.scheme() == "https";
        base.set_scheme(if secure { "wss" } else { "ws" })
            .map_err(|_| "invalid speech gateway protocol")?;
    }
    Ok(base.into())
}
/// Apply the source language stack after ordinary signed overrides/configured
/// named-voice selection. Noncommercial Hausa voices retain the operator gate.
pub fn apply(config: &mut Config, context: &AgentContext, overrides: &Value) -> Result<(), String> {
    let selected = [
        text(overrides, "tts_language_hint"),
        config.speech_language_override.as_str(),
        context.tts_language_hint.as_str(),
        config.tts_language.as_str(),
    ]
    .into_iter()
    .find(|s| !s.is_empty())
    .unwrap_or("English")
    .to_string();
    let language = code(&selected);
    if language.is_empty() {
        let selected = selected.trim().to_lowercase().replace('_', " ");
        let saved = context.tts_voice_id.trim();
        let wema = super::profiles::default_voice_allowlist()
            .iter()
            .any(|voice| voice.starts_with("wema-en-") && voice.eq_ignore_ascii_case(saved));
        if text(overrides, "tts_voice_id").is_empty()
            && text(overrides, "tts_voice").is_empty()
            && matches!(
                selected.as_str(),
                "" | "en" | "english" | "american english" | "nigerian english"
            )
            && wema
        {
            config.tts_voice = saved.into();
            config.tts_ref_audio.clear();
            config.tts_ref_text.clear();
        }
        return Ok(());
    }
    let values = data();
    if !values.is_object() {
        return Err("compiled speech routes unavailable".into());
    }
    let profiles = &values["profiles"];
    let profile = &profiles[language];
    let saved = if code(&context.tts_language_hint) == language {
        context.tts_voice_id.as_str()
    } else {
        ""
    };
    let configured = config.tts_voice.trim().to_lowercase();
    let requested = override_voice(overrides).trim();
    if language == "pcm" {
        let configured = if values["dynamic_voices"]
            .as_array()
            .is_some_and(|v| v.iter().any(|v| v == &configured))
        {
            configured.as_str()
        } else {
            ""
        };
        let selected = [requested, saved, configured]
            .into_iter()
            .find(|s| !s.is_empty())
            .unwrap_or("")
            .trim()
            .to_lowercase();
        let dynamic = values["dynamic_voices"]
            .as_array()
            .is_some_and(|v| v.iter().any(|v| v == &selected));
        config.tts_language = "Nigerian Pidgin".into();
        config.tts_model = text(
            values,
            if dynamic {
                "dynamic_model"
            } else {
                "ejiro_model"
            },
        )
        .into();
        config.tts_voice = if dynamic {
            selected
        } else {
            text(values, "pidgin_voice").into()
        };
        config.tts_url = gateway(
            config,
            text(values, if dynamic { "dynamic_url" } else { "ejiro_url" }),
        )?;
        config.tts_seed = if dynamic { "0" } else { "17" }.into();
    } else {
        config.tts_language = text(profile, "name").into();
        config.tts_model = text(profile, "model").into();
        config.tts_voice = text(profile, "voice").into();
        config.tts_url = gateway(config, text(profile, "url"))?;
        if language == "ig" || language == "ha" {
            let voices = &values[if language == "ig" {
                "igbo_voices"
            } else {
                "hausa_voices"
            }];
            let legacy = &values[if language == "ig" {
                "igbo_legacy"
            } else {
                "hausa_legacy"
            }];
            let configured = if voices.as_object().is_some_and(|voices| {
                voices.contains_key(&configured) || voices.values().any(|v| v == &configured)
            }) || language == "ha" && configured == "abdul"
            {
                configured.as_str()
            } else {
                ""
            };
            let requested = [requested, saved, configured]
                .into_iter()
                .find(|s| !s.is_empty())
                .unwrap_or("");
            if language == "ha" && requested.trim().eq_ignore_ascii_case("abdul") {
                config.tts_model = text(values, "abdul_model").into();
                config.tts_voice = "abdul".into();
            } else {
                let chosen = voice(
                    requested,
                    voices,
                    legacy,
                    if language == "ig" {
                        "chinedu"
                    } else {
                        "haisha"
                    },
                )?;
                if language == "ig" {
                    config.tts_voice = chosen;
                } else if config.hausa_twb_voices_enabled {
                    config.tts_model = "hausa".into();
                    config.tts_voice = chosen;
                } else if !legacy.as_array().is_some_and(|items| {
                    items.iter().any(|v| v == &requested.trim().to_lowercase())
                }) {
                    return Err("Hausa research voices are not enabled".into());
                }
            }
        }
    }
    config.tts_ref_audio.clear();
    config.stt_model = SBPN_MODEL.into();
    config.stt_ws_url = gateway(config, text(values, "sbpn_ws_url"))?;
    config.stt_batch_url = gateway(config, text(values, "sbpn_url"))?;
    config.stt_batch_timeout_seconds = 120.0;
    Ok(())
}
pub fn rules(language: &str) -> String {
    if french(language) {
        return "\n\nCALL LANGUAGE: French (fr). Speak natural French throughout the call, including greetings, questions, acknowledgements, progress, answers and goodbyes. Preserve all factual meaning, names, values and identifiers. Keep tool names, JSON keys and numeric arguments unchanged. Use one or two short spoken sentences and one question at a time.\n".into();
    }
    let language = code(language);
    if language == "pcm" {
        return text(data(), "pidgin_rules").into();
    }
    let profile = &data()["profiles"][language];
    let name = text(profile, "name");
    if name.is_empty() {
        return String::new();
    }
    format!("\n\nCALL LANGUAGE: {name} ({language}). Speak natural {name} throughout\nthis call. Give greetings, tool acknowledgements, progress updates, answers and\ngoodbyes in {name}, even when business instructions, retrieved information\nor tool results are in English. Use the language's correct native spelling and\ndiacritics. Preserve names, amounts, account numbers and factual meaning. Spell\nspoken numbers in {name} words; read identifiers digit by digit without\nlosing zeros. Keep numeric tool arguments as numbers and tool names and JSON keys\nunchanged. Use short, complete spoken sentences and ask one question at a time.\nKeep all existing safety and business rules. Do not invent facts.\n")
}
pub fn split_text(mut input: &str, limit: usize) -> Result<Vec<String>, String> {
    if limit == 0 {
        return Err("text limit must be positive".into());
    }
    let mut parts = Vec::new();
    while input.chars().count() > limit {
        let end = input
            .char_indices()
            .nth(limit)
            .map(|(i, _)| i)
            .ok_or("invalid text split")?;
        let window = &input[..end];
        let sentence = [". ", "? ", "! ", "; "]
            .iter()
            .filter_map(|mark| window.rfind(mark).map(|i| i + mark.len()))
            .max();
        let boundary = sentence
            .filter(|index| window[..*index].chars().count() > limit / 2)
            .or_else(|| window.rfind(' ').map(|i| i + 1))
            .filter(|i| *i > 0)
            .unwrap_or(end);
        parts.push(input[..boundary].into());
        input = &input[boundary..];
    }
    if !input.is_empty() {
        parts.push(input.into());
    }
    Ok(parts)
}
pub fn pidgin_spoken_text(input: &str) -> Result<String, String> {
    static DECIMALS: OnceLock<Result<std::collections::HashMap<String, u8>, String>> =
        OnceLock::new();
    let digits = DECIMALS
        .get_or_init(|| {
            serde_json::from_str(include_str!("unicode_decimals.json"))
                .map_err(|_| "compiled Unicode decimals unavailable".into())
        })
        .as_ref()
        .map_err(Clone::clone)?;
    let words = [
        "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    ];
    let input = input.replace('₦', " naira ").replace('%', " percent ");
    let chars = input.chars().collect::<Vec<_>>();
    let mut output = String::new();
    let mut numeric = false;
    for (i, c) in chars.iter().enumerate() {
        let digit = digits.get(&(*c as u32).to_string());
        if let Some(digit) = digit {
            output.push(' ');
            output.push_str(words[*digit as usize]);
            numeric = true;
            continue;
        }
        if numeric {
            output.push(' ');
            numeric = false;
        }
        if *c == '.'
            && i > 0
            && i + 1 < chars.len()
            && digits.contains_key(&(chars[i - 1] as u32).to_string())
            && digits.contains_key(&(chars[i + 1] as u32).to_string())
        {
            output.push_str(" point ");
        } else {
            output.push(*c);
        }
    }
    if numeric {
        output.push(' ');
    }
    Ok(output)
}

pub struct TtsRequest {
    pub body: Value,
    pub text: String,
    pub timeout_seconds: f64,
    pub sample_rate: u32,
    pub headers: std::collections::HashMap<String, String>,
}
/// Exact service contracts; speech-only expansion never changes model tool JSON.
pub fn tts_requests(config: &Config, input: &str) -> Result<Vec<TtsRequest>, String> {
    let code = code(&config.tts_language);
    if code.is_empty() {
        return Err("not a native-language speech route".into());
    }
    let values = data();
    let dynamic = code == "pcm"
        && values["dynamic_voices"]
            .as_array()
            .is_some_and(|items| items.iter().any(|v| v == &config.tts_voice));
    let spoken = if code == "pcm" && !dynamic {
        pidgin_spoken_text(input)?
    } else {
        input.into()
    };
    let mut requests = Vec::new();
    for part in split_text(&spoken, if code == "pcm" && !dynamic { 240 } else { 140 })? {
        if part.trim().is_empty() {
            continue;
        }
        let mut body = serde_json::json!({"model":config.tts_model,"voice":config.tts_voice,"input":part,"response_format":"pcm","stream":true});
        let mut headers =
            std::collections::HashMap::from([("content-type".into(), "audio/pcm".into())]);
        let mut timeout = 60.0;
        let mut sample_rate = 24000;
        if code == "pcm" && !dynamic {
            body["model"] = values["ejiro_model"].clone();
            body["voice"] = values["pidgin_voice"].clone();
            body["seed"] = serde_json::json!(17);
            body["speed"] = serde_json::json!(1);
            sample_rate = 16000;
            for (key, value) in [
                ("x-sample-rate", "16000"),
                ("x-channels", "1"),
                ("x-audio-format", "pcm_s16le"),
            ] {
                headers.insert(key.into(), value.into());
            }
        } else {
            headers.insert("x-audio-sample-rate".into(), "24000".into());
            headers.insert("x-audio-channels".into(), "1".into());
            if dynamic {
                body["model"] = values["dynamic_model"].clone();
                body["language"] = serde_json::json!("pcm");
                body["stream_format"] = serde_json::json!("audio");
                body["seed"] = serde_json::json!(0);
                body["speed"] = serde_json::json!(1);
                timeout = 180.0;
                for (key, value) in [
                    ("x-model", text(values, "dynamic_model").to_string()),
                    ("x-voice", config.tts_voice.clone()),
                    ("x-speaker-id", config.tts_voice.clone()),
                    (
                        "x-checkpoint-sha256",
                        text(values, "dynamic_checkpoint").to_string(),
                    ),
                    ("x-voice-profile", format!("{}-flash-v1", config.tts_voice)),
                    ("x-seed", "0".into()),
                    ("x-speed", "1".into()),
                ] {
                    headers.insert(key.into(), value);
                }
            } else if code == "ha" && config.tts_voice == "abdul" {
                body["model"] = values["abdul_model"].clone();
                body["speaker_id"] = serde_json::json!("abdul");
                body["language"] = serde_json::json!("ha");
                body["stream_format"] = serde_json::json!("audio");
                body["speed"] = serde_json::json!(1);
                body["seed"] = serde_json::json!(17);
                for (key, value) in [
                    ("x-model", text(values, "abdul_model")),
                    ("x-voice", "abdul"),
                    ("x-speaker-id", "abdul"),
                    ("x-checkpoint-sha256", text(values, "abdul_checkpoint")),
                ] {
                    headers.insert(key.into(), value.into());
                }
            } else if code == "ha" && config.hausa_twb_voices_enabled {
                let voice = voice(
                    &config.tts_voice,
                    &values["hausa_voices"],
                    &values["hausa_legacy"],
                    "haisha",
                )?;
                body["model"] = serde_json::json!("hausa");
                body["voice"] = serde_json::json!(voice);
                body["speaker_id"] = values["hausa_voices"][&voice].clone();
                body["stream_format"] = serde_json::json!("audio");
                body["speed"] = serde_json::json!(1);
                body["seed"] = serde_json::json!(17);
            }
            if code == "ig" {
                body["language"] = serde_json::json!("ig");
                timeout = 1800.0;
            }
        }
        requests.push(TtsRequest {
            body,
            text: part,
            timeout_seconds: timeout,
            sample_rate,
            headers,
        });
    }
    Ok(requests)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> Value {
        serde_json::from_str(include_str!("../../fixtures/python-parity-reference.json")).unwrap()
    }
    fn config() -> Config {
        serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap()
    }
    #[test]
    fn native_language_routes_match_independent_python_profiles() {
        let f = fixture();
        assert_eq!(&f["speech_language_data"], data());
        for case in f["speech_language_routes"].as_array().unwrap() {
            let mut body = serde_json::to_value(config()).unwrap();
            for (key, value) in f["speech_language_base"].as_object().unwrap() {
                body[key] = value.clone();
            }
            let mut config: Config = serde_json::from_value(body).unwrap();
            config.tts_voice = "helen-mavino-0030".into();
            config.tts_language = normalize(case["language"].as_str().unwrap());
            config.hausa_twb_voices_enabled = case["research"].as_bool().unwrap();
            let context = AgentContext {
                tts_language_hint: case["language"].as_str().unwrap().into(),
                tts_voice_id: case["saved_voice"].as_str().unwrap().into(),
                ..Default::default()
            };
            let result = apply(&mut config, &context, &case["overrides"]);
            assert_eq!(result.is_err(), case["error"], "{case}");
            if result.is_ok() {
                let body = serde_json::to_value(config).unwrap();
                for (key, value) in case["expected"].as_object().unwrap() {
                    assert_eq!(&body[key], value, "{key} {case}");
                }
            }
        }
    }
    #[test]
    fn native_speech_text_splits_and_spoken_only_numbers_match_python() {
        let f = fixture();
        for case in f["speech_text_splits"].as_array().unwrap() {
            assert_eq!(
                serde_json::to_value(
                    split_text(
                        case["text"].as_str().unwrap(),
                        case["limit"].as_u64().unwrap() as usize
                    )
                    .unwrap()
                )
                .unwrap(),
                case["expected"]
            );
        }
        assert!(split_text("text", 0).is_err());
        for case in f["pidgin_spoken_numbers"].as_array().unwrap() {
            assert_eq!(
                pidgin_spoken_text(case["text"].as_str().unwrap()).unwrap(),
                case["expected"]
            );
        }
    }
    #[test]
    fn native_tts_payloads_and_timeouts_match_actual_python_clients() {
        let f = fixture();
        for case in f["native_tts_contracts"].as_array().unwrap() {
            let mut c = config();
            c.tts_language = normalize(case["language"].as_str().unwrap());
            c.hausa_twb_voices_enabled = case["research"].as_bool().unwrap();
            let context = AgentContext {
                tts_language_hint: case["language"].as_str().unwrap().into(),
                tts_voice_id: case["voice"].as_str().unwrap().into(),
                ..Default::default()
            };
            apply(&mut c, &context, &Value::Null).unwrap();
            let requests = tts_requests(&c, case["text"].as_str().unwrap()).unwrap();
            assert_eq!(
                serde_json::to_value(requests.iter().map(|r| &r.body).collect::<Vec<_>>()).unwrap(),
                case["requests"],
                "{case}"
            );
            assert_eq!(
                requests
                    .iter()
                    .map(|request| request.timeout_seconds)
                    .collect::<Vec<_>>(),
                case["timeouts"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|value| value.as_f64().unwrap())
                    .collect::<Vec<_>>()
            );
        }
    }
    #[test]
    fn operator_gateway_overrides_only_speech_origins_and_rejects_credentials() {
        let mut c = config();
        c.speech_gateway_url = "http://127.0.0.1:18080".into();
        let context = AgentContext {
            tts_language_hint: "pcm".into(),
            tts_voice_id: "maro".into(),
            ..Default::default()
        };
        apply(&mut c, &context, &Value::Null).unwrap();
        assert_eq!(
            c.tts_url,
            "http://127.0.0.1:18080/tts-pidgin-dynamic/v1/audio/speech"
        );
        assert_eq!(c.stt_ws_url, "ws://127.0.0.1:18080/asr-sbpn/v1/realtime");
        assert_eq!(c.tts_seed, "0");
        for url in [
            "http://key:secret@127.0.0.1:1",
            "http://127.0.0.1:1?token=not-allowed",
            "file:///tmp/wrong",
        ] {
            c.speech_gateway_url = url.into();
            assert!(apply(&mut c, &context, &Value::Null).is_err());
        }
    }
}
