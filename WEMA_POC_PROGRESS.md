# Wema Bank POC Progress Log

This file tracks the current state of the Wema Bank POC branch so another
engineer or agent can pick up the work without losing context.

## Current branch

- Branch: `wema-bank-poc`
- Created from: `full-livekit-test`
- Main implementation repo: `sales-girl-voice-agent`
- Supporting repo if UI changes are needed: `sales-girl-dashboard`

## Current status

- `full-livekit-test` has been pushed in `sales-girl-dashboard`.
- `full-livekit-test` has been pushed in `sales-girl-voice-agent`.
- `wema-bank-poc` has been created locally from `full-livekit-test`.
- This guide/progress documentation is being added on `wema-bank-poc`.
- No Wema banking API tools have been implemented yet.
- No Wema voice-authentication background worker has been implemented yet.
- No Wema onboarding flow has been implemented yet.

## Decisions so far

- Wema work should happen on `wema-bank-poc`.
- The POC should extend the existing LiveKit voice-agent project rather than
  living in a separate standalone folder.
- The first implementation priority is Wema API tools plus background voice
  authentication.
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

1. Push `wema-bank-poc` for the voice-agent repo.
2. Push `wema-bank-poc` for the dashboard repo if dashboard UI work will be
   needed for Wema.
3. Review the Wema API PDF and create the first tool inventory.
4. Group tools by customer workflow: airtime, data, transfer, balance/account,
   FAQ/help, and any other documented action.
5. Implement mock-mode versions of the tools first.
6. Add voice-auth session state and transaction gates.
7. Add live API calls only after mock behavior and auth gates are verified.

## How to update this file

Whenever meaningful work is completed, add:

- what changed
- files touched
- tests run
- current blockers
- next recommended step

Use this file as the handoff point between agents and engineers.

