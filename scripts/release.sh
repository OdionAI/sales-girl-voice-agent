#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/release.sh <staging|prod|main> [-m "commit message"] [--skip-tests]
EOF
}

TARGET_ENV="${1:-}"
shift || true
COMMIT_MESSAGE=""
SKIP_TESTS="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -m|--message) COMMIT_MESSAGE="${2:-}"; shift 2 ;;
    --skip-tests) SKIP_TESTS="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

cat >&2 <<'EOF'
This script is disabled for staging/prod deployment work.

Use the branch-driven GitHub Actions flow instead:
- push or merge to `staging` for staging deploys
- push or merge to `main` for production deploys

If you need to inspect rollout status, use `gh run list/view`, `gcloud compute
ssh`, and runtime health checks instead of this script.
EOF
exit 1

case "${TARGET_ENV}" in
  staging) TARGET_BRANCH="staging"; DEPLOY_ARG="staging" ;;
  prod|main) TARGET_BRANCH="main"; DEPLOY_ARG="prod" ;;
  *) usage; exit 1 ;;
esac

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "${REPO_ROOT}"

DIRTY_STATE="$(git status --porcelain)"
if [[ -n "${DIRTY_STATE}" ]]; then
  if [[ -z "${COMMIT_MESSAGE}" ]]; then
    echo "Working tree has changes. Re-run with -m \"commit message\" to include them in the release." >&2
    exit 1
  fi
  git add -A
  git commit -m "${COMMIT_MESSAGE}"
fi

if [[ "${SKIP_TESTS}" != "true" && command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 ]]; then
  docker build -t sales-girl-voice-agent:ci .
elif [[ "${SKIP_TESTS}" != "true" ]]; then
  echo "Docker daemon unavailable; skipping local voice-agent image sanity build."
fi

git push origin "HEAD:${TARGET_BRANCH}"
"${REPO_ROOT}/scripts/deploy-from-cli.sh" "${DEPLOY_ARG}"
