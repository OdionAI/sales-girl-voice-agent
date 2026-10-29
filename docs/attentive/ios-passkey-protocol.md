# Private iOS FIDO2 contract — SDK brief Revision 2

Internal backend contract; exclude from public SDK documentation/distribution.
An opt-in simulated-bank worker now implements this contract for native SDK
tests. Real banking worker handlers and the Wema authorization gate remain
separate integration requirements.
The SDK implements the Revision 2 SDK brief dated 2026-10-21. The proposed topic
name below must be confirmed by the backend team before rollout.

## Routing and call start

Use reliable, destination-targeted structured packets on `attentive.fido2.v1`,
never chat, transcripts, tool activity or model arguments. The topic carries the
protocol version. An optional JSON `version` must equal 1; it is not required by
the brief. Maximum envelope size is 14,000 UTF-8 bytes.

The SDK selects the single agent-kind participant as the assigned worker. Room
permissions must reserve that role for the trusted worker. An ambiguous room
with multiple agents cannot perform a ceremony. No capability attribute or
acknowledgement handshake is required. At call start, once the assigned worker
is available, the SDK sends one context message:

```json
{"type":"session_context","correlationId":"SDK_CONTEXT_REQUEST_ID","deviceImei":null}
```

The context correlation ID identifies that context message only. It is not a
nonce that the worker must echo on later ceremonies. `deviceImei` is the optional
opaque identifier supplied in code by the signed-in host. Bind it to the caller
from the verified token; it does not prove identity. Reconnection sends a new
context once the assigned worker is available and invalidates any active prompt.
A failed context send is not retried automatically.

## Ceremony and native options

```json
{
  "type":"fido2_ceremony",
  "authenticationId":"WEMA_ID_VERBATIM","correlationId":"WORKER_REQUEST_ID",
  "action":1,"feature":"TransferFunds","mode":"authenticate",
  "expiresAt":"2099-10-21T12:34:56.123Z",
  "options":{"rpId":"auth.bank.example","challenge":"AQID","userVerification":"required","allowCredentials":[]}
}
```

`expiresAt` is an ISO-8601 timestamp with time zone, with optional fractional
seconds. Numeric Unix timestamps are rejected. Already expired requests are
ignored without opening a sheet or changing a previous authentication result.
The client timer is a UI guard; the server must enforce expiry authoritatively.

Every ceremony message requires a nonempty `authenticationId` and `correlationId`
(up to 256 UTF-8 bytes each). Replies echo both strings verbatim. Match both on
results and aborts. The SDK prevents authentication-ID/correlation-ID reuse in a
call, including reconnects. Duplicate active requests do not reopen a prompt;
another request while busy gets `fido2_failed` with reason `busy`. Withdraw the
old ceremony with a matching `fido2_abort` before starting its replacement.

Bind each ID to the authenticated customer, call and prepared transaction,
including amount/payee/operation/quote expiry. Never derive these from model text.
A changed quote needs a fresh challenge and IDs. Disconnected/expired/cancelled
ceremonies must not execute. Call teardown invalidates late native callbacks.

`options` is the flat WebAuthn public-key object, not a `publicKey` wrapper:

- Assertion: `rpId`, `challenge`, `allowCredentials`, `userVerification`, `timeout`.
- Registration: `rp: {id,name}`, `user: {id,name,displayName}`, `challenge`,
  `pubKeyCredParams`, `excludeCredentials`, `authenticatorSelection`, `attestation`,
  `timeout`. Registration is a separate native request and response schema.
- RP IDs must match the host-configured exact allowlist. IDs and challenges use
  canonical unpadded base64url. User handles are opaque bank values, not a CIF in
  a display field.
- `required`, `preferred` and `discouraged` user verification are mapped unchanged
  to the native request. Omission keeps WebAuthn's `preferred` default. For bank
  approval, the backend must request `required` and Wema must verify its policy;
  the SDK does not silently strengthen or weaken the supplied options.
- `timeout` is milliseconds and bounds the native prompt lifetime together with
  the host timeout and server expiry. The shortest limit wins.
- Native platform credentials are discoverable and ES256. Registration accepts
  platform attachment, explicit `residentKey: required` / `requireResidentKey: true`
  or omitted selection constraints. Other explicit resident-key preferences and
  cross-platform attachment fail with `unsupported_options`; they are not dropped.
- All algorithm descriptors are validated, then ES256 (`alg: -7`) is selected from
  the offered alternatives. No supported alternative means explicit failure.
- Only omitted/`none` attestation, empty extensions and omitted/empty/`internal`
  credential transport hints are supported. Unsupported hints/requirements fail.
  Nonempty registration exclusions require iOS 17.4; older iOS rejects them.

## Proofs and results

Assertion proof:

```json
{"type":"fido2_proof","authenticationId":"WEMA_ID_VERBATIM","correlationId":"WORKER_REQUEST_ID","proof":{"id":"...","rawId":"...","type":"public-key","response":{"clientDataJSON":"...","authenticatorData":"...","signature":"...","userHandle":null}}}
```

Registration proof has only `clientDataJSON` and `attestationObject` in its
response, not assertion signature/authenticator-data/user-handle fields. The SDK
serializes credentials; it never calls Wema or validates the proof cryptographically.
The isolated test bank validates synthetic signatures only in its test harness.

Native success leaves `actionAuthentication` pending. After Wema verifies the
proof, return the matching IDs and a JSON boolean:

```json
{"type":"fido2_result","authenticationId":"WEMA_ID_VERBATIM","correlationId":"WORKER_REQUEST_ID","accepted":true,"outcome":"executed"}
```

Only `accepted: true` after proof submission and before expiry marks the action
verified. `accepted: false` marks it failed. Missing/non-boolean acceptance,
wrong IDs, results before a proof, and stale results are ignored. Legacy
`status`/`authenticated` fields cannot authorize it. `outcome` never changes
whether authentication passed; transaction completion continues through existing
tool-outcome events and agent speech. Registration success alone cannot authorize
money movement; Wema must define its acceptance semantics.

Cancellation sends `fido2_cancelled` with both IDs, leaves authentication pending
and keeps the call usable. Failure sends `fido2_failed` with both IDs and `reason`:
`invalid_request`, `unsupported_options`, `untrusted_relying_party`, `expired`,
`busy`, `replayed`, `unavailable`, `presentation_unavailable`,
`authorization_failed` or `invalid_credential`.

The optional matching `fido2_abort` withdraws a ceremony and returns it to pending.
No native prompt, proof or completed ceremony is automatically retried. A fresh
ceremony uses a new correlation ID, including when retrying the same prepared
operation, and is needed after reconnect. Once AuthenticationServices returns a
credential, the SDK forwards it for backend verification even if the presenting
SwiftUI window or scene state changed while the Face ID sheet was open. Legacy action-status messages and spoken or
typed confirmation cannot substitute for bank acceptance while passkeys are enabled.
Never log, record or expose options/proofs/device IDs to the model or public events.

## Acceptance before release

Confirm Wema's companion guide and assertion/registration serialization vectors,
base64 versus base64url, approved RP domains and per-app associated-domain files,
device identifier meaning, registration authorization semantics, synced/device-bound
credential expectations and user-verification/attestation policy. These are external
acceptance requirements, not assumptions established by the SDK tests.

Both headless and supplied-UI entry points receive code-level passkey configuration.
The core supports iOS 16; supplied UI requires iOS 17. Integration notes must retain
that distinction. Automated state tests, typed native-request tests and a simulated
bank ceremony do not replace real-iPhone passkey and Wema test-account acceptance.
