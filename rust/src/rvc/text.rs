use regex::Regex;
use std::{collections::HashMap, sync::LazyLock};
static PHRASE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"(?s)^(.+?[.!?](?:["']|\s|$))"#).unwrap());
static TAG: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?i)^<(/?)(think|thinking|reasoning|tool_call|function|parameter)(?:[=\s/][^<>]*)?>",
    )
    .unwrap()
});
static LANGUAGE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)language\s+([a-z]+)").unwrap());
static ASR: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)<asr_text>").unwrap());
// Internal pipeline annotations (e.g. the interrupted-reply note the memory
// layer appends for the model) must never be spoken. Strip them from any text
// on its way to TTS as a final safety net, regardless of model echo or path.
static INTERNAL_ANNOTATION: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?is)\s*\[\s*response interrupted[^\]]*\]").unwrap());
pub fn strip_internal_annotations(text: &str) -> String {
    // Leave every string without the marker byte-for-byte unchanged (no trim),
    // so normal TTS input is identical to before; only annotated text changes.
    if !INTERNAL_ANNOTATION.is_match(text) {
        return text.to_string();
    }
    INTERNAL_ANNOTATION.replace_all(text, "").trim().to_string()
}
#[derive(Default)]
pub struct PhraseTokenizer {
    buffer: String,
}
impl PhraseTokenizer {
    pub fn push(&mut self, delta: &str) -> Vec<String> {
        self.buffer.push_str(delta);
        let mut out = Vec::new();
        loop {
            if let Some(m) = PHRASE.find(&self.buffer) {
                let text = m.as_str().trim().to_string();
                self.buffer = self.buffer[m.end()..].into();
                if !text.is_empty() {
                    out.push(text);
                }
                continue;
            }
            let words: Vec<_> = self.buffer.split_whitespace().collect();
            if words.len() >= 24 {
                let phrase = words[..24].join(" ");
                let chars = phrase.chars().count();
                let offset = self
                    .buffer
                    .char_indices()
                    .nth(chars)
                    .map_or(self.buffer.len(), |(i, _)| i);
                self.buffer = self.buffer[offset..].into();
                out.push(phrase);
                continue;
            }
            break;
        }
        out
    }
    pub fn finish(&mut self) -> Vec<String> {
        let text = std::mem::take(&mut self.buffer).trim().to_string();
        if text.is_empty() {
            vec![]
        } else {
            vec![text]
        }
    }
}
#[derive(Default)]
pub struct SpokenFilter {
    buffer: String,
    hidden: Vec<String>,
}
impl SpokenFilter {
    pub fn push(&mut self, text: &str) -> String {
        self.buffer.push_str(text);
        let mut out = String::new();
        while !self.buffer.is_empty() {
            let Some(start) = self.buffer.find('<') else {
                if self.hidden.is_empty() {
                    out.push_str(&self.buffer);
                }
                self.buffer.clear();
                break;
            };
            if start > 0 {
                if self.hidden.is_empty() {
                    out.push_str(&self.buffer[..start]);
                }
                self.buffer = self.buffer[start..].into();
            }
            if let Some(m) = TAG.captures(&self.buffer) {
                let end = m.get(0).unwrap().end();
                let name = m[2].to_ascii_lowercase();
                if &m[1] == "/" {
                    if let Some(i) = self.hidden.iter().rposition(|n| n == &name) {
                        self.hidden.truncate(i);
                    }
                } else if !m.get(0).unwrap().as_str().ends_with("/>") {
                    self.hidden.push(name);
                }
                self.buffer = self.buffer[end..].into();
            } else {
                let text = self.buffer.to_ascii_lowercase();
                let possible = [
                    "think",
                    "thinking",
                    "reasoning",
                    "tool_call",
                    "function",
                    "parameter",
                ]
                .iter()
                .any(|n| {
                    [format!("<{n}"), format!("</{n}")].iter().any(|p| {
                        p.starts_with(&text)
                            || (text.starts_with(p)
                                && text[p.len()..]
                                    .starts_with([' ', '=', '/', '>', '\t', '\r', '\n']))
                    })
                });
                if possible {
                    break;
                }
                if self.hidden.is_empty() {
                    out.push('<');
                }
                self.buffer.remove(0);
            }
        }
        out
    }
}
/// Text as it has to be spoken. A dash is read aloud as "minus", so it becomes
/// a comma between words; inside a number it is dropped and the digits are
/// glued back together.
pub fn speakable(raw: &str) -> String {
    fn is_dash(c: char) -> bool {
        matches!(
            c,
            '-' | '\u{2010}' | '\u{2011}' | '\u{2012}' | '\u{2013}' | '\u{2014}' | '\u{2015}'
        )
    }
    let chars: Vec<char> = raw.chars().collect();
    let mut out: Vec<char> = Vec::with_capacity(chars.len());
    let mut i = 0;
    while i < chars.len() {
        if !is_dash(chars[i]) {
            out.push(chars[i]);
            i += 1;
            continue;
        }
        // "0813-153-5013" is one number, not three.
        let prev_digit = i > 0 && chars[i - 1].is_ascii_digit();
        let next_digit = chars.get(i + 1).is_some_and(|c| c.is_ascii_digit());
        if prev_digit && next_digit {
            i += 1;
            continue;
        }
        while out
            .last()
            .is_some_and(|c| c.is_whitespace() || is_dash(*c) || *c == ',')
        {
            out.pop();
        }
        out.extend([',', ' ']);
        i += 1;
        while i < chars.len() && (chars[i].is_whitespace() || is_dash(chars[i])) {
            i += 1;
        }
    }
    let text: String = out.into_iter().collect();
    let mut cleaned = String::with_capacity(text.len());
    let mut last_space = false;
    for ch in text.chars() {
        if ch == ' ' {
            if !last_space {
                cleaned.push(' ');
            }
            last_space = true;
        } else {
            cleaned.push(ch);
            last_space = false;
        }
    }
    let cleaned = cleaned.trim().trim_matches(',').trim().to_string();
    spell_digits(&cleaned)
}

