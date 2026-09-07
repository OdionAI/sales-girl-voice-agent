# Coding-Agent Integration Handoff

Follow the installation and release status in the README accompanying this
package, and the host project's own instructions and security requirements.
Do not invent a package URL, release tag, API endpoint, customer identity or credential.

## Copyable Customer Prompt

```text
Integrate the Attentive voice caller into this iOS app.

First read the SDK's GETTING_STARTED.md, README.md, CALLER_UI.md and INTEGRATION_AGENT.md, inspect
Package.swift and its public Swift interfaces, and inspect this app's architecture.
Use the verified package URL/version or local preview directory supplied by
Attentive. Ask me for the deployment endpoint, business slug, public agent ID
and trusted caller context if these are not already supplied by the app.
Do not clone the entire backend repository, rebuild a binary SDK, or copy vendor
source into the host app. Keep the package version pinned during integration.

Use AttentiveVoice for headless integration. When using the provided caller UI,
also link AttentiveVoiceUI and embed AttentiveCallerView. Keep the call instance
in the host's main-actor model and pass a binding to CallRequest. The UI product
requires iOS 17; the core supports iOS 16. Do not copy the sample's caller views
into this app or import internal transport modules in application code.
AttentiveRTC is a vendored implementation, not a supported customer API.
Do not add an upstream SDK package; preserve the provided pins and notices.

Let me choose my own UI or the optional caller UI before changing the app's
design. Configure branding, avatar and tool display labels in
CallerUIConfiguration. The view defaults to ending the call when removed;
disable that only if the host deliberately owns the continuing call and its
cleanup. The host's onEvent listener must keep working.

Add the microphone usage description and connect lifecycle/error handling.
Use HTTPS/WSS, no global ATS exceptions or embedded service secrets. Caller
profile values are not authentication. Preserve the backend's session and action
voice checks. If enrollment is required, use the existing enrollment API and
the same normalized email for enrollment and calls. Never replace a real
voiceprint with test audio. Never retry a banking transaction automatically.

Do not change server models, credentials, authentication policy, VAD, turn
timing, realtime transport or remote infrastructure while integrating the UI.
gRPC, a raw-audio gateway and Android are roadmap items, not existing APIs.
Do not present the current public POC bootstrap as production-hardened.

Use the compile-checked snippets in GETTING_STARTED.md as integration examples,
adapting them to the host's ownership pattern without copying sample internals.
Build and test. Verify permission denial, start/end/mute/chat, presentation and
dismissal, transcript updates and package assets. Use a designated test identity
for a harmless live-call check only with authorization. Report separately what
was verified in a simulator, with text only, with real speech, and on a physical
device. No real banking transaction is required for a smoke test. Finish with
the exact changed files, test results and any deployment/release blockers.
```

## Public Surface Reference

- `AttentiveCall(endpoint:)` or `AttentiveCall(credentialProvider:)`: retained
  headless call; `start`, `end`, `setMicrophone`, `sendText`.
- `CallRequest`: business, agent, caller identity, language, optional profile and
  tool waiting phrase choice. Existing wire profile is `wemaContext`.
- `CallerProfile`: customer ID, optional account and phone, not authorization.
- `VoiceEnrollment`, `HTTPVoiceEnrollmentProvider`, `IOSVoiceEnrollmentRecorder`:
  optional explicit pre-call enrollment; reference voiceprint != live checks.
- `AttentiveCallerView`: optional SwiftUI view with `call`, `request`,
  `configuration`, optional `enrollment`, `microphoneOnStart` and `onSettings`.
- `CallerUIConfiguration`: presentation only; no provider or server controls.

The source of truth for HTTP is [call-api.md](../../docs/attentive/call-api.md).
The UI reference is [CALLER_UI.md](CALLER_UI.md). The quickstart shows package
consumption. Use only the verified package URL/version or preview folder supplied
for this integration.
