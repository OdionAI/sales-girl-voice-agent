# Isolated RVC / LiveKit comparison

September 7, 2026. Both integration repositories use `speculative-integration`.
This is a comparison build, not a replacement of the existing apps or a claim
of production parity or equal latency.

## Checkpoints and isolation

| Repository / baseline | Commit | Branch |
| --- | --- | --- |
| Voice agent, last pre-RVC SDK checkpoint | `d7b639c37078bd1620567014ce66611f64be4ee7` | `attentive-ios-sdk` |
| Voice agent, working RVC checkpoint | `fc2ffe8` | `codex/speculative-main-app` |
| Dashboard, working RVC checkpoint | `9b241ec` | `codex/speculative-main-app` |

Integration worktrees are `sales-girl-voice-agent-rvc-livekit` and
`sales-girl-dashboard-rvc-livekit`, beside the existing Odion projects.
The SDK checkout, its uncommitted Whisper changes, and the existing dashboard
checkout were left untouched. Credentials remain in ignored local environment
files, not in Git. No model server, jumper, SSH configuration, container image,
or remote startup script was changed.

## Three callers

| Strategy | Local caller URL |
| --- | --- |
| Original LiveKit orchestration | http://localhost:3000/wema-bank-poc-local/agt_59a007e81e |
| RVC orchestration / direct WebSocket | http://localhost:3003/wema-bank-poc-local/agt_59a007e81e |
| RVC orchestration / LiveKit transport | http://localhost:3004/wema-bank-poc-local/agt_73099afb71 |

The third caller is a new agent (`f90e2db3-6e12-4b4f-94fa-905b83a2adfd`), cloned
from SAW (`52f72165-ab36-472f-b465-19260e60d448`). Its spoken name remains SAW.
Prompt SHA-256: `b3591c5a2ae3848915e78d42b5074367719e7e9bd904f881c7a748483b6283fe`.
The clone has the same eight active tools, including `create_ticket`. Source
prompt and source tools were checked unchanged after provisioning. Wema still
requires session and action voice checks; no new bypass was added.

To provision against another local database, use the existing config service:

```sh
python -m latency_lab.provision_livekit_comparison \
  --business-id a0c8cda2-3b46-4ee0-898c-48f9a4d3a444 \
  --source-agent-id 52f72165-ab36-472f-b465-19260e60d448
```

The script is repeatable and refuses mismatched existing tool configurations.
Use its returned public ID in the dashboard allowlist; IDs differ in another DB.
It does not copy credentials into source code or modify the source agent.

## Implementation boundaries

- The existing public RVC bootstrap resolves the agent, runtime settings and
  server-owned banking profile. Only the explicit server allowlist selects the
  hybrid. Other public RVC callers retain their WebSocket route.
- Bootstrap creates a dedicated LiveKit room and explicitly dispatches
  `rvc-livekit-comparison`. A short-lived HMAC-signed dispatch binds the business,
  agent, voice policy and banking profile to that exact room and caller.
  Caller-editable participant metadata is not accepted as banking identity.
- The worker imports `ConversationPipeline` directly. There is no intermediate
  connection to the port-8012 lab and no additional `AgentSession` turn detector.
- LiveKit input is 16 kHz mono PCM in 100 ms frames. Output is 24 kHz mono PCM
  with the existing 240 ms queue capacity. Capacity is not a fixed 240 ms wait.
  RVC still owns VAD, endpointing, partial/final validation, speculative output
  authorization, tools and cancellation. No model or turn thresholds changed.
- Greeting and microphone consumption run independently. Cancellation also
  stops greeting generation; completed old playout cannot close a new attempt.
  Final answer/history is committed before transport drain closes a turn.
- `lk.chat` enters the same authoritative-final path, but supplies no voice
  evidence. Private `odion.tool.activity` and `odion.auth.*` messages target
  only the intended caller. Transcriptions support existing web text streams
  and legacy packets used by Swift. Chat is not duplicated as ASR text.
- The existing caller controls, visualizer, transcript and floating bank menu
  are reused. One tool-activity reducer serves both transports. The mic icon
  follows real track state, including delayed permission completion.

The signed comparison worker is currently single-caller/browser oriented.
Original worker/SIP dispatch is unchanged; SIP handoff and multi-human policy
are not implemented in this comparison entrypoint.

## Start locally

Keep the existing local LiveKit and supporting config, auth, banking and
voice-auth services running. Use their actual LiveKit credentials, not assumed
development keys. The new worker must use the same service token that signs
dashboard dispatch. Use the existing lab requirements and installed LiveKit
Python dependencies; the local test used LiveKit Agents 1.8.0 / RTC 1.1.17.

Create an ignored `.env.local` in the integration dashboard using the current
working environment, then set these additional values:

```dotenv
PUBLIC_AGENT_RVC_ENABLED=true
PUBLIC_AGENT_RVC_LIVEKIT_PUBLIC_IDS=agt_73099afb71
RVC_LAB_LIVEKIT_AGENT_NAME=rvc-livekit-comparison
RVC_LAB_LIVEKIT_WORKER_PORT=8189
LIVEKIT_URL=ws://127.0.0.1:7880
# LIVEKIT_API_KEY and LIVEKIT_API_SECRET: actual local server credentials
# Retain config-service credentials, banking profile, voice-auth URL and models.
```

