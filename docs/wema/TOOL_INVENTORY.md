# Wema composite banking tools

Branch: `wema-bank-poc-tools`. Source: **ALAT SAW Voice Banking System Technical
Documentation**, dated August 24, 2026, 27 PDF pages. Page numbers below refer to
the PDF page, not the stale "Page ... of 11" header. The PDF has 18 downstream
endpoints and nine enumerated intents (page 11); the component diagram says 16
intents, so the bank should clarify the discrepancy.

This document distinguishes documented contracts from our proposed orchestration.
It does not copy the source PDF or its embedded credentials into source control.

## Task grouping

All customer identity, debit account ownership and authentication state must come
from a trusted backend session, never from model-supplied customer IDs or flags.
Account selection is still a customer choice when multiple owned accounts exist.

| Customer task | Composite tool(s) | Downstream sequence | Customer slots | Current implementation |
| --- | --- | --- | --- | --- |
| Check balance | `wema_get_balance` | A1 owned accounts -> select account -> A2 details | Optional debit account selection | Live connector and mock; needs development customer ID |
| Recent transactions | `wema_get_transactions` | A1 ownership -> A3 history | Account selection, result count | Live connector and mock; documented `Skip=0`, `KeyWord=C` only |
| Buy airtime | Proposed `wema_prepare_airtime` -> execute prepared operation | A1 ownership -> collect network/phone/amount -> authorize -> D1 purchase | Network, phone, amount, debit account | Inventory only |
| Find/buy data | `wema_list_data_plans`, `wema_prepare_data_purchase` -> execute prepared operation | D2 plans -> select returned package -> A1 ownership -> refresh D2 price -> authorize -> D3 purchase | Network, returned package ID, phone, debit account | Live reads/preparation and mock; executor integration pending |
| Transfer money | `wema_list_transfer_banks`, `wema_prepare_transfer` -> execute prepared operation | A1 ownership -> T4 bank list -> T3 name enquiry -> confirm recipient/fees/amount -> authorize -> T2 if Wema, otherwise T1 | Bank, recipient account, amount, narration, debit account | Live reads/preparation and mock; executor integration pending |
| Pay a bill | Proposed `wema_list_bill_options`, `wema_prepare_bill_payment` -> execute prepared operation | B1 categories/billers -> B2 packages -> validate identifier if required (missing API) -> A1 ownership -> authorize -> B3 payment | Category, biller, package, identifier, amount if editable | Inventory only |
| Renew saved bill | Proposed `wema_prepare_bill_renewal` -> execute prepared operation | B4 saved beneficiaries -> select beneficiary -> refresh B1/B2 -> validate if required -> A1 ownership -> authorize -> B3 | Saved beneficiary, optional amount/package change | Inventory only; never reuse stale saved price |
| Create savings goal | Proposed `wema_prepare_savings_goal` -> execute prepared operation | A1 ownership -> validate goal terms/enums -> authorize -> S1 create | Name, target, initial deposit, dates, debit schedule and explicit auto-debit/lock choices | Inventory only; enum definitions missing |
| Product help | Proposed `wema_answer_faq` | F1 FAQ with server-owned user/session/history | Question | Inventory only; exact URL ambiguous |
| Authenticate customer | Internal auth service, not an LLM tool | V1 embedding URLs -> restricted storage retrieval -> voice checks; FIDO2 challenge/verify (missing APIs) | Frontend biometric ceremony, captured audio | Not implemented |

Optional P1 bank prediction is only a suggestion when the caller does not know the
bank. It must not replace bank selection or T3 name enquiry. The PDF mentions
transfers to beneficiaries but supplies no transfer-beneficiary listing API. B4
is specifically for **bill** beneficiaries, not transfer recipients.

## Endpoint inventory

Service origins below are fixed defaults for live mode. Mock mode never contacts
them. Write routes are mapped but not invoked on this branch.

