# SalesGirl Voice Agent

The LiveKit worker/runtime layer for SalesGirl voice sessions.

<!-- no-op dev sync marker for staging deployment trigger -->

## Read this first

Shared platform reference:

- [`../SALES_GIRL_PLATFORM_STATUS.md`](/Users/woron/Documents/sales-girl/_generated_repos/SALES_GIRL_PLATFORM_STATUS.md)

## What this service owns

This service owns the realtime voice runtime:

- joining LiveKit rooms
- starting room recording only after the runtime has joined when recording is enabled
- building runtime instructions/prompts for the active session
- running the stable voice pipeline
- selecting and invoking tools
- persisting conversation and session activity
- reporting call billing usage

## Core interactions

This worker talks to:

- LiveKit
- `sales-girl-agent-config-service`
- `sales-girl-conversation-service`
- `sales-girl-billing-service`
- `sales-girl-knowledge-service`
  - runtime business-knowledge lookup scoped to the agent's attached
    `knowledge_base_ids`
- optional external/custom tool endpoints

## Current runtime status

Current stable contract:

- stable cascade runtime is the production-safe path
- public URL sessions must load the requested agent config even when that
  agent is not the widget-active agent
- category-aware tools are supported for hotel, restaurant, fashion, and
  generic/custom agents
- generic/custom agents can optionally expose a `transfer_to_aicc` runtime tool
  that bridges the current live room back into a configured Huawei AICC SIP
  route for human escalation during a call
- dashboard-configured custom `http` tools are promoted into real callable
  runtime tools for the active agent at session start, not just appended to the
  prompt as text
- custom runtime tools use the saved tool description as part of the live
  contract, can expose request-schema fields when provided, and forward
  business/session metadata headers to downstream business endpoints
- generic/custom sessions refresh per-turn knowledge by updating the active
  agent instructions in-place instead of handing off to a new agent
- business-knowledge lookup reads the agent's attached `knowledge_base_ids` from
  runtime-config and searches only those knowledge bases through
  `/v1/knowledge/search`
- assistant transcript persistence should ignore obvious duplicate/partial
  fragments so dashboard and session transcripts reflect final replies cleanly
- ticket follow-up reconciliation should not create a second ticket when a
  recent successful ticket already exists for the same caller turn flow
- the cascade runtime should prefer Gemini `gemini-3-flash-preview` for English
  and French sessions and automatically retry on Gemini
  `gemini-3.1-flash-lite` when the primary model is unavailable
- NG TTS is the default non-Deepgram TTS path for configured English sessions
- deployed environments can switch the default direct-call STT/TTS path to
  Odion STT and NG TTS with `VOICE_AGENT_STT_PROVIDER`, `VOICE_AGENT_STT_MODEL`,
  `ODION_STT_BASE_URL`, and `NG_TTS_BASE_URL` without needing per-call
  dashboard overrides
- staging Voice Lab sessions can apply temporary per-call STT/TTS overrides,
  including the Odion STT adapter for `eu-stt.odion.ai`, without changing
  saved agent configuration
- NG TTS should use `NG_TTS_BASE_URL=https://ng-tts.odion.ai` on Huawei;
  the adapter appends `/api/v1/tts/stream` for base URLs and uses a full stream
  endpoint exactly when one is explicitly supplied
- `ODION_TTS_BASE_URL` remains a backward-compatible alias for older deploy
  scripts and saved runtime overrides, but new configuration should prefer
  `NG_TTS_BASE_URL`
- Runpod-backed NG TTS endpoints should additionally set `NG_TTS_API_KEY`; the
  adapter sends it as a bearer token and supports the same base-URL contract
  when the base host is a Runpod load-balancer URL
- LiveKit room recording is the supported conversation-audio capture path when
  enabled for the environment
- the recording target can be GCS or S3-compatible object storage such as
  Huawei OBS, depending on the recording env configuration
- new recordings should prefer a browser-friendly format such as `mp3` so
  dashboard playback does not depend on limited codec support
- Gemini Live is experimental and not the current stable production path

## Live-data contract

Configured live-data tools should point to full `http` or `https` endpoints.

Examples:

- hotel room inventory/pricing
- restaurant menu and pricing
- fashion inventory and pricing

The worker should fall back gracefully and not invent live data when these
endpoints are missing.

### Speech during tool waits

