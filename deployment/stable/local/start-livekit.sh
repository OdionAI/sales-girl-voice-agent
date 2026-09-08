#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
env_file="${VOICE_AGENT_ENV_FILE:-${repo_root}/.env}"

[[ -f "${env_file}" ]] || { echo "Missing ${env_file}" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "${env_file}"
set +a

: "${LIVEKIT_API_KEY:?LIVEKIT_API_KEY is required}"
: "${LIVEKIT_API_SECRET:?LIVEKIT_API_SECRET is required}"
export LIVEKIT_KEYS="${LIVEKIT_API_KEY}: ${LIVEKIT_API_SECRET}"

exec livekit-server --dev --bind 127.0.0.1
