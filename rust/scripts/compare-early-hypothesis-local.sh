#!/usr/bin/env bash
# Serial comparison using the existing caller, an owned provider fixture, and
# separate rooms on an already-running loopback-only development RTC server.
# Does not restart the browser demo or access production providers.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
if [[ $# -ne 3 ]]; then
 echo 'usage: compare-early-hypothesis-local.sh BEFORE_BINARY AFTER_BINARY NEW_OUTPUT_DIRECTORY' >&2
 exit 2
fi
sg_early_before=$1
sg_early_after=$2
sg_early_output=$3
sg_early_python=${SG_RVC_PYTHON:-python3}
[[ -x "$sg_early_before" && -x "$sg_early_after" && ! -e "$sg_early_output" ]]
curl --fail --silent --max-time 2 http://127.0.0.1:17880/ >/dev/null
if lsof -nP -iTCP:18093 -sTCP:LISTEN >/dev/null 2>&1; then
 echo 'Fixture port 18093 occupied; refusing to touch its owner.' >&2; exit 1
fi
mkdir -p "$sg_early_output"
sg_early_output=$(cd "$sg_early_output" && pwd)
"$sg_early_python" bench/asr_fixture.py --complete-hypothesis-replay \
 --write-config "$sg_early_output/config.json" --log "$sg_early_output/provider.jsonl" \
 > "$sg_early_output/fixture.log" 2>&1 &
sg_early_fixture=$!
cleanup() { kill "$sg_early_fixture" 2>/dev/null || true; wait "$sg_early_fixture" 2>/dev/null || true; }
trap cleanup EXIT
for _ in {1..50}; do
 if [[ -f "$sg_early_output/config.json" ]] && curl --silent --max-time 1 http://127.0.0.1:18093/ >/dev/null; then break; fi
 kill -0 "$sg_early_fixture"
 sleep .1
done
"$sg_early_python" - "$sg_early_before" "$sg_early_after" "$sg_early_output" <<'PY'
import asyncio, hashlib, json, sys
from pathlib import Path
from urllib.parse import urlparse
sys.path.insert(0, str(Path.cwd()/'bench'))
from run_calls import one

async def main():
    before,after,output=map(lambda p:Path(p).resolve(),sys.argv[1:])
    config=output/'config.json';c=json.loads(config.read_text())
    for key in ['stt_ws_url','stt_chat_url','stt_batch_url','llm_base_url','tts_url']:
        assert urlparse(c[key]).hostname=='127.0.0.1',key
    assert c['llm_api_key']=='fixture-not-a-real-key'
    results=[]
    for index,variant in enumerate(['before','after','before','after','before','after','interruption']):
        binary=before if variant=='before' else after
        kind='rust-interrupt' if variant=='interruption' else 'rust'
        result=await one(kind,index,config,Path.cwd().parent,output/variant,binary)
        results.append({'variant':variant,'result':result})
        print(json.dumps({'variant':variant,'result':result}),flush=True)
    report={'scope':'Local protocol/model-delay simulation, not production latency. Existing local RTC server left running. All workers are native Rust; Python is only the test caller/fixture.','baseline_sha256':hashlib.sha256(before.read_bytes()).hexdigest(),'experimental_sha256':hashlib.sha256(after.read_bytes()).hexdigest(),'config_sha256':hashlib.sha256(config.read_bytes()).hexdigest(),'results':results}
    (output/'comparison.json').write_text(json.dumps(report,indent=2)+'\n')
asyncio.run(main())
PY
for sg_early_variant in before after interruption; do
 "$sg_early_python" bench/analyze.py "$sg_early_output/$sg_early_variant"
done
