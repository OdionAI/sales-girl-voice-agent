#!/usr/bin/env bash
set -euo pipefail

export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export HCCL_BUFFSIZE=512
export OMP_PROC_BIND=false
export OMP_NUM_THREADS=1
export ASCEND_RT_VISIBLE_DEVICES="${ASCEND_RT_VISIBLE_DEVICES:-1,2}"

MODEL_DIR="${MODEL_DIR:-/data/models/Qwen3.8-27B-w8a8}"
PORT="${PORT:-8095}"

exec vllm serve "${MODEL_DIR}" \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --data-parallel-size 1 \
    --tensor-parallel-size 2 \
    --quantization ascend \
    --served-model-name qwen3.8_27b \
    --max-num-seqs 32 \
    --max-model-len 131072 \
    --max-num-batched-tokens 16384 \
    --trust-remote-code \
    --enable-auto-tool-choice \
    --tool-call-parser qwen3_xml \
    --reasoning-parser qwen3 \
    --enable-prefix-caching \
    --gpu-memory-utilization 0.85 \
    --speculative-config '{"method": "qwen3_5_mtp", "num_speculative_tokens": 3, "enforce_eager": true}' \
    --compilation-config '{"cudagraph_mode":"FULL_DECODE_ONLY"}' \
    --additional-config '{"enable_cpu_binding":true}'
