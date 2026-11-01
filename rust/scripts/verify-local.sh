#!/usr/bin/env bash
# Starts and stops only its own loopback LiveKit process. No production access.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p artifacts
cargo fmt --check > artifacts/native-fmt-final.log 2>&1
cargo test --locked --all-features > artifacts/native-tests-final.log 2>&1
cargo clippy --locked --all-features --all-targets -- -D warnings > artifacts/native-clippy-final.log 2>&1
cargo build --locked --all-features --bins > artifacts/native-build-final.log 2>&1
for port in 17880 17881; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Local test port $port is occupied; refusing to touch the existing process." >&2
    exit 1
  fi
done
if lsof -nP -iUDP:17882 >/dev/null 2>&1; then
  echo "Local media port 17882 is occupied." >&2
  exit 1
fi
livekit-server --dev --bind 127.0.0.1 --config local-livekit.yaml > artifacts/local-livekit-final.log 2>&1 &
sg_test_server_pid=$!
cleanup() { kill "$sg_test_server_pid" 2>/dev/null || true; wait "$sg_test_server_pid" 2>/dev/null || true; }
trap cleanup EXIT
for _ in {1..30}; do
  if curl --fail --silent --max-time 1 http://127.0.0.1:17880/ >/dev/null; then break; fi
  kill -0 "$sg_test_server_pid"
  sleep 0.1
done
target/debug/worker-control-smoke > artifacts/control-smoke-final.log 2>&1
target/debug/native-worker-smoke > artifacts/native-smoke-final.log 2>&1
cat artifacts/control-smoke-final.log artifacts/native-smoke-final.log
