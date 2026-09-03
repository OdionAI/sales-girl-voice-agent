# Wema composite tools

See [the task and endpoint inventory](TOOL_INVENTORY.md) for all customer-task
groups and documented downstream contracts. This isolated FastAPI service uses
dependencies already in the project and does not import or modify `main.py`,
LiveKit or the accepted ASR/TTS/LLM tuning.

## Architecture decision

Use composite HTTP APIs first. The dashboard and voice agent already support
HTTP tool records, including request schemas and service headers. The composite
workflow classes are transport-independent, so a later MCP adapter can expose
the same methods without duplicating bank logic. MCP should be added when a
consumer actually needs MCP discovery/session transport; it is not required for
the dashboard integration and does not replace workflow state.

This branch deliberately excludes customer/voice/FIDO2 authentication. That work
belongs on `wema-bank-poc-auth` and can supply customer context plus an authorized
transaction executor when merged. `X-Service-Token` is only protection for this
internal composite service; it is not bank-customer authentication.

## Current implementation

`wema_tools/live_bank.py` contains fixed routes for 17 unambiguous endpoints from
the bank PDF. It does not accept arbitrary origins or paths. The FAQ contract is
held back because the PDF specifies a base ending in `/chat` and also a `/chat`
endpoint, and its origin is production while the other banking services are
development. We should not guess between `/chat` and `/chat/chat`.

Seven dashboard-compatible tools are exposed:

| Tool | Composite behavior |
| --- | --- |
| `wema_get_balance` | A1 owned accounts -> A2 selected account details |
| `wema_get_transactions` | A1 account ownership -> A3 history |
| `wema_list_data_plans` | D2 current network/package catalogue |
| `wema_list_transfer_banks` | T4 current destination-bank directory and optional search |
| `wema_prepare_data_purchase` | A1 ownership -> fresh D2 package/price check -> immutable preview |
| `wema_prepare_transfer` | A1 ownership -> T4 bank resolution -> T3 name enquiry -> immutable preview |
| `wema_execute_prepared` | Integration point that currently returns blocked; no bank write occurs |

In `live` mode those first six tool paths use the documented Wema development
services. The current D2 service differs from the PDF: it returns `result` at the
top level rather than under `value.result`; the connector safely supports both.
In an actual smoke test it returned four network groups. T4 returned 499 banks but
omits the documented `abbreviation` field; the connector treats that field as
optional without inventing it. No account, recipient or transaction endpoint was
probed without approved test customer data.

`mock` mode remains available for deterministic unit and LLM-routing evaluation.
It is not the default in the current local process. Responses always identify
their source with `mode: "live"` or `mode: "mock"`.

Transaction calls remain split into prepare and execute so the caller can confirm
the resolved recipient/package and amount. The transaction executor is an
explicit merge boundary; this branch does not call D1, D3, B3, T1, T2 or S1.
`LiveBank.submit` implements fixed write transport and response-envelope checks,
but it is internal and not reachable from a tool route. The auth branch's executor
can call it after validating a prepared operation. This avoids building a second
authentication implementation or submitting with untrusted model-supplied fields.

## Run

```sh
export WEMA_TOOLS_SERVICE_TOKEN="$(openssl rand -hex 32)"
export WEMA_BANK_MODE=live
export WEMA_ACCOUNT_MAINTENANCE_API_KEY="from-secret-manager"
./.venv/bin/python -m uvicorn wema_tools.api:create_app --factory --host 127.0.0.1 --port 8097
```

The documented HTTPS development origins are defaults. Each can be overridden
with an HTTPS origin-only variable such as `WEMA_TRANSFERS_BASE_URL`; see
`DOCUMENTED_BASE_URLS`. `WEMA_BANK_TIMEOUT_SECONDS` defaults to 12 seconds.
Account endpoints require `WEMA_ACCOUNT_MAINTENANCE_API_KEY` server-side.
Never put that key in a tool schema, prompt, tracked env file or log.

The current workspace process is live at `http://127.0.0.1:8097` in screen session
`sg-wema-tools-live`. Its local configuration is in `.env.wema-tools.local`, which
is git-ignored and mode 600. `/health` is only liveness metadata; it reports the
connector mode and that writes are disabled. It is not a substitute bank endpoint.

## Tool transport

`GET /v1/tool-definitions` with `X-Service-Token` exports records in the exact
dashboard shape: `name`, `description`, `method`, `url`, `request_schema`, and
`is_active`. Add the service token as a server-side custom header on each record;
the definitions do not embed it.

Calls are `POST /v1/tools/{tool_name}` with a typed JSON object. Required internal
headers are `X-Service-Token`, `X-Business-Id`, `X-Agent-Id`, `X-Session-Id` and
`X-End-User-Id`; the existing dynamic HTTP adapter sends the metadata headers.
Live account-dependent tools additionally need `X-Wema-Customer-Id` supplied by
trusted session context. It is purposely not an LLM/tool argument. Until the auth
branch supplies that context, data-plan and bank-directory reads work live while
account-dependent composites return a clear failure.

| Tool | Body |
| --- | --- |
| `wema_get_balance` | Optional `source_account` |
| `wema_get_transactions` | Optional `source_account`, `count` (1-20, default 5) |
| `wema_list_data_plans` | Optional `network` |
| `wema_list_transfer_banks` | Optional bank name/code/abbreviation `query` |
| `wema_prepare_data_purchase` | `network`, returned `package_id`, `phone_number`; optional `source_account` |
| `wema_prepare_transfer` | `bank`, `recipient_account`, decimal-string `amount`; optional `narration`, `source_account` |
| `wema_execute_prepared` | `operation_id` only |

All inputs reject unknown fields. Package prices come from a fresh D2 response.
Bank/recipient fields come from T4/T3, not model assertions. A new valid
preparation replaces the session's previous preview; previews expire in five
minutes and bind to business, agent, session, caller and customer context. Money
uses decimal strings. The execute route accepts no `confirmed`, customer, amount,
proof or credential fields and cannot write on this branch.

Common results contain `mode`, `status`, `message` and `data`. Status is one of
`ok`, `needs_input`, `prepared`, `blocked`, or `failed`. HTTP/service errors are
normalized without returning upstream response bodies. Missing/invalid inputs
make no downstream request.

## Test agent

Create a new Wema test agent rather than changing an existing production agent.
Use [AGENT_PROMPT.md](AGENT_PROMPT.md), add only the seven exported records, and
retain the stable audio/model configuration. A worker running directly on this
machine can reach `http://127.0.0.1:8097`; a container needs a host address it can
resolve. The browser does not call these APIs directly.

For live account/transfer testing we still need a Wema development `customerId`
and owned source account approved for the POC. For T3 we also need approved test
recipient bank/account pairs. Do not use arbitrary real customer accounts.

## Verification

```sh
PYTHON_DOTENV_DISABLED=1 ./.venv/bin/python -m unittest discover -s tests -p 'test_wema_tools.py'
PYTHON_DOTENV_DISABLED=1 ./.venv/bin/python -m unittest discover tests
```

Tests cover both documented/current D2 envelopes, fixed routes and headers,
upstream failures, composite order, owned-account checks, package/network and
bank matching, amount validation, preview replacement/expiry/context isolation,
blocked writes, and the existing dynamic HTTP adapter end to end in-process.
