# Key-based integration

Key-based calling starts with SDK `0.1.0-staging.3`. Install that version or a
compatible newer release. The dashboard key API must also be deployed and enabled;
installing the binary alone does not activate a deployment's API routes.

## Supplied caller UI

After creating an API key in Dashboard > Deploy > API & SDK:

```swift
import AttentiveVoiceUI

AttentiveAgentView(
    apiKey: "YOUR_API_KEY",
    agentID: "YOUR_AGENT_ID"
)
```

Add `NSMicrophoneUsageDescription` to the host app's Info.plist. The UI requires
iOS 17+. Present it in your navigation or sheet. It loads the agent's name/title,
owns the call lifecycle and reuses our caller controls, audio-reactive avatar,
transcript, authentication badges and tool activity. Dismissing it ends the call
and cancels startup, including a pending caller-token request.

No `SampleModel`, subscriptions, endpoint, business slug, caller contact, shared
test customer ID, room creation or transport setup is required for general calls.
Prompts, model choices and tools remain dashboard/worker configuration. Runtime
override switches are not part of this integration.

Keep the key/agent pair stable while the view is presented. To change agents,
dismiss the caller first, then present a new view (or change its SwiftUI identity
with `.id(agentID)`). Never switch tenants mid-call.

The complete app is [Examples/QuickStart](Examples/QuickStart/AttentiveExampleApp.swift).
The older `AttentiveSample` remains an advanced diagnostic/regression harness;
customers do not need to reproduce its `SampleModel` or developer settings.

## Your own UI

Headless calls require iOS 16+. Retain one call instance on the main actor:

```swift
import AttentiveVoice

let call = AttentiveCall(apiKey: "YOUR_API_KEY", agentID: "YOUR_AGENT_ID")
try await call.start()
// Observe call.state, agentState, transcripts, toolActivity and authentication.
try await call.setMicrophone(enabled: false)
try await call.sendText("What can you help me with?") // After agent readiness.
await call.end()
```

`start()` returns after transport connection, not after the greeting. Observe
`agentState` for readiness. Failed starts are not automatically retried. The host
must call `end()` when dismissing a custom UI. `loadConfiguration()` is optional
for headless users and does not access the microphone or create a call.

The endpoint, credential-provider and `CallRequest` APIs remain available for
advanced/self-hosted integrations and existing apps. They are not prerequisites
for the new default integration.

## Identifying a bank customer

A publishable calling key identifies the **business integration**, not the human
using a phone. Never embed one customer's banking identifiers as defaults for
every app user. An API key alone cannot prove a caller's identity.

For authenticated operations, create a key through the authenticated management
API with `require_caller=true`; this advanced option is not in the simple key form.
Your backend authenticates its own user, maps that user to the correct bank
profile, then uses the separate `att_sk_...` identity key to request a caller token.
Never put the identity key in the app, public repository or mobile build settings.

Only on the customer's authenticated backend:

```javascript
// Derive the profile from your authenticated session/database, not client JSON.
const profile = await loadBankProfile(authenticatedSession.userId);
const response = await fetch("https://attentive.odion.ai/api/sdk/v1/caller-tokens", {
  method: "POST",
  headers: {
    Authorization: `Bearer ${process.env.ATTENTIVE_SERVER_IDENTITY_KEY}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    agent_id: process.env.ATTENTIVE_AGENT_ID,
    caller: {
      subject: profile.voiceOwner,
      customer_id: profile.customerId,
      account_number: profile.accountNumber,
      phone_number: profile.phoneNumber,
    },
  }),
});
if (!response.ok) throw new Error("Could not authorize caller");
const { caller_token } = await response.json();
// Return only caller_token to the same authenticated user, with Cache-Control: no-store.
```

Supply a fresh token per call using your app's existing authenticated API client:

```swift
AttentiveAgentView(apiKey: "YOUR_API_KEY", agentID: "YOUR_AGENT_ID") {
    try await yourBackend.fetchAttentiveCallerToken()
}
// Headless equivalent: try await call.start(callerToken: token)
```

`yourBackend` above belongs to the customer's existing authentication flow; it is
not an additional SDK model. Tokens last five minutes and are bound to the key,
business and agent. They are signed, not encrypted; never log or persist them.
Expiry controls call startup, not the duration of an already-connected call.

For current Wema voiceprints, `subject` must match the already-enrolled normalized
email/voice owner. This change does **not** add a public enrollment service, invent
a voiceprint, or bypass either voice check. Protected tools still require the
worker's checks to pass. Existing advanced enrollment APIs are unchanged;
production enrollment availability is a separate deployment dependency.

## Key boundaries and errors

- `att_pk_...` is publishable and scoped to selected agents in one business.
- `att_sk_...` is server-only and can attest caller identity for those agents.
- Active administrator membership and current agent ownership are checked.
- Active publishable keys can be copied again by a dashboard administrator.
  Server identity keys remain hash-only and are returned only at creation.
- Revocation blocks new token issuance/calls, not already-connected calls.
- Removing/demoting the issuing administrator also invalidates their keys.
- Call-start quotas are per key across installations: default 100 per UTC hour,
  configurable 1-1000. Failed bootstrap attempts count; configuration reads do
  not. This is not a concurrent-call or call-duration limit.
- Billing still applies and fails closed for key-based calls.
- The SDK encapsulates the URL, but endpoints and publishable keys remain
  discoverable in traffic/binaries. Encapsulation is not secrecy or app attestation.
- Require signed callers for sensitive apps and operate gateway rate limits and
  billing safeguards. A publishable key alone is not bot protection.
- Never put gateway keys, LiveKit admin secrets or dashboard credentials in apps.

Typed errors include `invalidAPIKey`, `agentNotAllowed`, `callerRequired`,
`callerSessionExpired`, `callLimitReached`, `serviceUnavailable` and
`insufficientCredits`. The supplied UI displays safe descriptions.

## Instructions for a customer's coding agent

Use this only after API deployment and a compatible binary release:

1. Install the account owner's specified exact version of `attentive-ios-sdk`.
2. For supplied UI, link `AttentiveVoiceUI`, add microphone usage text and present
   `AttentiveAgentView(apiKey:agentID:)`. Do not create a `SampleModel`.
3. For custom UI, link only `AttentiveVoice`, retain `AttentiveCall(apiKey:agentID:)`,
   call `start()`, observe state/events and end on dismissal.
4. Do not ask the mobile client for a service URL, business slug, runtime options,
   server key or shared bank profile.
5. If signed callers are required, obtain fresh tokens through the host's existing
   authenticated backend. Never mint them in the app or trust client customer IDs.
6. Handle denied microphone access, revoked keys, quota/credit failures, agent
   timeout and cancellation. Never repeatedly retry a failed call start.

These guides remain outside the distributed runtime package. Keep the clean
binary packaging policy and retain required license notices.
