#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
export RECORDING_RUNTIME_DIR="${RECORDING_RUNTIME_DIR:-${repo_root}/.local/recording}"
export VOICE_AGENT_ENV_FILE="${VOICE_AGENT_ENV_FILE:-${repo_root}/.env}"

"${repo_root}/.venv/bin/python" - <<'PY'
import os
from pathlib import Path
import yaml
from dotenv import dotenv_values

env = dotenv_values(os.environ['VOICE_AGENT_ENV_FILE'])
root = Path(os.environ['RECORDING_RUNTIME_DIR'])
root.mkdir(parents=True, exist_ok=True, mode=0o700)
root.chmod(0o700)
audio = root / 'audio'
audio.mkdir(exist_ok=True)
# Egress runs as a different UID; the private parent prevents other host users
# from traversing into this shared output directory.
audio.chmod(0o777)
config = {
    'api_key': env['LIVEKIT_API_KEY'],
    'api_secret': env['LIVEKIT_API_SECRET'],
    'ws_url': 'ws://host.docker.internal:7880',
    'insecure': True,
    'redis': {'address': 'redis:6379'},
    'health_port': 7981,
    'logging': {'level': 'info'},
}
path = root / 'egress.yaml'
path.write_text(yaml.safe_dump(config))
# Only the private parent is accessible on the host; container user needs read.
path.chmod(0o644)
PY

exec docker compose -p sg-recording \
  -f "${repo_root}/deployment/stable/local/recording-compose.yaml" up -d