Dashboard-configured HTTP tools use LiveKit's `RunContext.with_filler()` to speak
short waiting updates while voice authentication or the API request is pending.
The first update starts after 0.75 seconds of continuous quiet. Further updates
are at least 6 seconds apart, capped at three per tool call. Parallel calls share
that spacing, and fast calls finish without waiting for filler. The scope stops
scheduling updates on completion, failure, or interruption.

The first Wema update is selected by tool name: balance, transaction history,
data plans, bank lookup, data purchase preparation, transfer preparation, or
execution of a confirmed request. For example, balance uses "Let me check your
available balance." Unknown tools retain the generic acknowledgement; later
updates remain generic. These are fixed phrases spoken by the existing TTS,
without an additional LLM request. Edit `HTTP_TOOL_ACKNOWLEDGEMENTS` in
`agent/dynamic_tools.py` to adjust the tool-specific wording.

The public Wema caller menu offers **Tool-specific** (default) and
**LLM-generated** under **Speech during tool calls** before starting a call.
The selection is locked during the call and sent as the allowlisted participant
metadata field `tool_wait_speech_mode` (`tool_specific` or `llm_generated`).
It does not change saved agent settings, billing, or authentication.

LLM-generated mode uses the session's existing LLM and TTS, with up to six recent
text messages for context. This extra LLM request cannot call tools. It starts
only when filler is due, so fast tools do not make an extra request. Generation
is bounded by `AGENT_DYNAMIC_TOOL_FILLER_LLM_TIMEOUT_SECONDS` (default 2.5);
empty, invalid, or failed generation uses the fixed phrase, never another model
provider. Unfinished generation is cancelled when the tool finishes or the caller
interrupts. The extra generation step can increase first-filler latency.
Generated waiting speech is not added to the main LLM conversation history, so
it cannot split the tool call/result pair. It is still spoken through LiveKit.

Set `AGENT_DYNAMIC_TOOL_FILLER_DELAY_SECONDS` and
`AGENT_DYNAMIC_TOOL_FILLER_INTERVAL_SECONDS` in the worker environment to tune
these timings, then restart the voice worker. This uses the session's existing
TTS voice. Authentication and HTTP execution remain in their original order;
waiting messages do not indicate authorization or transaction success. This
requires LiveKit Agents 1.6.3 or newer, matching the stable dependency snapshot.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py dev
```

Create `.env` from `.env.example` before running locally.

### Stable low-latency Qwen snapshot

Branch `stable` preserves the complete Voice Lab profile validated on
2026-09-02: local LiveKit, realtime cumulative Qwen ASR, Qwen 3.8 27B, cached
Helen Qwen TTS, exact NPU launch scripts, immutable image/source identities,
model checksums, and recovery probes. Follow
[`deployment/stable/README.md`](deployment/stable/README.md) for replay or cold
recovery. Secrets, model weights, image archives, and biometric voice material
are intentionally kept in private storage rather than Git.

### Pre-RVC full LiveKit cached Helen voice branch

Use branch `full-livekit-version` for the preserved full LiveKit
`AgentSession` runtime with the Ascend/Qwen3-TTS server-cached ICL cloned
voice. This branch keeps the original LiveKit architecture intact: prompt/config
loading, tools, interruption handling, call lifecycle, and browser-room audio
all continue through the normal LiveKit worker path. It does not introduce the
separate RVC pipeline.

For isolated local browser experiments, do not register as the deployed
production/SIP worker names. Use a unique worker name such as:

```bash
export AGENT_NAME=sales-girl-agent-en-pre-rvc-helen-cached-fast
export AGENT_PORT=8188
```

Use Deepgram for ASR and Ascend/Qwen3-TTS for the cached Helen voice:

```bash
export VOICE_AGENT_STT_PROVIDER=deepgram
export VOICE_AGENT_STT_MODEL=nova-3
export VOICE_AGENT_TTS_PROVIDER=odion_tts
export VOICE_AGENT_TTS_MODE=cloned_voice
export ODION_TTS_BACKEND=ascend
export NG_TTS_BASE_URL=http://102.88.137.124:8080/tts/v1/audio/speech
export ODION_TTS_BASE_URL=http://102.88.137.124:8080/tts/v1/audio/speech
export ASCEND_TTS_TASK_TYPE=Base
export ASCEND_TTS_CACHED_VOICE=helen-mavino-0030
export ASCEND_TTS_X_VECTOR_ONLY=false
export ASCEND_TTS_INITIAL_CODEC_CHUNK_FRAMES=2
export ODION_TTS_FRAME_SIZE_MS=80
export ODION_TTS_HTTP_CHUNK_BYTES=2048
export ODION_TTS_INITIAL_BUFFER_MS=0
export VOICE_LATENCY_TRACE_ENABLED=true
```

The Ascend request contract for cached voice synthesis is:

```json
{
  "input": "<text to speak>",
  "model": "Qwen3-TTS",
  "task_type": "Base",
  "voice": "helen-mavino-0030",
  "language": "English",
  "x_vector_only_mode": false,
  "response_format": "pcm",
  "stream": true,
  "stream_format": "audio",
  "initial_codec_chunk_frames": 2
}
```

The adapter must send the cached voice as `voice`, not `voice_id`, and must not
send `ref_audio` or `ref_text` per phrase. The full ICL profile is already
stored by the gateway as `helen-mavino-0030`.

Before a test session, confirm the cached voice still exists:

```bash
curl -sS http://102.88.137.124:8080/tts/v1/audio/voices
```

Direct TTS smoke test:

```bash
curl -sS -m 90 \
  -X POST http://102.88.137.124:8080/tts/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"input":"Hello, this is Sarah speaking with the cached Helen cloned voice for a short latency smoke test.","model":"Qwen3-TTS","task_type":"Base","voice":"helen-mavino-0030","language":"English","x_vector_only_mode":false,"response_format":"pcm","stream":true,"stream_format":"audio","initial_codec_chunk_frames":2}' \
  -o /tmp/pre-rvc-helen-smoke.pcm \
  -w 'http_code=%{http_code} size=%{size_download} time_starttransfer=%{time_starttransfer} time_total=%{time_total}\n'
