# Local Wema caller comparison

Snapshot: 2026-09-07. Run the two strategies independently; do not start an
experimental LiveKit worker or replace the existing worker.

## Caller URLs

| Setup | Public caller | Runtime |
| --- | --- | --- |
| Existing banking-capable app | http://localhost:3000/wema-bank-poc-local/agt_59a007e81e | Existing LiveKit worker; Deepgram ASR; Qwen LLM/TTS |
| Engineer's new main-app integration | http://localhost:3003/wema-bank-poc-local/agt_59a007e81e | Standalone RVC pipeline over ws://127.0.0.1:8012/ws; Qwen ASR/LLM/TTS |
| Momo PSB / Yinka local demo | http://localhost:3003/wema-bank-poc-local/agt_a9996408aa | Same RVC runtime and staging connector, separate saved agent |

Both resolve the same saved agent, without cloning it or editing its prompt:

- Business: `a0c8cda2-3b46-4ee0-898c-48f9a4d3a444` (`Wema Bank POC Local`).
- Agent: `52f72165-ab36-472f-b465-19260e60d448` (`SAW`).
- Public agent: `agt_59a007e81e`.
- Local public resolver: auth service on 8090; runtime configuration: service on 8092.

The ASR providers and orchestration settings differ. This is a functional
comparison, not a controlled measurement of orchestration overhead alone.
Test one call at a time to avoid microphone contention or shared NPU contention.

## Isolation and revisions

- Original voice checkout: `sales-girl-voice-agent`, branch `attentive-ios-sdk`,
  checkpoint `d7b639c`. Existing worker PID 99661; health port 8188.
- Original dashboard: ` sales-girl-dashboard` (leading space), port 3000.
  Its existing uncommitted work was not changed.
- New voice checkout: `sales-girl-voice-agent-speculative-main`, branch
  `codex/speculative-main-app`, engineer commit `1bb260b`.
- New dashboard checkout: `sales-girl-dashboard-speculative-main`, branch
  `codex/speculative-main-app`, engineer commit `90eddb5`, port 3003.
- Earlier experimental dashboard on 3002 and lab on 8010 remain untouched,
  including the locally restored RVC toolbar and offline report corrections.
- New backend PID at startup: 57476. New dashboard listener observed: 57941.
  PIDs are a historical snapshot; verify their command and working directory
  before stopping any process. Stopping only these new processes reverts this
  local comparison without switching or resetting the original checkouts.

No model service, jumper configuration, voice-auth threshold or saved SAW prompt
was changed. The new backend reuses the standalone lab interpreter, with
`jsonschema` added for validating configured tool arguments. It does not use or
modify the production worker venv.

## Configuration

The new dashboard explicitly enables `PUBLIC_AGENT_RVC_ENABLED` and
`PUBLIC_AGENT_USE_VOICE_LAB_RUNTIME`, with `VOICE_LAB_RVC_RUNTIME_URL` set to
`http://127.0.0.1:8012`. Its local service URLs/tokens are configured privately;
never commit its `.env.local` or tokens.

The checked-in new public caller route supplies the engineer's runtime defaults:

- LLM: `http://102.88.137.124:8080/qwen38-standard/v1/chat/completions`,
  model `qwen3.8_27b`, thinking disabled.
- ASR: `ws://102.88.137.124:8080/asr-rt/v1/realtime`, model `Qwen3-ASR`.
- TTS: `http://102.88.137.124:8080/tts/v1/audio/speech`, model `Qwen3-TTS`.

These are explicit caller-route settings, not automatic provider failover.
The standalone backend's unoverridden `/qwen38/v1` default previously returned
502, so use the main-app route, not the standalone unscoped demo.

The new backend was launched from its own working directory with:

```sh
../sales-girl-voice-agent-speculative/.venv-latency-lab/bin/python \
  -m uvicorn latency_lab.server:app --host 127.0.0.1 --port 8012
```

At launch, platform URL/token variables were passed privately from the existing
local worker's effective configuration. To reproduce, supply the same local
platform configuration through the environment or set
`RVC_LAB_DASHBOARD_ENV_FILE` to the new dashboard's configured `.env.local`.
Do not copy unrelated model settings or secrets into tracked scripts.
Backend log: `/tmp/speculative-main-backend-20260907.log`.
For banking voice checks also set `VOICE_AUTH_SIDECAR_URL=http://127.0.0.1:8098`.
The existing sidecar owns the threshold (currently the user's local 0.20
experiment); RVC never supplies a threshold override. No sidecar restart needed.

