"""Derive scoped metrics from raw caller, worker and process samples; no missing zeros."""

import json, statistics, sys
from pathlib import Path

root = Path(sys.argv[1])
results = []


def first(rows, name, predicate=lambda e: True):
    return next((e for e in rows if e["event"] == name and predicate(e)), None)


def delta(a, b, unit):
    return None if a is None or b is None else (b["time"] - a["time"]) / unit


for folder in sorted(root.iterdir()):
    if not folder.is_dir() or not (folder / "summary.json").exists():
        continue
    summary = json.loads((folder / "summary.json").read_text())
    caller = [json.loads(x) for x in (folder / "caller.jsonl").read_text().splitlines()]
    trace = next(folder.glob("AJ*.jsonl"))
    rows = [json.loads(x) for x in trace.read_text().splitlines()]
    native = summary["kind"] == "rust"
    for e in rows:
        e["time"] = e["at_us"] if native else e["mono_ns"]
        e["attempt"] = e.get("attempt") if native else e.get("attempt_id")

    def normal(e):
        return (
            e.get("turn", 0) > 0
            if native
            else bool(e.get("turn_id") and not e["turn_id"].startswith("opening"))
        )

    eot = first(rows, "user_speech_end")
    final = first(rows, "stt_final" if native else "stt_chat_final")
    enqueue = first(
        rows,
        "audio_first_enqueued" if native else "livekit_audio_frame_enqueued",
        normal,
    )
    attempt = enqueue["attempt"] if enqueue else None
    chosen = lambda e: e["attempt"] == attempt
    token = first(rows, "llm_first_token", chosen)
    request = None
    if token:
        request = next(
            (
                e
                for e in reversed(rows[: rows.index(token)])
                if e["event"]
                == ("llm_request_start" if native else "llm_provider_request_start")
                and chosen(e)
            ),
            None,
        )
    tts = first(rows, "tts_first_pcm" if native else "tts_first_audio_chunk", chosen)
    response = [e for e in caller if e["phase"] == "response"]
    last = first(response, "input_last_voiced_block")
    onset = first(response, "receiver_active")
    receiver_ms = (
        None
        if not last or not onset
        else (onset["mono_ns"] - last["mono_ns"] - 100_000_000) / 1e6
    )
    summary.update(
        {
            "folder": folder.name,
            "asr_final_after_eot_ms": delta(eot, final, 1000 if native else 1e6),
            "llm_ttft_ms": delta(request, token, 1000 if native else 1e6),
            "final_to_adapter_enqueue_ms": delta(final, enqueue, 1000 if native else 1e6),
            "eot_to_adapter_enqueue_ms": delta(eot, enqueue, 1000 if native else 1e6),
            "winning_llm_head_start_before_final_ms": delta(request, final, 1000 if native else 1e6),
            "final_discarded_speculation": any(
                e["event"] in ("attempt_cancel", "speculation_cancel")
                and (e.get("data", {}).get("reason") or e.get("reason")) == "final_transcript_changed"
                for e in rows
            ),
            "first_pcm_to_adapter_enqueue_ms": delta(
                tts, enqueue, 1000 if native else 1e6
            ),
            "receiver_onset_after_sender_last_voiced_block_ms": receiver_ms,
            "speculative_attempts": sum(
                e["event"] == "speculation_start" for e in rows
            ),
            "interruption_cancellations": sum(
                e["event"] in (("attempt_cancel",) if native else ("speculation_cancel", "generation_cancel"))
                and (
                    e.get("data", {}).get("reason") == "user_barge_in"
                    or e.get("reason") == "user_barge_in"
                )
                for e in rows
            ),
            "peak_rss_mib": summary["peak_rss_bytes"] / 1048576,
            "idle_rss_mib": summary["idle"]["rss_bytes"] / 1048576,
            "idle_cpu_percent_one_core": summary["idle"]["cpu_s"]
            / summary["idle"]["wall_s"]
            * 100,
            "active_cpu_percent_one_core": summary["active"]["cpu_s"]
            / summary["active"]["wall_s"]
            * 100,
            "provider_underflows": None,
            "audible_stop_ms": None,
        }
    )
    results.append(summary)
metrics = [
    "receiver_onset_after_sender_last_voiced_block_ms",
    "asr_final_after_eot_ms",
    "final_to_adapter_enqueue_ms",
    "eot_to_adapter_enqueue_ms",
    "winning_llm_head_start_before_final_ms",
    "llm_ttft_ms",
    "first_pcm_to_adapter_enqueue_ms",
    "peak_rss_mib",
    "idle_rss_mib",
    "active_cpu_percent_one_core",
    "idle_cpu_percent_one_core",
]
aggregate = {}
for kind in ["rust", "python"]:
    group = [r for r in results if r["kind"] == kind and r["outcome"] == "completed"]
    aggregate[kind] = {"n": len(group)}
    for metric in metrics:
        values = [r[metric] for r in group if r[metric] is not None]
        aggregate[kind][metric] = (
            {
                "median": statistics.median(values),
                "min": min(values),
                "max": max(values),
            }
            if values
            else None
        )
body = {
    "calls": results,
    "aggregate": aggregate,
    "limits": [
        "Small serial sample; inspect n per group. No p95 or statistical significance claim.",
        "Receiver PCM threshold80 RMS, not a physical loudspeaker or human audible onset.",
        "Sender last voiced100ms block end is an estimate; includes capture/source pacing.",
        "Both use synchronous JSONL instrumentation, with different trace schemas and event counts.",
        "Aggregate RSS sums parent and active descendants, double-counting shared pages; not unique physical RAM.",
        "Python local adapter uses official worker process isolation and zero idle job processes, matching one-call test capacity; original production defaults use an idle pool.",
        "LLM content, ASR partials, live NPU workloads and WAN timing vary. This is not isolated model inference or a language-only CPU benchmark.",
        "No underflow counter or physical audible-stop measurement is exposed at the tested boundary; null means unmeasured.",
    ],
}
(Path(sys.argv[2]) if len(sys.argv) > 2 else root / "metrics.json").write_text(json.dumps(body, indent=2) + "\n")
print(json.dumps(aggregate, indent=2))
