#!/usr/bin/env bash
# Isolated Linux packaging/registration/readiness check. No production accounts,
# database, model provider, trunk or financial operation is used.
set -euo pipefail
image="${1:?usage: test-native-runtime.sh IMAGE}"
suffix="$$"
network="sg-native-ci-$suffix"
livekit="sg-native-lk-$suffix"
worker="sg-native-worker-$suffix"
cleanup() {
  docker logs "$worker" 2>/dev/null || true
  docker rm -f "$worker" "$livekit" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT
settings=(--env LIVEKIT_API_KEY=devkey --env LIVEKIT_API_SECRET=secret
  --env AGENT_CONFIG_API_BASE_URL=https://unused.example.test
  --env AGENT_CONFIG_SERVICE_TOKEN=public-native-ci-fixture
  --env RVC_LAB_LIVEKIT_AGENT_NAME=native-ci-worker
  --env RVC_LAB_ALLOW_INSECURE_LIVEKIT=true)
docker run --rm --network none "${settings[@]}" --env LIVEKIT_URL=wss://unused.example.test "$image" --check-env
docker network create "$network" >/dev/null
docker run -d --name "$livekit" --network "$network" livekit/livekit-server:v1.13.6 --dev --bind 0.0.0.0 >/dev/null
docker run -d --name "$worker" --network "$network" "${settings[@]}" --env "LIVEKIT_URL=ws://$livekit:7880" "$image" >/dev/null
ready() { docker exec "$worker" /usr/local/bin/rvc-worker --healthcheck >/dev/null 2>&1; }
for _ in {1..45}; do if ready; then break; fi; sleep 1; done
ready
docker stop "$livekit" >/dev/null
for _ in {1..20}; do if ! ready; then break; fi; sleep 1; done
if ready; then echo 'Readiness did not reject disconnected worker' >&2; exit 1; fi
docker start "$livekit" >/dev/null
for _ in {1..45}; do if ready; then break; fi; sleep 1; done
ready
echo 'Native Linux registration, disconnect readiness and reconnect passed.'
