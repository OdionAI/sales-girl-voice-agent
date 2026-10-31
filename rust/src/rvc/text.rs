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
pub fn clean_transcript_for_language(raw: &str, language: &str) -> String {
    if !super::languages::french(language) {
        return clean_transcript(raw);
    }
    static FRENCH: std::sync::OnceLock<Option<Regex>> = std::sync::OnceLock::new();
    if let Some(pattern) = FRENCH.get_or_init(|| {
        Regex::new(r"(?i)(\blanguage\s+)(french|français|francais|fra|fre|fr)\b").ok()
    }) {
        clean_transcript(&pattern.replace_all(raw, "${1}English"))
    } else {
        clean_transcript(raw)
    }
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
    ((((words + extra) as f64 / 2.5 + 0.5) * 12.5) as usize + 8).clamp(24, 360)
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