pub fn normalize(s: &str) -> String {
    s.to_lowercase()
        .chars()
        .filter(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == ' ')
        .collect::<String>()
        .trim()
        .into()
}
/// Python difflib.SequenceMatcher's non-junk matching-block ratio, including
/// its default popular-element heuristic. Inputs are the ASCII normalization.
pub fn similarity(a: &str, b: &str) -> f64 {
    let (a, b) = (a.as_bytes(), b.as_bytes());
    if a.is_empty() && b.is_empty() {
        return 1.0;
    }
    let mut positions: HashMap<u8, Vec<usize>> = HashMap::new();
    for (i, c) in b.iter().enumerate() {
        positions.entry(*c).or_default().push(i);
    }
    if b.len() >= 200 {
        let limit = b.len() / 100 + 1;
        positions.retain(|_, v| v.len() <= limit);
    }
    let mut regions = vec![(0, a.len(), 0, b.len())];
    let mut matches = 0;
    while let Some((alo, ahi, blo, bhi)) = regions.pop() {
        let (mut ai, mut bi, mut size) = (alo, blo, 0);
        let mut prev: HashMap<usize, usize> = HashMap::new();
        for (i, c) in a.iter().enumerate().take(ahi).skip(alo) {
            let mut next = HashMap::new();
            if let Some(js) = positions.get(c) {
                for j in js.iter().copied().filter(|j| *j >= blo && *j < bhi) {
                    let k = if j == 0 {
                        1
                    } else {
                        prev.get(&(j - 1)).copied().unwrap_or(0) + 1
                    };
                    next.insert(j, k);
                    if k > size {
                        ai = i + 1 - k;
                        bi = j + 1 - k;
                        size = k;
                    }
                }
            }
            prev = next;
        }
        while ai > alo && bi > blo && a[ai - 1] == b[bi - 1] {
            ai -= 1;
            bi -= 1;
            size += 1;
        }
        while ai + size < ahi && bi + size < bhi && a[ai + size] == b[bi + size] {
            size += 1;
        }
        if size > 0 {
            matches += size;
            if alo < ai && blo < bi {
                regions.push((alo, ai, blo, bi));
            }
            if ai + size < ahi && bi + size < bhi {
                regions.push((ai + size, ahi, bi + size, bhi));
            }
        }
    }
    2.0 * matches as f64 / (a.len() + b.len()) as f64
}
pub fn valid_final(hypothesis: &str, final_text: &str) -> bool {
    let (a, b) = (normalize(hypothesis), normalize(final_text));
    a == b || similarity(&a, &b) >= 0.92
}
/// Scripts no supported agent uses. A transcript that comes back in one of them
/// is a recogniser hallucination (short or noisy audio makes Qwen3-ASR pick a
/// language of its own), not something the caller said.
fn foreign_script(text: &str) -> bool {
    text.chars().any(|c| {
        let u = c as u32;
        (0x0370..=0x052F).contains(&u)        // Greek, Cyrillic
            || (0x0590..=0x08FF).contains(&u) // Hebrew, Arabic, Syriac, Thaana
            || (0x0900..=0x0DFF).contains(&u) // Indic scripts
            || (0x0E00..=0x0E7F).contains(&u) // Thai
            || (0x1100..=0x11FF).contains(&u) // Hangul jamo
            || (0x2E80..=0x9FFF).contains(&u) // CJK
            || (0xA960..=0xA97F).contains(&u)
            || (0xAC00..=0xD7FF).contains(&u) // Hangul
            || (0xF900..=0xFAFF).contains(&u)
    })
}

