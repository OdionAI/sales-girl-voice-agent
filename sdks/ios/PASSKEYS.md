# Native passkey confirmation (0.1.4)

Available in SDK 0.1.4. A compatible Attentive backend and bank-approved
associated domains are required before an end-to-end transaction can work.

Configure everything in code. There are no settings menus, sliders, enrollment
buttons or custom authentication screens. The native system sheet appears over
the call when authorization is requested.

<!-- snippet: passkey-entry -->
```swift
import AttentiveVoice
import AttentiveVoiceUI
import SwiftUI

@available(iOS 17, *)
@MainActor
struct BankAgentScreen: View {
    let customerID: String
    let bankDeviceIdentifier: String?
    let callerToken: () async throws -> String

    var body: some View {
        AttentiveAgentView(
            apiKey: "YOUR_API_KEY",
            agentID: "YOUR_AGENT_ID",
            customerID: customerID,
            passkeys: .init(
                relyingPartyIdentifiers: ["auth.bank.example"], // Replace with bank-approved domain.
                deviceIdentifier: bankDeviceIdentifier,
                ceremonyTimeoutSeconds: 120,
                verificationTimeoutSeconds: 30
            ),
            callerToken: callerToken
        )
    }
}
```

Existing `audioInput` settings work alongside `passkeys`; audio processing is
independent of native authentication. Configuration is immutable per call object.

| Setting | Meaning |
| --- | --- |
| `.disabled` | Default. Empty relying-party list disables the feature and preserves existing integrations. |
| `relyingPartyIdentifiers` | Up to 16 exact trusted domains, without scheme, port, path or wildcards. Never derive them from an untrusted message. |
| `deviceIdentifier` | Optional opaque bank-issued/host-supplied string, maximum 512 UTF-8 bytes. The SDK does not discover hardware identifiers or generate substitutes. Absence is sent as null. |
| `ceremonyTimeoutSeconds` | Finite 1–300 seconds, default 120. Earlier server expiry/request timeout takes precedence. |
| `verificationTimeoutSeconds` | Finite 1–120 seconds, default 30. Wait for backend verification after producing the proof, also bounded by server expiry. |

Invalid configuration fails before a call starts. Missing device identifiers do
not prevent conversation; the backend decides whether bank operations are allowed.

## Host setup

