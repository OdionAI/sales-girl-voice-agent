"""Run the internal staging-only app without printing credentials or raw SDK logs."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

parser = argparse.ArgumentParser()
parser.add_argument("--configuration", required=True, help="Path to a private JSON file, mode 0600")
parser.add_argument("--simulator", required=True, help="Booted simulator UDID")
parser.add_argument("--staging-verified", action="store_true", help="Confirm the endpoint is an isolated staging environment")
args = parser.parse_args()
path = Path(args.configuration)
if not args.staging_verified or path.stat().st_mode & 0o077:
    sys.exit("A verified staging endpoint and private configuration file (mode 0600) are required.")
config = json.loads(path.read_text())
base = urlsplit(config.get("baseURL", ""))
host = base.hostname or ""
labels = host.lower().split(".")
if (base.scheme != "https" or not host or host != config.get("confirmedHost", "").lower()
        or (host != "salesgirlstaging.odion.ai" and not any(label == "staging" or label.endswith("-staging") or label.startswith("staging-") for label in labels))
        or base.username or base.password or base.query or base.fragment or base.port not in (None, 443)
        or base.path != "/api/sdk/v1/" or not config.get("apiKey") or not config.get("agentID")):
    sys.exit("Staging configuration failed validation; no request was made.")

env = os.environ.copy()
for name, value in {
    "ATTENTIVE_STAGING_VERIFIED": "yes", "ATTENTIVE_STAGING_API_BASE": config["baseURL"],
    "ATTENTIVE_STAGING_CONFIRMED_HOST": host, "ATTENTIVE_STAGING_API_KEY": config["apiKey"],
    "ATTENTIVE_STAGING_AGENT_ID": config["agentID"], "ATTENTIVE_STAGING_CUSTOMER_ID": config.get("customerID"),
    "ATTENTIVE_STAGING_CALLER_TOKEN": config.get("callerToken"),
}.items():
    if value is not None:
        env["SIMCTL_CHILD_" + name] = value
with tempfile.NamedTemporaryFile(prefix="attentive-staging-", suffix=".log", mode="w+", delete=False) as log:
    os.chmod(log.name, 0o600)
    try:
        result = subprocess.run(["xcrun", "simctl", "launch", "--terminate-running-process", "--console", args.simulator,
                                 "ai.odion.attentive.animated-theme-staging"],
                                env=env, stdout=log, stderr=subprocess.STDOUT, timeout=100)
    except subprocess.TimeoutExpired:
        result = None
    log.flush()
    log.seek(0)
    markers = []
    for line in log:
        if line.startswith(("STAGING_THEME=", "STAGING_CALL_STATE=", "STAGING_AGENT_STATE=", "STAGING_MICROPHONE=",
                            "STAGING_ASSISTANT_OUTPUT=", "STAGING_RESULT=")):
            marker = line.rstrip()
            markers.append(marker)
            print(marker)
    print("Private log:", log.name)
    if result is None:
        print("STAGING_RESULT=launcher_timeout")
        sys.exit(1)
    required = {"STAGING_CALL_STATE=connected", "STAGING_MICROPHONE=off",
                "STAGING_ASSISTANT_OUTPUT=non_silent", "STAGING_RESULT=completed"}
    animated = any(re.fullmatch(r"STAGING_THEME=animated:#[0-9a-fA-F]{6}:(grain|smooth)", item) for item in markers)
    if result.returncode or not required.issubset(markers) or not animated:
        print("STAGING_RESULT=verification_incomplete")
        sys.exit(1)
    print("STAGING_RESULT=verified")