/// Function words that are common in the languages the recogniser drifts into
/// on unclear English audio, and rare in English.
const DRIFT_WORDS: &[&str] = &[
    "dat", "het", "een", "niet", "dit", "van", "voor", "maar", "ook", "nog", "wel", "geen",
    "hij", "zij", "wij", "jullie", "ja", "nee", "eens", "noemt", "vamos", "isso", "né", "não",
    "não", "voce", "você", "uma", "com", "por", "pero", "esta", "está", "muy", "auch", "nicht",
    "und", "ist", "ich", "für", "auf", "mit", "aber", "der", "die", "das", "oui", "pas", "avec",
    "pour", "mais", "vous", "nous", "les", "des", "une", "aux", "bonjour", "merci", "danke",
];

/// Unambiguous English function words - enough to tell a real sentence from a
/// drifted one, without flagging names, digits or short answers.
const ENGLISH_WORDS: &[&str] = &[
    "the", "is", "it", "and", "to", "of", "that", "for", "you", "my", "with", "have", "what",
    "please", "sorry", "thanks", "correct", "number", "account", "airtime", "transfer", "balance",
    "how", "much", "can", "would", "like", "want", "not", "this", "from", "on", "in", "at",
];

fn foreign_latin(text: &str) -> bool {
    let mut drift = 0;
    let mut english = 0;
    for word in text.split(|c: char| !c.is_alphanumeric() && c != '\'') {
        let word = word.to_ascii_lowercase();
        if word.is_empty() {
            continue;
        }
        if DRIFT_WORDS.contains(&word.as_str()) {
            drift += 1;
        } else if ENGLISH_WORDS.contains(&word.as_str()) {
            english += 1;
        }
    }
    // Two or more foreign function words with at most one English word to
    // balance them: this is not English speech, so it must not become the
    // caller's turn. ("… hij eens correct" carries a single English-looking
    // "correct", which is not enough to make the sentence English.)
    drift >= 2 && english <= 1
}

pub fn english_target(language: &str) -> bool {
    let language = language.trim().to_ascii_lowercase();
    language.is_empty() || language == "en" || language.starts_with("en-") || language == "english"
}

