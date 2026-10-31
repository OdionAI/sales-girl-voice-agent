#!/usr/bin/env bash
# Explicit local experiment only; no production environment or SSH access is loaded.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $# -lt 3 ]]; then echo 'usage: compare-rvc-local.sh PRIVATE_CONFIG PINNED_PYTHON_CHECKOUT OUTPUT_DIRECTORY [worker order...]' >&2; exit 2; fi
sg_rvc_config=$1
sg_rvc_reference=$2
sg_rvc_output=$3
shift 3
sg_rvc_python=${SG_RVC_PYTHON:-python3}
sg_rvc_binary=${SG_RVC_BINARY:-"$(pwd)/target/release/rvc-worker"}
[[ -x "$sg_rvc_binary" ]]
[[ -f "$sg_rvc_config" ]]
for sg_rvc_port in 17880 17881 18084; do
 if lsof -nP -iTCP:"$sg_rvc_port" -sTCP:LISTEN >/dev/null 2>&1; then echo "Local test port $sg_rvc_port occupied; refusing to touch it." >&2; exit 1; fi
done
if lsof -nP -iUDP:17882 >/dev/null 2>&1; then echo 'Local media port17882 occupied.' >&2; exit 1; fi
mkdir -p "$sg_rvc_output"
sg_rvc_output=$(cd "$sg_rvc_output" && pwd)
livekit-server --dev --bind 127.0.0.1 --config local-livekit.yaml > "$sg_rvc_output/server.log" 2>&1 &
sg_rvc_server=$!
cleanup(){ kill "$sg_rvc_server" 2>/dev/null || true; wait "$sg_rvc_server" 2>/dev/null || true; }
trap cleanup EXIT
for _ in {1..30}; do
 if curl --fail --silent --max-time 1 http://127.0.0.1:17880/ >/dev/null; then break; fi
 kill -0 "$sg_rvc_server"
 sleep .1
done
if [[ $# -eq 0 ]]; then set -- rust python python rust rust python; fi
"$sg_rvc_python" bench/run_calls.py "$sg_rvc_config" "$sg_rvc_reference" "$sg_rvc_output" "$sg_rvc_binary" "$@"
"$sg_rvc_python" bench/analyze.py "$sg_rvc_output"
