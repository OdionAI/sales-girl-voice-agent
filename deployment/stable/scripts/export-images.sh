#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${1:-}" ]]; then
  echo "Usage: $0 /private/backup/directory" >&2
  exit 2
fi

output_dir="$1"
mkdir -p "${output_dir}"

docker image save \
  -o "${output_dir}/odion-llm-vllm-ascend-v0.23.0.tar" \
  quay.io/ascend/vllm-ascend@sha256:4ee78def8f33d59d48f116d1dfa793332c23c99ecab4f0d7dd5cd62d0fb4e6c1
docker image save \
  -o "${output_dir}/odion-asr-vllm-ascend-v0.22.1rc1.tar" \
  quay.io/ascend/vllm-ascend@sha256:9008b47081282612abfe4d28069ce34436752c980fd06f7599343213205ce64d
docker image save \
  -o "${output_dir}/odion-tts-vllm-omni-v0.25.0.tar" \
  quay.io/ascend/vllm-omni@sha256:acba66221a39170cbc405c7e7aa69b73b1905e04ef92f04943a61bb3878d6761

sha256sum "${output_dir}"/*.tar > "${output_dir}/docker-images.sha256"
echo "Image archives and checksums written to ${output_dir}. Do not commit them to Git."