pub fn clean_transcript_for_language(raw: &str, language: &str) -> String {
    // A French agent's decoder may announce its own language; rewrite that
    // header first so the English-only header rule does not throw the sentence
    // away, then clean.
    if super::languages::french(language) {
        static FRENCH: std::sync::OnceLock<Option<Regex>> = std::sync::OnceLock::new();
        let prepared = match FRENCH.get_or_init(|| {
            Regex::new(r"(?i)(\blanguage\s+)(french|français|francais|fra|fre|fr)\b").ok()
        }) {
            Some(pattern) => pattern.replace_all(raw, "${1}English").to_string(),
            None => raw.to_string(),
        };
        let text = clean_transcript(&prepared);
        return if foreign_script(&text) { String::new() } else { text };
    }
    let text = clean_transcript(raw);
    // The recogniser picks the language itself - the endpoint ignores `language`
    // (proved: the same clip returns identical text for en, fr and zh). So keep
    // the answer only when it is actually English.
    if foreign_script(&text) || (english_target(language) && foreign_latin(&text)) {
        return String::new();
    }
    text
}

pub fn clean_transcript(raw: &str) -> String {
    let raw = raw.trim();
    let langs: Vec<_> = LANGUAGE
        .captures_iter(raw)
        .map(|c| c[1].to_ascii_lowercase())
        .filter(|s| !matches!(s.as_str(), "none" | "null" | "undefined"))
        .collect();
    if !langs.is_empty() && !langs.iter().any(|s| s == "english") {
        return String::new();
    }
    let text = if ASR.is_match(raw) {
        ASR.split(raw)
            .skip(1)
            .map(|s| {
                let end = LANGUAGE.find(s).map_or(s.len(), |m| m.start());
                s[..end].trim()
            })
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join(" ")
    } else {
        let prefix = Regex::new(r"(?i)^language\s+[a-z]+\s*").unwrap();
        prefix.replace(raw, "").trim().into()
    };
    if text.chars().any(|c| {
        ('\u{3040}'..='\u{30ff}').contains(&c)
            || ('\u{3400}'..='\u{9fff}').contains(&c)
            || ('\u{f900}'..='\u{faff}').contains(&c)
    }) {
        String::new()
    } else {
        text
    }
}
pub fn tts_budget(text: &str) -> usize {
    let words = text.split_whitespace().count().max(1);
    let digits = Regex::new(r"[0-9]+").unwrap();
    let extra: usize = digits.find_iter(text).map(|m| m.len() - 1).sum();
    ((((words + extra) as f64 / 2.5 + 0.5) * 12.5) as usize + 8).clamp(24, 1500)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn matches_pinned_python_text_and_matching_contracts() {
        let v: serde_json::Value =
            serde_json::from_str(include_str!("../../fixtures/text-vectors.json")).unwrap();
        for p in v["ratios"].as_array().unwrap() {
            assert!(
                (similarity(p["a"].as_str().unwrap(), p["b"].as_str().unwrap())
                    - p["expected"].as_f64().unwrap())
                .abs()
                    < 1e-12,
                "{p}"
            );
        }
        for p in v["filters"].as_array().unwrap() {
            let mut f = SpokenFilter::default();
            let out = p["pieces"]
                .as_array()
                .unwrap()
                .iter()
                .map(|s| f.push(s.as_str().unwrap()))
                .collect::<String>();
            assert_eq!(out, p["expected"].as_str().unwrap(), "{p}");
        }
        for p in v["phrases"].as_array().unwrap() {
            let mut t = PhraseTokenizer::default();
            let mut out = Vec::new();
            for c in p["text"].as_str().unwrap().chars() {
                out.extend(t.push(&c.to_string()));
            }
            out.extend(t.finish());
            assert_eq!(serde_json::json!(out), p["expected"], "{p}");
        }
        for p in v["tts"].as_array().unwrap() {
            assert_eq!(
                tts_budget(p["text"].as_str().unwrap()),
                p["budget"].as_u64().unwrap() as usize
            );
        }
        for p in v["deltas"].as_array().unwrap() {
            let mut d = DeltaAccumulator::default();
            let out: Vec<_> = p["parts"]
                .as_array()
                .unwrap()
                .iter()
                .filter_map(|raw| d.push(raw.as_str().unwrap()))
                .collect();
            assert_eq!(serde_json::json!(out), p["expected"], "{p}");
        }
        for p in v["cleanup"].as_array().unwrap() {
            assert_eq!(
                clean_transcript(p["text"].as_str().unwrap()),
                p["expected"].as_str().unwrap()
            );
        }
    }
}

