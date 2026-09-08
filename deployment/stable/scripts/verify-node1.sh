#!/usr/bin/env bash
set -euo pipefail

stable_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
verify_weights=false
if [[ "${1:-}" == "--weights" ]]; then
  verify_weights=true
elif [[ -n "${1:-}" ]]; then
  echo "Usage: $0 [--weights]" >&2
  exit 2
fi

[[ "$(uname -m)" == "aarch64" ]] || {
  echo "Warning: captured runtime platform is aarch64; current host is $(uname -m)." >&2
}

for image_id in \
  sha256:4ee78def8f33d59d48f116d1dfa793332c23c99ecab4f0d7dd5cd62d0fb4e6c1 \
  sha256:9008b47081282612abfe4d28069ce34436752c980fd06f7599343213205ce64d \
  sha256:acba66221a39170cbc405c7e7aa69b73b1905e04ef92f04943a61bb3878d6761
do
  docker image inspect "${image_id}" >/dev/null
  echo "Image present: ${image_id}"
done

for path in \
  /data/models/Qwen3-ASR-1.7B \
  /data/models/Qwen3-TTS-12Hz-1.7B-Base \
  /data/models/Qwen3.8-27B-w8a8
do
  [[ -d "${path}" ]] || { echo "Missing model directory: ${path}" >&2; exit 1; }
done

if [[ "${verify_weights}" == true ]]; then
  echo "Verifying large model weights. This is read-only but can take several minutes."
  sha256sum -c "${stable_root}/manifests/model-weights-asr-llm.sha256"
  sha256sum -c "${stable_root}/manifests/model-weights-tts.sha256"
fi

echo "Node 1 prerequisites verified. Use --weights for full model checksum verification."
