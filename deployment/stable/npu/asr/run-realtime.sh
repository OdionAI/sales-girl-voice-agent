#!/usr/bin/env bash
export QWEN3_ASR_REALTIME_SEGMENT_MODE="${QWEN3_ASR_REALTIME_SEGMENT_MODE:-cumulative}"
export QWEN3_ASR_REALTIME_SEGMENT_DURATION_S="${QWEN3_ASR_REALTIME_SEGMENT_DURATION_S:-0.8}"
export QWEN3_ASR_REALTIME_MAX_AUDIO_S="${QWEN3_ASR_REALTIME_MAX_AUDIO_S:-30}"
export QWEN3_ASR_REALTIME_LANGUAGE="${QWEN3_ASR_REALTIME_LANGUAGE:-English}"
set -euo pipefail

MODEL_DIR="${MODEL_DIR:-/data/models/Qwen3-ASR-1.7B}"
PORT="${PORT:-8093}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-8}"
export ASCEND_RT_VISIBLE_DEVICES="${ASCEND_RT_VISIBLE_DEVICES:-3}"
export VLLM_USE_MODELSCOPE=false

case "${MAX_MODEL_LEN}" in
  ''|*[!0-9]*) echo "MAX_MODEL_LEN must be numeric" >&2; exit 2 ;;
esac
case "${MAX_NUM_SEQS}" in
  ''|*[!0-9]*) echo "MAX_NUM_SEQS must be numeric" >&2; exit 2 ;;
esac

mkdir -p /data/logs/qwen3-asr-realtime
cd /data/logs/qwen3-asr-realtime
ulimit -n 65536

exec vllm serve "${MODEL_DIR}" \
  --served-model-name Qwen3-ASR \
  --hf-overrides '{"architectures": ["Qwen3ASRRealtimeGeneration"]}' \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --tensor-parallel-size 1 \
  --max-model-len "${MAX_MODEL_LEN}" \
  --max-num-seqs "${MAX_NUM_SEQS}" \
  --trust-remote-code \
  --gpu-memory-utilization 0.85 \
  --no-async-scheduling \
  --allowed-local-media-path /
