# Lagos voice worker — release & deploy

This is the release path for the **Lagos voice worker** (the orchestration + model-endpoint
config that answers calls on https://attentive.odion.ai). It is distinct from the older
node-B Huawei worker.

## What ships
The worker image is built **from this repo at `main`** (or any SHA), using
[`deployment/lagos/Dockerfile`](./Dockerfile), which mirrors the box's `Python.Dockerfile`.
The image is labelled with its source so every deployment is traceable:

```
org.opencontainers.image.source = https://github.com/OdionAI/sales-girl-voice-agent
org.opencontainers.image.revision = <full git SHA>
```

## Build
```
docker build -f deployment/lagos/Dockerfile \
  --label org.opencontainers.image.revision=$(git rev-parse HEAD) \
  --label org.opencontainers.image.source=https://github.com/OdionAI/sales-girl-voice-agent \
  -t attentive/voice:lagos-$(git rev-parse --short HEAD) .
```

## Active stack
The Lagos stack is a Compose project on the private app VM reached through the jump host:
`odion-recovery-lagos`, release dir `/opt/attentive/releases/recovery-20261009`, service `voice`.

Agents should pull GitHub `main`, make changes on a branch, and promote through
`dev` and `staging` before merging to `main`. Do not deploy directly to the VM.

## Must-not-lose behavior (verify on every release)
- **RNNoise input-noise suppression** — `latency_lab/input_noise.py` (with the `*` global
  allowlist and `ListeningNoiseRoute`), wired in `latency_lab/livekit_worker.py`, and the
  `pyrnnoise` dependency in the freeze. If `pyrnnoise` is missing the filter fails **open**
  (`implementation: off`) and the trace shows **no** `input_noise_filter_ready` event.
- **STT** `Qwen3-ASR` (realtime final), **LLM** local `qwen3.8-27b`, **TTS** `Qwen3-TTS`
  (`helen-mavino-0030`) — these are runtime env (`RVC_LAB_*`), not baked into the image.

## Rollback
Keep the previous image tag and the exact `-f` chain. Rollback = recreate `voice` from the
previous chain (or the earlier override). Do not prune rollback images.

## GitHub release
`.github/workflows/deploy-lagos.yml` is the Lagos path. A `main` push starts a
release only when `LAGOS_VOICE_AUTO_DEPLOY=true` is set in the repository and
the commit message does not contain `[skip deploy]`. The gate is currently off:
installing this workflow does not restart the running worker. Manual workflow
dispatch defaults to `verify_only=true` and checks the pinned SSH route,
Compose project, active file chain and container health without a release.

For a release, the workflow tests the source, uploads an immutable commit-SHA
archive, builds from `deployment/lagos/Dockerfile`, checks `pyrnnoise` and worker
imports, and adds an image-only override to the active Compose chain. It
recreates only `voice`, checks the new image and container health, environment
and mounts, and restores the prior image if verification fails. A post-release
real call still needs to confirm worker registration and the RNNoise trace.
`deploy.yml` remains available only for an explicit legacy node-B run.