/// Incremental Qwen tokens, with explicit cumulative-hypothesis boundaries.
/// A marker can contain no spoken text; it still replaces the previous hypothesis.
#[derive(Default)]
pub struct DeltaAccumulator {
    pub text: String,
    preamble: bool,
    drop_hypothesis: bool,
    french: bool,
}
impl DeltaAccumulator {
    pub fn for_language(language: &str) -> Self {
        Self {
            french: super::languages::french(language),
            ..Self::default()
        }
    }
    fn accepts(&self, language: &str) -> bool {
        matches!(language, "english" | "none")
            || self.french
                && matches!(
                    language,
                    "french" | "français" | "francais" | "fr" | "fra" | "fre"
                )
    }
    pub fn push(&mut self, raw: &str) -> Option<String> {
        let word = raw.trim().to_lowercase();
        if word == "language" {
            self.text.clear();
            self.preamble = true;
            self.drop_hypothesis = false;
            return None;
        }
        const LANGS: &[&str] = &[
            "english",
            "french",
            "français",
            "francais",
            "fr",
            "fra",
            "fre",
            "chinese",
            "german",
            "spanish",
            "arabic",
            "japanese",
            "korean",
            "russian",
            "portuguese",
            "italian",
            "hindi",
            "indonesian",
            "vietnamese",
            "turkish",
            "dutch",
            "polish",
            "thai",
            "swedish",
            "norwegian",
            "danish",
            "finnish",
            "hebrew",
            "ukrainian",
            "pidgin",
            "none",
        ];
        if self.preamble && LANGS.contains(&word.as_str()) {
            self.drop_hypothesis = !self.accepts(&word);
            self.preamble = false;
            return None;
        }
        // The forced-language server emits this marker as its own delta before
        // each cumulative decode. Reset before filtering empty spoken content.
        // Never infer a boundary from repeated words or sentence punctuation.
        let trimmed = raw.trim_start();
        if let Some(language) = LANGUAGE
            .captures(trimmed)
            .filter(|c| c.get(0).is_some_and(|m| m.start() == 0))
        {
            self.text.clear();
            self.preamble = false;
            self.drop_hypothesis = !self.accepts(&language[1].to_ascii_lowercase());
        } else if trimmed.to_ascii_lowercase().starts_with("<asr_text>") {
            self.text.clear();
            self.preamble = false;
        }
        if self.drop_hypothesis {
            return None;
        }
        let clean =
            clean_transcript_for_language(raw, if self.french { "French" } else { "English" });
        if clean.is_empty() {
            return None;
        }
        if !self.text.is_empty()
            && clean.chars().next().is_some_and(char::is_alphanumeric)
            && (raw.chars().next().is_some_and(char::is_whitespace)
                || self
                    .text
                    .chars()
                    .last()
                    .is_some_and(|c| c.is_alphanumeric() || ",;:".contains(c)))
        {
            self.text.push(' ');
        }
        self.text.push_str(&clean);
        Some(self.text.trim().into())
    }
    pub fn finish(&mut self, raw: Option<&str>) -> String {
        let result = clean_transcript_for_language(
            raw.filter(|s| !s.is_empty()).unwrap_or(&self.text),
            if self.french { "French" } else { "English" },
        );
        *self = Self {
            french: self.french,
            ..Self::default()
        };
        result
    }
}

