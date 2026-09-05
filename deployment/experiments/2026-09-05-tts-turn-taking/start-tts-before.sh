#!/bin/bash

export ASCEND_RT_VISIBLE_DEVICES=0
vllm-omni serve /data/models/Qwen3-TTS-12Hz-1.7B-Base --deploy-config /data/scripts/dwt/qwen3-tts/qwen3_tts_fidelity.yaml --host 0.0.0.0 --port 8091 --served-model-name Qwen3-TTS --allowed-local-media-path / --trust-remote-code --omni
