#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 [--apply] [--target-root /data/scripts/dwt]" >&2
  echo "Without --apply this script only prints the planned file copies." >&2
}

apply=false
target_root=/data/scripts/dwt
while (($#)); do
  case "$1" in
    --apply) apply=true ;;
    --target-root)
      shift
      target_root="${1:?missing value for --target-root}"
      ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
  shift
done

stable_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

files=(
  "npu/asr/create-container.sh|qwen3-asr/create_docker.sh|0755"
  "npu/asr/start-realtime.sh|qwen3-asr/start_asr_realtime.sh|0755"
  "npu/asr/run-realtime.sh|qwen3-asr/run_asr_realtime.sh|0755"
  "npu/asr/qwen3_asr_realtime.py|qwen3-asr/overlays/qwen3_asr_realtime.py.cumulative|0644"
  "npu/asr/connection.py|qwen3-asr/overlays/connection.py.cumulative|0644"
  "npu/asr/vllm-realtime.patch|qwen3-asr/vllm-realtime.patch|0644"
  "npu/tts/create-container.sh|qwen3-tts/create_docker.sh|0755"
  "npu/tts/start-tts.sh|qwen3-tts/start_tts.sh|0755"
  "npu/tts/qwen3_tts_fidelity.yaml|qwen3-tts/qwen3_tts_fidelity.yaml|0644"
  "npu/llm/create-container.sh|qwen3.8-27b/create_docker.sh|0755"
  "npu/llm/start-vllm.sh|qwen3.8-27b/start_vllm.sh|0755"
)

for entry in "${files[@]}"; do
  IFS='|' read -r source relative mode <<<"${entry}"
  destination="${target_root}/${relative}"
  printf '%s -> %s\n' "${stable_root}/${source}" "${destination}"
  if [[ "${apply}" == true ]]; then
    install -d "$(dirname "${destination}")"
    install -m "${mode}" "${stable_root}/${source}" "${destination}"
  fi
done

if [[ "${apply}" != true ]]; then
  echo "Dry run only. Re-run with --apply after reviewing the destinations."
fi