/// Suppress a copied private vocabulary hint while retaining ordinary mentions
/// of a bank/network name. Exact policy from current Python providers.py.
pub fn is_asr_context_echo(transcript: &str, context: &str) -> bool {
    if context.is_empty() || transcript.is_empty() {
        return false;
    }
    static WORD: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[a-z0-9]+").unwrap());
    let transcript = transcript.to_lowercase();
    let context = context.to_lowercase();
    let words: Vec<_> = WORD.find_iter(&transcript).map(|m| m.as_str()).collect();
    let hint: Vec<_> = WORD.find_iter(&context).map(|m| m.as_str()).collect();
    if words.len() < 4 || hint.is_empty() {
        return false;
    }
    if words.len() <= 5 && hint.starts_with(&words) {
        return true;
    }
    let mut previous = vec![0usize; hint.len() + 1];
    let mut longest = 0;
    for word in &words {
        let mut current = vec![0usize; hint.len() + 1];
        for (index, hint_word) in hint.iter().enumerate() {
            if word == hint_word {
                current[index + 1] = previous[index] + 1;
                longest = longest.max(current[index + 1]);
            }
        }
        previous = current;
    }
    words.len() >= 8 && longest as f64 / words.len() as f64 >= 0.8
}

/// Recognize the Python worker's first-person promise to consult attached
/// knowledge. This routes read-only retrieval, never caller confirmation.
pub fn requests_knowledge_lookup(answer: &str) -> bool {
    static PATTERNS: LazyLock<Vec<Regex>> = LazyLock::new(|| {
        let verb = r"(?:check|confirm|verify|look\s+(?:that|this|it)\s+up|look\s+up|search|consult|find\s+out)";
        [format!(r"\b(?:let\s+me|allow\s+me\s+to)\s+(?:just\s+|quickly\s+)?{verb}\b"),
        format!(r"\bi\s+(?:(?:will|ll|need\s+to|must|am\s+going\s+to|want\s+to|would\s+like\s+to)(?:\s+just|\s+quickly)?\s+){verb}\b"),
        format!(r"\bi\s+(?:would|d)\s+be\s+(?:happy|glad)\s+to\s+{verb}\b"),
        format!(r"\bwhile\s+i\s+(?:just\s+|quickly\s+)?{verb}\b"),
        format!(r"\b(?:may|can|could)\s+i\s+(?:just\s+|quickly\s+)?{verb}\b"),
        format!(r"\b(?:may|can|could)\s+i\b.{{0,40}}\b(?:moment|second|minute)\b.{{0,50}}\b{verb}\b")]
        .into_iter().map(|p|Regex::new(&p).expect("static knowledge policy pattern")).collect()
    });
    static CLEAN: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"[^a-z0-9' ]+").expect("static ASCII policy pattern"));
    static WILL: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\bi'll\b").expect("static contraction"));
    static WOULD: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\bi'd\b").expect("static contraction"));
    static AM: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\bi'm\b").expect("static contraction"));
    let text = answer.to_lowercase().replace('’', "'");
    let text = WILL.replace_all(&text, "i will");
    let text = WOULD.replace_all(&text, "i would");
    let text = AM.replace_all(&text, "i am");
    let text = CLEAN.replace_all(&text, " ").replace('\'', "");
    let text = text.split_whitespace().collect::<Vec<_>>().join(" ");
    text == "let me check that for you" || PATTERNS.iter().any(|p| p.is_match(&text))
}

