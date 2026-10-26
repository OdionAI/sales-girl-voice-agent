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

## Deploy (Lagos box, manual / scripted fallback)
The Lagos stack is a Compose project on the private app VM reached through the jump host:
`odion-recovery-lagos`, release dir `/opt/attentive/releases/recovery-20261009`, service `voice`.

1. Stage the source on the box: `sources/voice-<short-sha>/`.
2. Build there with the release recipe (same Dockerfile), tag `attentive/voice:lagos-<short-sha>-N`.
3. Pin it with a trailing override and recreate **only** voice, keeping the whole active `-f` chain:
   ```
   printf 'services:\n  voice:\n    image: attentive/voice:lagos-<short-sha>-N\n' \
     > voice-lagos-<date>.override.yaml
   # read the ACTIVE chain from the running container and append the new override LAST
   docker compose -p odion-recovery-lagos -f <every active file> -f ./voice-lagos-<date>.override.yaml up -d voice
   ```
4. Verify: container healthy, worker registered as `rvc-livekit-comparison`, and a real call
   shows `input_noise_filter_ready {enabled:true, implementation:rnnoise}`.

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

## Automated deploy (pending)
There is currently no Lagos deploy workflow in `.github/workflows/`. The existing
`deploy.yml` targets the older Huawei node-B voice workers. A future Lagos
workflow must build from a reviewed Git commit, preserve the active Compose
configuration, verify the registered worker and call path, and retain the prior
image for rollback. Until that workflow is installed and verified, use the
manual release procedure above.
