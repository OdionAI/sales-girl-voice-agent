//! Balogun-only, explicitly triggered transaction phone-number capture.
//!
//! Audio windows and parsed candidates stay in coordinator memory. This module
//! contains no logging and deliberately accepts only complete digit runs.

use serde_json::{json, Value};
use std::time::Instant;

pub(crate) const BALOGUN_AGENT_ID: &str = "3801e455-224a-4872-b406-95a4db6b8ffa";
pub(crate) const TOOL: &str = "collect_transaction_phone_number";
pub(crate) const WINDOW_SECONDS: usize = 15;
pub(crate) const POST_SPEECH_SILENCE_SAMPLES: usize = 16_000;
pub(crate) const MAX_SAMPLES: usize = 240_000;
pub(crate) const PREROLL_SAMPLES: usize = 32_000;

pub(crate) const PROMPT_RULES: &str = r###"
BALOGUN CUSTOMER SUPPORT FLOW:
Your job is simple: understand the issue, answer briefly from attached knowledge, create a support ticket when needed, or connect the caller to a human when requested. Use one or two short Yoruba sentences and ask one question at a time.
Ask for a phone number only for a complaint about a transaction, and only when the affected transaction number is needed. Do not ask for a phone number for general questions, account setup, app navigation, data-purchase instructions, or a human-transfer request. For a transaction number, call collect_transaction_phone_number instead of asking for it in free-form speech; the application will ask in Yoruba and capture the answer. Never call that tool for a non-transaction issue. Do not ask for the same number again after confirmation unless the caller explicitly says a different number applies or starts a distinct transaction issue. Do not use or put an unconfirmed candidate in a ticket.
The application transcribes the bounded number-answer audio separately. Short SBPN fragments are not a complete number; do not guess, combine fragments yourself, or ask again while capture is pending. Read every candidate digit in Yoruba and ask whether you heard it correctly. Use the number only after a clear confirmation. On a correction, replace the candidate and confirm the new number; never append correction digits to the old candidate. Keep the caller's number out of unnecessary explanations.
"###;

pub(crate) const YORUBA_TRANSFER_RULES: &str = r###"
YORUBA HUMAN-TRANSFER INTENT:
Treat a direct Yoruba request for a human representative as a request to transfer now. Examples include “Ẹ so mí pọ̀ mọ́ aṣojú oníbàárà,” “Mo fẹ́ bá ènìyàn sọ̀rọ̀,” “Ẹ jọ̀wọ́, gbe mi lọ sí ẹ̀ka ìtọ́jú oníbàárà,” and “Ẹ fi mí sọ̀rọ̀ pẹ̀lú aṣojú gidi.” These are examples of intent, not exact-match phrases. A caller accepting an offered human transfer also means transfer immediately. “Má so mí pọ̀ mọ́ ẹnikẹ́ni” is a refusal and must not trigger transfer. Do not ask for a phone number before a requested human handoff. The transfer tool performs the handoff; never announce that you are connecting unless you call the tool in this turn, and never say a human answered before the runtime reports a connected result.
"###;

#[derive(Debug, Clone)]
pub(crate) enum State {
    Ready,
    Collecting {
        id: u64,
        pcm: Vec<i16>,
        speech_seen: bool,
        silence_samples: usize,
        started: Instant,
    },
    Transcribing {
        id: u64,
    },
    AwaitingConfirmation {
        digits: String,
    },
    Confirmed {
        digits: String,
    },
}

impl Default for State {
    fn default() -> Self {
        Self::Ready
    }
}

impl State {
    pub(crate) fn is_collecting(&self) -> bool {
        matches!(self, Self::Collecting { .. })
    }

    pub(crate) fn is_transcribing(&self) -> bool {
        matches!(self, Self::Transcribing { .. })
    }

    pub(crate) fn can_offer_tool(&self) -> bool {
        matches!(
            self,
            Self::Ready | Self::AwaitingConfirmation { .. } | Self::Confirmed { .. }
        )
    }

    pub(crate) fn pending_digits(&self) -> Option<&str> {
        match self {
            Self::AwaitingConfirmation { digits } | Self::Confirmed { digits } => Some(digits),
            _ => None,
        }
    }

