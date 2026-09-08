#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" != "--apply" ]]; then
  cat >&2 <<'EOF'
This changes files inside the ASR container but does not restart it.
Usage: apply-asr-overlays.sh --apply [container-name]
Use the derived ASR image instead when rebuilding from scratch.
EOF
  exit 2
fi

container="${2:-asr}"
stable_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
model_target=/workspace/vllm/vllm/model_executor/models/qwen3_asr_realtime.py
connection_target=/workspace/vllm/vllm/entrypoints/speech_to_text/realtime/connection.py

docker container inspect "${container}" >/dev/null
docker cp "${stable_root}/npu/asr/qwen3_asr_realtime.py" "${container}:${model_target}"
docker cp "${stable_root}/npu/asr/connection.py" "${container}:${connection_target}"

docker exec "${container}" sha256sum "${model_target}" "${connection_target}"
cat <<'EOF'
Overlays copied. A running ASR process still has the old Python modules loaded;
restart only during an approved maintenance window.
EOF