#[cfg(test)]
mod french_decoder_tests {
    use super::*;
    #[test]
    fn french_cleanup_and_incremental_headers_are_scoped_and_survive_resets() {
        let raw = "language French<asr_text>Bonjour, comment allez-vous ?";
        assert_eq!(
            clean_transcript_for_language(raw, "French"),
            "Bonjour, comment allez-vous ?"
        );
        assert_eq!(clean_transcript_for_language(raw, "English"), "");
        let mut decoder = DeltaAccumulator::for_language("French");
        assert!(decoder.push("language").is_none());
        assert!(decoder.push("French").is_none());
        assert!(decoder.push("<asr_text>").is_none());
        assert_eq!(decoder.push("Bonjour").as_deref(), Some("Bonjour"));
        assert_eq!(decoder.finish(Some(raw)), "Bonjour, comment allez-vous ?");
        assert_eq!(
            decoder.push("language French<asr_text>Salut").as_deref(),
            Some("Salut")
        );
        assert!(decoder.push("language Chinese<asr_text>您好").is_none());
    }
}

#[cfg(test)]
mod internal_annotation_tests {
    use super::*;
    #[test]
    fn interrupted_note_is_removed_before_speech() {
        let raw = "Your balance is 100 naira.\n[Response interrupted; the caller may not have heard it in full.]";
        assert_eq!(strip_internal_annotations(raw), "Your balance is 100 naira.");
        let inline =
            "Okay. [Response interrupted; the caller may not have heard it in full.] Let me check.";
        assert_eq!(strip_internal_annotations(inline), "Okay. Let me check.");
        assert_eq!(strip_internal_annotations("Hello there."), "Hello there.");
        // A message that is only the note yields nothing to speak.
        assert_eq!(
            strip_internal_annotations("[Response interrupted; the caller may not have heard it in full.]"),
            ""
        );
    }
}

