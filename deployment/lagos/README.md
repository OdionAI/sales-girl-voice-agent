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
- Wema English voice selection is stored in Agent Config as `tts_voice_id`.
  The worker applies the saved Wema cached ICL voice when no signed session
  override is present. The supported IDs are `wema-en-us-female`,
  `wema-en-us-male`, `wema-en-ng-female`, and `wema-en-ng-male`. The matching
  short-reference IDs append `-short` and are enrolled in the gateway. All
  Wema IDs use `x_vector_only_mode=false` and the same deterministic seed as
  the configured Helen clone. The main-branch Lagos release deploys this saved
  voice-ID handling to the live SAW worker.

The shared Lagos LLM can be switched for all agents through the repository
variable `RVC_LAB_LLM_PROVIDER` (`qwen` or `deepseek`; defaults to `qwen`).
DeepSeek uses `deepseek-flash` with thinking disabled. This is required for
tool-enabled conversational requests: DeepSeek rejects `tool_choice` while
thinking mode is enabled. The configured
`RVC_LAB_DEEPSEEK_REASONING_EFFORT` is sent as `none` while thinking is off.
Set the private Actions secret
`DEEPSEEK_API_KEY` before selecting DeepSeek. The release workflow passes the
secret through stdin to the deploy script; neither the key nor model reasoning
is written to source control or diagnostics. Roll back by setting the provider
variable to `qwen` and running the normal Lagos release workflow on `main`.

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
The same `main` workflow now releases the Wema voice-auth sidecar before the
voice worker. The sidecar source is `agent/voice_auth.py`,
`agent/voice_enroll_http.py`, `agent/wema_store.py`, and
`agent/wema_voiceprint.py`. Its image extends the existing Lagos model image,
which supplies the two mounted ONNX models. The workflow passes the
`WEMA_BLOB_CONNECTION_STRING` Actions secret through stdin; the deploy script
writes it to a root-only environment file on the app VM. It builds an image
labelled with the exact commit, switches the sidecar to `VOICE_AUTH_STORE=wema`,
checks health, and restores the previous local-store image if health fails.
The worker sends the dashboard-signed Wema `customer_id` to `/verify`. A
successful health check does not prove an enrolled caller matches; confirm with
a real Wema call before relying on transactions.

Before any Wema banking tool runs, the worker finishes any generated speech and
waits for its LiveKit playout queue to drain. If that speech did not acknowledge
the check, it speaks and drains a short application-owned wait cue before
starting authentication or bank I/O. Each sequential banking tool gets the same
check, so an earlier acknowledgement cannot mask a later silent gap. If a
background bank call still runs after eight seconds, it gives one progress
update once the voice pipeline is idle. The voice trace records caller barge-in
and session-disconnect reasons; these cancel unfinished speech because the
caller or call interrupted it.

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
`RVC_AICC_SIP_ROUTE`. The caller `+2348112812216` follows the `0301051` DID
route to the MoMo PSB feedback agent; its prior Leadway override is removed.
The separate `+2349040760651` Leadway caller route remains mapped to the
Leadway new-member check-in agent (`c4f4925d-0188-431e-a881-15533f6bb31b`),
with an explicit per-caller override so a carrier-rewritten shared DID cannot
take precedence for that caller. Other callers retain DID-first routing.
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

## Abdul Hausa Voice

An explicit saved `abdul` voice selects `hausa-ljspeech-vits-epoch271` through
the existing `/tts-hausa/v1/audio/speech` endpoint. It keeps SBPN ASR and the
configured LLM. Aisha, Adamu, Muhammad, legacy defaults and the TWB research
opt-in remain unchanged; Abdul is a separate fine-tuned LJSpeech checkpoint,
not a new TWB speaker ID.

The API converts this model's native 22050 Hz output to the call contract of
24000 Hz mono PCM16. The worker must not resample it again or label native
22050 Hz bytes as 24000 Hz. It verifies the PCM rate, `abdul` voice/speaker,
canonical model, and checkpoint SHA
`a92c761456510945bd1a556987c673bc7768546f97436eb840bf6e06b4f64dc4`
before forwarding audio. Streaming, seed 17 and speed 1 are retained. Failed
requests are not retried or substituted with another speaker.

Only enable Abdul agents after the public API and worker are deployed and a
real public PCM request passes these bindings. Unit tests alone do not prove
end-to-end call readiness or Hausa pronunciation quality.

## Igbo evaluation speakers

Igbo agents can save `chinedu` (Ibo1), `eze` (Ibo2) or `ada` (Ibo3).
The worker preserves that choice on every sentence and routes input through SBPN,
without changing the configured LLM. Legacy generic Igbo IDs resolve to Chinedu,
matching the endpoint default. Unknown explicit speakers fail rather than silently
switching voices. The model is `igbo-sorotts-three-voices-v1` at the existing
`/tts-igbo/v1/audio/speech` jumper route, 24 kHz mono PCM16 with `stream:true`.

This SoroTTS checkpoint is CC-BY-NC-SA-4.0 and is for noncommercial evaluation,
not commercial licensing clearance. Its current CPU deployment emits audio only
after fully generating each sentence; it is not token-streaming or real-time ready.
Only this route uses the endpoint's bounded 1800-second request timeout so the
old 60-second cap does not truncate generation. Caller cancellation still closes
the client stream; no retries or fallback to another speaker are performed.
