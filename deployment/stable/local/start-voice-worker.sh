#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
env_file="${VOICE_AGENT_ENV_FILE:-${repo_root}/.env}"

[[ -f "${env_file}" ]] || { echo "Missing ${env_file}" >&2; exit 1; }
[[ -x "${repo_root}/.venv/bin/python" ]] || {
  echo "Missing ${repo_root}/.venv; create it from manifests/voice-agent-pip-freeze.txt." >&2
  exit 1
}

set -a
# shellcheck disable=SC1090
source "${env_file}"
set +a
export LIVEKIT_URL="${LOCAL_LIVEKIT_URL:-ws://127.0.0.1:7880}"

cd "${repo_root}"
exec .venv/bin/python main.py dev