/// Every digit spelled, however short the number. Used for identifiers the
/// caller must hear one digit at a time (account endings, phone numbers).
pub fn spell_each(digits: &str) -> String {
    digits
        .chars()
        .filter(|c| c.is_ascii_digit())
        .map(|d| match d {
            '0' => "zero",
            '1' => "one",
            '2' => "two",
            '3' => "three",
            '4' => "four",
            '5' => "five",
            '6' => "six",
            '7' => "seven",
            '8' => "eight",
            '9' => "nine",
            _ => unreachable!("filtered to ASCII digits"),
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// Long numbers as spoken digits. "08112812216" is read as one huge number by
/// the speech engine; "zero eight one one two eight one two two one six" is what
/// a person says, and what the caller can follow and correct.
pub fn spell_digits(raw: &str) -> String {
    fn word(d: char) -> &'static str {
        match d {
            '0' => "zero",
            '1' => "one",
            '2' => "two",
            '3' => "three",
            '4' => "four",
            '5' => "five",
            '6' => "six",
            '7' => "seven",
            '8' => "eight",
            '9' => "nine",
            _ => unreachable!("only ASCII digits are passed"),
        }
    }
    let chars: Vec<char> = raw.chars().collect();
    let mut out = String::with_capacity(raw.len() * 2);
    let mut i = 0;
    while i < chars.len() {
        if !chars[i].is_ascii_digit() {
            out.push(chars[i]);
            i += 1;
            continue;
        }
        let mut j = i;
        while j < chars.len() && chars[j].is_ascii_digit() {
            j += 1;
        }
        // 7+ digits is a phone or account number, never an amount in these calls.
        if j - i >= 7 {
            let words: Vec<&str> = chars[i..j].iter().map(|d| word(*d)).collect();
            out.push_str(&words.join(" "));
        } else {
            out.extend(&chars[i..j]);
        }
        i = j;
    }
    out
}

/// Phone and account numbers as the caller actually said them. Spoken digit
/// words are glued back into runs so the model can copy figures instead of
/// transcribing them itself (which is where digits get transposed).
pub fn digit_runs(raw: &str) -> Vec<String> {
    fn digit(word: &str) -> Option<char> {
        match word {
            "zero" | "oh" | "odo" | "o" => Some('0'),
            "one" | "won" | "okan" => Some('1'),
            "two" | "tu" | "meji" => Some('2'),
            "three" | "tree" | "meta" => Some('3'),
            "four" | "for" | "merin" => Some('4'),
            "five" | "fife" | "marun" => Some('5'),
            "six" | "sik" | "mefa" => Some('6'),
            "seven" | "meje" => Some('7'),
            "eight" | "ate" | "mejo" => Some('8'),
            "nine" | "niner" | "mesan" => Some('9'),
            _ => None,
        }
    }
    let mut runs = Vec::new();
    let mut current = String::new();
    let mut repeat = 1usize;
    let flush = |current: &mut String, runs: &mut Vec<String>| {
        if (7..=15).contains(&current.len()) {
            runs.push(std::mem::take(current));
        }
        current.clear();
    };
    for word in raw.split(|c: char| !c.is_alphanumeric()) {
        let word = word.to_ascii_lowercase();
        if word.is_empty() {
            continue;
        }
        if word == "double" || word == "triple" {
            repeat = if word == "double" { 2 } else { 3 };
            continue;
        }
        if let Some(d) = digit(&word) {
            for _ in 0..repeat {
                current.push(d);
            }
            repeat = 1;
        } else if word.bytes().all(|b| b.is_ascii_digit()) {
            current.push_str(&word);
            repeat = 1;
        } else {
            repeat = 1;
            flush(&mut current, &mut runs);
        }
    }
    flush(&mut current, &mut runs);
    runs
}

#[cfg(test)]
mod spoken_tests {
    use super::*;

    #[test]
    fn a_dash_is_never_read_as_minus() {
        assert_eq!(
            speakable("Using your account ending 9185 - 2000 naira of MTN airtime to 08131535013."),
            "Using your account ending 9185, 2000 naira of MTN airtime to zero eight one three one five three five zero one three."
        );
        assert_eq!(speakable("Got it — the number ends 013"), "Got it, the number ends 013");
        assert_eq!(
            speakable("0813-153-5013"),
            "zero eight one three one five three five zero one three"
        );
        assert_eq!(speakable("no dashes here"), "no dashes here");
    }

    #[test]
    fn a_transcript_in_another_language_is_not_the_callers_turn() {
        // All four were produced by the live recogniser on 2026-11-17.
        assert_eq!(clean_transcript_for_language("Да, действительно.", "English"), "");
        assert_eq!(
            clean_transcript_for_language("ja, dat noemt hij eens correct.", "English"),
            ""
        );
        assert_eq!(clean_transcript_for_language("vamos ver isso né", "English"), "");
        assert_eq!(clean_transcript_for_language("はい、そうです。", "English"), "");
        // Real English survives, including digits and short answers.
        for kept in [
            "Yes, that number is correct.",
            "zero eight one three one five three five zero one three",
            "I said it's not supposed to end in three zero one.",
            "I would like to transfer airtime.",
            "No.",
            "MTN.",
        ] {
            assert_eq!(clean_transcript_for_language(kept, "English"), kept, "{kept}");
        }
    }

    #[test]
    fn numbers_are_spoken_as_digits_not_figures() {
        // The whole point: the caller hears and can correct each digit.
        assert_eq!(spell_each("9185"), "nine one eight five");
        assert_eq!(
            spell_digits("transfer to 08112812216 now"),
            "transfer to zero eight one one two eight one two two one six now"
        );
        // Amounts and short figures are left alone.
        assert_eq!(spell_digits("2000 naira"), "2000 naira");
        assert_eq!(spell_digits("ending 9185"), "ending 9185");
    }

    #[test]
    fn spoken_digits_become_one_number() {
        assert_eq!(
            digit_runs("the number is zero eight one three one five three five zero one three"),
            vec!["08131535013".to_string()]
        );
        assert_eq!(digit_runs("double two nine"), Vec::<String>::new());
        assert_eq!(
            digit_runs("zero eight one one two double three five"),
            vec!["08112335".to_string()]
        );
        assert_eq!(
            digit_runs("send two thousand naira to 08131535013"),
            vec!["08131535013".to_string()]
        );
    }
}
