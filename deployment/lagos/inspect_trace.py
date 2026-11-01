"""Read a caller-owned diagnostic trace without exporting conversation content."""
import collections
import json
from pathlib import Path
import re
import sys

job = sys.argv[1]
if job == "RECENT":
    root = Path("/opt/attentive/releases/recovery-20261009/.private/traces/traces")
    for path in sorted(root.glob("AJ_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:12]:
        if path.stat().st_size > 16 * 1024 * 1024:
            continue
        counts = collections.Counter()
        errors = []
        with path.open() as stream:
            for line in stream:
                try: item = json.loads(line)
                except ValueError: continue
                event = item.get("event", "")
                counts[event] += 1
                if event in {"error", "engagement_failed", "tts_error", "session_failed"}:
                    message = str(item.get("data", {}).get("error", ""))
                    message = re.sub(r"https?://[^\s]+", "[endpoint]", message)
                    errors.append({"event": event, "error": message[:240]})
        print(json.dumps({"job_id": path.stem, "modified": path.stat().st_mtime, "counts": dict(counts), "errors": errors}))
    raise SystemExit(0)
if not re.fullmatch(r"AJ_[A-Za-z0-9_-]{6,80}", job):
    raise SystemExit("Invalid diagnostic job ID")
root = Path("/opt/attentive/releases/recovery-20261009/.private/traces/traces")
path = root / (job + ".jsonl")
if path.stat().st_size > 16 * 1024 * 1024:
    raise SystemExit("Diagnostic trace exceeds read limit")
selected = {"session_ready", "input_noise_filter_ready", "input_noise_filter_metrics",
            "user_speech_start", "user_speech_end", "engagement_spoken",
            "attempt_cancel", "speculation_authorized", "audio_first_enqueued",
            "stt_final", "llm_first_token", "tts_first_pcm", "turn_complete",
            "session_disconnect", "session_closed"}
fields = {"enabled", "implementation", "backend", "speech_prob", "reason",
          "sample_rate", "frame_ms", "rms_in", "rms_out", "duration_ms"}
counts = collections.Counter()
events = []
with path.open() as trace:
    for line in trace:
        item = json.loads(line)
        event = item.get("event")
        if event not in selected:
            continue
        counts[event] += 1
        data = item.get("data", {})
        events.append({key: item[key] for key in ("event", "at_us", "turn", "attempt") if key in item}
                      | {"data": {key: value for key, value in data.items() if key in fields}})
print(json.dumps({"job_id": job, "counts": dict(counts), "events": events}))
