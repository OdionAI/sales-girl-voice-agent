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
- each session exposes only the built-in and custom tools enabled by that
  agent's active runtime config (plus scoped knowledge search); unrelated
  built-in tools are not sent to the LLM
- Huawei MaaS textual fallback calls in the exact
  `<function>name{...}</function>` form are withheld from TTS and recovered as
  structured calls only when the named tool is enabled; malformed or
  unauthorized markup is never spoken to the caller
- built-in tools that publish explicit raw JSON schemas receive the model's
  top-level JSON object through LiveKit's `raw_arguments` contract before the
  worker validates and forwards individual fields
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
- the production cascade runtime uses Qwen 3.8 27B
  (`qwen3.8_27b` at `QWEN_LLM_BASE_URL`) with thinking disabled for English and
  French sessions; Huawei MaaS `glm-5.2` remains an opt-in alternative via
  `LLM_PROVIDER=maas`
- optional post-call summary/intent analysis also uses Huawei MaaS and reads
  only messages belonging to the completed session, not older calls in the
  same conversation
- agents configured with `record_caller_details` treat it as an internal
  post-call caller-intake marker, not as a live callable tool; the live agent
  asks for and confirms first name, last name, phone number, and email at the
  beginning of the call without updating the Sheet itself
- after those calls end, a separate GLM-5.2 analysis pass extracts the caller
  details plus the sheet-specific theme, sub-theme, request summary, treatment,
  status, optional consular/order references, and transfer outcome, then asks
  conversation service to create and export the complete record
- conversation-service mutation responses use domain statuses such as
  `active`, `ended`, and `ready`; the worker treats only explicit failure
  markers as failed writes
- configured agent names are pinned into the first-turn instruction, and
  generic/custom voice responses are kept concise and free of Markdown syntax
  before they reach TTS
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

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py dev
```

Create `.env` from `.env.example` before running locally.

## Key environment variables

- `LIVEKIT_URL`
- `LIVEKIT_API_KEY`
- `LIVEKIT_API_SECRET`
- `DEEPGRAM_API_KEY`
- `LLM_PROVIDER` (`qwen` for the Qwen 3.8 27B production runtime with thinking
  off; `maas`, `google`, and `groq` remain supported alternatives)
- `QWEN_LLM_BASE_URL`
- `QWEN_LLM_MODEL_DEFAULT`
- `QWEN_LLM_MODEL_EN`
- `QWEN_LLM_MODEL_FR`
- `QWEN_LLM_API_KEY`
- `MAAS_API_KEY`
- `MAAS_BASE_URL`
- `MAAS_LLM_MODEL_DEFAULT`
- `MAAS_LLM_MODEL_EN`
- `MAAS_LLM_MODEL_FR`
- `CONVERSATION_ANALYSIS_ENABLED`
- `CONVERSATION_ANALYSIS_MODEL`
- `VOICE_LATENCY_TRACE_ENABLED`
- `VOICE_LATENCY_TRACE_AGENT_IDS`
- `VOICE_LATENCY_TRACE_BUSINESS_IDS`
- `GOOGLE_API_KEY` (only required when the Google provider is selected)
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

## Latency tracing

The voice worker emits observe-only structured timing logs prefixed with
`VOICE_LATENCY_TRACE`. These records are safe for staging/prod logs: they include
IDs, provider/model labels, stage names, durations, counts, and hashed caller or
conversation references, but not transcript content, phone numbers, emails, names,
tool arguments, or API keys.

Useful events include:

- `turn_started`
- `llm_request_started`, `llm_first_chunk`, `llm_first_text`, `llm_stream_completed`
- `knowledge_lookup_completed`
- `dynamic_knowledge_prefetch_completed`
- `assistant_message_ready`
- `conversation_message_persist_completed`
- `post_session_analysis_completed`
- `caller_record_analysis_completed`

Filter by `turn_id` to reconstruct one caller turn. To reduce log volume, set
`VOICE_LATENCY_TRACE_AGENT_IDS` or `VOICE_LATENCY_TRACE_BUSINESS_IDS` to a
comma-separated allowlist.

Deployed staging and production VM environments must set
`KNOWLEDGE_SERVICE_BASE_URL` explicitly. Knowledge lookup may fall back to
`CONVERSATION_SERVICE_TOKEN` for auth when a dedicated `KNOWLEDGE_SERVICE_TOKEN`
is not provided, but the base URL itself must still be configured or business
knowledge retrieval is effectively disabled.

Dynamic HTTP tools whose URL has the exact same origin as
`CONVERSATION_API_BASE_URL` receive `CONVERSATION_SERVICE_TOKEN` from the voice
runtime at call time. Do not store that token in an agent tool definition. The
runtime never forwards this credential to a different scheme, host, or port.

## CI and deployment

- dependency install/build sanity checks run on `dev`, `staging`, and `main`
- pushes to `staging` and `main` deploy both language workers to the matching
  Huawei node-B Compose stack through node A as an SSH bastion
- the workflow runs unit and Docker-build gates, uploads the exact commit,
  serializes Compose changes, and rolls back the source target if startup fails
- it does not authenticate to GCP, publish to Artifact Registry, or use IAP
- the approved personal-GCP dependency is the external NG/Odion TTS endpoint;
  Huawei MaaS `glm-5.2` remains the live and post-call text model

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
