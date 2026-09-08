# Reproduce the RVC + LiveKit comparison

Snapshot: 2026-09-08. This guide is for the integrated SAW caller, not the SDK,
original LiveKit worker, standalone RVC caller, or a production release.
Follow the numbered gates in order. Record PASS, FAIL or NOT TESTED at each gate;
stop at the first failure instead of tuning models or switching providers.

## What this reproduces

A commit SHA is a commit ID, not a separate version number. Use the full 40-character
SHA. Checking out that SHA includes its complete file tree and ancestors.
Cherry-picking the last documentation commit does NOT install the integration.

There are two different recovery targets:

- **Application replay:** rebuild this worker/dashboard and supporting platform
  against the existing shared Whisper/Qwen endpoints. The instructions below cover it.
- **Full model-server recovery:** recreate those remote containers, weights, voice
  assets, gateway and accelerator configuration. This handoff does NOT have a newly
  captured immutable inventory of that infrastructure. See gate 5. Do not claim exact
  infrastructure or performance parity until that inventory is supplied and verified.

No running service, hybrid pipeline code, prompt or model setting was changed to
publish this guide. A missing config-service migration is supplied as a patch for
the clean deployment. The original code SHAs remain the application baseline:

| Component | Exact source revision |
| --- | --- |
| Voice integration code | `46bf98bd9c3442eddce381f3e3a942adf4e2f16d` |
| Dashboard, including env template | `c14ad11a9578b4aa3d12a8593551eeb4c76ac86e` |
| Auth/public resolver | `14dd1bfd1f43004afdd5f8369f70fbb76ff50824` |
| Agent config | `55a84767eba4a55fb6a1cce840fdb1e248731015` plus both bundled config patches |
| Conversations/tickets | `2df5f45c4cb016070548aab4dea6aa6651fd7747` plus bundled patch |
| Banking + voice-auth sidecar | `601e465032de148af392abdef801bdcc1c99cf1f` in the voice repository |

The newer **handoff SHA** supplied with this guide adds documentation, snapshots,
patch files and an offline verifier on top of the voice baseline. Use that newer
SHA for the voice checkout so these files are present. Dashboard stays at `c14ad11`.
The machine-readable comparison values are [handoff/runtime-manifest.json](handoff/runtime-manifest.json).

## 1. Create clean checkouts and verify identity

Use new directories on the engineer's machine. Do not reset, clean, upgrade or
stop an existing deployment. Set `HYBRID_SHA` to the full NEW handoff commit from
the release message; do not literally use the placeholder below.

```sh
export HYBRID_SHA=REPLACE_FULL_HANDOFF_COMMIT
export REPLAY_ROOT="$HOME/odion-hybrid-replay"
mkdir -p "$REPLAY_ROOT"
cd "$REPLAY_ROOT"
git clone https://github.com/OdionAI/sales-girl-voice-agent.git voice
git -C voice checkout --detach "$HYBRID_SHA"
git clone https://github.com/OdionAI/sales-girl-dashboard.git dashboard
git -C dashboard checkout --detach c14ad11a9578b4aa3d12a8593551eeb4c76ac86e
git -C voice rev-parse HEAD
git -C dashboard rev-parse HEAD
git -C voice status --short
git -C dashboard status --short
```

PASS: exact supplied SHAs and initially clean worktrees. The branch label is not
proof of a version. For subsequent fixes, create your own branch from this checkout.
Do not merge SDK work into the replay.

## 2. Install isolated application dependencies

Observed machine: macOS 26.6.2 arm64, Python 3.12.7, Node 22.23.1,
LiveKit server 1.13.6. The worker's local venv was borrowed from another checkout,
but the imported pipeline source came from the hybrid directory. This recipe creates
its own venv; that sibling checkout is NOT required.