| Service | Documented base URL | Pages |
| --- | --- | --- |
| D: Airtime/data | `https://airtimeanddataplatformservice-alat-two.apps.alatarodev.westeurope.aroapp.io` | 12-15 |
| B: Bills | `https://billspayment-alat-two.apps.alatarodev.westeurope.aroapp.io` | 15-19 |
| T: Transfers | `https://transferplatformservice-route-alat-two.apps.alatarodev.westeurope.aroapp.io` | 19-22 |
| S: Savings | `https://savings-alat-two.apps.alatarodev.westeurope.aroapp.io` | 22-23 |
| A: Accounts | `https://accountmaintenance-route-alat-two.apps.alatarodev.westeurope.aroapp.io` | 23-24 |
| P: Bank prediction | `https://bankprediction.westeurope.inference.ml.azure.com` | 25 |
| F: FAQ | PDF base already ends `/chat`; endpoint is also `/chat`. Confirm one `/chat` versus `/chat/chat`; do not guess or call production. | 25-26 |
| V: Voice storage | `https://voicebankingsetup-route-alat-two.apps.alatarodev.westeurope.aroapp.io` | 26-27 |

| ID | Method and path | Request/response contract notes |
| --- | --- | --- |
| D1 | POST `/api/Airtime/PurchaseAirtimeV2` | `accountNumber`, `network`, `cif`, `phoneNumber`, `amount`, `authenticationId`, `fido2CredentialProof`; beneficiary/auto-topup options. Response: `isSuccess`, `value.hasError`, `value.result`. |
| D2 | GET `/api/Data/GetDataPlans` | No query documented. `value.result[]` groups `dataPackages[]` by `networkProvider`; package IDs, prices and validity come from this result. |
| D3 | POST `/api/Data/PurchaseDataV2` | D1-like fields plus `packageId`; server derives amount from selected fresh package, not a model quote. |
| B1 | GET `/api/SharedBillsPayment/GetBillerCategories/{customerId}` | `data[]` contains nested billers with identifier label, validation flag and charge. |
| B2 | GET `/api/SharedBillsPayment/GetBillerPackages/{billerId}` | `data[]`: price, charge, editable flag, minimum/maximum amount and optional feature flags. |
| B3 | POST `/api/SharedBillsPayment/PayBillV2` | `cif`, `customerAccount`, `amount`, `charge`, `packageId`, `customerIdentifier`, trusted customer contact details, proof fields; optional beneficiary fields. `data` result envelope. |
| B4 | GET `/api/Beneficiaries/GetSavedBeneficiaries/{customerId}` | Saved bill/package/identifier details; revalidate current price before purchase. |
| T1 | POST `/api/InterbankTransfer/VBSendMoneyToOtherBank` | Source/destination account, verified destination name/bank, amount, narration, transaction UUID, channel, proof fields, beneficiary choice. |
| T2 | POST `/api/IntrabankTransfer/VBSendMoney` | Wema route, bank code `035`; similar to T1 but no destination account name in sample request. |
| T3 | GET `/api/Shared/AccountNameEnquiry/{bankCode}/{accountNumber}` | `result`: account identity, currency, terms and `chargeFee[]` amount bands. Fee calculation rules are not specified. |
| T4 | GET `/api/Shared/GetAllBanks` | `result[]`: bank name, code, logo, abbreviation. |
| S1 | POST `/api/goal/create` | Goal/debit/target amounts and dates, customer/profile/account fields, enum-valued goalType/frequency/tenor/duration, optional savings triggers, proof fields. `status` string, `value`, `eventId`. |
| A1 | GET `/api/account_maintenance/accounts?customerID={customerId}` | Bare account array containing available/ledger balances; `x-api-key` required. Treat entire response as sensitive. |
| A2 | GET `/api/account_maintenance/account_details?accountNumber={accountNumber}` | Bare account detail object. Enforce ownership using A1 first. |
| A3 | GET `/api/account_maintenance/transaction_history?Skip=0&Count={take}&AccountNumber={accountNumber}&KeyWord=C` | Bare array of title/date/amount. Meaning of `C`, pagination and filtering need clarification. |
| P1 | POST `/score` | `account_number` -> candidate banks/probability strings. Not authoritative recipient verification. |
| F1 | POST `/chat` (full URL unresolved) | `user_id`, `session_id`, `message`, `faq_history` -> `answer`. Documented origin is production, unlike other development services. |
| V1 | GET `/api/voicebanking/customersamplevoices/{customerId}` | `data[]` has embedding URLs. Retrieval is internal only, with storage-host restrictions and credentials outside the model. |