## Capabilities and verification

The original engineer revision exposed `create_ticket` only for SAW. The local
tool integration now also exposes the seven configured Wema tools: balance,
transactions, data plans, bank search, data preparation, transfer preparation,
and prepared execution. It invokes the same configured connector URLs and
schemas rather than implementing another bank adapter. Create Ticket retains
its separate confirmation and uses the existing conversation service.

All banking tool requests wait for an authoritative final transcript. SAW also
requires a successful session voice check and a fresh per-tool voice check. A failed session check is
retried on the next utterance. Speaker comparisons retain the existing worker's
rolling eight-second caller-audio window, including fresh short confirmations.
Approximate speculative transcript matches cannot
authorize banking arguments. The HTTP connector never runs speculatively.
Actual tool results are returned to the LLM and published as `odion.tool.activity`;
authentication states use the existing `odion.auth.*` payloads. The model cannot
set authentication flags. Reads do not require an extra verbal confirmation.

Prepared transfers/data purchases produce a preview, not a transaction. Only a
separate unqualified confirmation can submit that latest operation. The current
shared connector still returns `transaction_executor_not_configured` for final
execution; this integration does not enable a new money-movement implementation.

The dashboard signs a 120-second, one-use bootstrap token binding the business,
agent, caller, voice-reference owner, voice-check policy and customer profile. Key derivation is
HMAC-SHA256(service token, `odion-rvc-session-v1`); token format is
base64url(JSON).base64url(HMAC-SHA256(derived key, encoded JSON)). Both services
must use the same `AGENT_CONFIG_SERVICE_TOKEN`. Unsigned standalone lab sessions
remain conversational but have no tools. Raw browser profile/auth flags cannot
override signed claims.

This remains a LOCAL DEMO. Email-keyed voice enrollment is the existing mechanism,
not production customer identity proofing. The signed bootstrap is not a bank
login. Production onboarding must
bind a bank-verified customer to a protected enrollment; do not expose these demo
prefills or unauthenticated enrollment endpoints publicly. Recordings and full
LiveKit/SDK parity are separate roadmap items and are not supplied by this patch.

The Momo PSB agent, Yinka, is `e1fe1e5e-a48e-4ced-94bc-50133a19d55a`, with the same
eight tools in the existing local business. Its brand-specific prompt is separate;
SAW's saved prompt hash and active-agent selection were preserved. It uses the
same customer ID `R008448055` and phone `08161540638`, not Momo PSB production APIs.
On September 7 the former MTN MoMo branding was replaced with Momo PSB and the
saved agent name changed to Yinka. The saved prompt and provisioning template
introduce her as Yinka from Momo PSB. The public ID, tools, customer profile,
voice-auth exception and model settings are unchanged; SAW was not renamed.

### MoMo voice-check exception (September 7)

At the user's request, only MoMo public ID `agt_a9996408aa` on the new RVC
dashboard omits voice enrollment, background verification and per-tool voice
verification. `lib/rvc-agent-policy.js` in the dashboard owns this exact-ID
policy and the session route signs `voice_auth_required: false`. All other IDs
sign `true`; missing or non-boolean claims remain protected. Browser request
fields and model tool arguments cannot set this policy. The same helper hides
MoMo's enrollment control and voice-auth badges, not its profile/activity menu.
MoMo tool results report `auth_status: not_required`, never a fake voice match.
No caller audio is buffered for speaker matching on exempt sessions.

Configured-tool restrictions, customer profile validation, the authoritative
final transcript and separate transaction/ticket confirmations remain intact.
No bank executor is enabled by this exception. SAW, the old port-3000 app,
model settings and sidecar threshold are unchanged. To restore MoMo checks,
make `rvcVoiceAuthRequired` return true for its ID and start a new call; no
model/sidecar restart is needed. This exception is for the local demo only.

Verification: 85 backend tests (73 passed, 12 existing skips), the dashboard's
exact-ID policy test and touched-file ESLint passed. Live bootstrap requests
ignored caller-supplied bypass fields: SAW signed `true`, MoMo signed `false`.
An unenrolled synthetic MoMo caller reached the actual bank connector. The first
balance request correctly returned an account-selection prompt; a second call
specifying the previously supplied account returned `ok`, activity start/complete
events and 372,480 bytes of response PCM, with no voice-auth events. No write
transaction was attempted. Browser inspection confirmed MoMo has no enrollment
control while SAW retains it; MoMo's profile/activity drawer remains visible.
Only the comparison backend on 8012 was restarted. The original worker on 8188
remained healthy. MoMo's saved prompt was narrowly updated to follow the session
voice policy; SAW's saved instructions were checked unchanged.