Sign with a paid Apple Developer team; Personal Teams do not support Associated
Domains. Add `webcredentials:<approved-domain>` to the host app's Associated Domains
entitlement. The bank must serve an `apple-app-site-association` file authorizing
that app's application identifier. The sample app needs its own approved entry;
the SDK cannot inherit another app's association. See [Apple's passkey setup](https://developer.apple.com/documentation/authenticationservices/supporting-passkeys).

`AttentiveAgentView` supplies its own presenting window, including sheets and
multi-window apps. Custom UI hosts initialize `AttentiveCall` with the same
`passkeys` value and set `call.passkeyPresentationAnchor` to a closure returning
that call screen's window. Retain the call and call `end()` on dismissal.

Native presentation is iOS-only. The core supports iOS 16; supplied caller views
require iOS 17.
Registration exclusion lists require iOS 17.4; older systems reject those requests
rather than ignoring their constraints.

## Diagnostics (0.1.5)

Diagnostics are off by default. Enable them in the same `passkeys` configuration
on `AttentiveAgentView` or `AttentiveCall`:

```swift
passkeys: .init(
    relyingPartyIdentifiers: ["YOUR_APPROVED_PASSKEY_DOMAIN"],
    deviceIdentifier: nil,
    diagnosticsEnabled: true
),
```

Set `diagnosticsEnabled: false` (or omit it) and rebuild to disable SDK diagnostics.
No UI controls are added. In Xcode's debug console, filter for `passkey stage=`.
For an attached iPhone in macOS Console, select the device, start streaming, and
filter subsystem `ai.odion.attentive` and category `Passkeys`.

Stages cover configuration, connection/context, request receipt, validation,
native presentation, proof production/delivery, backend verification and cleanup.
Validation failures include a fixed check label such as `challenge` or
`relyingParty`; presentation setup failures identify a missing anchor, inactive
scene, or hidden window. Native failures include a bounded system error category and numeric
Apple error code; system descriptions and nested payloads are not logged.
`presentation outcome=requested` means the SDK asked iOS to present, not that a
sheet was visibly shown. `verification outcome=started` means a proof was sent,
not accepted. Only backend acceptance verifies authentication, separately from
transaction completion.

An enabled SDK may send bounded diagnostic stage/error metadata with a failure
to a compatible Attentive backend. Backend operator diagnostics are independently
controlled; disabling SDK diagnostics does not turn off server operational logs
or the existing failure reason sent for protocol correctness. No raw options,
challenges, signatures, proofs, device/customer identifiers, keys or tokens enter
these diagnostics. Enable before reproducing the issue and share the relevant
stage/reason/code lines with your integration support team. This feature does not
fix the underlying failure or guarantee that every Apple code explains its cause.

## Authentication state and privacy

- Existing `sessionAuthentication` / `actionAuthentication` remain the public states.
  Identity still comes from the signed-in host's backend-issued `callerToken`.
  Public API keys, customer selection and device identifiers do not prove identity.
- Native approval only produces a proof. Action authentication stays `pending`
  until a matching, authoritative backend verification result arrives.
- `verified` means authentication passed, not that money moved. Transaction
  completion is reported separately through the existing tool outcome.
- The bank’s user-verification preference is carried into the native request.
  The backend must request required verification for protected banking actions.
  Unsupported extensions, credential preferences, algorithms and non-`none`
  attestation fail explicitly instead of being silently dropped.
- Dismissing the sheet leaves the call usable, pending, without a call-failure
  alert. Local failures/expiry set action authentication to failed, not call failure.
  Quote expiry/cancellation remains a backend responsibility.
- After Face ID returns a credential, the SDK does not discard it because the
  host window or scene changed while the native sheet was visible. The backend
  still verifies the credential before any protected action.
- One ceremony at a time. No automatic prompt retry, proof replay, or spoken/typed
  substitute. Each new ceremony, including a retry for the same prepared action,
  has a fresh correlation ID. Reconnect invalidates the active ceremony; the
  backend must start anew.
- Sensitive options, proofs and device identifiers never enter chat, transcripts,
  tool activity or the public event callback. Do not log the device property yourself.

Passkeys can sync across devices and permit device-passcode authentication. This
feature does not promise biometric-only or device-bound credentials.

## Confirm with Wema before release

1. Obtain `FIDO2_Passkey_Integration_Guide.docx` and accepted examples for assertion
   and registration. Confirm unpadded base64url versus base64, the complete response
   schema, optional fields and null/omitted user-handle behavior.
2. Confirm relying-party domains and each host/test app's domain association.
3. Confirm whether registration only enrolls or also authorizes the transaction.
   The SDK never infers authorization from native registration success.
4. Define the bank's `deviceImei` identifier, its source on iOS and missing-value policy.
5. Confirm synced credentials/device passcodes are permitted and that platform
   ES256 keys, attestation `none` and no-extension requests satisfy bank policy.
6. Verify backend challenge binding to customer, prepared transaction/amount/payee,
   expiry and call, and duplicate-execution/cancellation/retry behavior.

Until these are confirmed and backend integration is complete, use mocked SDK
ceremonies. Do not claim a Wema transaction has been tested or enable live money
movement. End-to-end acceptance requires a real iPhone and bank test account.

## Revision 2 integration status

The SDK now uses the revised structured ceremony contract: per-request
correlation, ISO-8601 expiry, and explicit bank acceptance before authentication
is verified. Configuration remains in code through both startup paths. No extra
customer authentication screen or settings controls are added.

Native registration and assertion options have separate mappings and proofs.
Unsupported credential preferences fail explicitly. Host-supplied prompt limits
and bank expiry bound the WebAuthn timeout; the SDK does not retry prompts or
reuse proofs after reconnect. Transaction completion remains a separate outcome.

The integration notes and simulated tests are available in source. Real-iPhone
credential testing and Wema acceptance remain integration gates; a simulated approval does not satisfy them.

## Minimal sample

The QuickStart example sets `passkeys:` beside `audioInput:` without adding app
controls. `auth.bank.example` and `DEMO-DEVICE-ID` are demo placeholders. Replace
them with the approved relying-party domain and bank-issued identifier (or nil
if unavailable). The placeholder domain cannot complete a native ceremony. The
sample never fabricates backend acceptance. Use `.disabled` to omit passkeys.
Simulated-bank verification remains isolated in tests, outside the shipped SDK
and customer app.

For the public SDK 0.1.4 setup, code settings and provisioned simulated-bank demo,
see https://docs.odion.ai/ios/#native-passkey-confirmation. The demo requires its
own approved app association and a compatible worker; it is not an SDK mock.
