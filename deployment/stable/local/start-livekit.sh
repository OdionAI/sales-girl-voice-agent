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

node_ip="${LOCAL_LIVEKIT_NODE_IP:-}"
if [[ -z "${node_ip}" ]] && command -v route >/dev/null 2>&1 && command -v ipconfig >/dev/null 2>&1; then
  default_interface="$(route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}')"
  if [[ -n "${default_interface}" ]]; then
    node_ip="$(ipconfig getifaddr "${default_interface}" 2>/dev/null || true)"
  fi
fi
node_ip="${node_ip:-127.0.0.1}"

echo "Starting local LiveKit with RTC node IP ${node_ip}"
recording_args=()
if [[ -n "${LOCAL_LIVEKIT_REDIS_HOST:-}" ]]; then
  recording_args+=(--redis-host "${LOCAL_LIVEKIT_REDIS_HOST}")
fi
exec livekit-server \
  --dev \
  --bind 127.0.0.1 \
  --node-ip "${node_ip}" \
  "${recording_args[@]}"