    pub(crate) fn prompt_summary(&self) -> Option<String> {
        match self {
            Self::Ready => None,
            Self::Collecting { .. } => Some("A transaction phone-number audio window is open. Do not ask again or use any short SBPN fragment as a number. Wait for the application result. If the caller asks for a human, call transfer_to_aicc immediately.".into()),
            Self::Transcribing { .. } => Some("The transaction phone-number audio window has closed and the application is transcribing it. Do not ask again, guess digits, or use SBPN fragments. If the caller asks for a human, call transfer_to_aicc immediately.".into()),
            Self::AwaitingConfirmation { .. } => Some("An unconfirmed transaction phone-number candidate is held privately by the application and has been read back in Yoruba. Do not use or repeat a number for a ticket. Only a clear caller confirmation makes it usable. If corrected, replace the candidate and confirm the replacement; do not append digits.".into()),
            Self::Confirmed { digits } => Some(format!("Caller-confirmed transaction phone number (data): {digits}. Keep it for this transaction issue and do not ask again unless the caller explicitly says a different number applies or starts a distinct transaction issue.")),
        }
    }

    pub(crate) fn append_audio(
        &mut self,
        audio: &[i16],
        speaking: bool,
    ) -> Option<(u64, Vec<i16>)> {
        let state = std::mem::replace(self, Self::Ready);
        let Self::Collecting {
            id,
            pcm: mut window,
            mut speech_seen,
            mut silence_samples,
            started,
        } = state
        else {
            *self = state;
            return None;
        };
        if speaking {
            speech_seen = true;
            silence_samples = 0;
            let room = MAX_SAMPLES.saturating_sub(window.len());
            window.extend_from_slice(&audio[..audio.len().min(room)]);
        } else if speech_seen {
            silence_samples = silence_samples.saturating_add(audio.len());
        }
        if window.len() >= MAX_SAMPLES
            || (speech_seen && silence_samples >= POST_SPEECH_SILENCE_SAMPLES)
        {
            *self = Self::Transcribing { id };
            return Some((id, window));
        }
        *self = Self::Collecting {
            id,
            pcm: window,
            speech_seen,
            silence_samples,
            started,
        };
        None
    }
}

pub(crate) fn enabled(config: &super::Config) -> bool {
    config.balogun_number_capture_enabled
        && config.agent_context.loaded
        && config.agent_context.agent_id == BALOGUN_AGENT_ID
        && super::languages::normalize(&config.tts_language) == "Yoruba"
        && !config.stt_chat_url.trim().is_empty()
        && !config.stt_chat_model.trim().is_empty()
}

pub(crate) fn definition() -> Value {
    json!({"type":"function","function":{"name":TOOL,"description":"Start the application's bounded Yoruba transaction-number capture. Use only when a transaction complaint requires the affected phone number. If the caller already volunteered a number in the immediately preceding turn, set volunteered_number=true so the bounded pre-roll can include it. Otherwise set false. The application speaks the request and captures audio; do not ask for the number yourself.","parameters":{"type":"object","properties":{"volunteered_number":{"type":"boolean","description":"True only when the caller already stated the affected number in the immediately preceding turn."}},"required":["volunteered_number"],"additionalProperties":false}}})
}

fn fold_yoruba(input: &str) -> String {
    let folded = input
        .to_lowercase()
        .chars()
        .filter_map(|c| match c {
            'ẹ' => Some('e'),
            'ọ' => Some('o'),
            'ṣ' => Some('s'),
            'à' | 'á' | 'â' | 'ä' => Some('a'),
            'è' | 'é' | 'ê' | 'ë' => Some('e'),
            'ì' | 'í' | 'î' | 'ï' => Some('i'),
            'ò' | 'ó' | 'ô' | 'ö' => Some('o'),
            'ù' | 'ú' | 'û' | 'ü' => Some('u'),
            c if c.is_ascii_alphanumeric() => Some(c),
            c if c.is_whitespace() || c.is_ascii_punctuation() => Some(' '),
            _ => None,
        })
        .collect::<String>()
        .split_whitespace()
        .map(str::to_owned)
        .collect::<Vec<_>>();
    let mut tokens = Vec::with_capacity(folded.len());
    let mut i = 0;
    while i < folded.len() {
        if i + 1 < folded.len()
            && ((folded[i] == "marun" && folded[i + 1] == "un")
                || (folded[i] == "mesan" && folded[i + 1] == "an"))
        {
            tokens.push(folded[i].clone());
            i += 2;
        } else {
            tokens.push(folded[i].clone());
            i += 1;
        }
    }
    tokens.join(" ")
}

