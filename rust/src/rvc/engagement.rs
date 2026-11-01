//! Optional speech is prepared out of the audio actor, then revalidated there.
//! It never changes caller confirmation, transaction state or the output gate.
use super::{
    profiles::inactivity_phrase, prompts::DECISION_PROMPT, providers::Providers, python_json,
};
use serde_json::{json, Value};
use std::time::Duration;
use tokio::sync::mpsc;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Idle,
    IdleClose,
    ToolStarted,
    ToolWait,
    ToolCompleted,
}
impl Kind {
    pub fn is_tool(self) -> bool {
        matches!(
            self,
            Self::ToolStarted | Self::ToolWait | Self::ToolCompleted
        )
    }
    pub fn reason(self) -> &'static str {
        match self {
            Self::Idle => "idle",
            Self::IdleClose => "idle_close",
            Self::ToolStarted => "tool_started",
            Self::ToolWait => "tool_wait",
            Self::ToolCompleted => "tool_completed",
        }
    }
}
pub struct PreparedSpeech {
    pub text: String,
    pub pcm: Vec<Vec<i16>>,
}

pub(crate) async fn synthesize(p: &Providers, text: String) -> Result<PreparedSpeech, String> {
    if text.is_empty() {
        return Ok(PreparedSpeech { text, pcm: vec![] });
    }
    let (tx, mut rx) = mpsc::channel::<Vec<i16>>(8);
    let collect = async {
        let (mut pcm, mut samples) = (Vec::new(), 0);
        while let Some(chunk) = rx.recv().await {
            samples += chunk.len();
            if samples > 24000 * 12 {
                return Err("engagement PCM bound".to_string());
            }
            pcm.push(chunk);
        }
        Ok(pcm)
    };
    let produce = async {
        let result = p.tts(&text, 0, 0, &tx).await;
        drop(tx);
        result
    };
    let (_, pcm) = tokio::try_join!(produce, collect)?;
    Ok(PreparedSpeech { text, pcm })
}

pub async fn prepare(p: &Providers, kind: Kind, payload: Value) -> Result<PreparedSpeech, String> {
    let timeout = Duration::from_secs_f64(p.config.engagement_generation_timeout_seconds);
    if kind == Kind::IdleClose {
        let text = inactivity_phrase(&p.config.tts_language, true)
            .unwrap_or("")
            .to_string();
        return tokio::time::timeout(timeout, synthesize(p, text))
            .await
            .map_err(|_| "inactivity goodbye timeout".to_string())?;
    }
    let action = async {
        let output=p.collect(vec![json!({"role":"system","content":format!("{}\n{}",p.config.base_system_prompt,DECISION_PROMPT)}),json!({"role":"user","content":python_json(&payload)})],160,0.2).await?;
        let decision: Value =
            serde_json::from_str(&output).map_err(|_| "invalid engagement JSON".to_string())?;
        let valid = decision.as_object().is_some_and(|o| {
            o.len() == 3
                && ["speak", "text", "quiet"]
                    .iter()
                    .all(|k| o.contains_key(*k))
        }) && decision["speak"].is_boolean()
            && decision["text"]
                .as_str()
                .is_some_and(|s| s.chars().count() <= 240 && s.split_whitespace().count() <= 24)
            && matches!(
                decision["quiet"].as_str(),
                Some("none" | "until_user" | "until_result")
            );
        if !valid {
            return Err("invalid engagement decision".to_string());
        }
        let mut text = decision["text"].as_str().unwrap().trim().to_string();
        if decision["speak"] != true || text.is_empty() {
            text = if p.config.inactivity_pilot_enabled && kind == Kind::Idle {
                inactivity_phrase(&p.config.tts_language, false)
                    .unwrap_or("")
                    .into()
            } else {
                String::new()
            };
        }
        synthesize(p, text).await
    };
    let result = tokio::time::timeout(timeout, action)
        .await
        .map_err(|_| "engagement timeout".to_string())
        .and_then(|r| r);
    if result.is_ok() || !p.config.inactivity_pilot_enabled || kind != Kind::Idle {
        return result;
    }
    // The pilot explicitly falls back to an approved language-specific check-in
    // when optional model generation or its first synthesis fails.
    let text = inactivity_phrase(&p.config.tts_language, false)
        .unwrap_or("")
        .to_string();
    tokio::time::timeout(timeout, synthesize(p, text))
        .await
        .map_err(|_| "inactivity check-in timeout".to_string())?
}
