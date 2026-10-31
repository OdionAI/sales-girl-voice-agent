//! Post-call diagnostics only. Never run report rendering on the audio actor.
use super::{Trace, TraceEvent};
use std::{collections::BTreeMap, fmt::Write as _, path::PathBuf};

/// Covers startup errors as well as ordinary disconnect. Blocking work remains
/// owned by Tokio through runtime shutdown, outside the voice/cancellation loop.
pub struct OnClose {
    trace: Trace,
    path: PathBuf,
}
impl OnClose {
    pub fn new(trace: Trace, path: PathBuf) -> Self {
        Self { trace, path }
    }
}
impl Drop for OnClose {
    fn drop(&mut self) {
        let trace = self.trace.clone();
        let path = self.path.clone();
        let write = move || {
            let result = (|| {
                let events = trace.events();
                if let Some(parent) = path.parent() {
                    std::fs::create_dir_all(parent)?;
                }
                std::fs::write(&path, render(&events))
            })();
            if result.is_err() {
                trace.emit("local_report_failed", 0, 0, serde_json::json!({}));
            }
        };
        if let Ok(runtime) = tokio::runtime::Handle::try_current() {
            runtime.spawn_blocking(write);
        } else {
            write();
        }
    }
}

const MILESTONES: &[(&str, &str)] = &[
    ("speech_candidate", "First voiced frame"),
    ("user_speech_start", "Speech start"),
    (
        "user_audio_end_candidate",
        "Last voiced audio / silence begins",
    ),
    ("user_speech_end", "Speech end / EOU"),
    ("stt_final", "Authoritative caller final"),
    ("speculation_start", "Speculation start"),
    ("knowledge_lookup_executed", "Authorized knowledge request"),
    ("knowledge_lookup_complete", "Knowledge retrieval complete"),
    ("llm_first_token", "LLM first token"),
    ("llm_text_released", "Stable speech text released"),
    ("tts_request_start", "TTS request"),
    ("tts_first_pcm", "First TTS audio"),
    ("speculation_authorized", "Held generation authorized"),
    ("audio_first_enqueued", "First LiveKit audio enqueue"),
    ("browser_playback_confirmed", "Browser reported playback"),
    ("playout_complete", "LiveKit playout queue complete"),
];
fn ms(start: &TraceEvent, end: &TraceEvent) -> Option<f64> {
    end.at_us
        .checked_sub(start.at_us)
        .map(|v| v as f64 / 1000.0)
}
fn cell(value: &str) -> String {
    value
        .chars()
        .filter(|c| !c.is_control())
        .take(100)
        .collect::<String>()
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('|', "\\|")
        .replace('`', "&#96;")
}
fn headline(out: &mut String, label: &str, start: Option<&TraceEvent>, end: Option<&TraceEvent>) {
    match start.zip(end).and_then(|(a, b)| ms(a, b)) {
        Some(value) => {
            let _ = writeln!(out, "- {label}: **{value:.1} ms**");
        }
        None => {
            let _ = writeln!(out, "- {label}: unavailable");
        }
    }
}
pub fn render(events: &[TraceEvent]) -> String {
    let mut out = String::from("# Native RVC call report\n\nTimes use the worker's monotonic clock. LiveKit enqueue and queue completion are software boundaries; physical audibility and speaker-stop latency are unavailable.\n\nThe chart includes speculative work. Headline generation timings select the attempt that actually enqueued audio; cancelled attempts cannot become its latency. Raw text, prompts, reasoning, tools and device proofs are omitted.\n");
    if events.is_empty() {
        out.push_str("\nNo events were captured. Latency and state validation are unavailable.\n");
        return out;
    }
    if events.len() >= 100_000 {
        out.push_str("\nThe bounded trace reached its event cap. Later boundaries may be missing; this report is partial.\n");
    }
    let mut turns: BTreeMap<u64, Vec<&TraceEvent>> = BTreeMap::new();
    for event in events {
        turns.entry(event.turn).or_default().push(event);
    }
    let mut largest: Option<(f64, &str, u64)> = None;
    let (mut duplicate_finals, mut duplicate_playouts) = (0, 0);
    for (turn, mut rows) in turns {
        rows.sort_by_key(|e| e.at_us);
        let first = |name: &str| rows.iter().copied().find(|e| e.event == name);
        let final_ = first("stt_final");
        let enqueue = first("audio_first_enqueued");
        let winning = |name: &str| {
            enqueue.and_then(|played| {
                rows.iter()
                    .copied()
                    .find(|e| e.event == name && e.attempt == played.attempt)
            })
        };
        let token = winning("llm_first_token");
        let request = token.and_then(|token| {
            rows.iter().copied().rev().find(|e| {
                e.event == "llm_request_start"
                    && e.attempt == token.attempt
                    && e.at_us <= token.at_us
                    && e.data.get("request_id") == token.data.get("request_id")
            })
        });
        let _ = writeln!(out, "\n## Turn {turn}\n\n### Observed latency\n");
        headline(
            &mut out,
            "Speech end → authoritative final",
            first("user_speech_end"),
            final_,
        );
        headline(&mut out, "Final → first LiveKit enqueue", final_, enqueue);
        headline(
            &mut out,
            "Speech end → first LiveKit enqueue",
            first("user_speech_end"),
            enqueue,
        );
        headline(&mut out, "Winning LLM request → first text", request, token);
        headline(
            &mut out,
            "First TTS PCM → first LiveKit enqueue",
            winning("tts_first_pcm"),
            enqueue,
        );
        let cancels = rows.iter().filter(|e| e.event == "attempt_cancel").count();
        let restarts = rows
            .iter()
            .filter(|e| e.event == "speculation_start")
            .count();
        let _ = writeln!(
            out,
            "- Speculative attempts: {restarts}; cancelled attempts: {cancels}"
        );
        // Only validated caller turns with actual playback have a critical path.
        // Provider work before the final is overlap, not an added serial delay.
        if let Some((final_, enqueue)) = final_.zip(enqueue).filter(|(a, b)| b.at_us >= a.at_us) {
            let mut chain = vec![];
            for (name, label) in [
                ("user_audio_end_candidate", "Silence begins"),
                ("user_speech_end", "Speech end / EOU"),
            ] {
                if let Some(event) = first(name).filter(|e| e.at_us <= final_.at_us) {
                    chain.push((label, event));
                }
            }
            chain.push(("Authoritative final", final_));
            for (label, event) in [
                ("LLM first text", token),
                ("First TTS PCM", winning("tts_first_pcm")),
            ] {
                if let Some(event) =
                    event.filter(|e| e.at_us >= final_.at_us && e.at_us <= enqueue.at_us)
                {
                    chain.push((label, event));
                }
            }
            chain.push(("LiveKit enqueue", enqueue));
            chain.sort_by_key(|(_, e)| e.at_us);
            for pair in chain.windows(2) {
                if let Some(gap) = ms(pair[0].1, pair[1].1) {
                    if largest.is_none_or(|(prior, _, _)| gap > prior) {
                        largest = Some((gap, pair[1].0, turn));
                    }
                }
            }
        }
        duplicate_finals += rows
            .iter()
            .filter(|e| e.event == "stt_final")
            .count()
            .saturating_sub(1);
        let mut playouts = BTreeMap::<u64, usize>::new();
        for event in rows.iter().filter(|e| e.event == "playout_complete") {
            *playouts.entry(event.attempt).or_default() += 1;
        }
        duplicate_playouts += playouts
            .values()
            .map(|n| n.saturating_sub(1))
            .sum::<usize>();
        out.push_str("\n### Waterfall\n\n| Offset | Milestone | Attempt |\n|---:|---|---:|\n");
        let origin = rows[0].at_us;
        let mut seen = std::collections::BTreeSet::new();
        for event in &rows {
            if let Some((_, label)) = MILESTONES.iter().find(|(name, _)| *name == event.event) {
                if seen.insert((event.event.as_str(), event.attempt)) {
                    let _ = writeln!(
                        out,
                        "| {:.3} ms | {label} | {} |",
                        (event.at_us - origin) as f64 / 1000.0,
                        event.attempt
                    );
                }
            }
        }
        out.push_str(
            "\n### Event chart\n\n| Offset | Event | Attempt | State |\n|---:|---|---:|---|\n",
        );
        for event in &rows {
            let from = cell(event.data["from"].as_str().unwrap_or(""));
            let to = cell(event.data["to"].as_str().unwrap_or(""));
            let _ = writeln!(
                out,
                "| {:.3} ms | {} | {} | {from} → {to} |",
                (event.at_us - origin) as f64 / 1000.0,
                cell(&event.event),
                event.attempt
            );
        }
    }
    let violations = events
        .iter()
        .filter(|e| e.event == "invariant_violation")
        .count();
    let _ = writeln!(out,"\n## State observations\n\n- Duplicate caller finals: {duplicate_finals}\n- Duplicate playout completions for one turn/attempt: {duplicate_playouts}\n- Explicitly emitted invariant violations: {violations}\n\nSequential acknowledgement/result stages may complete separately within one caller turn. These observations do not prove unobserved states correct.\n\n## Bottleneck\n");
    if let Some((gap, label, turn)) = largest {
        let _ = writeln!(out,"Largest observed gap on a validated playback path: **{gap:.1} ms**, ending at **{label}** in turn {turn}. Earlier provider work may overlap the caller; this observation does not measure remote queueing separately from network or inference.");
    } else {
        out.push_str("No validated caller/playback chain was captured. Inspect missing milestones and cancellation/error events before attributing delay to a provider.\n");
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn event(name: &str, at_us: u64, attempt: u64) -> TraceEvent {
        TraceEvent {
            event: name.into(),
            at_us,
            turn: 1,
            attempt,
            data: json!({}),
        }
    }
    #[test]
    fn winning_generation_missing_boundaries_and_private_data() {
        let mut rows = vec![
            event("user_speech_end", 0, 0),
            event("llm_first_token", 10, 1),
            event("stt_final", 300_000, 0),
            event("llm_request_start", 301_000, 2),
            event("llm_first_token", 1_000_000, 2),
            event("tts_first_pcm", 1_600_000, 2),
            event("audio_first_enqueued", 1_601_000, 2),
            event("playout_complete", 2_000_000, 2),
            event("playout_complete", 2_000_001, 2),
        ];
        rows[2].data = json!({"text":"secret caller", "proof":"secret proof", "reasoning_content":"private reasoning"});
        let report = render(&rows);
        assert!(report.contains("Final → first LiveKit enqueue: **1301.0 ms**"));
        assert!(report.contains("Winning LLM request → first text: **699.0 ms**"));
        assert!(report.contains("**700.0 ms**, ending at **LLM first text**"));
        assert!(report.contains("Duplicate playout completions for one turn/attempt: 1"));
        assert!(!report.contains("secret") && !report.contains("private reasoning"));
        let report = render(&[event("llm_first_token", 1, 1)]);
        assert!(report.contains("Final → first LiveKit enqueue: unavailable"));
        assert!(report.contains("No validated caller/playback chain"));
        assert!(render(&[]).contains("validation are unavailable"));
        let report = render(&[
            event("user_speech_end", 0, 0),
            event("stt_final", 5_000_000, 0),
            event("audio_first_enqueued", 5_001_000, 1),
        ]);
        assert!(report.contains("**5000.0 ms**, ending at **Authoritative final**"));
    }
    #[test]
    fn held_audio_is_overlap_and_unvalidated_audio_is_not_a_critical_path() {
        let rows = vec![
            event("llm_request_start", 0, 1),
            event("llm_first_token", 100_000, 1),
            event("tts_first_pcm", 200_000, 1),
            event("stt_final", 900_000, 0),
            event("audio_first_enqueued", 901_000, 1),
        ];
        let report = render(&rows);
        assert!(report.contains("**1.0 ms**, ending at **LiveKit enqueue**"));
        assert!(report.contains("Winning LLM request → first text: **100.0 ms**"));
        let report = render(&[
            event("audio_first_enqueued", 10, 1),
            event("stt_final", 20, 0),
        ]);
        assert!(report.contains("No validated caller/playback chain"));
        assert!(report.contains("Final → first LiveKit enqueue: unavailable"));
    }
    #[tokio::test]
    async fn startup_failure_still_writes_native_report() {
        let path =
            std::env::temp_dir().join(format!("native-call-report-{}.md", uuid::Uuid::new_v4()));
        let trace = Trace::default();
        let report = OnClose::new(trace.clone(), path.clone());
        trace.emit("startup_failed", 0, 0, json!({"proof":"must stay private"}));
        drop(report);
        tokio::time::timeout(std::time::Duration::from_secs(2), async {
            while !path.exists() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        // File creation can precede the completed write: await the blocking pool.
        let text = loop {
            let text = std::fs::read_to_string(&path).unwrap();
            if text.contains("startup_failed") {
                break text;
            }
            tokio::task::yield_now().await;
        };
        assert!(!text.contains("must stay private"));
        std::fs::remove_file(path).unwrap();
    }
}
