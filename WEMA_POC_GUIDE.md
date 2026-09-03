# Wema Bank POC Guide

This branch is the working branch for the Wema Bank voice banking POC.

## Branch

- Branch name: `wema-bank-poc-tools`
- Baseline branch: `wema-bank-poc`, incorporating the accepted `stable` runtime
- Main implementation repo: `sales-girl-voice-agent`
- Supporting UI repo, if needed: `sales-girl-dashboard`

New Wema tool implementation work should happen on `wema-bank-poc-tools`, not
on the stable runtime branches or old temporary experiment folders. The current
API grouping and mock implementation are documented in
[`docs/wema/README.md`](docs/wema/README.md) and
[`docs/wema/TOOL_INVENTORY.md`](docs/wema/TOOL_INVENTORY.md).

## Goal

Extend the existing LiveKit-based voice-agent runtime so it can support Wema
Bank voice banking flows.

The POC has three major parts:

1. Implement Wema/ALAT APIs as agent tools.
2. Add voice authentication as a background process during calls.
3. Later, design the voice onboarding/enrollment flow for customers.

The first priority is the banking tools and transaction-time voice
authentication. Voice onboarding is important, but it should come after the
runtime transaction/authentication model is clear.

## What we are building

The agent should speak naturally with a customer, understand what the customer
wants to do, collect the required details, confirm the action, and then call the
right tool.

Supported banking categories should be derived from the Wema API documentation,
including:

- Airtime purchase
- Data subscription
- Transfers
- Balance/account enquiries
- FAQ/help flows
- Any other Wema-supported action confirmed in the API documentation

Each API should become a function tool with:

- tool name
- short description
- required user-provided details
- required system/session details
- API endpoint and request payload
- expected response shape
- success behavior
- failure behavior
- authentication/security requirement

## Task 1 — Wema banking tools

This task is to turn the Wema API documentation into agent tools.

Do not create a flat, confusing list of endpoints. Group the tools by what the
customer is trying to do.

Example categories:

### Airtime

Tools should cover the full airtime flow, including any API needed to identify
network/provider options and complete an airtime purchase.

The agent should collect:

- network/provider
- phone number
- amount
- confirmation from the user

### Data subscription

Tools should cover fetching available data bundles and purchasing the selected
bundle.

The agent should collect:

- network/provider
- phone number
- available bundle list from the API
- selected bundle
- confirmation from the user

Important behavior: the agent should not invent bundle options. It should fetch
available bundles first, then help the user select one of the returned options.

### Transfers

Tools should cover bank lookup, recipient/account verification, and transfer
execution or transfer preparation.

The agent should collect:

- destination bank
- account number
- recipient name from name enquiry, where the API supports it
- amount
- narration, if needed
- confirmation from the user

Important behavior: the agent should confirm the recipient and amount before any
transfer execution.

### Balance and account enquiries

Tools should cover account lookup, balance checks, and other safe account
information supported by the API.

These tools should require the correct authentication state before returning
sensitive information.

### FAQ/help

FAQ/help flows should answer general product or process questions without
triggering transaction tools.

## Task 1 deliverable

The first deliverable is a Wema tool inventory and tool implementation set.

For each tool, provide:

- purpose
- input schema
- output schema
- required slots
- whether it is read-only or transactional
- whether it requires voice authentication
- mock response for local testing
- live API integration notes

The local test goal is to run an agent that has these tools and verify that:

- it chooses the right tool for each user request
- it asks for missing details
- it handles user corrections
- it can switch between intents during the same conversation
- it does not call irrelevant tools
- it confirms before transactional actions

## Authentication integration boundary

Authentication is owned by the separate `wema-bank-poc-auth` branch and is out of
scope for `wema-bank-poc-tools`. This branch defines task orchestration, resolves
downstream data, prepares immutable transaction previews, and exposes a narrow
execution integration point. It must not create a competing authentication flow.

When the auth branch is merged, its trusted session/customer context and approved
transaction executor should be injected at this boundary. The requirements below
are retained as integration requirements, not work assigned to this branch.

Voice authentication should run asynchronously in the background while the
conversation continues.

The customer should not have to wait silently while the system processes voice
authentication.

Expected behavior:

1. Start voice-auth processing as early as possible during the call.
2. Store the result in session state.
3. Let the conversation continue while authentication is pending.
4. Block sensitive transactions unless authentication has passed.
5. Fail closed if authentication fails, times out, or is unavailable.

Suggested session auth state:

- `auth_not_started`
- `auth_pending`
- `auth_passed`
- `auth_failed`
- `auth_unavailable`

## Transaction gate

Transactional tools must not complete just because the LLM called them.

Before execution, each sensitive tool should check:

- all required user details have been collected
- the user explicitly confirmed the action
- voice authentication passed
- the API call is allowed for the current workflow

If any required condition is missing, the tool should return a blocked result
and explain the next step. It should not call the live Wema API.

## Non-negotiables

- Do not fabricate API responses, account details, balances, recipient names, or
  transaction references.
- Do not invent data bundles or bank options if the API is supposed to provide
  them.
- Do not bypass authentication gates for transactional or sensitive tools.
- Do not report a transaction as successful unless the API returns a clear
  success result.
- If voice authentication fails or is unavailable, transactions must fail closed.
- Keep Wema-specific logic scoped to the Wema branch/configuration.
- Keep logs detailed enough to debug routing, tool calls, auth state, and API
  outcomes.

## Testing expectations

Test with both scripted and live voice conversations.

Minimum scenarios:

- airtime happy path
- data subscription happy path
- transfer happy path
- missing details
- user correction before confirmation
- user changes intent mid-conversation
- ambiguous user request
- tool call attempted before confirmation
- tool call attempted before voice authentication passes
- voice authentication failure
- Wema API failure

The POC is working when the agent can complete the supported flows in test mode,
use the correct tools, avoid irrelevant tools, and block transactions whenever
the auth/session gate is not satisfied.
