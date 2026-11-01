#!/usr/bin/env bash
# Before/after replay using only owned local fixtures and the existing caller.
# No SSH, production environment, credentials, or production provider requests.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $# -ne 4 ]]; then
 echo 'usage: compare-latency-local.sh BEFORE_BINARY AFTER_BINARY PINNED_PYTHON_CHECKOUT OUTPUT_DIRECTORY' >&2
 exit 2
fi
sg_latency_before=$1
sg_latency_after=$2
sg_latency_reference=$3
sg_latency_output=$4
sg_latency_python=${SG_RVC_PYTHON:-python3}
[[ -x "$sg_latency_before" && -x "$sg_latency_after" ]]
for sg_latency_port in 18093 17880 17881 18084; do
 if lsof -nP -iTCP:"$sg_latency_port" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Local test port $sg_latency_port occupied; refusing to touch its owner." >&2
  exit 1
 fi
done
if lsof -nP -iUDP:17882 >/dev/null 2>&1; then exit 1; fi
mkdir -p "$sg_latency_output"
sg_latency_output=$(cd "$sg_latency_output" && pwd)
# Refuse mixing repetitions with older measurements.
[[ ! -e "$sg_latency_output/before" && ! -e "$sg_latency_output/after" ]]
"$sg_latency_python" bench/asr_fixture.py --latency-replay \
 --write-config "$sg_latency_output/config.json" --log "$sg_latency_output/provider.jsonl" \
 > "$sg_latency_output/fixture.log" 2>&1 &
sg_latency_fixture=$!
cleanup() { kill "$sg_latency_fixture" 2>/dev/null || true; wait "$sg_latency_fixture" 2>/dev/null || true; }
trap cleanup EXIT
for _ in {1..50}; do
 if [[ -f "$sg_latency_output/config.json" ]] && curl --silent --max-time 1 http://127.0.0.1:18093/ >/dev/null; then break; fi
 kill -0 "$sg_latency_fixture"
 sleep .1
done
"$sg_latency_python" - "$sg_latency_output/config.json" <<'PY'
import json,sys
from urllib.parse import urlparse
config=json.load(open(sys.argv[1]))
for key in ['stt_ws_url','stt_chat_url','stt_batch_url','llm_base_url','tts_url']:
    assert urlparse(config[key]).hostname=='127.0.0.1', key
assert config['llm_api_key']=='fixture-not-a-real-key'
PY
for sg_latency_variant in before after; do
 sg_latency_binary=$sg_latency_before
 if [[ "$sg_latency_variant" == after ]]; then sg_latency_binary=$sg_latency_after; fi
 SG_RVC_BINARY="$sg_latency_binary" SG_RVC_PYTHON="$sg_latency_python" \
  scripts/compare-rvc-local.sh "$sg_latency_output/config.json" "$sg_latency_reference" \
  "$sg_latency_output/$sg_latency_variant" rust rust rust > "$sg_latency_output/$sg_latency_variant-run.log" 2>&1
done
SG_RVC_BINARY="$sg_latency_after" SG_RVC_PYTHON="$sg_latency_python" \
 scripts/compare-rvc-local.sh "$sg_latency_output/config.json" "$sg_latency_reference" \
 "$sg_latency_output/interruption" rust-interrupt > "$sg_latency_output/interruption-run.log" 2>&1
