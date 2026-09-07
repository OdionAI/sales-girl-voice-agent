# Attentive iOS SDK

Private binary staging prerelease: **0.1.0-staging.1**.
Use the matching Git tag in `OdionAI/attentive-ios-sdk` for a pinned installation.
This is a local-backend staging release, not a production service deployment.

| Product | Use | Minimum iOS |
| --- | --- | --- |
| `AttentiveVoice` | Connect your own UI to the agent | 16 |
| `AttentiveVoiceUI` | Embed the supplied SwiftUI caller screen | 17 |

The SDK handles microphone capture, playback and realtime transport. Agent
behavior, models, tools and voice authentication remain on the backend.
This release does not change the call engine or add a gateway hop.

## Installation

You need read access to the private repository through your GitHub account.
In Xcode, select **File > Add Package Dependencies**, enter
`https://github.com/OdionAI/attentive-ios-sdk.git`, and choose exact version
`0.1.0-staging.1`. Add `AttentiveVoice`; add
`AttentiveVoiceUI` only when using the supplied screen.

For a Swift package consumer:

```swift
// Package dependencies:
.package(url: "https://github.com/OdionAI/attentive-ios-sdk.git", exact: "0.1.0-staging.1")
// App target dependencies:
.product(name: "AttentiveVoice", package: "attentive-ios-sdk")
.product(name: "AttentiveVoiceUI", package: "attentive-ios-sdk") // Optional
```

Alternatively, add this folder as a local package. The binaries are carried in
the repository; there are no separately authenticated release downloads or
upstream source-package installs. Do not manually add the native frameworks.

Use Xcode 26.6 with Swift 6.3.3 for this release. Core supports iOS 16+ and the
optional UI iOS 17+. Slices cover arm64 iPhone and arm64/x86_64 iOS Simulator.
Compatibility with older Xcode compilers is not claimed by this preview.

## Integration

Follow [GETTING_STARTED.md](GETTING_STARTED.md) for complete copyable snippets:
retain a session, configure the endpoint and caller, and present
`AttentiveCallerView` or connect your own controls. See [CALLER_UI.md](CALLER_UI.md)
for branding and panel options. For a coding assistant, use
[INTEGRATION_AGENT.md](INTEGRATION_AGENT.md).

Add `NSMicrophoneUsageDescription` to your app. Obtain your endpoint, business
slug, public agent ID and caller context from the deployment owner. The SDK
contains no deployment URL, customer identity, voiceprint or provider key.
Do not copy account details from someone else's test app.

## Staging Backend

Publishing this library does not deploy the backend. The existing local service
must be running, including its realtime service and worker.

- Simulator on the server Mac: the current bootstrap path is
  `http://localhost:3000/api/public-agent/connection-details`.
- Physical iPhone: use the Mac's reachable LAN address, not `localhost`.
  Both the HTTP endpoint and returned realtime address must be reachable.
- Optional enrollment uses `/api/public-agent/voice-enroll`. The current POC
  identifies the reference voiceprint using the same normalized email as calls.
- HTTP/WS staging requires explicitly opting into
  `allowsInsecureDevelopmentConnections` when constructing the call/provider.
  A host app may also need development-only local-network/ATS settings. Do not
  enable arbitrary insecure connections in a production app.

The public POC bootstrap is not a production banking authorization boundary.
Profile fields do not authenticate a caller. Preserve server-side voice checks
and transaction confirmation. Do not embed server secrets or automatically
retry financial operations. Production deployment needs trusted caller/tenant
binding, protected enrollment and scoped credentials before external use.

## Verification

Candidate checks on September 7, 2026:

- Core-only and optional-UI quickstart consumers compiled against the binaries
  for arm64 and x86_64 Simulator.
- A separate binary-consuming app launched on iPhone 17 Pro Simulator (iOS 26.5)
  and passed its caller-screen/panel UI test. No live call was started.
- Public interfaces, native linking, packaged resources, file checksums and
  documentation links were checked. No implementation Swift files are shipped.
- Physical-iPhone testing of these release binaries remains pending. The
  previous source-built phone app worked, but that is not
  a substitute for testing the packaged release on the phone.

See `BUILD_INFO.json` for the source revision/toolchain and `SHA256SUMS` for the
exact packaged files. Treat the GitHub release notes as the acceptance record;
build success alone does not establish voice quality, bank-tool correctness or
background-call support. Test permission denial, start/end/mute/chat, real
speech, auth/tool events, reconnection and dismissal in your host app.

The implementation is compiled, not distributed as Swift source. Required
third-party credits and native binary identifiers remain discoverable; this is
not a promise of removing every upstream trace. Preserve `ThirdPartyNotices`
and the privacy manifests/resource bundles inside the frameworks.

The Attentive frameworks in this staging release are unsigned. No Apple
Developer Program purchase is required to download this Swift package. Your
app's signing and distribution are separate. Before App Store use, complete
publisher signing, third-party privacy/signature compliance and device testing.

## Roadmap

Production API deployment, the Attentive streaming gateway, gRPC, Android SDK,
web SDK and broader mobile release hardening are separate work. This staging
release retains the current transport and optional UI.
