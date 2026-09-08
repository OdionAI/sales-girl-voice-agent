#!/usr/bin/env bash
set -euo pipefail

port_is_listening() {
  ss -lntH 2>/dev/null | awk '{print $4}' | grep -Eq "(^|:)${1}$"
}

start_in_container() {
  local container="$1"
  local port="$2"
  local npu="$3"
  local command="$4"
  local log="$5"

  docker container inspect "${container}" >/dev/null
  if port_is_listening "${port}"; then
    echo "Port ${port} is already listening; leaving ${container} untouched."
    return
  fi

  docker exec -d \
    -e ASCEND_RT_VISIBLE_DEVICES="${npu}" \
    "${container}" bash -lc \
    "mkdir -p \"$(dirname "${log}")\"; nohup ${command} > \"${log}\" 2>&1 & echo \$! > \"${log}.pid\""
  echo "Started ${container} on NPU ${npu}, port ${port}; log: ${log}"
}

start_in_container \
  tts 8091 0 \
  /data/scripts/dwt/qwen3-tts/start_tts.sh \
  /data/logs/qwen3-tts/serve-8091.log

start_in_container \
  qwen3.8_27b 8095 1,2 \
  /data/scripts/dwt/qwen3.8-27b/start_vllm.sh \
  /data/logs/qwen3.8-27b/serve-8095.log

QWEN3_ASR_REALTIME_SEGMENT_MODE=cumulative \
QWEN3_ASR_REALTIME_SEGMENT_DURATION_S=0.8 \
QWEN3_ASR_REALTIME_MAX_AUDIO_S=30 \
QWEN3_ASR_REALTIME_LANGUAGE=English \
ASCEND_RT_VISIBLE_DEVICES=3 \
PORT=8093 \
  /data/scripts/dwt/qwen3-asr/start_asr_realtime.sh
