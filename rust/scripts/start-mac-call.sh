#!/usr/bin/env bash
# Local RTC + Rust worker + browser caller. Existing model services only.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
if [[ $# -lt 1 || $# -gt 2 ]]; then echo 'usage: start-mac-call.sh PRIVATE_CONFIG [LOG_DIRECTORY]' >&2; exit 2; fi
sg_mac_config=$1
sg_mac_logs=${2:-artifacts/mac-live-call}
[[ -f "$sg_mac_config" && -x target/release/rvc-worker && -x target/release/rvc-local-call ]]
for sg_mac_port in 17880 17881 17900; do
 if lsof -nP -iTCP:"$sg_mac_port" -sTCP:LISTEN >/dev/null 2>&1; then echo "Port $sg_mac_port occupied; refusing to touch its owner." >&2; exit 1; fi
done
if lsof -nP -iUDP:17882 >/dev/null 2>&1; then echo 'Media port17882 occupied.' >&2; exit 1; fi
scripts/prepare-local-call-sdk.sh
mkdir -p "$sg_mac_logs"
sg_mac_agent="sg-rust-mac-$$"
sg_mac_server='';sg_mac_worker_loop='';sg_mac_ui=''
cleanup() {
 for sg_mac_pid in "$sg_mac_ui" "$sg_mac_worker_loop" "$sg_mac_server"; do
  if [[ -n "$sg_mac_pid" ]]; then kill "$sg_mac_pid" 2>/dev/null || true; wait "$sg_mac_pid" 2>/dev/null || true; fi
 done
}
trap cleanup EXIT
trap 'exit 0' INT TERM
livekit-server --dev --bind 127.0.0.1 --config local-livekit.yaml > "$sg_mac_logs/livekit.log" 2>&1 &
sg_mac_server=$!
for _ in {1..50}; do
 if curl --fail --silent --max-time 1 http://127.0.0.1:17880/ >/dev/null; then break; fi
 kill -0 "$sg_mac_server"
 sleep .1
done
worker_loop() {
 sg_mac_child=''
 trap 'if [[ -n "$sg_mac_child" ]]; then kill "$sg_mac_child" 2>/dev/null || true; wait "$sg_mac_child" 2>/dev/null || true; fi' EXIT
 trap 'exit 0' INT TERM
 while true; do
  target/release/rvc-worker "$sg_mac_config" "$sg_mac_logs" "$sg_mac_agent" >> "$sg_mac_logs/worker.log" 2>&1 &
  sg_mac_child=$!
  wait "$sg_mac_child" || true
  sg_mac_child=''
  sleep .5
 done
}
worker_loop &
sg_mac_worker_loop=$!
for _ in {1..100}; do
 if [[ -f "$sg_mac_logs/worker.log" ]] && rg -q '^worker_ready' "$sg_mac_logs/worker.log"; then break; fi
 kill -0 "$sg_mac_worker_loop"
 sleep .1
done
if ! rg -q '^worker_ready' "$sg_mac_logs/worker.log"; then echo 'Rust worker did not register; see local worker log.' >&2; exit 1; fi
target/release/rvc-local-call target/browser-sdk/livekit-client.umd.js "$sg_mac_agent" > "$sg_mac_logs/browser-server.log" 2>&1 &
sg_mac_ui=$!
printf '%s\n' "launcher=$$" "livekit=$sg_mac_server" "worker_supervisor=$sg_mac_worker_loop" "browser_server=$sg_mac_ui" > "$sg_mac_logs/pids.txt"
echo 'Local voice call: http://localhost:17900'
wait "$sg_mac_ui"