```sh
cd "$REPLAY_ROOT/voice"
python3.12 --version
node --version
python3.12 -m venv .venv
.venv/bin/python -m pip install -r latency_lab/handoff/hybrid-python-freeze.txt
.venv/bin/python -m pip check
.venv/bin/python -m unittest discover -s latency_lab/tests
cd "$REPLAY_ROOT/dashboard"
npm ci
node --test scripts/test-whisper-runtime.mjs scripts/test-rvc-agent-policy.mjs scripts/test-call-tool-activity.mjs
```

PASS: dependency consistency and tests. Record skips, not just the exit status.
`hybrid-python-freeze.txt` captures every installed package version, including
transitives, but is not a hash-locked wheel archive or a guarantee of Linux/other-CPU
compatibility. If an exact package is unavailable, stop and record it; do not silently
upgrade. Extra OS packages/wheel differences must be recorded when changing platform.
`npm ci`, not an unconstrained `npm install`, uses the committed dashboard lockfile.

## 3. Restore the supporting platform

If using approved existing platform services, verify their versions/config and go
to gate 4. For a fresh local platform, clone these into `REPLAY_ROOT`:

```sh
cd "$REPLAY_ROOT"
git clone https://github.com/OdionAI/sales-girl-auth-service.git auth
git -C auth checkout --detach 14dd1bfd1f43004afdd5f8369f70fbb76ff50824
git clone https://github.com/OdionAI/sales-girl-agent-config-service.git config
git -C config checkout --detach 55a84767eba4a55fb6a1cce840fdb1e248731015
git clone https://github.com/OdionAI/sales-girl-conversation-service.git conversations
git -C conversations checkout --detach 2df5f45c4cb016070548aab4dea6aa6651fd7747
git clone https://github.com/OdionAI/sales-girl-voice-agent.git banking
git -C banking checkout --detach 601e465032de148af392abdef801bdcc1c99cf1f
git -C config apply --check ../voice/latency_lab/handoff/agent-config-tool-descriptions.patch
git -C config apply ../voice/latency_lab/handoff/agent-config-tool-descriptions.patch
git -C config apply --check ../voice/latency_lab/handoff/agent-config-schema-parity.patch
git -C config apply ../voice/latency_lab/handoff/agent-config-schema-parity.patch
git -C conversations apply --check ../voice/latency_lab/handoff/conversation-local-parity.patch
git -C conversations apply ../voice/latency_lab/handoff/conversation-local-parity.patch
```

The patches preserve previously uncommitted local service changes without changing
their shared branches. The tool-description patch carries descriptions into runtime config.
The additional schema patch adds a migration for `agents.theme_key` and
`agent_versions.training_prompt`: the pinned code uses these columns but its old
migration chain omits them. The existing local DB already has them. Apply the patch
BEFORE `alembic upgrade head` on the clean deployment, then verify head `20260908_0004`.
Do not downgrade an existing populated database as a rollback shortcut.
The conversation patch carries SQLite timestamp normalization and local recording
retrieval, with tests. It does NOT add the missing hybrid recording lifecycle.
Apply only to the pinned clean bases; if a patch fails, inspect before proceeding.

For each of `auth`, `config`, `conversations`, in that directory:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r ../voice/latency_lab/handoff/platform-python-freeze.txt
.venv/bin/python -m pip check
cp .env.example .env
# Edit .env with approved private values before migrating or starting.
.venv/bin/alembic upgrade head
```

The shared local platform uses SQLite. Set `DATABASE_URL=sqlite+pysqlite:///./replay.db`
in EACH service's private `.env` (different working directories give separate files).
Use separate database names/paths, never a developer's existing production database.
Match JWT issuer/audience/algorithm and signing secret across the three services.
Keep `SERVICE_AUTH_ENABLED=true`. Match each service's `SERVICE_AUTH_TOKEN` to its
dashboard/worker client token. Set auth `FRONTEND_URL=http://localhost:3004` and
`AGENT_CONFIG_SERVICE_BASE_URL=http://127.0.0.1:8092`. Match the conversation config URL too.
Use the pinned services' README and migrations for their database backend; their
requirements are NOT included in the hybrid worker's Python snapshot. The separate
platform snapshot includes the dependencies/tests of all three services. It was
reconstructed from their temporary venv's remaining distribution directory names:
some original files are missing, so it is not a byte-for-byte environment recovery.
Do not copy that broken temporary directory; install fresh and verify tests/migrations.

