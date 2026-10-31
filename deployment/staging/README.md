# Staging deployment (`odion-staging`)

Deploys the native Rust voice worker from the `dev` branch to the staging VM
(`attentive-staging-server`, private `10.40.40.155`, EIP `83.101.48.66`).

## Relationship to Lagos

`deployment/staging/` mirrors `deployment/lagos/`. The staging stack is a copy
of the Lagos Compose stack (project `odion-staging`, working dir
`/opt/attentive/releases/recovery-20261009`) plus a pinned-image override
(`staging-images.override.yaml`). Only the `voice` service is replaced on each
release; everything else keeps its prod-mirror image.

Differences from Lagos, by design:

- **SIP is not wired.** The `sip` service is not started on staging so the
  staging worker never registers the prod AICC trunk or competes for prod DIDs.
  Staging is exercised through the browser/LiveKit path.
- **TTS/ASR/LLM reuse prod** (`102.88.137.124`), so the copied `runtime.env`
  works unchanged.
- `deploy.py` therefore omits the Lagos SIP-route and AICC capacity mutations;
  it only rebuilds/recreates the `voice` service.

## CI

`.github/workflows/deploy-staging.yml` deploys `dev` on every push (and via
manual dispatch). It authenticates through the jump host with the
`STAGING_SSH_PRIVATE_KEY` secret; add `[skip deploy]` to a commit to suppress a
run. Manual dispatch defaults to a read-only `--verify-only` check.

Required repository configuration:

- Secret `STAGING_SSH_PRIVATE_KEY` — private key whose public half is authorized
  on both the jump host (`83.101.50.32`) and the staging VM.
- Secrets `DEEPSEEK_API_KEY` (only when `RVC_LAB_LLM_PROVIDER=deepseek`).
- Optional variables: `RVC_LAB_LLM_PROVIDER`, `RVC_LAB_DEEPSEEK_MODEL`,
  `RVC_LAB_DEEPSEEK_REASONING_EFFORT`, `RVC_LAB_REPLY_GRACE_MS`,
  `RVC_LAB_REPLY_GRACE_AGENT_IDS`, `ENABLE_HAUSA_TWB_VOICES`.

## First-time stack bring-up (already performed)

```sh
cd /opt/attentive/releases/recovery-20261009
docker compose -p odion-staging -f compose.yaml -f staging-images.override.yaml \
  up -d --no-build redis livekit config auth conversations billing notifications \
  knowledge banking voice-auth text-chat dashboard voice egress gateway
```
