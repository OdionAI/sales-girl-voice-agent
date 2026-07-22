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

Dynamic HTTP tools whose URL has the exact same origin as
`CONVERSATION_API_BASE_URL` receive `CONVERSATION_SERVICE_TOKEN` from the voice
runtime at call time. Do not store that token in an agent tool definition. The
runtime never forwards this credential to a different scheme, host, or port.

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