Start each in its own terminal and directory, with its own venv:

```sh
# auth/
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8090
# config/
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8092
# conversations/
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8091
```

Run `tests/test_conversation_flow.py` and `tests/test_prompt_theme_migration.py` in
config, and `tests/test_local_recording.py`
plus `tests/test_session_timing.py` in conversations using their venvs' `python -m pytest`.
Each service's `/v1/health` must return success. Health alone does not verify DB data.
Restore the approved business/account data privately or register a target business
in the dashboard after gate 9. The private SAW agent import happens in gate 7.
Billing/knowledge services are additionally required if exercising their dashboard
features; they are not substitutes for the conversations service used by tickets.

## 4. Restore banking and voice authentication

In `banking/`, create two isolated environments using the captured package snapshots:

```sh
python3.12 -m venv .venv-bank
.venv-bank/bin/python -m pip install -r ../voice/latency_lab/handoff/banking-python-freeze.txt
.venv-bank/bin/python -m pip check
python3.12 -m venv .venv-auth
.venv-auth/bin/python -m pip install -r ../voice/latency_lab/handoff/voice-auth-python-freeze.txt
.venv-auth/bin/python -m pip check
```

These are observed macOS environments, not cross-platform wheel archives; model weights
and enrollment are separate. Both existing environments passed `pip check`. Do not
install sidecar dependencies into the hybrid worker venv. Load approved private
environment, then run in separate terminals:

```sh
.venv-bank/bin/python -m uvicorn wema_tools.api:create_app --factory --host 127.0.0.1 --port 8097
.venv-auth/bin/python scripts/run_voice_auth_http.py
```

Bank environment: `WEMA_BANK_MODE=live`, `WEMA_TOOLS_SERVICE_TOKEN`, and
`WEMA_ACCOUNT_MAINTENANCE_API_KEY`. The token must match imported tool headers.
PASS: `GET http://127.0.0.1:8097/health` reports `mode: live` and
`bank_writes_enabled: false`. Unconfigured connector mode is mock; do not use it
for this comparison. Final writes remain `transaction_executor_not_configured`.

Auth environment: `VOICE_AUTH_EMBEDDER=ecapa`, `VOICE_AUTH_HTTP_PORT=8098`,
`VOICE_AUTH_DIR=<private writable enrollment directory>` and the approved cutoff.
The actual local process uses **`VOICE_AUTH_COSINE_THRESHOLD=0.20`**. This is the
user-approved local experiment, not 20% accuracy or a production recommendation.
Do not silently use the launcher's 0.40 default or disable authentication.
Re-enroll the authorized tester; do not commit biometric files. The owner is the
caller's enrolled contact/email, not the Wema customer ID. Both the session check
and fresh per-bank-tool check must remain enabled. Typed chat is not voice evidence.

## 5. Verify the model endpoints before connecting a caller

Use the same existing shared endpoints for application parity:

| Layer | Endpoint / required setting |
| --- | --- |
| ASR | `ws://102.88.137.124:8080/whisper-rt/v1/realtime`; `odion_stt`, WS, `whisper-large-v3-turbo` |
| LLM | `http://102.88.137.124:8080/qwen38-standard/v1/chat/completions`; `qwen3.8_27b` |
| TTS | `http://102.88.137.124:8080/tts/v1/audio/speech`; `Qwen3-TTS`, named voice `helen-mavino-0030` |

Verify Whisper receives `session.created`, accepts `session.update` with model and
language `en`, then `input_audio_buffer.commit` with `final:false`, 16 kHz mono
PCM16 append frames, and `commit final:true`. Require `transcription.done`, then
re-arm `final:false`. There is NO Whisper HTTP batch endpoint. For speech probes,
use an approved test recording, never private enrollment audio without permission.

