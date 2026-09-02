#!/usr/bin/env bash
set -euo pipefail

endpoint="${QWEN_TTS_ENDPOINT:-http://102.88.137.124:8080/tts/v1/audio/speech}"
output="${1:-/tmp/odion-stable-tts-smoke.pcm}"
text="${2:-Hello, this is Helen from Odion. The stable voice service is reachable.}"
payload="$(jq -cn --arg input "${text}" '{input:$input,model:"Qwen3-TTS",task_type:"Base",voice:"helen-mavino-0030",language:"English",x_vector_only_mode:false,response_format:"pcm",stream:true,stream_format:"audio",initial_codec_chunk_frames:2}')"

curl --fail-with-body -sS "${endpoint}" \
  -H 'Content-Type: application/json' \
  -d "${payload}" \
  --output "${output}"

bytes="$(wc -c < "${output}" | tr -d ' ')"
[[ "${bytes}" -gt 0 ]] || { echo "TTS returned no audio" >&2; exit 1; }
echo "TTS wrote ${bytes} PCM bytes to ${output}."