The repeatable provisioning command (service credentials via environment) is:

```sh
python -m latency_lab.provision_momo \
  --business-id a0c8cda2-3b46-4ee0-898c-48f9a4d3a444 \
  --source-agent-id 52f72165-ab36-472f-b465-19260e60d448
```

Checks completed:

- Both public caller pages returned HTTP 200; original worker health returned 200.
- New backend `/health` returned 200.
- Exact new backend tests: 67 discovered, 55 passed, 12 existing explicit skips.
- After banking integration: 81 discovered, 69 passed, 12 existing skips.
  Tests use synthetic audio and a local HTTP fixture, never live transactions.
  They cover both-check failures, retries, own/other-line numbers, signed-token
  tampering/expiry/replay, exact speculative authority and actual result history.
- Both signed public bootstraps loaded all eight tools. SAW returned 134400
  nonzero PCM bytes; MTN MoMo returned 176640 bytes in their greeting probes.
- Real Qwen requests selected `wema_get_balance` and `create_ticket` for both
  agents. Tool-selection probes did not execute banking requests.
- A synthetic spoken MTN call was transcribed as "Please check my account
  balance." It selected the balance tool, emitted running/completed activity,
  and was blocked by both real sidecar checks (`not_enrolled`). No bank request
  was made. This checks the ASR-to-tool/auth/event path without impersonating
  an enrolled caller or weakening the gate.
- The existing Create Ticket executor created a labelled local integration-test
  ticket (`4a1137da-9bef-416b-bf5c-33a094c7466a`) through the conversation API.
  It was immediately closed after verification.
- A successful live speaker-match and protected banking read still require the
  user's microphone test with their existing enrolled email. The prefilled bank
  phone number is separate from that email-keyed voice reference.
- Qwen chat-completions request returned 200; ASR/TTS model-list routes returned 200.
- Actual new public `/api/public-agent/rvc-session` returned the expected Wema
  IDs, SAW name, runtime overrides and port-8012 WebSocket.
- WebSocket session `86d86a5b2f9f` confirmed saved agent configuration loaded,
  emitted ASR readiness, and returned the greeting
  `Hello! This is SAW. How can I help you today?` with 126720 bytes of nonzero
  PCM (2.64 seconds at 24 kHz mono) followed by `tts_end`.
- This was a synthetic startup probe, not a browser microphone conversation,
  audible-quality certification, banking transaction, or speaker-auth test.

The new engineer commit itself changes ASR warm-up and the opening greeting.
No additional startup-silence tuning was applied here. SDK and integration
roadmaps remain in the preserved SDK checkpoint and earlier integration review.

## Whisper ASR switch (September 7)

User requested Whisper on BOTH the original LiveKit app (3000) and the RVC
comparison app (3003), for SAW and Yinka / Momo PSB, including Voice Lab.
Pulled the engineer's backend `94d2a92` from `speculative-generation` and
dashboard `2608ef0` from `voice-lab-preemptive` into the comparison worktrees
using fast-forward merges with autostash. Existing local banking/UI changes
were restored without conflicts. The original branches were not switched or
merged wholesale; only the STT option and necessary runtime wiring were added.

All four public caller bootstraps now supply:

```json
{
  "stt_provider": "odion_stt",
  "stt_transport": "ws",
  "stt_model": "whisper-large-v3-turbo",
  "stt_base_url": "ws://102.88.137.124:8080/whisper-rt/v1/realtime"
}
```

`odion_whisper` is only a Voice Lab dropdown identifier; the wire provider is
always `odion_stt`. Whisper's transport cannot be set to HTTP in that selector.
Qwen and Deepgram remain explicitly selectable, not automatic fallbacks.

| Surface | Previous default | Current default |
| --- | --- | --- |
| 3003 SAW / Yinka public calls | Qwen ASR | Whisper WebSocket |
| 3000 SAW / Yinka public calls | Worker default (Deepgram) | Whisper runtime override |
| Both Voice Lab pages | Qwen ASR | Whisper WebSocket |

Implementation locations:

- Comparison dashboard: `lib/voice-lab-runtime.js` owns defaults and wire
  normalization; `app/internal/voice-lab/voice-lab-page-client.jsx` selects
  Whisper. The public RVC session route uses those defaults.
