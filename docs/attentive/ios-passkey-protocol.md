# Private iOS passkey protocol v1 — backend implementation required

Internal contract only; exclude from public SDK documentation/distribution. The
current comparison worker has no handlers for this contract. Do not
advertise its capability until the handlers and banking authorization gate exist.

## Readiness and transport

Use the existing agent connection, reliable destination-targeted data packets on
`odion.auth.passkey`, never `lk.chat`, transcripts, tool activity or LLM arguments.
Each complete envelope is limited to 14,000 UTF-8 bytes. The SDK accepts only the
single agent-kind participant advertising `attentive.passkeys.version=1`. Multiple
agents, capability removal or reconnect invalidate readiness. Room permissions
must reserve that participant role for the trusted worker.

Every message carries `version: 1`, `type` and `contextId`, a random SDK nonce per
connection. Every ceremony message also carries `authenticationId` verbatim. The
SDK prevents case-insensitive reuse within a call; the backend must enforce
customer/room/transaction binding, expiry, single use and execution idempotency.

On readiness SDK sends:

```json
{"version":1,"type":"session_context","contextId":"SDK_NONCE","deviceImei":null,"platform":"ios","supportedModes":["authenticate","register"]}
```

Bind this to the token-authenticated caller, not model text. The opaque device
identifier is host-supplied, not identity proof. Acknowledge within ten seconds:

```json
{"version":1,"type":"session_context_ack","contextId":"SDK_NONCE"}
```

Wait for acknowledgement before starting a ceremony. Reconnect/capability loss
invalidates the context and active ceremony; acknowledge a fresh context and
issue a new challenge. No proofs are replayed. Unsupported clients/workers may
converse, but must not execute passkey-required actions.

## Ceremony

```json
{
  "version":1,"type":"fido2_ceremony","contextId":"SDK_NONCE",
  "authenticationId":"WEMA_ID_VERBATIM","action":1,"feature":"TransferFunds",
  "mode":"authenticate","expiresAt":1900000000,
  "options":{"rpId":"auth.bank.example","challenge":"AQID","userVerification":"required","allowCredentials":[]}
}
```

`expiresAt` is absolute Unix seconds, bounded by the Wema challenge and prepared
quote lifetime. The client timer is an additional UI guard, not authoritative
expiry enforcement. Bind amount/payee/operation/customer to that challenge on the
server; changing a quote must invalidate its pending authorization.

`options` is the flat WebAuthn public-key object, not a `publicKey` wrapper. Native
APIs require parsing the supported fields; challenge/client-data bytes are never
regenerated or signatures inspected/verified by the SDK.

- Assertion: `rpId`, `challenge`, `allowCredentials` with public-key IDs and
  optional transport hints, `userVerification`, `timeout` (milliseconds).
- Registration: `rp: {id,name}`, `user: {id,name,displayName}`, `challenge`,
  `pubKeyCredParams` including public-key ES256 (`alg: -7`), optional
  `excludeCredentials`, `authenticatorSelection`, `attestation: none`, `timeout`.
  Nonempty exclusions require iOS 17.4; older versions reject rather than ignore.
- Platform/resident credentials and required user verification are enforced.
  Missing verification is strengthened to required; explicitly weaker values fail.
- Nonempty extensions, unknown option keys, unsupported algorithms or requested
  direct/enterprise attestation fail. Empty extension objects are permitted.
- Challenges, user handles and credential IDs use canonical unpadded base64url.
  Do not put a CIF in display fields; use Wema's opaque WebAuthn user handle.

## Replies and verification

Native assertion success sends:

```json
{"version":1,"type":"fido2_proof","contextId":"SDK_NONCE","authenticationId":"WEMA_ID_VERBATIM","proof":{"id":"...","rawId":"...","type":"public-key","clientExtensionResults":{},"response":{"clientDataJSON":"...","authenticatorData":"...","signature":"...","userHandle":null}}}
```

Registration has `clientDataJSON` and `attestationObject`, not assertion
signature/authenticatorData/userHandle fields. Confirm this minimal profile with
Wema; it does not claim to implement every browser Level 3 `toJSON()` field.

Cancellation sends `type: fido2_cancelled` and both IDs. Failure sends
`type: fido2_failed`, both IDs and a stable `reason`: `invalid_request`,
`unsupported_options`, `untrusted_relying_party`, `expired`, `busy`, `replayed`,
`unavailable`, `presentation_unavailable`, `authorization_failed`, `invalid_credential`.
No OS error text or credential/device data goes into logs.

Native success leaves action authentication pending. After authoritative Wema
verification, publish the correlated result:

```json
{"version":1,"type":"fido2_result","contextId":"SDK_NONCE","authenticationId":"WEMA_ID_VERBATIM","status":"verified","authenticated":true}
```

Use `status: failed` for rejection. SDK accepts success only after proof submission
starts, before expiry, for the exact active IDs. Authentication and transaction
completion are separate. Legacy `odion.auth.action_status` cannot authorize an
action while this SDK feature is enabled. A voice check may still authenticate
the session, but cannot replace the transaction's required passkey proof.

Send `type: fido2_abort` with both IDs to withdraw an active quote. SDK dismisses
the prompt and returns to pending. Never execute after abort/cancel/expiry,
unsupported capability or missing bank verification. A retry needs a new challenge
and ID, never a spoken/typed/model-confirmed fallback. Native creation of a passkey
does not by itself authorize a transaction; Wema must define enrollment semantics.

## Acceptance before rollout

Confirm Wema's guide, serialization vectors for both modes, bank-approved domain
association for each app, device identifier meaning, registration semantics and
synced/device-bound credential policy. Verify backend no-logging/no-recording/no-LLM
exposure of all sensitive messages. Test wrong sender/context/ID, result-before-proof,
changed quotes, duplicates, cancellation, expiry, reconnect, delivery failure and
unknown outcomes without duplicate execution. Run real-iPhone test-account cases.
Review bank-required transaction display before enabling money movement.

The SDK does not call Wema, store proofs, change workers, or bypass caller tokens.
Public docs should describe only Attentive configuration and native behavior.
