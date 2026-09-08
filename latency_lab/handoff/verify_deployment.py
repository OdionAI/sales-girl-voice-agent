"""Read-only configuration/dependency check; no calls, banking or service changes."""

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def config_errors(config, expected):
    return [f"{key}: does not match the captured configuration"
            for key, value in expected.items() if getattr(config, key) != value]


def trace_errors(events, manifest):
    opened = next((e.get("data", {}) for e in events if e.get("event") == "session_open"), {})
    job = next((e.get("data", {}) for e in events if e.get("event") == "livekit_job_received"), {})
    expected = {
        "transport": "livekit", "agent_config_loaded": True,
        "configured_agent_name": "SAW", "configured_tool_count": 8,
        "supported_action_tool_count": 8, "stt_final_authority": "whisper_realtime",
        "stt_batch_url": "", "initial_codec_chunk_frames": 4,
        "end_silence_ms": 300, "memory_compaction_enabled": True,
        "memory_context_tokens_estimate": 6000,
    }
    errors = [f"trace {key}: mismatch or absent" for key, value in expected.items()
              if opened.get(key) != value]
    if job.get("agent_name") != manifest["worker"]["agent_name"]:
        errors.append("trace: unexpected or missing named worker")
    if not str(job.get("room_name", "")).startswith(manifest["worker"]["room_prefix"]):
        errors.append("trace: unexpected or missing room prefix")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--dashboard", type=Path, required=True)
    parser.add_argument("--public-id", required=True)
    parser.add_argument("--overrides", type=Path, help="Optional actual runtime_overrides JSON object")
    parser.add_argument("--trace", type=Path, help="Optional JSONL from one completed hybrid call")
    args = parser.parse_args()
    if not args.env_file.is_file():
        parser.error("The private environment file does not exist")

    from dotenv import load_dotenv
    load_dotenv(args.env_file, override=True)
    os.environ["RVC_LAB_DASHBOARD_ENV_FILE"] = "/dev/null"
    os.environ["RVC_LAB_LIVEKIT_ENV_FILE"] = "/dev/null"
    # Import only after loading the environment: LabConfig defaults are import-time.
    from latency_lab.config import apply_runtime_overrides, lab_config_from_environ

    manifest = json.loads((ROOT / "runtime-manifest.json").read_text())
    errors = []
    for key in (
        "AUTH_SERVICE_BASE_URL", "AGENT_CONFIG_SERVICE_BASE_URL", "AGENT_CONFIG_SERVICE_TOKEN",
        "CONVERSATION_SERVICE_BASE_URL", "CONVERSATION_SERVICE_TOKEN", "WEMA_TOOLS_BASE_URL",
        "WEMA_TOOLS_SERVICE_TOKEN", "VOICE_AUTH_SIDECAR_URL", "VOICE_AUTH_WORKER_URL",
        "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
        "WEMA_CALLER_PREFILL_CUSTOMER_ID", "WEMA_CALLER_PREFILL_PHONE_NUMBER",
    ):
        value = os.getenv(key, "").strip()
        if not value or value.startswith("REPLACE_"):
            errors.append(f"{key}: missing or placeholder (value withheld)")
    for key, value in {
        "PUBLIC_AGENT_RVC_ENABLED": "true", "PUBLIC_AGENT_USE_VOICE_LAB_RUNTIME": "true",
        "RVC_LAB_LIVEKIT_AGENT_NAME": "rvc-livekit-comparison",
        "RVC_LAB_LIVEKIT_WORKER_PORT": "8189",
    }.items():
        if os.getenv(key, "").strip().lower() != value:
            errors.append(f"{key}: expected {value}")
    ids = os.getenv("PUBLIC_AGENT_RVC_LIVEKIT_PUBLIC_IDS", "").split(",")
    if not args.public_id.startswith("agt_") or args.public_id not in [s.strip() for s in ids]:
        errors.append("The target public ID is not allowlisted for the hybrid")

    node_script = (
        'import {defaultPublicAgentRvcRuntimeOverrides as defaults} from "./lib/voice-lab-runtime.js";'
        'console.log(JSON.stringify(defaults({llmDisableThinking:true})))'
    )
    result = subprocess.run(["node", "--input-type=module", "-e", node_script],
                            cwd=args.dashboard, capture_output=True, text=True, timeout=15, check=True)
    overrides = json.loads(result.stdout)
    if args.overrides:
        overrides.update(json.loads(args.overrides.read_text()))
    config = apply_runtime_overrides(lab_config_from_environ(), overrides, language="en")
    errors.extend(config_errors(config, manifest["expected_config"]))
    if platform.python_version() != manifest["versions"]["python"]:
        errors.append("Python version differs from the captured 3.12.7")
    node_version = subprocess.check_output(["node", "--version"], text=True).strip().lstrip("v")
    if node_version != manifest["versions"]["node"]:
        errors.append("Node version differs from the captured 22.23.1")
    for line in (ROOT / "hybrid-python-freeze.txt").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        name, version = line.split("==", 1)
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            installed = None
        if installed != version:
            errors.append(f"Package {name}: expected {version}, found {installed or 'missing'}")
    if args.trace:
        with args.trace.open() as handle:
            events = [json.loads(line) for line in handle if line.strip()]
        errors.extend(trace_errors(events, manifest))
    for error in errors:
        print(f"FAIL: {error}")
    if errors:
        return 1
    print("PASS: hybrid defaults, thinking disabled, model/timing settings and package versions match.")
    print("This does not test reachability, DB contents, model-server state or audible latency.")
    print("Actual per-call overrides may differ; use --overrides and --trace to inspect a specific call.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError):
        # Do not dump subprocess output or JSON that could contain private configuration.
        print("FAIL: could not read configuration or execute Node; check paths and JSON formats.")
        sys.exit(2)
