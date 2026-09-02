#!/usr/bin/env bash
set -euo pipefail

export ASCEND_RT_VISIBLE_DEVICES="${ASCEND_RT_VISIBLE_DEVICES:-0}"

MODEL_DIR="${MODEL_DIR:-/data/models/Qwen3-TTS-12Hz-1.7B-Base}"
DEPLOY_CONFIG="${DEPLOY_CONFIG:-/data/scripts/dwt/qwen3-tts/qwen3_tts_fidelity.yaml}"
PORT="${PORT:-8091}"

exec vllm-omni serve "${MODEL_DIR}" \
  --deploy-config "${DEPLOY_CONFIG}" \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --served-model-name Qwen3-TTS \
  --allowed-local-media-path / \
  --trust-remote-code \
  --omni
