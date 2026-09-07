from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


MILESTONES = [
    ("user_speech_candidate_start", "First voiced frame"),
    ("user_speech_start", "Speech start"),
    ("user_speech_audio_end_candidate", "Last voiced audio / silence begins"),
    ("user_speech_end", "Speech end / EOU"),
    ("stt_final", "STT final"),
    ("speculation_start", "Speculation start"),
    ("knowledge_lookup_requested", "LLM requested knowledge"),
    ("knowledge_retrieval_start", "Knowledge retrieval start"),
    ("knowledge_retrieval_complete", "Knowledge retrieval complete"),
    ("llm_first_token", "LLM first token"),
    ("tokenizer_phrase_ready", "First stable phrase"),
    ("tts_request_start", "TTS request"),
    ("tts_first_audio_chunk", "First TTS audio"),
    ("playback_release_authorized", "Playback release"),
    ("browser_playback_start", "Browser playback"),
    ("livekit_playback_start", "LiveKit audio enqueue"),
    ("browser_playback_confirmed", "Browser audible playback"),
    ("browser_playback_stop", "Playback complete"),
    ("livekit_playback_stop", "LiveKit playout queue complete"),
]


def load_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _ms(ns: int, origin: int) -> float:
    return round((ns - origin) / 1_000_000, 3)


