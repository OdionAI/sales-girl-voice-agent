//! Independent caller-audio windows for background Qwen. SBPN owns ordinary
//! turns. Reading this cache never starts or flushes an ASR request.
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, VecDeque},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio::sync::watch;
pub(crate) const READ_TOOL: &str = "get_secondary_number_transcript";
const RATE: usize = 16000;
const MAX: usize = 30 * RATE;
const SILENCE: usize = RATE;
#[derive(Clone, Debug)]
pub(crate) struct Meta {
    pub id: u64,
    pub first_turn: u64,
    pub last_turn: u64,
    pub samples: usize,
    pub utterance_ids: Vec<u64>,
}
pub(crate) struct Audio {
    pub meta: Meta,
    pub pcm: Vec<i16>,
}
struct Active {
    meta: Meta,
    pcm: Vec<i16>,
    silence: usize,
    last_speech: Instant,
}
#[derive(Default)]
pub(crate) struct Recorder {
    active: Option<Active>,
    preroll: VecDeque<i16>,
    next: u64,
    preroll_speech: bool,
}
impl Recorder {
    pub fn append(
        &mut self,
        pcm: &[i16],
        speech: bool,
        confirmed_turn: Option<u64>,
    ) -> (Option<Meta>, Option<Audio>) {
        if self.active.is_none() {
            self.preroll_speech |= speech;
            self.preroll.extend(pcm.iter().copied());
            while self.preroll.len() > RATE {
                self.preroll.pop_front();
            }
            if self.preroll_speech {
                if let Some(turn) = confirmed_turn {
                    self.next += 1;
                    let data: Vec<_> = self.preroll.drain(..).collect();
                    self.preroll_speech = false;
                    let meta = Meta {
                        id: self.next,
                        first_turn: turn,
                        last_turn: turn,
                        samples: data.len(),
                        utterance_ids: vec![turn],
                    };
                    self.active = Some(Active {
                        meta: meta.clone(),
                        pcm: data,
                        silence: 0,
                        last_speech: Instant::now(),
                    });
                    return (Some(meta), None);
                }
            }
            return (None, None);
        }
        let a = self.active.as_mut().unwrap();
        a.pcm.extend_from_slice(pcm);
        a.meta.samples = a.pcm.len();
        let changed = confirmed_turn.is_some_and(|t| t > a.meta.last_turn);
        if let Some(turn) = confirmed_turn {
            a.meta.last_turn = a.meta.last_turn.max(turn);
            if !a.meta.utterance_ids.contains(&turn) {
                a.meta.utterance_ids.push(turn);
            }
        }
        if speech {
            a.silence = 0;
            a.last_speech = Instant::now();
        } else {
            a.silence += pcm.len();
        }
        let meta = changed.then(|| a.meta.clone());
        if a.pcm.len() >= MAX {
            let extra = a.pcm.split_off(MAX);
            a.meta.samples = MAX;
            self.preroll = extra.into();
            self.preroll_speech = speech && !self.preroll.is_empty();
            return (meta, self.finish());
        }
        if a.silence >= SILENCE {
            return (meta, self.finish());
        }
        (meta, None)
    }
    fn finish(&mut self) -> Option<Audio> {
        let a = self.active.take()?;
        Some(Audio {
            meta: a.meta,
            pcm: a.pcm,
        })
    }
    pub fn due(&mut self) -> Option<Audio> {
        if self
            .active
            .as_ref()
            .is_some_and(|a| a.last_speech.elapsed() >= Duration::from_secs(1))
        {
            self.finish()
        } else {
            None
        }
    }
}
#[derive(Clone)]
pub(crate) struct Cache {
    inner: Arc<Mutex<BTreeMap<u64, Entry>>>,
    changed: watch::Sender<u64>,
}
struct Entry {
    meta: Meta,
    result: Option<Result<String, String>>,
}
impl Default for Cache {
    fn default() -> Self {
        let (tx, _) = watch::channel(0);
        Self {
            inner: Arc::new(Mutex::new(BTreeMap::new())),
            changed: tx,
        }
    }
}
impl Cache {
    fn notify(&self) {
        self.changed.send_modify(|v| *v = v.wrapping_add(1));
    }
    pub fn recording(&self, meta: Meta) {
        let mut s = self.inner.lock().unwrap();
        s.entry(meta.id)
            .and_modify(|e| e.meta = meta.clone())
            .or_insert(Entry { meta, result: None });
        while s.len() > 8 {
            s.pop_first();
        }
        drop(s);
        self.notify();
    }
    pub fn complete(&self, id: u64, result: Result<String, String>) {
        if let Some(e) = self.inner.lock().unwrap().get_mut(&id) {
            e.result = Some(result);
        }
        self.notify();
    }
    pub fn owns(&self, turn: u64) -> bool {
        self.inner
            .lock()
            .unwrap()
            .values()
            .any(|e| e.meta.utterance_ids.contains(&turn))
    }
    pub fn pending(&self, turn: u64) -> bool {
        let s = self.inner.lock().unwrap();
        s.values()
            .any(|e| e.meta.utterance_ids.contains(&turn) && e.result.is_none())
    }
    pub fn reference(&self) -> Value {
        let s = self.inner.lock().unwrap();
        json!(s.values().rev().take(2).map(|e|json!({"window_id":e.meta.id,"first_turn":e.meta.first_turn,"last_turn":e.meta.last_turn,"utterance_ids":e.meta.utterance_ids,"status":match &e.result{None=>"recording_or_transcribing",Some(Ok(_))=>"complete",Some(Err(_))=>"unavailable"},"transcript":e.result.as_ref().and_then(|r|r.as_ref().ok()).filter(|t|number_bearing(t)),"digit_runs":e.result.as_ref().and_then(|r|r.as_ref().ok()).map(|t|digit_runs(t))})).collect::<Vec<_>>())
    }
    fn ready(&self, turn: u64) -> Option<Value> {
        let s = self.inner.lock().unwrap();
        let matches: Vec<_> = s
            .values()
            .filter(|e| e.meta.utterance_ids.contains(&turn))
            .collect();
        if matches.is_empty() {
            return Some(json!({"status":"unavailable","reason":"no_owned_audio","source":"Qwen"}));
        }
        if matches.iter().any(|e| e.result.is_none()) {
            return None;
        }
        let ok = matches.iter().all(|e| matches!(e.result, Some(Ok(_))));
        Some(
            json!({"status":if ok{"ok"}else{"unavailable"},"source":"Qwen","windows":matches.iter().map(|e|json!({"window_id":e.meta.id,"first_turn":e.meta.first_turn,"last_turn":e.meta.last_turn,"transcript":e.result.as_ref().and_then(|r|r.as_ref().ok()),"digit_runs":e.result.as_ref().and_then(|r|r.as_ref().ok()).map(|t|digit_runs(t))})).collect::<Vec<_>>()}),
        )
    }
    pub async fn read(&self, turn: u64) -> Value {
        let mut rx = self.changed.subscribe();
        tokio::time::timeout(Duration::from_secs(46), async {
            loop {
                if let Some(v) = self.ready(turn) {
                    return v;
                }
                if rx.changed().await.is_err() {
                    return json!({"status":"unavailable","source":"Qwen"});
                }
            }
        })
        .await
        .unwrap_or_else(|_| json!({"status":"unavailable","reason":"timeout","source":"Qwen"}))
    }
}
pub(crate) fn number_bearing(text: &str) -> bool {
    let folded = super::number_capture::fold_yoruba(text);
    let mut words = 0;
    let mut other = 0;
    for word in folded.split_whitespace() {
        if word.bytes().all(|b| b.is_ascii_digit()) {
            return true;
        }
        if matches!(
            word,
            "zero"
                | "oh"
                | "one"
                | "two"
                | "three"
                | "four"
                | "five"
                | "six"
                | "seven"
                | "eight"
                | "nine"
                | "odo"
                | "okan"
                | "meji"
                | "meta"
                | "merin"
                | "marun"
                | "mefa"
                | "meje"
                | "mejo"
                | "mesan"
        ) {
            words += 1;
        } else if !matches!(
            word,
            "no" | "rara"
                | "not"
                | "o"
                | "and"
                | "is"
                | "the"
                | "number"
                | "my"
                | "phone"
                | "nomba"
                | "nọmba"
        ) {
            other += 1;
        }
    }
    words >= 2 || (words > 0 && other == 0)
}