## Composite contract

Use a small set of task-named HTTP tools with typed JSON inputs. Raw bank API paths,
auth proofs, CIFs, customer IDs, API keys and storage URLs are not tool arguments.
The existing `agent/dynamic_tools.py` already sends a tool's top-level JSON body and
business/agent/session/caller metadata to a configured HTTP URL. No audio-runtime
or dashboard code change is necessary for this first slice.

Preparation returns a short preview and an opaque `operation_id`. It must not
execute any write. Execution takes only that ID, not revised recipient/amount
fields or `confirmed=true`. The backend retrieves the immutable prepared payload,
checks binding to the customer/session, freshness and authorization, and executes
once. Any correction requires a new preparation and a new confirmation.

Proposed live state machine:

`needs_input -> prepared -> awaiting_authorization -> submitted -> pending -> succeeded/failed`

Also support `blocked`, `expired`, `cancelled`, and `unknown` (for an ambiguous
write timeout). An HTTP 200, `isSuccess=true` or undocumented `status=0` is not
proof that money moved. Airtime/data/transfers have asynchronous completion in
the document (pages 6-8). Never retry a write blindly after a timeout; reconcile
by reference after the bank supplies the status/idempotency contract.

Auth is shared orchestration, not model reasoning. Pages 6-8 require first-level
voice verification, filled slots, customer authorization, FIDO2 verification and
second-level voice verification before handler execution. Confirm the bank's
read-only exemptions, if any; until then gate sensitive live reads too. Our mock
reads expose only synthetic fixtures and **do not demonstrate authentication**.

Keep source-account ownership checks, package validation, amounts, fees, errors
and authorization deterministic. Initially disable saving beneficiaries,
auto-topup, BNPL and recurring/locked savings unless separately confirmed and
covered by a verified contract. Do not add them as incidental purchase effects.

## HTTP versus MCP

Start with the dashboard's existing HTTP API tools. Keep orchestration independent
of FastAPI so an MCP adapter can later expose the same functions and schemas.
MCP is a transport/discovery option, not a substitute for workflow state,
authorization or idempotency. There is no MCP consumer wired into this runtime
today. Adding one now would increase the change surface without improving these
first workflow tests.

## Questions for the bank before live mode

1. Sandbox access and credentials per service, trusted app-session/customer/CIF/
   profile/account mapping, scopes, limits and required channel headers.
2. FIDO2 challenge/verification endpoints, exact proof types (samples use null,
   string and array), authenticationId lifecycle and how each proof binds to
   transaction details. Voice model, thresholds, liveness, freshness and both
   authentication levels are unspecified.
3. Transaction status enum meanings, rejection codes, WebSocket URL/auth/event
   schemas and correlation IDs, reconciliation endpoint and idempotency behavior.
4. Bill identifier-validation endpoint, renewal semantics and authoritative fee
   rules. Required validation must never be silently skipped.
5. Valid airtime networks/amount limits; savings enums, date rules, recurring
   debit/lock implications; history `KeyWord=C` semantics and pagination.
6. Transfer-beneficiary retrieval, transaction limits, currency support and fee
   band boundaries/tax rules. Bank prediction authentication and reliability.
7. Correct FAQ URL and a non-production test environment. Enrollment/embedding
   schemas and permitted storage retrieval hosts.

The source PDF includes an account-maintenance API key and a storage account key.
Do not commit it, fixture those credentials, log proofs or expose them to tools.
Ask the bank to provision scoped sandbox secrets through its approved secret
channel and review rotation of the distributed credentials.

## Delivery order

1. This change: complete grouping, live and mock balance/history/data/transfer
   preparation, dashboard-shaped definitions, draft agent prompt and tests.
2. Connect a dedicated test agent to these tools; evaluate slot collection,
   corrections and routing using `README.md`. No live transactions.
3. Add airtime, bills/renewals, goals and FAQ after agreeing their contracts.
4. Merge trusted customer-session and approved transaction-executor integration
   from `wema-bank-poc-auth`; add durable operation storage, confirmation UI and
   audit redaction before enabling bank writes.
5. Enable transaction adapters one workflow at a time, then measure end-to-end voice/task
   latency. MCP remains optional. No changes to the accepted ASR/TTS/LLM tuning.