pub(crate) fn announces_human_transfer(text: &str) -> bool {
    let folded = fold_yoruba(text);
    [
        "connect you now",
        "connecting you now",
        "transfer you now",
        "put you through to",
        "let me connect you",
        "so yin po bayi",
        "so yin po mo",
        "so yin po pelu asoju",
        "maa so yin po",
        "ma a so yin po",
        "gbe yin lo si",
    ]
    .iter()
    .any(|phrase| folded.contains(phrase))
}

fn digit_word(token: &str) -> Option<char> {
    match token {
        "zero" | "oh" | "odo" => Some('0'),
        "one" | "okan" => Some('1'),
        "two" | "meji" => Some('2'),
        "three" | "meta" => Some('3'),
        "four" | "merin" => Some('4'),
        "five" | "marun" | "marunun" => Some('5'),
        "six" | "mefa" => Some('6'),
        "seven" | "meje" => Some('7'),
        "eight" | "mejo" => Some('8'),
        "nine" | "mesan" | "mesanan" => Some('9'),
        _ => None,
    }
}

fn connector(token: &str) -> bool {
    matches!(
        token,
        "my" | "the" | "phone" | "number" | "is" | "ni" | "nomba" | "foon" | "mi" | "je" | "na"
    )
}

fn ambiguous_asr_number_word(token: &str) -> bool {
    matches!(token, "fine" | "so" | "pain")
}

fn human_transfer_word(token: &str) -> bool {
    matches!(
        token,
        "human" | "person" | "agent" | "representative" | "eniyan" | "asoju"
    )
}

/// Accept a single, complete, explicit digit run and nothing inferred.
pub(crate) fn parse_digits(transcript: &str) -> Option<String> {
    let tokens: Vec<_> = fold_yoruba(transcript)
        .split_whitespace()
        .map(str::to_owned)
        .collect();
    if tokens.is_empty() {
        return None;
    }
    let mut digits = String::new();
    let mut started = false;
    let mut ended = false;
    for token in &tokens {
        if !ended && token.bytes().all(|b| b.is_ascii_digit()) && !token.is_empty() {
            started = true;
            digits.push_str(token);
        } else if !ended && token.len() == 1 && token.as_bytes()[0].is_ascii_digit() {
            started = true;
            digits.push_str(token);
        } else if !ended && digit_word(token).is_some() {
            started = true;
            digits.push(digit_word(token).unwrap());
        } else if connector(token) && (!started || ended) {
            // A short label may precede or follow the one digit run.
        } else if connector(token) && started {
            ended = true;
        } else {
            return None;
        }
    }
    (started && (7..=15).contains(&digits.len())).then_some(digits)
}

/// A fragment may be too short to use but still belongs to the active answer.
pub(crate) fn is_digit_fragment(transcript: &str) -> bool {
    let tokens: Vec<_> = fold_yoruba(transcript)
        .split_whitespace()
        .map(str::to_owned)
        .collect();
    let mut has_digit = false;
    for token in tokens {
        if (!token.is_empty() && token.bytes().all(|b| b.is_ascii_digit()))
            || digit_word(&token).is_some()
        {
            has_digit = true;
        } else if human_transfer_word(&token) {
            return false;
        } else if !connector(&token) && !ambiguous_asr_number_word(&token) {
            return false;
        }
    }
    has_digit
}

/// Detect a replacement number spoken in the same turn as a clear rejection
/// of the previous read-back, so its recent audio can be captured instead of
/// being discarded while asking the caller to repeat it.
pub(crate) fn is_correction_number_fragment(transcript: &str) -> bool {
    let folded = fold_yoruba(transcript);
    ["no ", "nope ", "rara ", "bee ko "]
        .iter()
        .find_map(|prefix| folded.strip_prefix(prefix))
        .is_some_and(is_digit_fragment)
}

