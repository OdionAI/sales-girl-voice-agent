# Lagos production reconciliation — 2026-11-14

This records the read-only production comparison and the source reconciliation
prepared for review. No production deployment was made during this audit.

## Source comparison

- Active Compose project/service: `odion-recovery-lagos` / `voice`.
- Running image revision: `f1d5a8d6c9cadf412b60c25d416354d61b5e4a15`.
- Current `main` before reconciliation: `36413e6f7f75e1fb183af409901fa5fed7eee46f`.
- The live image revision was not contained in `main`; it was six commits ahead
  of that `main` revision on `feat/demo-airtime-tool`.
- Those production commits supply the operator-configured airtime purchase
  tool, its `/v1/airtime` route handling and Zuri prompt, and per-agent
  end-of-utterance timing with the Zuri agent scope.
- The reconciliation branch merges that exact production source history and
  carries the reviewed `2427c7a` recording codec fix. The fix selects MP3 for
  MP3 output, AAC for MP4, and Opus for OGG, resolving the Egress `no common
  format` error caused by the old MP3/Opus mismatch.

## Live Compose and recording settings

At inspection, the worker was healthy and Docker reported 100 Compose files in
its active config chain. The chain starts from
`/opt/attentive/releases/recovery-20261009/compose.yaml` and includes the
following host-managed overrides (along with immutable `voice-ci.json` release
files):

- `igbo-ebuka-20261024.override.yaml`
- `ella-passkey-20261024-10.override.yaml`
- `empty-retry-20261025.override.yaml`
- `stt-hint-20261026.override.yaml`
- `tts-seed-20261026.override.yaml`
- `transfer-stall-20261026.override.yaml`
- `msgfix-20261026.override.yaml`
- `budget-20261027.override.yaml`
- `leadway-checkin-route-20261028T173722Z.override.json`
- `momo-call-capacity-20261029.override.json`
- `voice-auth-20261029.override.yaml`
- `asr-ticket-all-20261101.override.yaml`
- `aicc-target-20261102.override.yaml`
- `momo-language-routes.override.json`
- `airtime-stt-hint-20261112.override.yaml`
- `zuri-endpause-20261112.override.yaml`
- `zuri-endpause-global-test.override.yaml`

The live container had `LIVEKIT_RECORDING_ENABLED=true`,
`LIVEKIT_RECORDING_FORMAT=mp3`, `LIVEKIT_RECORDING_LOCAL_DIRECTORY=/recordings`,
and `LIVEKIT_RECORDING_FILE_PREFIX=livekit-recordings`. Recording storage
credentials and other secret values are intentionally not included here.

The Lagos deployer inspects the current healthy worker, takes its complete
Compose file chain, environment, and mounts, then appends an immutable release
override. It verifies the recreated worker preserves the environment and mounts
and rolls back on a failed release. This is the mechanism that carries the
host's direct runtime configuration forward; do not replace it with a deploy
that starts from only the repository's base Compose file.

## Release status

This reconciliation is intended to put the already-live source changes and the
recording codec fix under the canonical `main` history. Production remains on
the existing `f1d5a8d` image until the reviewed PR passes required checks and the
normal main release succeeds. After release, verify the image revision,
container health, preserved Compose/environment/mount state, and a new MP3
recording through Egress.
