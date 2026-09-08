#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="${TTS_CONTAINER_NAME:-tts}"
IMAGE="${TTS_IMAGE:-quay.io/ascend/vllm-omni@sha256:acba66221a39170cbc405c7e7aa69b73b1905e04ef92f04943a61bb3878d6761}"

if docker container inspect "${CONTAINER_NAME}" >/dev/null 2>&1; then
  echo "Container ${CONTAINER_NAME} already exists; refusing to replace it." >&2
  exit 1
fi

docker run -d -t \
  --privileged \
  --name "${CONTAINER_NAME}" \
  --net=host \
  --shm-size=4g \
  --device /dev/davinci0 \
  --device /dev/davinci1 \
  --device /dev/davinci2 \
  --device /dev/davinci3 \
  --device /dev/davinci4 \
  --device /dev/davinci5 \
  --device /dev/davinci6 \
  --device /dev/davinci7 \
  --device /dev/davinci_manager \
  --device /dev/devmm_svm \
  --device /dev/hisi_hdc \
  -v /usr/local/dcmi:/usr/local/dcmi \
  -v /usr/local/sbin:/usr/local/sbin \
  -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
  -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
  -v /etc/ascend_install.info:/etc/ascend_install.info \
  -v /data:/data \
  "${IMAGE}" bash
