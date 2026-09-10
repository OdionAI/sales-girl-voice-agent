"""Prepare private Lagos recording config from the running worker's credentials."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess


def private_write(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_directory", type=Path)
    parser.add_argument("--worker", default="attentive-huawei-voice-1")
    parser.add_argument("--egress-uid", type=int, required=True)
    parser.add_argument("--egress-gid", type=int, required=True)
    args = parser.parse_args()
    inspected = json.loads(subprocess.check_output(["docker", "inspect", args.worker]))[0]
    values = dict(item.split("=", 1) for item in inspected["Config"]["Env"])
    credentials = {}
    for name in ("LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        value = values.get(name, "")
        if not value or any(char in value for char in "\r\n"):
            raise SystemExit(f"Missing or invalid {name}; no configuration written.")
        credentials[name] = value
    private = args.release_directory.resolve() / ".private"
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    recordings = private / "recordings"
    recordings.mkdir(mode=0o750, exist_ok=True)
    os.chown(recordings, args.egress_uid, args.egress_gid)
    os.chmod(recordings, 0o750)
    private_write(private / "egress.env", "".join(
        f"{key}={json.dumps(value)}\n" for key, value in credentials.items()
    ) + "LIVEKIT_WS_URL=ws://127.0.0.1:17880\n")
    config = private / "egress.yaml"
    private_write(config, """redis:
  address: 127.0.0.1:6380
insecure: true
enable_chrome_sandbox: true
log_level: info
""")
    os.chown(config, args.egress_uid, args.egress_gid)
    print("Private recorder configuration prepared; credentials were not printed.")


if __name__ == "__main__":
    main()
