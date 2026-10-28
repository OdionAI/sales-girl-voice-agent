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
- **STT** `Qwen3-ASR` chat transcription is authoritative for Wema SAW's
  business and the intended Amaka agent; an empty or failed chat transcript falls back to the
  realtime Qwen final. Other businesses remain on the existing realtime/batch
  path. The vocabulary context is preserved from the active environment.
  **LLM** local `qwen3.8-27b`, **TTS** `Qwen3-TTS` — these are runtime env
  (`RVC_LAB_*`), not baked into the image.

## Rollback
Keep the previous image tag and the exact `-f` chain. Rollback = recreate `voice` from the
previous chain (or the earlier override). Do not prune rollback images.

The optional reply-grace trial is controlled by GitHub repository variables
`RVC_LAB_REPLY_GRACE_AGENT_IDS` and `RVC_LAB_REPLY_GRACE_MS`. The Lagos release
passes them into the voice runtime environment; it is off when the ID list is
empty or the delay is `0`. To stop the trial, set the delay to `0` and run the
normal Lagos release workflow with `verify_only=false` on `main`. The active
Compose environment is shared by agents, so the ID list limits the trial to
the selected agent rather than changing all calls. The release checks both
settings and restores the previous values if deployment fails.

## GitHub release
`.github/workflows/deploy-lagos.yml` is the Lagos path. A `main` push starts a
release unless the commit message contains `[skip deploy]`. Manual workflow
dispatch defaults to `verify_only=true` and checks the pinned SSH route,
Compose project, active file chain and container health without a release.

For a release, the workflow tests the source, uploads an immutable commit-SHA
archive, builds from `deployment/lagos/Dockerfile`, checks `pyrnnoise` and worker
imports, and adds an image override to the active Compose chain. It
recreates only `voice`, checks the new image and container health, environment
and mounts, and restores the prior image if verification fails. A post-release
real call still needs to confirm worker registration and the RNNoise trace.
`deployment/lagos/deploy.py` also maintains explicit caller-number routes in
`RVC_AICC_SIP_ROUTE`. Leadway caller IDs resolve to the Leadway new-member
check-in agent (`c4f4925d-0188-431e-a881-15533f6bb31b`), with an explicit
per-caller override so a carrier-rewritten shared DID cannot take precedence
for those callers. Other callers retain DID-first
routing. Existing routes are preserved; conflicting numbers fail the release
instead of being silently reassigned.
`deploy.yml` remains available only for an explicit legacy node-B run.

For the explicitly opted-in noncommercial Hausa research pilot, set the GitHub
repository variable `ENABLE_HAUSA_TWB_VOICES=true` before promoting to `main`.
That release adds only `RVC_LAB_ENABLE_HAUSA_TWB_VOICES=true` to the worker's
environment; all other environment values and mounts must remain identical.
Failed releases restore both the prior image and the prior toggle value (or
remove it if previously absent). Without that repository variable the workflow
preserves the current environment. Pair this with the dashboard production
build flag `NEXT_PUBLIC_ENABLE_HAUSA_TWB_VOICES=true`. These are account-wide
research controls, not commercial licensing clearance.
