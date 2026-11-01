#!/usr/bin/env bash
# Local Docker validation; no published ports, production settings, or credentials.
set -euo pipefail
cd "$(dirname "$0")/.."
sg_rvc_artifacts=${SG_RVC_ARTIFACTS:-artifacts}
mkdir -p "$sg_rvc_artifacts"
sg_rvc_image=${SG_RVC_IMAGE:-sg-rvc-local:rust-review}
if [[ ${1:-} != --skip-build ]]; then
 docker build --progress=plain -t "$sg_rvc_image" . > "$sg_rvc_artifacts/linux-build-review.log" 2>&1
fi
sg_rvc_server=''
cleanup() {
 if [[ -n "$sg_rvc_server" ]]; then
  docker logs "$sg_rvc_server" > "$sg_rvc_artifacts/linux-livekit-review.log" 2>&1 || true
  docker rm -f "$sg_rvc_server" >/dev/null 2>&1 || true
 fi
}
trap cleanup EXIT
sg_rvc_server=$(docker run -d --rm --label sg-rvc-local-review=true --read-only --tmpfs /tmp --cap-drop ALL \
 -v "$PWD/local-livekit.yaml:/etc/livekit.yaml:ro" \
 livekit/livekit-server:v1.13.6 --dev --bind 127.0.0.1 --config /etc/livekit.yaml)
for _ in {1..100}; do
 if docker logs "$sg_rvc_server" 2>&1 | rg -q 'starting LiveKit server'; then break; fi
 if [[ $(docker inspect -f '{{.State.Running}}' "$sg_rvc_server") != true ]]; then exit 1; fi
 sleep .1
done
for sg_rvc_bin in worker-control-smoke native-worker-smoke; do
 docker run --rm --read-only --tmpfs /tmp --cap-drop ALL --network "container:$sg_rvc_server" \
  --entrypoint "/usr/local/bin/$sg_rvc_bin" "$sg_rvc_image" > "$sg_rvc_artifacts/linux-$sg_rvc_bin-review.log" 2>&1
done
docker image inspect "$sg_rvc_image" --format '{{.Id}} {{.Architecture}} {{json .Config.User}} {{json .Config.Entrypoint}}' > "$sg_rvc_artifacts/linux-image-review.txt"
docker image inspect livekit/livekit-server:v1.13.6 --format '{{json .RepoDigests}} {{.Architecture}}' >> "$sg_rvc_artifacts/linux-image-review.txt"
cat "$sg_rvc_artifacts/linux-worker-control-smoke-review.log" "$sg_rvc_artifacts/linux-native-worker-smoke-review.log"
