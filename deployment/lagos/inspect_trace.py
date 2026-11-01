"""Read a caller-owned diagnostic trace without exporting conversation content."""
import collections
import json
from pathlib import Path
import re
import sys

job = sys.argv[1]
if job == "BALOGUN":
    import subprocess
    import urllib.request
    import urllib.parse
    ids = subprocess.check_output(["docker", "ps", "-q"], text=True).split()
    containers = json.loads(subprocess.check_output(["docker", "inspect", *ids], text=True))
    voice = next(c for c in containers if c["Config"].get("Labels", {}).get("com.docker.compose.service") == "voice")
    env = dict(item.split("=", 1) for item in voice["Config"]["Env"] if "=" in item)
    base = env.get("AGENT_CONFIG_API_BASE_URL") or env.get("AGENT_CONFIG_SERVICE_BASE_URL")
    token = env.get("AGENT_CONFIG_SERVICE_TOKEN") or env.get("CONVERSATION_SERVICE_TOKEN")
    parsed = urllib.parse.urlsplit(base)
    hostname = parsed.hostname
    address = hostname
    for c in containers:
        for network in c["NetworkSettings"]["Networks"].values():
            if hostname in (network.get("Aliases") or []) or c["Name"].lstrip("/") == hostname:
                address = network["IPAddress"]
    url = urllib.parse.urlunsplit((parsed.scheme, address + (":"+str(parsed.port) if parsed.port else ""), parsed.path.rstrip("/") + "/v1/agents/internal/agents/3801e455-224a-4872-b406-95a4db6b8ffa/runtime-config", "", ""))
    headers = {"X-Service-Token": token, "X-Service-Name": env.get("AGENT_CLIENT_ID", "sales-girl-voice-agent"), "X-Agent-ID": "3801e455-224a-4872-b406-95a4db6b8ffa", "X-Business-ID": "f2ff0fe3-d56c-4688-9048-9a76a6310d4a", "Host": parsed.netloc}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as response:
        body = json.load(response)
    clean = {key: body.get(key) for key in ("agent_id", "name", "tts_language_hint", "tts_voice_id")}
    clean["tools"] = [{"name": t.get("name"), "url": str(t.get("url", "")).split("?")[0], "is_active": t.get("is_active")} for t in body.get("tools", [])]
    print(json.dumps(clean))
    raise SystemExit(0)
if job == "RECENT":
    root = Path("/opt/attentive/releases/recovery-20261009/.private/traces/traces")
    for path in sorted(root.glob("AJ_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:12]:
        if path.stat().st_size > 16 * 1024 * 1024:
            continue
        counts = collections.Counter()
        errors = []
        timings = []
        timing_names = {"user_audio_end_candidate", "user_speech_start", "user_speech_end", "stt_final", "llm_request_start", "llm_first_token", "llm_text_released", "tts_request_start", "tts_first_pcm", "tts_request_complete", "audio_first_enqueued", "attempt_cancel", "speculation_start", "speculation_authorized", "turn_complete", "engagement_spoken", "speech_gate_changed", "llm_provider_request_complete"}
        timing_fields = {"reason", "provider_ttft_ms", "duration_ms", "audio_seconds", "bytes", "chunks", "text_chars", "enabled", "request_id"}
        with path.open() as stream:
            for line in stream:
                try: item = json.loads(line)
                except ValueError: continue
                event = item.get("event", "")
                counts[event] += 1
                if event in timing_names:
                    data = item.get("data", {})
                    timings.append({key: item[key] for key in ("event", "at_us", "turn", "attempt") if key in item} | {"data": {key: value for key, value in data.items() if key in timing_fields}})
                if event in {"error", "engagement_failed", "tts_error", "session_failed"}:
                    message = str(item.get("data", {}).get("error", ""))
                    message = re.sub(r"https?://[^\s]+", "[endpoint]", message)
                    errors.append({"event": event, "error": message[:240]})
        print(json.dumps({"job_id": path.stem, "modified": path.stat().st_mtime, "counts": dict(counts), "errors": errors, "timings": timings[-300:]}))
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
