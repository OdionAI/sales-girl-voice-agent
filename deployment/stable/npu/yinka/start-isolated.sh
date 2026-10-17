#!/usr/bin/env bash
set -euo pipefail

# Run on node1 only, after checking this physical NPU is idle. Never replaces live containers.
: "${TEST_NPU:?Set TEST_NPU to an unused physical NPU}"
: "${TEST_ROOT:?Set TEST_ROOT to the absolute test artifact directory}"
name="${TEST_CONTAINER:-odion-yinka-stream-test-20261018}"
port="${TEST_PORT:-8919}"
image="${TEST_IMAGE:-sha256:4300d9c34beeff15b37ef30cd87a426e3230b613e76fb48ef5f10a84e5673a31}"
config="${TEST_DEPLOY_CONFIG:-production-deploy.yaml}"
[[ "$TEST_NPU" =~ ^[0-7]$ && "$port" =~ ^[0-9]+$ && "$TEST_ROOT" = /* ]]
[[ "$config" =~ ^[a-zA-Z0-9_-]+\.yaml$ ]]
if [[ "$name" != odion-yinka-stream-test-* || "$port" == 8909 ]]; then
  echo "Refusing production container name or port" >&2
  exit 1
fi
if docker container inspect "$name" >/dev/null 2>&1; then
  echo "Container already exists: $name; inspect it before replacing it" >&2
  exit 1
fi

docker run -d --name "$name" --entrypoint /bin/bash --network host \
  --shm-size 16g --privileged --security-opt label=disable \
  --device "/dev/davinci${TEST_NPU}" --device /dev/davinci_manager \
  --device /dev/devmm_svm --device /dev/hisi_hdc \
  -e "ASCEND_RT_VISIBLE_DEVICES=$TEST_NPU" -e OMP_NUM_THREADS=1 \
  -v /usr/local/Ascend/driver:/usr/local/Ascend/driver:ro \
  -v /usr/local/dcmi:/usr/local/dcmi:ro \
  -v /usr/local/sbin:/usr/local/sbin:ro \
  -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi:ro \
  -v /etc/ascend_install.info:/etc/ascend_install.info:ro \
  -v /data/finetune/qwen3_tts/runs/yinka_customer_service_20261016_v7_batch2_lr2e5_e3/checkpoint-epoch-2:/models/Qwen3-TTS-12Hz-0.6B-Yinka-CustomVoice:ro \
  -v "$TEST_ROOT/config:/test-config:ro" \
  -v "$TEST_ROOT/results:/test-results" \
  "$image" -lc "exec vllm-omni serve /models/Qwen3-TTS-12Hz-0.6B-Yinka-CustomVoice --deploy-config /test-config/$config --host 127.0.0.1 --port $port --served-model-name yinka-v7-test --trust-remote-code --omni"
