#!/usr/bin/env bash
# Start the verified realtime ASR profile without replacing a running process.
set -euo pipefail
PORT="${PORT:-8093}"
NPU="${ASCEND_RT_VISIBLE_DEVICES:-3}"
LOG_DIR=/data/logs/qwen3-asr-realtime
LOG="${LOG_DIR}/serve-${PORT}.log"
INNER=/data/scripts/dwt/qwen3-asr/run_asr_realtime.sh

if ss -lntp 2>/dev/null | grep -q ":${PORT} "; then
  echo "Port ${PORT} is already listening; not starting a second process."
  exit 0
fi

mkdir -p "${LOG_DIR}"
chmod +x "${INNER}"

docker exec -d \
  -e ASCEND_RT_VISIBLE_DEVICES="${NPU}" \
  -e PORT="${PORT}" \
  -e QWEN3_ASR_REALTIME_SEGMENT_MODE="${QWEN3_ASR_REALTIME_SEGMENT_MODE:-cumulative}" \
  -e QWEN3_ASR_REALTIME_SEGMENT_DURATION_S="${QWEN3_ASR_REALTIME_SEGMENT_DURATION_S:-0.8}" \
  -e QWEN3_ASR_REALTIME_MAX_AUDIO_S="${QWEN3_ASR_REALTIME_MAX_AUDIO_S:-30}" \
  -e QWEN3_ASR_REALTIME_LANGUAGE="${QWEN3_ASR_REALTIME_LANGUAGE:-English}" \
  asr bash -lc "nohup ${INNER} > ${LOG} 2>&1 & echo \$! > ${LOG_DIR}/serve-${PORT}.pid"

echo "Started realtime ASR sidecar on NPU ${NPU} :${PORT}"
echo "Log: ${LOG}"
echo "WebSocket: ws://127.0.0.1:${PORT}/v1/realtime"