- Original dashboard: `lib/odion-stt-runtime.js` scopes public overrides to
  `agt_59a007e81e` and `agt_a9996408aa`. The connection-details route puts the
  identical caller metadata in both the participant JWT and agent dispatch
  (JWT room configuration and explicit dispatch). This avoids selecting the
  worker's default STT before participant metadata arrives. Unrelated agents
  retain their existing metadata/dispatch behavior.
- RVC backend: `config.py` leaves Whisper's batch URL empty; `providers.py`
  uses the engineer's same-socket finalization and fails visibly if Whisper
  cannot connect. It never invents an HTTP transcription route.
- Original worker: `agent/odion_stt.py` sends Whisper language `en` / `fr`
  while retaining Qwen's display-language convention and transcript filtering.
  The existing native WebSocket adapter and VAD remain in use.

Protocol: wait for `session.created`, send model/language via `session.update`,
arm with `input_audio_buffer.commit` (`final:false`), stream 16 kHz mono PCM16
append messages, commit `final:true` at the utterance boundary, accept
`transcription.done.text` as authoritative, then re-arm on the same socket.
Partials are optional. Whisper may wait until final commit, so speculative work
cannot assume it always has an early transcript. No latency improvement is
claimed from this switch alone.

Preserved: all agent prompts, tool definitions, confirmation rules, voice-auth
policy, sidecar threshold, LLM, TTS, and turn thresholds. The incoming dashboard
commit changed TTS initial codec frames to 6; this was explicitly kept at the
previous comparison value of 4. Original Voice Lab's value of 2 was left alone.
Worker global environment defaults were not rewritten. No remote jumper/model
service, startup script, Docker image, or port configuration was changed.

Local reloads: comparison backend 8012 (PID 4057 -> 12164) and original worker
8188 (99661 -> 12186), preserving their launch environments. The original worker
registered with local LiveKit. Next.js picked up frontend changes through its
existing dev servers. LiveKit itself has NOT been restarted for this switch.

Verification:

- RVC backend: 90 tests, 78 passed and 12 existing skips. Original STT: 25 passed.
- Dashboard runtime tests: comparison 5 passed; original 2 passed. Touched-file
  ESLint and all four worktrees' `git diff --check` passed.
- Original full suite: 186 tests with six unrelated TTS failures (two
  assertions and four payload-key errors). All six reproduce when loading the
  committed, pre-change STT module in memory. No files were reverted and no
  TTS settings were changed to force those tests green.
- Live RVC provider test transcribed "Hello, can you hear me clearly?" twice
  on the same Whisper socket with zero HTTP batch events.
- The original worker's actual STT builder, native adapter and existing VAD
  transcribed "Can you hear me clearly?" correctly on two consecutive turns.
  An earlier two-clause probe received "Hello." as its first final and stopped
  on an overly broad first-final assertion. The follow-up uses one continuous
  phrase; it does not certify pause handling or long-utterance accuracy, and no
  VAD threshold was changed to make it pass.
- Actual RVC Yinka session `4b810f3b5dda` loaded all eight tools, received a
  Whisper final for the spoken account-balance request, emitted balance
  started/completed activity with `status:ok`, and returned 195,840 bytes of
  response PCM after its 157,440-byte greeting. This was a read-only check,
  not a financial transaction or a successful speaker-match test.
- Both SAW/Yinka bootstrap responses on both ports were checked for the exact
  four Whisper fields. RVC SAW still requires voice auth; Yinka still does not.
  Original JWT agent-dispatch and participant metadata were checked identical.
- Both Voice Lab pages were inspected in a browser, including switching
  Qwen -> Whisper on 3003. Screenshots are under the workspace's
  `output/playwright/whisper-voice-lab-3000.png` and `...-3003.png`.
- The original app's end-to-end media test hit a separate existing local
  transport issue: LiveKit advertises `192.168.100.241` while the Mac is now
  `172.20.10.5`; RTC reports `wait_pc_connection timed out`. Requested approval
  to restart only local LiveKit with the current IP. Until that is approved
  and retested, do not describe port 3000 as end-to-end verified.

Rollback: select Qwen explicitly in Voice Lab for a one-call comparison. To
restore public defaults, remove the four Whisper fields from the comparison
dashboard's `defaultPublicAgentRvcRuntimeOverrides` and remove the exact-ID
Whisper override/dispatch addition in the original dashboard. Its worker then
uses its unchanged default provider. Retain all unrelated dirty-worktree
changes. Restart only affected local workers if reverting Python code; there
is no remote model state to roll back for this change.
