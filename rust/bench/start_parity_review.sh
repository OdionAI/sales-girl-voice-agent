#!/bin/bash
# Local native demo supervisor. No Python worker or coordinator is launched.
set -u
umask 077
if [[ $# -lt 2 || $# -gt 4 ]]; then
  echo 'usage: start_parity_review.sh PRIVATE_CONFIG_JSON LIVEKIT_SDK_UMD_JS [PORT [LOG_DIRECTORY]]' >&2
  exit 2
fi
review_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
review_config="$1"
review_sdk="$2"
review_port="${3:-17901}"
review_logs="${4:-$review_root/artifacts/python-parity-review-$review_port}"
review_agent="sg-rust-parity-review-$review_port"
review_worker="$review_root/target/python-parity/release/rvc-worker"
review_server="$review_root/target/python-parity/release/rvc-local-call"
if [[ ! "$review_port" =~ ^[1-9][0-9]{0,4}$ ]] || (( review_port > 65535 )); then
  echo 'Expected a nonzero local port.' >&2
  exit 2
fi
if [[ "$review_config" != /* || "$review_sdk" != /* || "$review_logs" != /* || ! -r "$review_config" || ! -r "$review_sdk" || ! -x "$review_worker" || ! -x "$review_server" ]]; then
  echo 'Use absolute readable config/SDK paths and build the parity release binaries first.' >&2
  exit 2
fi
if lsof -nP -iTCP:"$review_port" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Local port $review_port is occupied; its process was left untouched." >&2
  exit 2
fi
mkdir -p -- "$review_logs"
review_server_pid=''
review_worker_pid=''
cleanup() {
  trap - EXIT INT TERM
  if [[ -n "$review_worker_pid" ]]; then kill -TERM "$review_worker_pid" 2>/dev/null || true; fi
  if [[ -n "$review_server_pid" ]]; then kill -TERM "$review_server_pid" 2>/dev/null || true; fi
  wait || true
}
trap cleanup EXIT
trap 'exit 0' INT TERM
echo "$$" > "$review_logs/supervisor.pid"
"$review_server" "$review_sdk" "$review_agent" "$review_port" >> "$review_logs/server.log" 2>&1 &
review_server_pid=$!
echo "$review_server_pid" > "$review_logs/server.pid"
echo "Native review at http://localhost:$review_port/; logs: $review_logs"
while kill -0 "$review_server_pid" 2>/dev/null; do
  "$review_worker" "$review_config" "$review_logs" "$review_agent" >> "$review_logs/worker.log" 2>&1 &
  review_worker_pid=$!
  echo "$review_worker_pid" > "$review_logs/worker.pid"
  wait "$review_worker_pid" || true
  review_worker_pid=''
  # The synthetic worker intentionally serves one call. Register a fresh native
  # worker for the next call; keep the separate --platform lifecycle unchanged.
  sleep 1
done
