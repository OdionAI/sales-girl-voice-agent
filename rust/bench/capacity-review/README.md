# Capacity and existing-account acceptance

This runs the actual native worker entrypoint with official LiveKit SDK SIP
participants and the unchanged Conversation capacity router from commit
`a9feeeb`. Authentication, billing, agent configuration and ASR are local fixtures.
No production URL, key, account, trunk or quota is used. The driver clears its
worker child's environment and supplies a loopback-only operator configuration.

The checks contend for eight slots with 24 requests, start eight native SIP
sessions, observe heartbeats, hang up a caller, race 12 requests for the freed
slot, and verify one-use releases and zero active leases. The worker must publish
RTC output and reach ASR-ready state. The human/IVR destinations are **simulated
HTTP sinks**, not an external AICC/PSTN switch. Actual AICC handoff remains a
separate acceptance gate requiring a non-production AICC route.

The second half uses real HTTP through local dashboard/frontend production
builds, joins their returned RTC rooms, and requires a ready packet from the Rust
worker. It covers public dashboard, logged-in owner testing, frontend public URL,
custom-host resolution, spoofed tenant/worker inputs and billing refusal.

## Reproduce

Use the unchanged Conversation-service checkout at `a9feeeb` and its isolated
Python requirements environment. This Python process is the existing capacity
API, not a voice-worker bridge. Start the fixture from a fresh temporary working
directory, with `PYTHONPATH` containing that checkout and this directory:

- `DATABASE_URL=sqlite+pysqlite:////private/tmp/sg-capacity-UNIQUE/fixture.db`
- `AUTH_JWT_ALGORITHM=HS256`, a local fixture `AUTH_JWT_SECRET_KEY`
- `SERVICE_AUTH_ENABLED=true`, `SERVICE_AUTH_TOKEN=local-internal-service-token`
- `AUTH_SERVICE_BASE_URL=http://127.0.0.1:17930`
- `MOMO_CALL_CAPACITY_BUSINESS_ID=11111111-2222-3333-4444-555555555555`
- `MOMO_CALL_CAPACITY_MAX_CONCURRENT=8`

Run `python -m uvicorn server:app --host 127.0.0.1 --port 17930`.
Use a local LiveKit dev server at port 17880 (`devkey`/`secret`). Run the dashboard
production build on loopback 17931 with all auth/agent/billing service URLs set
to the fixture, all service tokens `local-internal-service-token`, LiveKit URL
`ws://127.0.0.1:17880`, `FORCE_ENV_LIVEKIT_URL=true`, and
`RVC_LAB_LIVEKIT_AGENT_NAME=sg-capacity-review-local`. Run the standalone frontend
on loopback 17932 with `DASHBOARD_CALL_API_BASE_URL=http://127.0.0.1:17931`,
`AUTH_SERVICE_BASE_URL=http://127.0.0.1:17930`, `WEB_IDENTITY_MODE=email`.
Start these processes with explicit fixture environments, without production
`.env` files. The fixture owner cookie used by the driver works only against the
local authentication stub.

From **rust/** (so the macOS SDK's `-ObjC` linker config is loaded):

```
cargo test --locked --release --all-features --bin rvc-capacity-smoke --no-run
```

Run the printed executable with a fresh output-directory argument. It spawns
itself in `--platform` mode to execute the exact production worker code. The
acceptance binary is excluded from default test execution and the production
runtime image. Retain `summary.json` for evidence; raw traces stay local.

## 2026-10-31 result

24 contenders admitted exactly 8; simulated human/IVR branches received 8 each.
Hangup released a slot in 58 ms in this local trial; 12 contenders admitted one
replacement. All 9 leases were released once, with 0 active after hangup. All four
web paths reached a ready native session; insufficient credits returned 402.
See `rust/reviews/capacity-and-web-2026-10-31.json`.

The test first reproduced stranded capacity: reporting 7/8 as load 0.875 gave
zero server affinity above LiveKit's target load. Fixed-slot admission now reports
available/full capacity as load 0/1 and retains actual `job_count`. The regression
checks server affinity at every occupancy, and the real-server run confirms reuse.
Lease release follows RTC cleanup. Native calls also keep the worker's configured
ASR URL instead of receiving the dashboard's old automatic infrastructure URL.
