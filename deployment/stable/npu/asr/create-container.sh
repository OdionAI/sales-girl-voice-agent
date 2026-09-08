#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="${ASR_CONTAINER_NAME:-asr}"
IMAGE="${ASR_IMAGE:-quay.io/ascend/vllm-ascend@sha256:9008b47081282612abfe4d28069ce34436752c980fd06f7599343213205ce64d}"

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