Run the worker from the integration backend, loading that environment before
the Python modules are imported. `python` below means the lab environment with
LiveKit installed (the local comparison reuses `.venv-latency-lab`).

```sh
RVC_LAB_DASHBOARD_ENV_FILE=/dev/null python -m dotenv \
  -f ../sales-girl-dashboard-rvc-livekit/.env.local run -- \
  python -m latency_lab.livekit_worker start
```

Run from the integration dashboard:

```sh
npm install
npx next dev --webpack --hostname 127.0.0.1 --port 3004
```

Do not launch a second copy if these ports are already occupied. Current local
logs are `/tmp/rvc-livekit-integration-worker.log` and
`/tmp/rvc-livekit-integration-dashboard.log`. The worker must register under the
comparison name, never the original worker name. Loopback media URL is for
local browser testing; physical-device deployment is a separate SDK task.

## Verification and remaining work

- Backend: 97 discovered unit tests, 85 passing and 12 existing optional skips.
  Tests cover scoped dispatch, private events, audio format, queue clearing,
  cancellation, chat not authorizing voice checks, and transcript contracts.
- Frontend: six focused reducer, voice-policy and Whisper tests passing;
  ESLint checked changed frontend modules.
- A real LiveKit test against the new SAW clone received nonzero greeting PCM,
  attempted `wema_get_balance`, and received the expected authentication denial
  without caller voice evidence. Local trace: `ba715ad7d6fb`.
- Browser testing exercised the reused controls, transcript, caller prefills,
  microphone toggle and bank activity/authentication display.
- An audio-input smoke test published synthesized speech as a caller microphone
  track. Whisper returned "What services can you help me with?" and SAW answered
  through RVC/LLM/TTS. This proves routing, not natural-speaker recognition or
  voice-authentication accuracy. An SDK FFI handle warning appeared during
  cleanup; sessions still disconnected and reports were written.

Successful two-stage voice verification and every configured tool still need
an authorized caller test. A blocked balance attempt is not a successful bank
transaction test. No transfer or purchase was executed during smoke testing.
The hybrid currently retains RVC trace/report artifacts, not the original
worker's dashboard recording/session persistence. Add those lifecycle hooks
before claiming full production parity. iOS, reconnect and multi-call load
testing also remain outstanding.

Compare identical spoken prompts and model/voice settings across the three
URLs. Measure from the last voiced input sample to actual client audio onset;
report median/p95, interruption tail and tool-result latency separately.
Server enqueue time is not listener-audible time. Existing displayed timing
totals are not sufficient proof of latency parity. Whisper often produces text
only after final commit, so a fast RVC call does not prove speculation occurred.
See `LIVEKIT_INTEGRATION_PLAN.md` for the measurement boundaries.

## Rollback and roadmap

### September 7: internal markup reaching speech

Trace `0b9f4c9847de`, turn 10, showed a knowledge follow-up emitting raw
`<tool_call><function=wema_list_data_plans>...` content. The exact markup reached
`tts_request_start`; this was not evidence of an ASR fault or a need to change
remote TTS chunk settings. The follow-up had omitted the configured banking
tools and the existing banking instructions/caller context.

The follow-up now retains those definitions and context, and uses the existing
bank tool runner with its unchanged final-input, voice and confirmation gates.
An incremental provider filter excludes thinking and tool-markup blocks before
text reaches transcripts, history or phrase TTS. It handles split/truncated tags
without buffering ordinary speech until completion. Raw markup is never parsed
into an executable action; only structured `tool_calls` remain executable.
Incremental UTF-8 decoding also preserves characters split across network chunks.

Regression suite: 105 tests, 93 passing and 12 existing skips. Coverage includes
every split of the reported markup, thinking blocks, incomplete tags, TTS and
transcript filtering, native tool calls, Unicode, and both outcomes of the
existing voice checks. A real-model follow-up returned a structured
`wema_list_data_plans` call with `network: MTN`; that probe executed no bank API.
Live hybrid trace `68f349011c30` then showed plan-tool activity and a clean
voice-verification response. The chat-only request was correctly denied without
voice evidence. The smoke call was disconnected after verification.
This patch is confined to the hybrid backend worktree. Remote configuration,
saved agent prompts, ASR, TTS tuning, authentication policy and frontend code
are unchanged. The pre-fix integration checkpoint is `21a3655`.

### Stop the comparison

End hybrid test calls and stop only the processes launched from the two
integration worktrees (frontend 3004, worker 8189). Verify PID/cwd before stopping;
do not stop shared LiveKit, models or services. Continue on ports 3000/3003.
The separately provisioned agent can remain unused. No remote state restoration
is necessary. For a clean source comparison, create another worktree at the
checkpoint commits rather than resetting a dirty SDK or baseline checkout.

The SDK roadmap remains separate: headless Attentive SDK plus optional caller
UI, human and coding-agent integration documentation, Attentive streaming
gateway, gRPC transport, and Android SDK. Resume iOS work from the pre-RVC SDK
checkpoint above; this comparison does not merge into `attentive-ios-sdk`.