pub(crate) fn definition() -> Value {
    json!({"type":"function","function":{"name":READ_TOOL,"description":"Read the already-running background Qwen transcription for this caller's utterance, including adjacent short SBPN turns in the same longer audio window. Use when the caller has supplied or corrected a transaction phone number that is needed for their issue. This read does not start ASR or recording and does not ask the caller for a number. If still recording, the application waits for the longer window to finish. Use the returned Qwen text for the digits, preserve leading zeros and confirm the complete candidate in Yoruba before using it. Never guess a number from SBPN or from missing/failed secondary data.","parameters":{"type":"object","properties":{},"additionalProperties":false}}})
}

pub(crate) const PROPOSE_TOOL: &str = "propose_transaction_phone_number";
pub(crate) fn proposal_definition() -> Value {
    json!({"type":"function","function":{"name":PROPOSE_TOOL,"description":"Select the complete transaction phone-number candidate from a completed Qwen digit_run (or literal typed caller digits). This does not confirm the number: the application reads every digit in Yoruba and asks the caller to confirm. Preserve leading zeros exactly. Never read a phone candidate back yourself, guess missing digits, normalize it to a different prefix, or append correction digits. For corrections select the complete replacement number.","parameters":{"type":"object","properties":{"digits":{"type":"string","pattern":"^[0-9]{7,15}$"}},"required":["digits"],"additionalProperties":false}}})
}
pub(crate) fn digit_runs(text: &str) -> Vec<String> {
    let folded = super::number_capture::fold_yoruba(text);
    let tokens: Vec<_> = folded.split_whitespace().collect();
    fn digit(w: &str) -> Option<char> {
        match w {
            "zero" | "oh" | "odo" => Some('0'),
            "one" | "okan" => Some('1'),
            "two" | "meji" => Some('2'),
            "three" | "meta" => Some('3'),
            "four" | "merin" => Some('4'),
            "five" | "marun" => Some('5'),
            "six" | "mefa" => Some('6'),
            "seven" | "meje" => Some('7'),
            "eight" | "mejo" => Some('8'),
            "nine" | "mesan" => Some('9'),
            _ => None,
        }
    }
    let mut runs = vec![];
    let mut current = String::new();
    let mut multiplier = 1;
    let flush = |current: &mut String, runs: &mut Vec<String>| {
        if (7..=15).contains(&current.len()) {
            runs.push(current.clone());
        }
        current.clear();
    };
    for (i, word) in tokens.iter().enumerate() {
        if matches!(*word, "double" | "triple") {
            multiplier = if *word == "double" { 2 } else { 3 };
            continue;
        }
        let o = *word == "o"
            && ([i.checked_sub(1), Some(i + 1)]
                .into_iter()
                .flatten()
                .filter_map(|n| tokens.get(n))
                .any(|w| {
                    digit(w).is_some() || (w.len() == 1 && w.bytes().all(|b| b.is_ascii_digit()))
                }));
        if let Some(d) = digit(word).or_else(|| o.then_some('0')) {
            for _ in 0..multiplier {
                current.push(d);
            }
            multiplier = 1;
        } else if word.bytes().all(|b| b.is_ascii_digit()) {
            current.push_str(word);
            multiplier = 1;
        } else if matches!(*word, "and" | "dash" | "hyphen" | "un" | "an") && !current.is_empty() {
        } else {
            flush(&mut current, &mut runs);
            multiplier = 1;
        }
    }
    flush(&mut current, &mut runs);
    runs
}
impl Cache {
    pub fn observed_digits(&self, turn: u64) -> Vec<String> {
        let s = self.inner.lock().unwrap();
        let texts: Vec<_> = s
            .values()
            .filter(|e| e.meta.utterance_ids.contains(&turn))
            .filter_map(|e| e.result.as_ref()?.as_ref().ok())
            .collect();
        let mut runs: Vec<_> = texts.iter().flat_map(|t| digit_runs(t)).collect();
        runs.extend(digit_runs(
            &texts
                .into_iter()
                .map(String::as_str)
                .collect::<Vec<_>>()
                .join(" "),
        ));
        runs.sort();
        runs.dedup();
        runs
    }
}