Probe the LLM with a short neutral request and record time to first streamed token.
The outbound JSON must include `chat_template_kwargs` with BOTH `enable_thinking:false`
and `thinking:false`. Normal calls use `temperature:0.35`, `max_tokens:180`.
Probe TTS with a neutral sentence and verify nonzero playable 24 kHz mono PCM16,
streaming enabled, English, named voice, and `initial_codec_chunk_frames:4`.
Measure first byte AND gaps between chunks; successful HTTP status is insufficient.
These probes consume inference; do not run load tests against shared NPUs without approval.

For **new model hosts**, obtain from the inference engineer: exact image digests,
in-container source commits and diffs, launch commands/systemd units, model revisions
and hashes, quantization, NPU allocation/driver/CANN versions, decode/scheduling
parameters, gateway config, and the approved cached voice asset/hash. Preserve private
tokens outside Git. Record current load and network distance for both deployments.
The older `deployment/stable` bundle at the banking SHA contains historical model
scripts/digests but predates Whisper and later TTS changes; do NOT apply it to running
NPUs or claim it reproduces this snapshot. Current remote server inventory remains
an explicit prerequisite for a full infrastructure rebuild, not a guessed setting.

## 6. Configure LiveKit and the application environment

Run LiveKit **1.13.6** with its existing matching API key/secret. On a fresh local-only
host, the observed shape is `livekit-server --dev --bind 127.0.0.1 --node-ip <HOST_LAN_IP>
--redis-host 127.0.0.1:6380`, with Redis available. This is a development topology,
not a public deployment. Do not start a duplicate listener. Never copy the old Mac's IP.
Native mobile clients need reachable signaling AND WebRTC ICE/media candidates;
localhost on an iPhone is the iPhone. A remote/public browser needs HTTPS/WSS and
appropriate certificate/ICE/TURN configuration, separately validated.

```sh
cd "$REPLAY_ROOT/dashboard"
cp .env.rvc-livekit.example .env.local
```

Replace every `REPLACE_*` entry privately. Use service URLs reachable FROM EACH process.
Loopback inside a container is not the host. Match LiveKit credentials across server,
dashboard and worker; match the agent-config token across dashboard and worker because
it signs/verifies dispatch. Never put these tokens in `NEXT_PUBLIC_*` fields.

Use all model/turn values in the template and manifest. Critical switches:

```dotenv
PUBLIC_AGENT_RVC_ENABLED=true
PUBLIC_AGENT_USE_VOICE_LAB_RUNTIME=true
PUBLIC_AGENT_RVC_LIVEKIT_PUBLIC_IDS=REPLACE_IMPORTED_AGENT_PUBLIC_ID
RVC_LAB_LIVEKIT_AGENT_NAME=rvc-livekit-comparison
RVC_LAB_LIVEKIT_WORKER_PORT=8189
RVC_LAB_LLM_ENABLE_THINKING=false
RVC_LAB_STT_BATCH_URL=
RVC_LAB_TTS_INITIAL_CODEC_CHUNK_FRAMES=4
RVC_LAB_END_SILENCE_MS=300
```

The worker's env flag is `llm_enable_thinking=false`; the public runtime override
has the opposite name, **`llm_disable_thinking="true"`**. Both mean thinking OFF.
The dashboard defaults send this override. Request overrides are merged afterwards,
so `llm_disable_thinking="false"` can turn it back on: inspect the actual request.
Legacy `turn_preemptive_generation`/`turn_preemptive_tts` flags are not RVC's speculation
controls. Do not copy old LiveKit endpointing delays over RVC's 300 ms silence setting.

Caller customer ID/own phone come from the private export and server-side env prefills.
Leave `WEMA_CALLER_PREFILL_ACCOUNT_NUMBER` absent to match the current snapshot.
Do not invent an account from a screenshot. Profile fields are signed into dispatch.