def generate_report(trace_path: Path, report_dir: Path) -> Path:
    events = load_events(trace_path)
    if not events:
        raise ValueError(f"Trace is empty: {trace_path}")
    report_dir.mkdir(parents=True, exist_ok=True)
    session_id = str(events[0]["session_id"])
    session_open = next(
        (event for event in events if event.get("event") == "session_open"),
        events[0],
    )
    transport = str((session_open.get("data") or {}).get("transport") or "browser")
    agent_config = next(
        (event for event in events if event.get("event") == "agent_config_load_complete"),
        None,
    )
    output = report_dir / f"{session_id}.md"
    turns: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if event.get("turn_id"):
            turns[str(event["turn_id"])].append(event)

    lines = [
        f"# RVC latency-lab trace: `{session_id}`",
        "",
        f"- Source trace: `{trace_path}`",
        "- Pipeline: transport PCM → RSTT → speculative RLLM → phrase tokenizer → held RTTS → authorized transport playback.",
        f"- Transport: **{transport}**.",
        "- Scope: room-scoped agent configuration and explicit post-final attached-knowledge retrieval are enabled.",
        "- Scope exclusions: no AgentSession orchestration or action-tool execution.",
        "",
    ]
    if agent_config is not None:
        data = agent_config.get("data") or {}
        lines.extend(
            [
                "## Agent configuration",
                "",
                f"- Agent: **{data.get('configured_name') or data.get('agent_id') or 'unknown'}**.",
                f"- Agent ID: `{data.get('agent_id') or ''}`.",
                f"- Business ID: `{data.get('business_id') or ''}`.",
                f"- Attached knowledge bases: **{int(data.get('knowledge_base_count') or 0)}**.",
                f"- Configuration load: **{float(data.get('elapsed_ms') or 0):,.1f} ms** before the conversation path.",
                "",
            ]
        )
    all_gaps: list[tuple[float, str, str]] = []
    for turn_id, turn_events in turns.items():
        origin = turn_events[0]["mono_ns"]
        first: dict[str, dict[str, Any]] = {}
        for event in turn_events:
            first.setdefault(event["event"], event)
        lines.extend([f"## {turn_id}", "", "### Waterfall", "", "| Offset | Milestone | Δ from prior | Reason |", "|---:|---|---:|---|"])
        prior_ns: int | None = None
        prior_label = "turn origin"
        observed = sorted(
            ((first[event_name], label) for event_name, label in MILESTONES if event_name in first),
            key=lambda item: item[0]["mono_ns"],
        )
        for event, label in observed:
            current_ns = int(event["mono_ns"])
            delta = 0.0 if prior_ns is None else _ms(current_ns, prior_ns)
            lines.append(f"| {_ms(current_ns, origin):,.3f} ms | {label} | {delta:,.3f} ms | {event.get('reason') or ''} |")
            if (
                prior_ns is not None
                and delta >= 0
                and label not in {"Playback complete", "LiveKit playout queue complete"}
            ):
                all_gaps.append((delta, f"{prior_label} → {label}", turn_id))
            prior_ns = current_ns
            prior_label = label
        final_event = first.get("stt_final")
        speech_end = first.get("user_speech_end")
        browser_playback = first.get("browser_playback_confirmed")
        livekit_enqueue = first.get("livekit_playback_start")
        playback = (
            browser_playback
            or first.get("browser_playback_start")
            or livekit_enqueue
        )
        speech_start = first.get("user_speech_start")
        lines.extend(["", "### Headline latency", ""])
        if speech_end and browser_playback:
            lines.append(f"- Speech end → browser audible playback: **{_ms(browser_playback['mono_ns'], speech_end['mono_ns']):,.1f} ms**")
        elif speech_end and playback:
            label = "LiveKit enqueue" if livekit_enqueue else "audible playback"
            lines.append(f"- Speech end → {label}: **{_ms(playback['mono_ns'], speech_end['mono_ns']):,.1f} ms**")
        if speech_start and playback:
            lines.append(f"- Speech start → measured playback boundary: **{_ms(playback['mono_ns'], speech_start['mono_ns']):,.1f} ms**")
        if livekit_enqueue and browser_playback:
            lines.append(f"- LiveKit enqueue → browser audible playback: **{_ms(browser_playback['mono_ns'], livekit_enqueue['mono_ns']):,.1f} ms**")
        if speech_end and final_event:
            lines.append(f"- Speech end → STT final: **{_ms(final_event['mono_ns'], speech_end['mono_ns']):,.1f} ms**")
        knowledge_start = first.get("knowledge_retrieval_start")
        knowledge_complete = first.get("knowledge_retrieval_complete")
        if knowledge_start and knowledge_complete:
            lines.append(
                f"- Authorized knowledge retrieval: **{_ms(knowledge_complete['mono_ns'], knowledge_start['mono_ns']):,.1f} ms**"
            )
        cancels = [e for e in turn_events if e["event"] in {"speculation_cancel", "generation_cancel"}]
        restarts = [e for e in turn_events if e["event"] == "speculation_restart_scheduled"]
        violations = [e for e in turn_events if e["event"] == "invariant_violation"]
        lines.extend(
            [
                f"- Speculation cancellations: **{len(cancels)}**",
                f"- Speculation restarts: **{len(restarts)}**",
                f"- Invariant violations: **{len(violations)}**",
                "",
                "### Full state/event chart",
                "",
                "| Offset | Event | State | Attempt | Reason | Details |",
                "|---:|---|---|---|---|---|",
            ]
        )
        for event in turn_events:
            state = ""
            if event.get("state_from") or event.get("state_to"):
                state = f"{event.get('state_from') or '—'} → {event.get('state_to') or '—'}"
            details = json.dumps(event.get("data") or {}, ensure_ascii=False).replace("|", "\\|")
            if len(details) > 220:
                details = details[:217] + "..."
            lines.append(
                f"| {_ms(event['mono_ns'], origin):,.3f} ms | `{event['event']}` | {state} | {event.get('attempt_id') or '—'} | {event.get('reason') or ''} | `{details}` |"
            )
        lines.append("")

    violations = [event for event in events if event["event"] == "invariant_violation"]
    closed = Counter(event["turn_id"] for event in events if event["event"] == "turn_close")
    finals = Counter(event["turn_id"] for event in events if event["event"] == "stt_final")
    duplicate_finals = [turn for turn, count in finals.items() if count > 1]
    duplicate_closes = [turn for turn, count in closed.items() if count > 1]
    lines.extend(
        [
            "## State validations",
            "",
            f"- At most one final per turn: **{'PASS' if not duplicate_finals else 'FAIL'}**",
            f"- At most one close per turn: **{'PASS' if not duplicate_closes else 'FAIL'}**",
            f"- No explicitly detected events after close: **{'PASS' if not violations else 'FAIL'}**",
            "",
            "## Root-cause bottleneck",
            "",
        ]
    )
    playback_events = [
        event
        for event in events
        if event["event"] in {
            "browser_playback_start",
            "livekit_playback_start",
            "browser_playback_confirmed",
        }
    ]
    authorized_attempts = {
        str(event.get("attempt_id"))
        for event in events
        if event["event"] == "generation_start" and (event.get("data") or {}).get("authorized")
    }
    authorized_cancels = [
        event
        for event in events
        if event["event"] == "generation_cancel"
        and str(event.get("attempt_id")) in authorized_attempts
    ]
    generation_errors = [event for event in events if event["event"] == "generation_error"]
    if not playback_events and generation_errors:
        first_error = generation_errors[0]
        error_data = first_error.get("data") or {}
        lines.append(
            "No transport playback was observed. The first breaking boundary was a "
            f"**generation error** (`{first_error.get('reason') or 'unknown'}`) in "
            f"`{first_error.get('turn_id')}` / `{first_error.get('attempt_id')}` before any "
            f"audio reached the browser: `{error_data.get('error') or 'no error detail'}`."
        )
    elif not playback_events and authorized_cancels:
        first_cancel = authorized_cancels[0]
        attempt_id = str(first_cancel.get("attempt_id"))
        sent_bytes = sum(
            int((event.get("data") or {}).get("bytes") or 0)
            for event in events
            if event["event"] == "audio_chunk_sent" and str(event.get("attempt_id")) == attempt_id
        )
        cancel_reason = str(first_cancel.get("reason") or "unknown")
        if cancel_reason == "user_barge_in":
            explanation = (
                "The VAD classified a subsequent audio burst as user barge-in and canceled the "
                "committed response before playback. Validate that the interruption met the minimum "
                "speech-duration threshold before treating this as an intentional user interruption."
            )
        else:
            explanation = (
                "Post-final STT evidence incorrectly retained authority to cancel the committed "
                "response, so provider-latency optimization is not yet the first fix."
            )
        lines.append(
            "No transport playback was observed. The first breaking boundary was an "
            f"**authorized generation cancellation** (`{cancel_reason}`) in "
            f"`{first_cancel.get('turn_id')}` / `{attempt_id}` after only **{sent_bytes:,} bytes** "
            f"had reached the browser. {explanation}"
        )
    elif not playback_events:
        lines.append(
            "No transport playback start was observed. Inspect the first missing milestone between "
            "TTS request, first audio, authorized send, and the transport queue before drawing a latency conclusion."
        )
    elif all_gaps:
        largest, stage, turn_id = max(all_gaps)
        lines.append(f"The largest observed critical-path gap was **{largest:,.1f} ms** at **{stage}** in `{turn_id}`. This is the first data-backed optimization target; cancellations and invalid state transitions should be corrected before interpreting downstream timing as provider latency.")
    else:
        lines.append("No complete milestone chain was captured, so the trace cannot yet name a data-backed latency bottleneck.")
    lines.extend(
        [
            "",
            "## Interpretation guardrail",
            "",
            "This report measures the RVC coordinator with the selected transport and explicit read-only knowledge retrieval. It does not include action tools, billing, or production persistence; those capabilities must be added and measured incrementally.",
            "",
        ]
    )
    output.write_text("\n".join(lines), encoding="utf-8")
    return output
