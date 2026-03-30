# SalesGirl Voice Agent

The LiveKit worker/runtime layer for SalesGirl voice sessions.

## Responsibilities
- join LiveKit rooms
- resolve agent config
- persist conversation/session activity
- call demo CRM hooks

## Hotel live availability contract
For hotel agents, the optional live availability endpoint should be a full `http` or `https` URL that returns current room inventory and pricing.

### Request
The voice agent sends a `POST` request with JSON:
```json
{
  "room_type": "Deluxe King",
  "check_in_date": "2026-04-01",
  "check_out_date": "2026-04-03",
  "guest_count": 2
}
```

### Expected response
The service may return either an object or an array.

Object example:
```json
{
  "status": "success",
  "rooms": [
    {
      "room_type": "Deluxe King",
      "available": true,
      "price": 24500,
      "currency": "NGN",
      "notes": "Breakfast included"
    }
  ]
}
```

Array example:
```json
[
  {
    "room_type": "Deluxe King",
    "available": true,
    "price": 24500,
    "currency": "NGN"
  }
]
```

### Notes
- If the endpoint is missing, the agent falls back gracefully and does not invent live pricing.
- The dashboard stores the endpoint as `live_data_endpoint` in the business profile.
- The voice runtime reads the configured endpoint from the agent/session context or the direct tool argument.

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
- `HOTEL_OPS_SERVICE_BASE_URL`
- `AGENT_CLIENT_ID`
- `REQUIRE_VERIFIED_PHONE`

The staging/prod environment contract for this repo is documented centrally in:
- `sales-girl-platform-infra/docs/voice-stack-wiring.md`

## CI
- install dependencies on `dev`, `staging`, and `main`
- build Docker image on `dev`, `staging`, and `main`

## Branch workflow
- `dev`: active changes
- `staging`: deploy candidate
- `main`: production