```

For browser testing, start the dashboard locally with dispatch forced to the
same isolated worker name:

```bash
PUBLIC_AGENT_BASELINE_ENABLED=true \
PUBLIC_AGENT_BASELINE_RUNTIME_AGENT_NAME=sales-girl-agent-en-pre-rvc-helen-cached-fast \
PUBLIC_AGENT_EXPLICIT_DISPATCH=true \
FORCE_ENV_LIVEKIT_URL=true \
LOCAL_RUNTIME_AGENT_NAME_EN=sales-girl-agent-en-pre-rvc-helen-cached-fast \
LOCAL_RUNTIME_AGENT_NAME_FR=sales-girl-agent-en-pre-rvc-helen-cached-fast \
npm run dev:next -- -p 3000
```

Expected log evidence:

- `Using Odion Ascend cached-voice TTS ... cached_voice=helen-mavino-0030`
- `Ascend TTS payload: model=Qwen3-TTS task_type=Base cached_voice=helen-mavino-0030`
- `TTS request -> ... ascend_openai=True ... voice_id=None cached_voice=helen-mavino-0030`
- `TTS response <- ... sample_rate=24000 channels=1 frame_size_ms=80`
- no per-phrase `ref_audio` or `ref_text`

With `ASCEND_TTS_INITIAL_CODEC_CHUNK_FRAMES=2`, direct gateway probes measured
first audio body bytes around `400ms`. Earlier `16`-frame tests measured around
`1.0s`, so use `2` for lower-latency conversational browser tests unless audio
stability requires increasing to `4`.

## Key environment variables

- `LIVEKIT_URL`
- `LIVEKIT_API_KEY`
- `LIVEKIT_API_SECRET`
- `DEEPGRAM_API_KEY`
- `GOOGLE_API_KEY`
- `LLM_PROVIDER` (`google` default, or `groq`)
- `GROQ_API_KEY` (required when `LLM_PROVIDER=groq`)
- `GROQ_LLM_MODEL_DEFAULT`
- `GROQ_LLM_MODEL_EN`
- `GROQ_LLM_MODEL_FR`
- `GROQ_LLM_BACKUP_MODEL_DEFAULT`
- `GROQ_LLM_BACKUP_MODEL_EN`
- `GROQ_LLM_BACKUP_MODEL_FR`
- `GOOGLE_LLM_MODEL_DEFAULT`
- `GOOGLE_LLM_MODEL_EN`
- `GOOGLE_LLM_MODEL_FR`
- `GOOGLE_LLM_BACKUP_MODEL_DEFAULT`
- `GOOGLE_LLM_BACKUP_MODEL_EN`
- `GOOGLE_LLM_BACKUP_MODEL_FR`
- `APPOINTMENTS_API_BASE_URL`
- `AGENT_CONFIG_API_BASE_URL`
- `AGENT_CONFIG_API_TIMEOUT_SECONDS`
- `AGENT_CLIENT_ID`
- `CONVERSATION_API_BASE_URL`
- `CONVERSATION_SERVICE_TOKEN`
- `CONVERSATION_API_TIMEOUT_SECONDS`
- `CONVERSATION_SERVICE_REQUIRED`
- `KNOWLEDGE_SERVICE_BASE_URL`
- `KNOWLEDGE_SERVICE_TOKEN`
- `BILLING_HOOK_BASE_URL`
- `BILLING_HOOK_SERVICE_TOKEN`
- `BILLING_HEARTBEAT_INTERVAL_SECONDS`
- `BILLING_FAIL_CLOSED`
- `OPS_SERVICE_BASE_URL`
- `OPS_SERVICE_TOKEN`
- `HOTEL_OPS_SERVICE_BASE_URL`
- `AICC_OUTBOUND_TRUNK_NAME`
- `AICC_OUTBOUND_TRUNK_ID`
- `AICC_TEST_ACCESS_CODE`
- `AICC_TRANSFER_TARGET_NUMBER`
- `AICC_TRANSFER_FROM_NUMBER`
- `REQUIRE_VERIFIED_PHONE`
- `ENABLE_FRENCH_AGENT`
- `VOICE_AGENT_STT_PROVIDER`
- `VOICE_AGENT_STT_MODEL`
- `ODION_STT_BASE_URL`
- `NG_TTS_BASE_URL`
- `NG_TTS_API_KEY`
- `NG_TTS_REQUEST_TIMEOUT_SECONDS`
- `NG_TTS_RETRY_ATTEMPTS`
- `NG_TTS_RETRY_BACKOFF_SECONDS`
- `ODION_TTS_BASE_URL`
- `ODION_TTS_API_KEY`
- `ODION_TTS_REQUEST_TIMEOUT_SECONDS`
- `ODION_TTS_RETRY_ATTEMPTS`
- `ODION_TTS_RETRY_BACKOFF_SECONDS`
- `LIVEKIT_RECORDING_STORAGE_PROVIDER`
- `LIVEKIT_RECORDING_BUCKET`
- `LIVEKIT_RECORDING_GCS_BUCKET`
- `LIVEKIT_RECORDING_GCP_CREDENTIALS_JSON`
- `LIVEKIT_RECORDING_S3_ACCESS_KEY`
- `LIVEKIT_RECORDING_S3_SECRET_KEY`
- `LIVEKIT_RECORDING_S3_SESSION_TOKEN`
- `LIVEKIT_RECORDING_S3_REGION`
- `LIVEKIT_RECORDING_S3_ENDPOINT`
- `LIVEKIT_RECORDING_S3_FORCE_PATH_STYLE`
- `LIVEKIT_RECORDING_PUBLIC_BASE_URL`

Deployed staging and production VM environments must set
`KNOWLEDGE_SERVICE_BASE_URL` explicitly. Knowledge lookup may fall back to
`CONVERSATION_SERVICE_TOKEN` for auth when a dedicated `KNOWLEDGE_SERVICE_TOKEN`
is not provided, but the base URL itself must still be configured or business
knowledge retrieval is effectively disabled.

## CI and deployment

- dependency install/build sanity checks run on `dev`, `staging`, and `main`
- pushes to `staging` deploy the staging VM runtime through GitHub Actions
- pushes to `main` deploy the production VM runtime through GitHub Actions
- GitHub Actions can publish a release image to Artifact Registry for parity,
  but the active runtime still deploys onto the managed VM
- the VM deploy flow should pull the target branch and restart the systemd
  services instead of relying on local manual SSH deploy habits

Branch convention:

- `dev` = active work
- `staging` = deploy candidate
- `main` = production

## Documentation maintenance

Update this README whenever changes affect:

- runtime selection
- tool behavior
- prompt/runtime contract
- external service dependencies
- LiveKit worker expectations

Keep it aligned with:

- [`../SALES_GIRL_PLATFORM_STATUS.md`](/Users/woron/Documents/sales-girl/_generated_repos/SALES_GIRL_PLATFORM_STATUS.md)
