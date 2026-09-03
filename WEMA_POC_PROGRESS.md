# Wema Bank POC Progress Log

This file tracks the current state of the Wema Bank POC branch so another
engineer or agent can pick up the work without losing context.

## Current branch

- Branch: `wema-bank-poc-tools`
- Baseline: `wema-bank-poc`, incorporating the accepted `stable` runtime
- Main implementation repo: `sales-girl-voice-agent`
- Supporting repo if UI changes are needed: `sales-girl-dashboard`

## Current status

- Both voice-agent and dashboard working copies are on `wema-bank-poc-tools`.
- The 27-page bank PDF has been reviewed; 18 endpoints are grouped by customer
  task in `docs/wema/TOOL_INVENTORY.md`, with missing contracts explicitly listed.
- `wema_tools/` contains an isolated HTTP composite service with real Wema
  development-service read adapters plus deterministic mock mode. It handles
  balance/history, data and bank directories, and data/transfer preparation. The
  execute tool always blocks on this branch; it cannot submit a bank transaction.
- A draft test-agent prompt and caller evaluation script are in `docs/wema/`.
- No Wema agent has been created or attached in the dashboard by this change.
- Authentication is explicitly out of scope here and owned by
  `wema-bank-poc-auth`; this branch exposes its integration boundary only.
- No Wema onboarding flow has been implemented yet.
- Airtime, bills, savings and FAQ remain inventory/design only.
- Live D2 data-plan and T4 bank-directory GETs were checked. They returned four
  network groups and 499 banks. No account, recipient or transaction endpoint was
  called without approved test data. No remote infrastructure, audio tuning or
  dashboard changes were made.
- These changes have not been committed or pushed yet.

## Decisions so far

- New tool work happens on `wema-bank-poc-tools`; preserve the stable audio baseline.
- The POC should extend the existing LiveKit voice-agent project rather than
  living in a separate standalone folder.
- This branch owns Wema API tools. The auth branch owns authentication.
- Voice onboarding/enrollment will be designed after the transaction-time
  authentication model is working.
- Transaction tools must be blocked unless confirmation and authentication
  requirements are satisfied.

## Done

- Cleaned up previous isolated Wema experiment artifacts.
- Pushed dashboard integration work into `full-livekit-test`.
- Pushed voice-agent LiveKit baseline into `full-livekit-test`.
- Created `wema-bank-poc` branch locally.
- Added `WEMA_POC_GUIDE.md`.
- Added `WEMA_POC_PROGRESS.md`.

## Next work

1. Review the task grouping and connect a dedicated Wema development agent using
   the seven exported HTTP tool definitions; do not attach raw bank endpoints.
2. Evaluate the scripted conversations and record intent/slot/correction errors.
3. Obtain approved development customer/source/recipient data, plus the missing
   bill validation, status/reconciliation and enum contracts from the bank.
4. Implement remaining workflow connectors and durable state.
5. Merge trusted customer context/transaction executor from the auth branch, then
   enable writes one workflow at a time after integration failure tests pass.
6. Consider an MCP adapter only if an additional client requires it; the existing
   dashboard HTTP transport already reaches the composite methods.

## 2026-09-03: First composite-tool slice

- Added `wema_tools/{contracts,service,api}.py`, `tests/test_wema_tools.py`, and
  `docs/wema/{TOOL_INVENTORY,README,AGENT_PROMPT}.md`.
- Updated this log and the guide's active branch reference.
- Initial verification: 26 new tests and all 114 project tests passed with
  `PYTHON_DOTENV_DISABLED=1 ./.venv/bin/python -m unittest discover tests`.
- Latest live-connector verification: 37 Wema tool tests and all 125 project
  tests pass. Live HTTP smoke checks through the composite API return four D2
  network groups and the Wema match from the 499-bank T4 directory.
- Local live service is listening on loopback port 8097 under screen session
  `sg-wema-tools-live`. D2 and T4 read smoke checks succeeded against Wema. Its
  separate local token/API-key file is ignored by git and has mode 600.
- The pre-existing timing-metrics edits in `main.py` and
  `tests/test_recording_flow.py` were left untouched. The dashboard's pre-existing
  edit in `use-agent-call-session.js` was also left untouched.
- PDF credentials and the PDF itself were not copied into source control.
- Added `wema_tools/live_bank.py`: fixed documented HTTPS service origins/routes,
  account-key isolation, response validation and safe errors. Found and supported
  live contract differences: D2 uses top-level `result` rather than
  `value.result`, and T4 omits bank `abbreviation`.
- Added internal fixed write transport for D1/D3/B3/T1/T2/S1 as the auth-branch
  executor handoff. It is not exposed by any tool route and was unit-tested only;
  no real transaction request was sent.
- Current limitation: no approved test customer/recipient data, no live voice
  agent attachment and no transaction executor on this branch.

## How to update this file

Whenever meaningful work is completed, add:

- what changed
- files touched
- tests run
- current blockers
- next recommended step

Use this file as the handoff point between agents and engineers.