#[cfg(test)]
mod digit_grounding_tests {
    use super::*;
    #[test]
    fn leading_zeroes_are_grounded_and_different_candidates_are_not_equivalent() {
        assert_eq!(
            digit_runs("My phone is zero zero one two three four five six seven eight nine."),
            vec!["00123456789"]
        );
        assert!(
            !digit_runs("zero zero one two three four five six seven eight nine")
                .contains(&"110123456789".to_string())
        );
        assert_eq!(
            digit_runs("O eight O three one two three four five six seven"),
            vec!["08031234567"]
        );
        assert!(digit_runs("o nikan ni mo fe").is_empty());
    }
    #[test]
    fn ordinary_questions_do_not_schedule_number_waits_but_single_digit_fragments_do() {
        assert!(!number_bearing("Báwo ni mo ṣe lè ṣí account?"));
        assert!(number_bearing("zero"));
        assert!(number_bearing("nine"));
        assert!(!number_bearing("I have one question"));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn short_sbpn_turns_share_one_longer_window_and_reads_do_not_flush_it() {
        let mut r = Recorder::default();
        let c = Cache::default();
        let (mut starts, mut done) = (0, 0);
        for (turn, speech, n) in [
            (1, true, 8000),
            (1, false, 3200),
            (2, true, 8000),
            (2, false, 15999),
        ] {
            let (m, a) = r.append(&vec![if speech { 1000 } else { 0 }; n], speech, Some(turn));
            if let Some(m) = m {
                starts += usize::from(m.id == 1 && starts == 0);
                c.recording(m);
            }
            assert!(a.is_none());
        }
        assert!(c.owns(1) && c.owns(2));
        let waiter = tokio::spawn({
            let c = c.clone();
            async move { c.read(2).await }
        });
        tokio::task::yield_now().await;
        assert!(!waiter.is_finished());
        let (_, a) = r.append(&[0], false, Some(2));
        let a = a.unwrap();
        done += 1;
        assert_eq!(a.pcm.len(), 35200);
        assert_eq!(a.meta.first_turn, 1);
        assert_eq!(a.meta.last_turn, 2);
        c.recording(a.meta.clone());
        c.complete(a.meta.id, Ok("zero zero one two".into()));
        let result = waiter.await.unwrap();
        assert_eq!(result["windows"][0]["transcript"], "zero zero one two");
        assert_eq!((starts, done), (1, 1));
    }
    #[tokio::test]
    async fn out_of_order_windows_and_failures_remain_scoped() {
        let c = Cache::default();
        for (id, turn) in [(1, 1), (2, 3)] {
            c.recording(Meta {
                id,
                first_turn: turn,
                last_turn: turn,
                samples: 32000,
                utterance_ids: vec![turn],
            });
        }
        c.complete(2, Ok("replacement digits".into()));
        assert_eq!(
            c.read(3).await["windows"][0]["transcript"],
            "replacement digits"
        );
        assert!(c.ready(1).is_none());
        c.complete(1, Err("failed".into()));
        assert_eq!(c.read(1).await["status"], "unavailable");
    }
    #[test]
    fn hard_limit_bounds_continuous_audio() {
        let mut r = Recorder::default();
        r.append(&[1; 320], true, Some(1));
        let (_, a) = r.append(&vec![1; MAX - 320], true, Some(1));
        assert_eq!(a.unwrap().pcm.len(), MAX);
    }
}
