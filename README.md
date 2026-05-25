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
- optional external/custom tool endpoints

## Current runtime status

Current stable contract:

- stable cascade runtime is the production-safe path
- public URL sessions must load the requested agent config even when that
  agent is not the widget-active agent
- category-aware tools are supported for hotel, restaurant, fashion, and
  generic/custom agents
- generic/custom sessions refresh per-turn knowledge by updating the active
  agent instructions in-place instead of handing off to a new agent
- assistant transcript persistence should ignore obvious duplicate/partial
  fragments so dashboard and session transcripts reflect final replies cleanly
- ticket follow-up reconciliation should not create a second ticket when a
  recent successful ticket already exists for the same caller turn flow
- Odion cloned TTS can be used for English sessions when configured
- staging Voice Lab sessions can apply temporary per-call STT/TTS overrides,
  including the Odion STT adapter for `eu-stt.odion.ai`, without changing
  saved agent configuration
- LiveKit room recording is the supported conversation-audio capture path when
  enabled for the environment
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
- `APPOINTMENTS_API_BASE_URL`
- `AGENT_CONFIG_API_BASE_URL`
- `AGENT_CONFIG_API_TIMEOUT_SECONDS`
- `AGENT_CLIENT_ID`
- `CONVERSATION_API_BASE_URL`
- `CONVERSATION_SERVICE_TOKEN`
- `CONVERSATION_API_TIMEOUT_SECONDS`
- `CONVERSATION_SERVICE_REQUIRED`
- `BILLING_HOOK_BASE_URL`
- `BILLING_HOOK_SERVICE_TOKEN`
- `BILLING_HEARTBEAT_INTERVAL_SECONDS`
- `BILLING_FAIL_CLOSED`
- `OPS_SERVICE_BASE_URL`
- `OPS_SERVICE_TOKEN`
- `HOTEL_OPS_SERVICE_BASE_URL`
- `REQUIRE_VERIFIED_PHONE`
- `ENABLE_FRENCH_AGENT`

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
