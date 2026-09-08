#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${1:-}" ]]; then
  echo "Usage: $0 docker.io/organization [--push]" >&2
  exit 2
fi

registry="${1%/}"
push=false
[[ "${2:-}" == "--push" ]] && push=true
[[ -z "${2:-}" || "${2:-}" == "--push" ]] || { echo "Unknown option: $2" >&2; exit 2; }

sources=(
  "quay.io/ascend/vllm-ascend@sha256:4ee78def8f33d59d48f116d1dfa793332c23c99ecab4f0d7dd5cd62d0fb4e6c1"
  "quay.io/ascend/vllm-ascend@sha256:9008b47081282612abfe4d28069ce34436752c980fd06f7599343213205ce64d"
  "quay.io/ascend/vllm-omni@sha256:acba66221a39170cbc405c7e7aa69b73b1905e04ef92f04943a61bb3878d6761"
)
targets=(
  "${registry}/odion-vllm-llm:v0.23.0-stable-20260902"
  "${registry}/odion-vllm-asr:v0.22.1rc1-stable-20260902"
  "${registry}/odion-vllm-tts:v0.25.0-stable-20260902"
)

for index in "${!sources[@]}"; do
  source_image="${sources[$index]}"
  target_image="${targets[$index]}"
  docker pull "${source_image}"
  docker tag "${source_image}" "${target_image}"
  echo "Tagged ${source_image} as ${target_image}"
  if [[ "${push}" == true ]]; then
    docker push "${target_image}"
  fi
done

if [[ "${push}" != true ]]; then
  echo "Images were tagged only. Authenticate, review, then re-run with --push."
fi