## 7. Restore the exact SAW agent, not a rewritten prompt

Obtain `wema-saw-agent-config-20260908.zip` privately from the owner. It contains
`agent-create.json`, `tools-create.json`, configuration and an ordered Postman collection.
The export is deliberately not in Git: the prompt includes customer-specific examples
and the configuration includes approved customer data. Credentials are still redacted.
Verify the ZIP SHA-256 before import:
`17897db48e9eb1dcd10efa8faf8d2a490e22a639f852fa314d3f51055257f9ea`.

Create/select the target business in the dashboard. Import the collection, set private
service credentials/business UUID/connector URL, run **Create SAW once**, then the eight
**Configure** requests, then **Verify saved runtime configuration**. Stop on any failed
request; rerunning the entire collection creates duplicates. Follow the bundle README.
For direct import: POST agent JSON to `/v1/agents`, each tool to `/v1/agents/<id>/tools`
with `X-Business-ID`, `X-Service-Token` and `X-Service-Name: sales-girl-voice-agent`.
Substitute `${WEMA_TOOLS_SERVICE_TOKEN}` BEFORE posting; the server does not expand it.

PASS: name SAW; saved `instructions` SHA-256
`b3591c5a2ae3848915e78d42b5074367719e7e9bd904f881c7a748483b6283fe`; these eight active tools:

```text
wema_get_balance
wema_get_transactions
wema_list_data_plans
wema_list_transfer_banks
wema_prepare_data_purchase
wema_prepare_transfer
wema_execute_prepared
create_ticket
```

Descriptions, methods, URLs, headers and schemas must match the export. `create_ticket`
is a system tool at `internal://create-ticket`, not an invented HTTP endpoint. There is
no standalone airtime tool in this exact agent. Do not paste the generated effective
system prompt into saved instructions; the worker adds those rules itself.

If the source SAW already exists, `python -m latency_lab.provision_livekit_comparison`
can clone it as documented in ENGINEER_HANDOFF; it does not restore a missing source DB.
New UUIDs/public IDs are expected. Put the NEW `agt_*` public ID in the env allowlist,
not the config UUID or the old local ID. The local original was `agt_73099afb71`.

## 8. Run the offline preflight

From `voice/` (replace the ID):

```sh
.venv/bin/python -m latency_lab.handoff.verify_deployment \
  --env-file ../dashboard/.env.local --dashboard ../dashboard \
  --public-id YOUR_IMPORTED_PUBLIC_ID
```

PASS: matching dependencies and effective public default configuration, including
thinking disabled. This reads local config; it does not create calls, send banking
requests, verify credentials, or prove remote health. To inspect actual request
overrides, supply `--overrides /private/path/runtime-overrides.json`, containing only
the JSON `runtimeOverrides` object. It is merged over the public defaults as in the app.
Never share raw bootstrap responses: they contain short-lived access tokens.

## 9. Start only the correct worker and dashboard

Worker terminal, from `voice/`:

```sh
RVC_LAB_DASHBOARD_ENV_FILE=/dev/null RVC_LAB_LIVEKIT_ENV_FILE=/dev/null \
  .venv/bin/python -m dotenv -f ../dashboard/.env.local run -- \
  .venv/bin/python -m latency_lab.livekit_worker start
```

PASS: log says `registered worker`, `agent_name: rvc-livekit-comparison`; health
`http://127.0.0.1:8189/` responds. Load env BEFORE importing Python modules.
Do not use `main.py`, `make run`, or `latency_lab.server`: those are different stacks.

Dashboard terminal, from `dashboard/`:

```sh
npx next dev --webpack --hostname 127.0.0.1 --port 3004
```

Do not use `npm run dev` (this repository starts port 3000). If you started the dashboard
earlier to create the business/agent, restart only your idle dashboard after changing
its env allowlist. No original worker, SDK, baseline app or NPU restart is required.

## 10. Prove that the call actually uses the integration

