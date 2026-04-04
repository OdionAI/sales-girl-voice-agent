# SalesGirl Voice Agent

The LiveKit worker/runtime layer for SalesGirl voice sessions.

## Read this first

Shared platform reference:

- [`../SALES_GIRL_PLATFORM_STATUS.md`](/Users/woron/Documents/sales-girl/_generated_repos/SALES_GIRL_PLATFORM_STATUS.md)

## What this service owns

This service owns the realtime voice runtime:

- joining LiveKit rooms
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
- category-aware tools are supported for hotel, restaurant, fashion, and
  generic/custom agents
- Odion cloned TTS can be used for English sessions when configured
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
- `OPS_SERVICE_BASE_URL`
- `OPS_SERVICE_TOKEN`
- `HOTEL_OPS_SERVICE_BASE_URL`
- `REQUIRE_VERIFIED_PHONE`
- `ENABLE_FRENCH_AGENT`

## CI and deployment

- dependency install/build sanity checks on `dev`, `staging`, and `main`
- Docker image build in CI

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
