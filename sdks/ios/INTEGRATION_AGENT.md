# Coding-Agent Integration Handoff

This document describes the local preview, not a published SDK version. Use the
source checkout accompanying this file. Follow the host project's own
instructions and security requirements. Do not invent a package URL, release tag,
API endpoint, customer identity or credential.

## Copyable Customer Prompt

```text
Integrate the Attentive voice caller into this iOS app.

First read the SDK's README.md, CALLER_UI.md and INTEGRATION_AGENT.md, inspect
Package.swift and its public Swift types, and inspect this app's architecture.
The local preview package is at <path-to-checkout>/sdks/ios. Ask me for the
deployment endpoint, business slug, public agent ID and trusted caller context
if these are not already supplied by the app. There is no published remote
package/tag to assume for this preview.

Use AttentiveVoice for headless integration. When using the provided caller UI,
also link AttentiveVoiceUI and embed AttentiveCallerView. Keep the call instance
in the host's main-actor model and pass a binding to CallRequest. The UI product
requires iOS 17; the core supports iOS 16. Do not copy the sample's caller views
into this app or import LiveKit in application code.

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
timing, LiveKit transport or remote infrastructure while integrating the UI.
gRPC, a raw-audio gateway and Android are roadmap items, not existing APIs.
Do not present the current public POC bootstrap as production-hardened.

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
The UI reference is [CALLER_UI.md](CALLER_UI.md). The sample shows actual package
consumption and developer-only connection settings. Future remote-install steps
must use a verified released package URL/version, not the local preview setup.
