#!/usr/bin/env bash
set -euo pipefail

expected_name=helen-mavino-0030_user-approved-2026-08-25_1787674731.safetensors
expected_sha=80f7f1af82958a2b878f8c2d20d53209fe944af4cd1af8e5cca5fec3d29defa4
destination_dir=/root/.cache/vllm-omni/speakers

if [[ "${1:-}" != "--apply" || -z "${2:-}" ]]; then
  cat >&2 <<EOF
Usage: $0 --apply /private/path/${expected_name} [container-name]

The voice profile contains biometric audio and must come from private storage.
This script verifies its checksum before copying it into the TTS container.
EOF
  exit 2
fi

source_file="$2"
container="${3:-tts}"
actual_sha="$(sha256sum "${source_file}" | awk '{print $1}')"
if [[ "${actual_sha}" != "${expected_sha}" ]]; then
  echo "Voice profile checksum mismatch: expected ${expected_sha}, got ${actual_sha}" >&2
  exit 1
fi

docker container inspect "${container}" >/dev/null
docker exec "${container}" mkdir -p "${destination_dir}"
docker cp "${source_file}" "${container}:${destination_dir}/${expected_name}"
docker exec "${container}" sha256sum "${destination_dir}/${expected_name}"

echo "Restored private voice profile ${expected_name} to container ${container}."
