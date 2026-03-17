# SalesGirl Voice Agent

The LiveKit worker/runtime layer for SalesGirl voice sessions.

## Responsibilities
- join LiveKit rooms
- resolve agent config
- persist conversation/session activity
- call demo CRM and billing hooks

## Local run
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py dev
```

Create `.env` from `.env.example` before running locally.

Some legacy deployment scripts are still present and will be migrated in a later pass.

## Key environment variables
- `LIVEKIT_URL`
- `LIVEKIT_API_KEY`
- `LIVEKIT_API_SECRET`
- `DEEPGRAM_API_KEY`
- `GOOGLE_API_KEY`
- `APPOINTMENTS_API_BASE_URL`
- `AGENT_CONFIG_API_BASE_URL`
- `CONVERSATION_API_BASE_URL`
- `CONVERSATION_SERVICE_TOKEN`
- `OPS_SERVICE_BASE_URL`
- `OPS_SERVICE_TOKEN`
- `AGENT_CLIENT_ID`
- `REQUIRE_VERIFIED_PHONE`
- `BILLING_HOOK_BASE_URL`
- `BILLING_HOOK_SERVICE_TOKEN`

The staging/prod environment contract for this repo is documented centrally in:
- `sales-girl-platform-infra/docs/voice-stack-wiring.md`

## CI
- install dependencies on `dev`, `staging`, and `main`
- build Docker image on `dev`, `staging`, and `main`

## Branch workflow
- `dev`: active changes
- `staging`: deploy candidate
- `main`: production