Open `http://localhost:3004/<target-business-slug>/<new-public-id>`.
Use the enrolled tester's email/contact; grant microphone and audio permissions.
Start a call. Its bootstrap must return `serverUrl`, `roomName`, `participantToken`.
If it returns standalone `websocketUrl`/`runtimeUrl` instead, STOP: wrong route/allowlist.
Do not start port 8012 just to make that wrong route work.

PASS: room starts `rvc-livekit-`, named worker is `rvc-livekit-comparison`, one agent
joins, greeting is audible, microphone input reaches the transcript. The server trace
must contain `session_open` with transport `livekit`, agent SAW, eight configured tools,
Whisper authoritative final, codec frames 4, silence 300, memory enabled/context 6000.
Run the preflight again with `--trace latency_lab/artifacts/traces/<THIS_SESSION>.jsonl`.
Correlate room/session IDs, never assume the latest file is your call.

## 11. Verify functionality and measure latency separately

1. Say a neutral greeting, then ask a short non-banking question. Check full audio and transcript.
2. Ask for balance. Verify session voice badge AND fresh tool voice badge pass before the
   actual bank request. Confirm the floating activity and returned value agree. No placeholders.
3. Ask for recent transactions and data plans. Confirm the same voice gates and tool results.
4. Prepare a data purchase/transfer using approved staging data, including own-line behavior.
   Preview must not be called a completed transaction. The current final executor is blocked;
   a refusal at that stage is expected, not proof of a missing integration.
5. Report a support complaint. Answer follow-ups and confirm ticket creation. Inspect activity
   and the conversations service; do not create duplicate tickets during retries.
6. Exercise interruption and a longer conversation. Preserve the exact prompt/settings;
   record incomplete audio, repeated clarification or failed tools instead of masking them.

For latency, use the same scripted utterances, audio device, model hosts and comparable
load across baseline/hybrid. Record cold vs warm separately and repeated-turn median/p95.
Split VAD/EOU wait, Whisper final, LLM first token, first TTS audio, frame enqueue and
client-audible output. Server enqueue is NOT client audio. A 240 ms source queue is
capacity, not automatically a fixed added delay. The existing report chooses the first
silence candidate even if speech resumes, so inspect raw events when attributing VAD delay.
Whisper may be final-only, leaving no useful partial for speculative generation.

Known limits remain: LLM 180-token cutoff, TTS cap for very long speech, model quality,
lossy summaries, shared-NPU contention, hybrid recording lifecycle, reconnect and mobile
parity. A successful greeting or green health check does not certify all these features.

## 12. Handoff evidence and rollback

Return the following privately, redacting contacts, banking data, tokens and enrollment:
full SHAs; patch check results; Python/Node/LiveKit versions; pip check/test output;
preflight output; exact startup commands; active model endpoints and outbound thinking
flags; selected public-ID/room; one trace and client playback measurements; model-host
load; which numbered gates passed, failed or remain untested. Keep the originals private.

Do not commit `.env`, data exports, transcripts, recordings, voice profiles or tokens.
Do not enable Groq/provider fallback, bypass Wema authentication, retune thresholds,
edit prompts or restart shared models to make verification pass. Ask first.
Rollback means stopping only your idle replay processes and using a fresh pinned checkout;
never `reset --hard` a shared worktree or `docker compose down -v` a shared database.

### Brief for another coding agent

> Check out the full supplied handoff SHA in voice and dashboard c14ad11a9578b4aa3d12a8593551eeb4c76ac86e.
> Read latency_lab/CLEAN_DEPLOY.md. Follow every gate, record evidence, stop on mismatch.
> Restore private agent/service configuration without rewriting it. Verify thinking OFF
> in both environment and actual runtime override/payload. Confirm named hybrid dispatch,
> Whisper and eight SAW tools. Never substitute a different stack because it starts.
> Separate source/config parity from remote-server parity and measured audible latency.
> Reproduce and isolate failures before making small, tested fixes on your own branch.
