# RVC + LiveKit integration handoff

Snapshot: 2026-09-08. Branch in BOTH repositories: `speculative-integration`.
This is the current experimental comparison build, not a production release.
Use this guide over older baseline tables in `MAIN_APP_COMPARISON.md` and the
Qwen-only defaults in the standalone lab's `.env.example`.

## Revisions and changes

| Repository | Revision / role |
| --- | --- |
| [sales-girl-voice-agent](https://github.com/OdionAI/sales-girl-voice-agent) | This handoff commit on `speculative-integration`; latest runtime change `585d92a` |
| [sales-girl-dashboard](https://github.com/OdionAI/sales-girl-dashboard) | `c14ad11a9578b4aa3d12a8593551eeb4c76ac86e`; runtime `8efe1f8` plus setup docs/template |

The backend branch contains these checkpoints:

- `fc2ffe8`: RVC banking tools, signed caller identity, two-stage voice checks,
  create-ticket integration and Whisper, before adding LiveKit transport.
- `21a3655`: isolated LiveKit transport adapter importing `ConversationPipeline`
  directly, without a second orchestration engine or proxy WebSocket hop.
- `b09e3dc`: incremental thinking/tool-markup filtering; knowledge follow-ups keep
  configured native banking tools and caller context. Markup never executes tools.
- `5116d77`: retain final caller messages and tool results despite interruptions;
  background summary compaction plus recent exact exchanges and pending-action state.
- `585d92a`: numeric TTS allowance counts spoken digits, not just whitespace words.
  A five-account phrase changes from 74 to 299 audio tokens. Text is unchanged.

Dashboard runtime `8efe1f8` selects the hybrid by server-side public-ID allowlist
and reuses the caller controls, transcript, audio visualizer, floating bank menu,
tool activity and voice-auth badges. It does not replace the original callers.

## What owns what

LiveKit owns rooms, explicit agent dispatch and WebRTC audio/data transport.
RVC owns VAD, turn decisions, speculative generation/validation, interruption,
LLM/TTS scheduling, tools and conversation memory. This worker does NOT run a
LiveKit `AgentSession` beside RVC. Its entrypoint is:

```sh
python -m latency_lab.livekit_worker start
```

Do not substitute `python main.py`, `make run`, or `python -m latency_lab.server`.
Those start different orchestration/transport paths. Port 8012 is not in the
hybrid media path; it is only for optional standalone RVC comparisons/Voice Lab.

## Prerequisites outside these two checkouts

Git does not contain the database, SAW's saved prompt/tool records, customer test
data, service credentials, bank keys, speaker enrollments, model weights or
running server/container state. Obtain approved environment/data handoff privately.
Do not recreate or regenerate the Wema prompt from memory.

| Dependency | Local address | Source / requirement |
| --- | --- | --- |
| LiveKit | `ws://127.0.0.1:7880` | Running LiveKit server; real key/secret shared by dashboard and worker |
| Auth/public-agent resolver | `http://127.0.0.1:8090` | `OdionAI/sales-girl-auth-service`, observed base `14dd1bfd1f43004afdd5f8369f70fbb76ff50824` |
| Agent config | `http://127.0.0.1:8092` | `OdionAI/sales-girl-agent-config-service`, base `55a84767eba4a55fb6a1cce840fdb1e248731015` plus patch below |
| Conversation / tickets | `http://127.0.0.1:8091` | `OdionAI/sales-girl-conversation-service`, observed base `2df5f45c4cb016070548aab4dea6aa6651fd7747` |
| Banking connector | `http://127.0.0.1:8097` | Separate banking checkout described below, approved Wema development credentials |
| Voice authentication | `http://127.0.0.1:8098` | Same separate checkout; ECAPA dependencies, local enrollment storage |
| Billing / knowledge | Ports `8094` / `8095` | Existing platform services if exercising their features |

Use each platform repository's README/Makefile, migrations and private `.env`.
Their observed base SHAs are not a complete platform/database release. The local
conversation service also has uncommitted recording/timing work outside this
handoff; it was not silently committed here. The hybrid does not yet integrate
the original dashboard recording lifecycle.

The current config service includes a small uncommitted change that exposes each
tool's saved description in runtime config. Its exact code/test diff is included
as [handoff/agent-config-tool-descriptions.patch](handoff/agent-config-tool-descriptions.patch).
In a clean engineer-owned config-service checkout at the base above, review it:

```sh
git apply --check ../sales-girl-voice-agent-rvc-livekit/latency_lab/handoff/agent-config-tool-descriptions.patch
git apply ../sales-girl-voice-agent-rvc-livekit/latency_lab/handoff/agent-config-tool-descriptions.patch
python -m pytest tests/test_conversation_flow.py
```

Skip if equivalent support is already present; do not reverse another engineer's
work. This handoff preserves the patch without changing/pushing the shared config
service checkout. Restart only that service if applying its patch on a new host.

### Banking and voice-auth service source

The published `wema-bank-poc-tools` revision
`601e465032de148af392abdef801bdcc1c99cf1f` in the voice-agent repository contains
the same `wema_tools/`, `agent/voice_auth.py`, `agent/voice_enroll_http.py` and
`scripts/run_voice_auth_http.py` files as our current local service source.
Use a separate checkout; these services are intentionally not copied into RVC:

```sh
git clone --branch wema-bank-poc-tools https://github.com/OdionAI/sales-girl-voice-agent.git sales-girl-banking-services
cd sales-girl-banking-services
git checkout --detach 601e465032de148af392abdef801bdcc1c99cf1f
```

Follow its `docs/wema/README.md` and configure its dependencies in a separate venv.
With private environment loaded, the service entrypoints are:

```sh
python -m uvicorn wema_tools.api:create_app --factory --host 127.0.0.1 --port 8097
```

In another terminal/environment with ECAPA/SpeechBrain/Torch available:

```sh
python scripts/run_voice_auth_http.py
```

The connector needs `WEMA_BANK_MODE=live`, `WEMA_TOOLS_SERVICE_TOKEN` and
`WEMA_ACCOUNT_MAINTENANCE_API_KEY`. Its default mode without configuration is
mock; check `/health` and do not mistake mock data for bank results. Its token
must match the saved tool headers and dashboard profile lookup configuration.

The sidecar needs its existing `VOICE_AUTH_DIR`, `VOICE_AUTH_EMBEDDER=ecapa`,
`VOICE_AUTH_HTTP_PORT=8098` and approved `VOICE_AUTH_COSINE_THRESHOLD`. The earlier
local experiment used `0.20`; that is a cosine cutoff, NOT 20% accuracy and NOT a
production recommendation. The launcher defaults to `0.40` without an override.
Do not change thresholds to force tests through; request approval. Re-enroll the
authorized tester using their own voice/email instead of copying biometric files
into Git. Wema must pass session and fresh per-tool checks. Text chat supplies
no voice evidence and should not pass protected tools.

## Bring up the integration

Keep existing apps intact and use fresh directories. GitHub access is required.

```sh
git clone --branch speculative-integration https://github.com/OdionAI/sales-girl-voice-agent.git sales-girl-voice-agent-rvc-livekit
git clone --branch speculative-integration https://github.com/OdionAI/sales-girl-dashboard.git sales-girl-dashboard-rvc-livekit
```

Use the release SHAs supplied with this handoff if either branch has moved. Start
fixes on an engineer-owned branch/worktree, not an SDK or shared baseline checkout.
Observed runtimes: Python 3.12.7 and Node 22.23.1. From the hybrid backend:

```sh
python3.12 -m venv .venv-hybrid
source .venv-hybrid/bin/activate
python -m pip install -r latency_lab/requirements.txt -c latency_lab/constraints-livekit.txt 'python-dotenv[cli]==1.2.3'
python -m unittest discover -s latency_lab/tests
```

The constraints pin observed direct runtime versions, including LiveKit Agents
1.8.0 / RTC 1.1.17. They are not a complete transitive lock or model-server backup.
Do not install/change packages in the existing original worker's venv.

In the dashboard, copy `.env.rvc-livekit.example` to the ignored `.env.local` and
replace ALL `REPLACE_*` entries from the private handoff. Verify the template's
loopback service URLs against your machine. Never put server tokens in
`NEXT_PUBLIC_*` variables. The dashboard and worker must share the same
`AGENT_CONFIG_SERVICE_TOKEN` for signed dispatch.

Restore/import the existing business and source SAW agent (including tool records
and approved bank credentials) through the platform. Do not assume these IDs
exist in a fresh DB. In the backend, with its venv active, clone the source agent:

```sh
RVC_LAB_DASHBOARD_ENV_FILE=/dev/null python -m dotenv -f ../sales-girl-dashboard-rvc-livekit/.env.local run -- python -m latency_lab.provision_livekit_comparison --business-id YOUR_BUSINESS_UUID --source-agent-id YOUR_SOURCE_SAW_UUID
```

The script returns `agent_id`, `public_id`, name, prompt hash and tool names.
It checks that source prompt/tools remain unchanged. Put its **public_id** into
`PUBLIC_AGENT_RVC_LIVEKIT_PUBLIC_IDS`; do not use its UUID. On our DB, the clone
is `agt_73099afb71`, config UUID `f90e2db3-6e12-4b4f-94fa-905b83a2adfd`, business
slug `wema-bank-poc-local`. The source is config UUID
`52f72165-ab36-472f-b465-19260e60d448` in business
`a0c8cda2-3b46-4ee0-898c-48f9a4d3a444`. Its prompt checksum at provisioning was
`b3591c5a2ae3848915e78d42b5074367719e7e9bd904f881c7a748483b6283fe`.

Start the worker in its own terminal; the environment MUST be loaded before
importing the Python modules:

```sh
RVC_LAB_DASHBOARD_ENV_FILE=/dev/null python -m dotenv -f ../sales-girl-dashboard-rvc-livekit/.env.local run -- python -m latency_lab.livekit_worker start
```

Wait for `registered worker` with `agent_name: rvc-livekit-comparison`. Its local
health port is 8189, not the original worker's 8188. In the dashboard terminal:

```sh
npm ci
node --test scripts/test-whisper-runtime.mjs scripts/test-rvc-agent-policy.mjs scripts/test-call-tool-activity.mjs
npx next dev --webpack --hostname 127.0.0.1 --port 3004
```

Do not use its `npm run dev`, which targets port 3000. Open
`http://localhost:3004/<business-slug>/<comparison-public-id>`.
Our URL is `http://localhost:3004/wema-bank-poc-local/agt_73099afb71`.
Allow microphone/audio playback and enroll the tester before protected tools.
If LiveKit media cannot connect after a network change, inspect its advertised
node IP and ICE candidates; do not copy the previous machine's IP blindly.

## Active model and timing configuration

| Component | Current configuration |
| --- | --- |
| ASR | `odion_stt`, `ws`, `whisper-large-v3-turbo`, `ws://102.88.137.124:8080/whisper-rt/v1/realtime` |
| LLM | `qwen3.8_27b`, `http://102.88.137.124:8080/qwen38-standard/v1`, thinking off; normal output cap 180 |
| TTS | `Qwen3-TTS`, `http://102.88.137.124:8080/tts/v1/audio/speech`, `helen-mavino-0030`, English, PCM 24 kHz mono |
| TTS startup | 4 initial codec frames; digit-aware budget, min 24 / max 360 tokens |
| RVC turn controls | RMS 280; minimum speech 240 ms; silence 300 ms; stable partial 180 ms; minimum 8 characters |
| LiveKit adapter | Input 16 kHz mono / 100 ms frames; output 24 kHz mono / 240 ms queue capacity (not a fixed added wait) |
| Memory | Check every 60 s when idle, preserve at least 8 exchange groups, context estimate 6000 tokens, summary timeout 15 s |

The public caller uses the runtime defaults in dashboard `lib/voice-lab-runtime.js`,
then signs them into dispatch. Caller overrides can alter these, so inspect the
actual session trace. Whisper uses final WebSocket commit and has NO HTTP batch
path. Never add an invented provider, automatic Groq fallback, or another ASR.

## Verification and error investigation

Latest backend suite: 129 tests, 117 passed, 12 existing skips. Dashboard: six
focused tests passed. These are not a claim that every real banking flow works.

To confirm the integrated solution, inspect the active room: its name starts
`rvc-livekit-`, with agent `rvc-livekit-comparison`. Port 3004 alone is insufficient;
non-allowlisted public agents and standalone Voice Lab RVC can use the direct
WebSocket route instead. Do not compare those as if all used the hybrid.

Useful code: `livekit_worker.py` (transport), `pipeline.py` (orchestration),
`providers.py` (model streams), `memory.py`, `llm_output.py`, `banking_tools.py`,
`action_tools.py`. Dashboard bootstrap: `app/api/public-agent/rvc-session/route.js`.

Every hybrid call writes `latency_lab/artifacts/traces/<session>.jsonl` and a
report under `latency_lab/artifacts/reports/`. Current machine console logs are
`/tmp/rvc-livekit-integration-worker.log` and
`/tmp/rvc-livekit-integration-dashboard.log`; new terminal launches write to their
terminal unless redirected. Traces can contain banking information: do not commit
them or publicize raw logs. Correlate by room/session, not the most recent file.

For cutoff reports compare `generation_complete`, each `tts_request_start`
(including `max_new_tokens`), generated audio, `generation_cancel` and
`turn_close`. Full transcript text does not prove complete audible playback.
For tools, distinguish structured tool selection, voice gate, connector response,
and caller activity delivery. Never bypass a gate merely to make a test pass.

Known limits / next engineer's work:

- The numeric-budget direct test now reaches the last of five accounts, with
  16-17 seconds of audio instead of 5.84 seconds. An ASR round trip misread one
  middle account, so exact digit pronunciation remains unverified. Very long
  phrases can still exhaust the unchanged 360-token cap. See `TTS_NUMERIC_BUDGET.md`.
- The native LiveKit smoke call connected and played its complete response, but
  the LLM declined the chat-only account-list repeat request. No prompt edits
  were made to force it. This is not a successful authenticated balance test.
- Separately, the LLM can stop mid-sentence at its own 180-token cap. Generic
  completion recovery was deferred. Period-separated digit output can also
  create many tiny TTS requests. Do not silently broaden this TTS-budget fix.
- User reports of repeated clarification need longer voice tests. Memory now
  retains interrupted messages and compacts history concurrently, but summaries
  remain lossy; model accuracy and fragmented ASR are not guaranteed fixed.
- Both voice checks and each real banking flow still need authorized speaker
  tests. The staging connector deliberately blocks final money movement with
  `transaction_executor_not_configured`; preparation is not execution.
- Hybrid dashboard recordings/session persistence, reconnect, iOS parity,
  SIP/multi-human policy and concurrent-call load tests are not finished.
- No controlled median/p95 latency comparison has established transport parity.
  Measure last voiced input to CLIENT audible output, not server enqueue time.
  Whisper may emit only final text, so not every fast turn used speculation.
- Shared-NPU contention can still affect latency. Summary cancellation does not
  guarantee immediate inference cancellation on the remote server.

Preserve the existing Momo-only signed voice-auth exception; do not apply it to
Wema or guess an exception for new public IDs. Prefills and email-keyed enrollment
are local-demo identity plumbing, not production bank login. Do not expose these
services or their enrollment endpoints publicly as-is.

## Rollback and retained roadmap

Only reload the isolated worker after its call ends. Check process command/cwd
before stopping it; preserve apps 3000/3003, LiveKit, shared model services and SDK.
Pre-TTS-fix backend checkpoint: `5116d77`. Pre-memory checkpoint: `b09e3dc`.
Use a separate worktree at a checkpoint, not `reset --hard` on shared work.
No remote model/server configuration was changed by these integration fixes.

The original pre-RVC SDK checkpoint is `d7b639c37078bd1620567014ce66611f64be4ee7`
in the separate SDK work history. This handoff does not publish or change that
SDK branch. Preserve its roadmap: headless SDK plus optional AttentiveVoiceUI,
iOS/device validation and human/coding-agent docs, Attentive streaming gateway,
gRPC and Android SDK. Do not merge experimental orchestration into it implicitly.

## Copyable coding-agent brief

> Work on the RVC + LiveKit integration in `speculative-integration` of
> OdionAI/sales-girl-voice-agent and OdionAI/sales-girl-dashboard, starting from
> the supplied release SHAs. Read `latency_lab/ENGINEER_HANDOFF.md` first. Use a
> separate worktree and follow the documented dependency/configuration steps;
> ask for missing private credentials and approved test data, never invent them.
> Reproduce each error and correlate its room, trace, model output, audio and
> tool/auth events before editing. Keep changes small, reuse existing components,
> and add targeted tests. Preserve the saved SAW prompt, Whisper ASR, Qwen NPU
> LLM/TTS, both Wema voice gates, confirmation rules, caller UI and SDK. Never
> introduce Groq/provider fallback or change shared servers/thresholds without
> asking. Report verification and remaining failures honestly. Commit each fix
> separately and document rollback; do not restart an active call or shared
> services without checking with the owner.
