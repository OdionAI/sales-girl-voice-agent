#!/usr/bin/env python3
"""CI-only launch for one explicitly scoped companion; never touches shared services."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

NAME = "odion-bonsai-demo-voice"
SOURCE = "odion-recovery-lagos-voice-1"
BUSINESS = "6ddb0d72-8bd7-4a66-b0bd-a1d2cddd4b9d"
AGENTS = ("1330ef61-4341-430d-b83d-23f5780cbe0f", "6826e3d5-4870-47a1-b98b-c4d4742bf391", "24568766-e169-4ffd-bbbf-7d8160c6f89b", "f3f7a4fc-e2ed-4ac2-bc4f-8320dc78e108", "1d08fd00-1675-409c-8a52-c4debb891c72")
MODEL = "bonsai-2-27b-ascend-q4-repack"


def environment(source_env, api_key):
    if not api_key or "\n" in api_key or "\r" in api_key:
        raise ValueError("trial API key is missing or invalid")
    values = {}
    for entry in source_env:
        key, separator, value = entry.partition("=")
        if not separator or "\n" in value or "\r" in value:
            raise ValueError("source environment cannot be safely copied")
        values[key] = value
    values.update({
        "RVC_LAB_LIVEKIT_AGENT_NAME": "rvc-bonsai-demo",
        "RVC_LAB_LIVEKIT_WORKER_PORT": "18084",
        "RVC_LAB_TRACE_DIR": "/tmp/bonsai-traces",
        "LIVEKIT_RECORDING_ENABLED": "false",
        "RVC_LAB_TRIAL_LLM_BUSINESS_ID": BUSINESS,
        "RVC_LAB_TRIAL_LLM_AGENT_IDS": ",".join(AGENTS),
        "RVC_LAB_TRIAL_LLM_BASE_URL": "http://127.0.0.1:18143/v1",
        "RVC_LAB_TRIAL_LLM_MODEL": MODEL,
        "RVC_LAB_TRIAL_LLM_API_KEY": api_key,
        "RVC_LAB_TRIAL_LLM_FIRST_OUTPUT_TIMEOUT_SECONDS": "90",
        "RVC_LAB_TRIAL_LLM_EXCLUSIVE": "true",
    })
    return values


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def verify_model():
    with urllib.request.urlopen("http://127.0.0.1:18143/health", timeout=5) as response:
        if json.load(response).get("status") != "ok":
            raise RuntimeError("trial model is not healthy")
    with urllib.request.urlopen("http://127.0.0.1:18143/v1/models", timeout=5) as response:
        models = json.load(response).get("data", [])
    if not any(m.get("id") == MODEL and m.get("meta", {}).get("n_ctx", 0) >= 8192 for m in models):
        raise RuntimeError("expected model alias and context were not verified")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--image", required=True)
    parser.add_argument("--secret-stdin", action="store_true")
    args = parser.parse_args()
    if not args.image.startswith("attentive/voice:bonsai-ci-"):
        raise ValueError("image is not a CI-tagged Bonsai artifact")
    source = json.loads(run("docker", "inspect", SOURCE, capture_output=True).stdout)[0]
    if not source["State"]["Running"] or source["HostConfig"]["NetworkMode"] != "host":
        raise RuntimeError("shared worker is not in the expected running host network")
    if subprocess.run(["docker", "inspect", NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        raise RuntimeError("companion already exists; do not replace an active trial")
    api_key = sys.stdin.read().strip() if args.secret_stdin else os.environ.get("BONSAI_TRIAL_API_KEY", "")
    values = environment(source["Config"]["Env"], api_key)
    print(json.dumps({"mode": "apply" if args.apply else "plan", "source_image": source["Config"]["Image"], "companion": NAME, "image": args.image, "worker": values["RVC_LAB_LIVEKIT_AGENT_NAME"], "agent_count": len(AGENTS), "business": BUSINESS, "host_port": 18084, "recording": False}))
    if not args.apply:
        return
    # A temporary transport can disappear while the dashboard still dispatches
    # here. Only a separately managed durable tunnel may back a live canary.
    unit = "odion-bonsai-durable-client.service"
    run("systemctl", "is-active", "--quiet", unit)
    lifetime = run("systemctl", "show", unit, "--property=RuntimeMaxUSec", "--value", capture_output=True).stdout.strip()
    if lifetime not in ("infinity", "0"):
        raise RuntimeError("trial tunnel is time-limited; dashboard rollback is not automatic")
    verify_model()
    with tempfile.NamedTemporaryFile(mode="w", dir="/dev/shm", prefix="bonsai-env-", delete=False) as handle:
        path = handle.name
        os.chmod(path, 0o600)
        for key, value in values.items():
            handle.write(f"{key}={value}\n")
    created = False
    try:
        run("docker", "run", "-d", "--name", NAME, "--network", "host", "--restart", "no", "--memory", "2g", "--cpus", "2", "--env-file", path, args.image, stdout=subprocess.DEVNULL)
        created = True
        for _ in range(30):
            probe = subprocess.run(["docker", "exec", NAME, "/usr/local/bin/rvc-worker", "--healthcheck"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if probe.returncode == 0:
                print("companion registered")
                return
            time.sleep(2)
        raise RuntimeError("companion failed readiness")
    except Exception:
        if created:
            subprocess.run(["docker", "rm", "-f", NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        raise
    finally:
        os.unlink(path)


if __name__ == "__main__":
    main()
