#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
namespace="${1:-local}"
push=false
[[ "${2:-}" == "--push" ]] && push=true
[[ -z "${2:-}" || "${2:-}" == "--push" ]] || { echo "Unknown option: $2" >&2; exit 2; }

images=(asr tts llm)
for service in "${images[@]}"; do
  tag="${namespace%/}/odion-${service}:stable-20260902"
  docker build \
    --platform linux/arm64 \
    -f "${repo_root}/deployment/stable/images/${service}.Dockerfile" \
    -t "${tag}" \
    "${repo_root}"
  if [[ "${push}" == true ]]; then
    docker push "${tag}"
  fi
done

if [[ "${push}" != true ]]; then
  echo "Derived images built locally. Re-run with a registry namespace and --push to publish."
fi