pub(crate) fn readback(digits: &str) -> Option<String> {
    if !(7..=15).contains(&digits.len()) || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let words = digits
        .bytes()
        .map(|digit| match digit {
            b'0' => "odo",
            b'1' => "ọ̀kan",
            b'2' => "méjì",
            b'3' => "mẹ́ta",
            b'4' => "mẹ́rin",
            b'5' => "márùn-ún",
            b'6' => "mẹ́fà",
            b'7' => "méje",
            b'8' => "mẹ́jọ",
            b'9' => "mẹ́sàn-án",
            _ => return None,
        })
        .collect::<Vec<_>>();
    Some(format!("Mo gbọ́: {}. Ṣé mo gbọ́ ọ́ dáadáa?", words.join(" ")))
}

pub(crate) fn confirmation(transcript: &str) -> Option<bool> {
    let folded = fold_yoruba(transcript);
    match folded.as_str() {
        "yes" | "yeah" | "correct" | "that is correct" | "thats correct" | "beeni" | "bee ni"
        | "bẹẹni" => Some(true),
        "no" | "nope" | "wrong" | "not correct" | "rara" | "beeko" | "bee ko" | "ki i se" => {
            Some(false)
        }
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_explicit_ascii_and_english_digits_preserving_zeroes() {
        assert_eq!(
            parse_digits("My phone number is 08012345678"),
            Some("08012345678".into())
        );
        assert_eq!(
            parse_digits("zero eight zero one two three four five six seven eight"),
            Some("08012345678".into())
        );
    }

    #[test]
    fn parses_yoruba_digit_words_and_rejects_ambiguous_fillers() {
        assert_eq!(
            parse_digits("Nọ́mbà mi ni odo mẹ́jọ odo ọ̀kan meji meta mẹ́rin márùn-ún mẹ́fà méje mẹ́jọ"),
            Some("08012345678".into())
        );
        assert_eq!(
            parse_digits("zero eight fine one two three four five six seven eight"),
            None
        );
        assert_eq!(
            parse_digits("zero eight so one two three four five six seven eight"),
            None
        );
        assert_eq!(
            parse_digits("zero eight pain one two three four five six seven eight"),
            None
        );
    }

    #[test]
    fn fragments_are_not_promoted_to_complete_numbers() {
        assert!(is_digit_fragment("zero eight zero"));
        assert!(is_digit_fragment("zero eight fine"));
        assert!(parse_digits("zero eight zero").is_none());
        assert!(parse_digits("zero eight fine").is_none());
        assert!(!is_digit_fragment("I need to speak with a human"));
        assert!(!is_digit_fragment(
            "I need a human representative 08012345678"
        ));
        assert!(is_correction_number_fragment("No, 08012345678"));
        assert!(is_correction_number_fragment(
            "Rárá, nọ́mbà mi ni 08012345678"
        ));
        assert!(!is_correction_number_fragment(
            "No, I want a human representative"
        ));
    }

    #[test]
    fn reads_every_digit_in_yoruba_and_confirmation_requires_clear_answer() {
        assert_eq!(readback("08012345678").as_deref(), Some("Mo gbọ́: odo ọ̀kan mẹ́jọ odo ọ̀kan méjì mẹ́ta mẹ́rin márùn-ún mẹ́fà méje mẹ́jọ. Ṣé mo gbọ́ ọ́ dáadáa?"));
        assert_eq!(confirmation("Bẹ́ẹ̀ni"), Some(true));
        assert_eq!(confirmation("Rárá"), Some(false));
        assert_eq!(confirmation("maybe"), None);
    }

    #[test]
    fn candidate_digits_are_withheld_from_llm_context_until_confirmation() {
        let pending = State::AwaitingConfirmation {
            digits: "08012345678".into(),
        };
        let pending_summary = pending.prompt_summary().unwrap();
        assert!(!pending_summary.contains("08012345678"));
        let confirmed = State::Confirmed {
            digits: "08012345678".into(),
        };
        assert!(confirmed.prompt_summary().unwrap().contains("08012345678"));
    }

    #[test]
    fn feature_and_qwen_chat_are_scoped_to_balogun_yoruba() {
        let mut config: super::super::Config =
            serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
        config.tts_language = "Yoruba".into();
        config.stt_model = super::super::languages::SBPN_MODEL.into();
        config.stt_chat_url = "https://asr.invalid/chat".into();
        config.stt_chat_model = "Qwen3-ASR".into();
        config.agent_context.loaded = true;
        config.agent_context.agent_id = BALOGUN_AGENT_ID.into();
        assert!(!enabled(&config), "feature defaults off");
        config.balogun_number_capture_enabled = true;
        assert!(enabled(&config));

        config.agent_context.agent_id = "hausa-agent".into();
        assert!(!enabled(&config), "Hausa remains outside the feature");
        config.agent_context.agent_id = BALOGUN_AGENT_ID.into();
        config.tts_language = "Hausa".into();
        assert!(
            !enabled(&config),
            "Yoruba tool requires Yoruba runtime language"
        );
        config.tts_language = "Yoruba".into();
        config.stt_model = super::super::languages::SBPN_MODEL.into();
        config.stt_chat_url.clear();
        assert!(
            !enabled(&config),
            "tool is unavailable without the Qwen endpoint"
        );
    }

    #[test]
    fn sbpn_remains_ordinary_asr_while_scoped_chat_is_retained_for_number_capture() {
        let mut config: super::super::Config =
            serde_json::from_str(include_str!("../../fixtures/reference-config.json")).unwrap();
        config.tts_language = "Yoruba".into();
        config.stt_model = super::super::languages::SBPN_MODEL.into();
        config.stt_chat_url = "https://asr.invalid/chat".into();
        config.stt_context = "digits".into();
        config.stt_chat_scope = vec![BALOGUN_AGENT_ID.into()];
        config.balogun_number_capture_enabled = true;
        config.agent_context.loaded = true;
        config.agent_context.agent_id = BALOGUN_AGENT_ID.into();
        super::super::profiles::apply_stt_scope(&mut config);
        assert_eq!(config.stt_model, super::super::languages::SBPN_MODEL);
        assert_eq!(config.stt_chat_url, "https://asr.invalid/chat");
        assert_eq!(config.stt_context, "digits");

        config.agent_context.agent_id = "another-agent".into();
        super::super::profiles::apply_stt_scope(&mut config);
        assert!(config.stt_chat_url.is_empty());
        assert!(config.stt_context.is_empty());
    }

    #[test]
    fn bounded_window_joins_short_turns_and_closes_after_full_second_silence() {
        let mut state = State::Collecting {
            id: 9,
            pcm: Vec::new(),
            speech_seen: false,
            silence_samples: 0,
            started: Instant::now(),
        };
        assert!(state.append_audio(&vec![100; 4_000], true).is_none());
        assert!(state.append_audio(&vec![0; 3_200], false).is_none());
        assert!(state.append_audio(&vec![200; 4_000], true).is_none());
        assert!(state.append_audio(&vec![0; 15_999], false).is_none());
        let (id, window) = state.append_audio(&vec![0; 1], false).unwrap();
        assert_eq!(id, 9);
        assert_eq!(window.len(), 8_000);
        assert!(matches!(state, State::Transcribing { id: 9 }));
    }

    #[test]
    fn bounded_window_hard_caps_at_fifteen_seconds_including_preroll() {
        let mut state = State::Collecting {
            id: 10,
            pcm: vec![100; PREROLL_SAMPLES],
            speech_seen: true,
            silence_samples: 0,
            started: Instant::now(),
        };
        let (id, window) = state.append_audio(&vec![200; MAX_SAMPLES], true).unwrap();
        assert_eq!(id, 10);
        assert_eq!(window.len(), MAX_SAMPLES);
    }

    #[test]
    fn yoruba_transfer_intent_rules_include_positive_and_refusal_examples() {
        for phrase in [
            "Ẹ so mí pọ̀ mọ́ aṣojú oníbàárà",
            "Mo fẹ́ bá ènìyàn sọ̀rọ̀",
            "Ẹ jọ̀wọ́, gbe mi lọ sí ẹ̀ka ìtọ́jú oníbàárà",
            "Ẹ fi mí sọ̀rọ̀ pẹ̀lú aṣojú gidi",
            "Má so mí pọ̀ mọ́ ẹnikẹ́ni",
        ] {
            assert!(YORUBA_TRANSFER_RULES.contains(phrase));
        }
        assert!(YORUBA_TRANSFER_RULES.contains("call transfer_to_aicc"));
    }

    #[test]
    fn transfer_announcements_are_detectable_for_the_runtime_guard() {
        assert!(announces_human_transfer(
            "Ó dáa, màá so yín pọ̀ mọ́ aṣojú báyìí."
        ));
        assert!(announces_human_transfer("I am connecting you now."));
        assert!(!announces_human_transfer(
            "I can answer your question here."
        ));
    }
}
