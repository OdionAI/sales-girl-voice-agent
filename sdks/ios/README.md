# AttentiveVoice (Preview)

A native Swift wrapper for the existing Attentive/Odion voice-agent backend.
The app imports `AttentiveVoice`, not LiveKit. LiveKit 2.16.0 is pinned internally
for WebRTC media, WebSocket signaling, audio capture/playback and reconnection.
Requires iOS 16+ (macOS 13+ for the diagnostic executable) and Swift 6.1+ tooling.

This package is library-first, not a standalone signed iOS app. It has not yet
been released as a hosted Swift package. In Xcode, add `sdks/ios` as a **local
package**, then link the `AttentiveVoice` product to the customer's app. Because
the manifest is in a subdirectory, do not add the repository root as a remote
SwiftPM dependency. Publishing a standalone SDK repository/package is a later
release step.

## Call From Your UI

```swift
import AttentiveVoice

// Retain on the main actor, e.g. in a SwiftUI @StateObject.
let call = try AttentiveCall(
    endpoint: URL(string: "https://your-dashboard.example.com/api/public-agent/connection-details")!
)

call.onEvent = { event in
    switch event {
    case .toolActivity(let activity):
        // Update the customer's own banking UI using activity.payload["result"].
        // Do not log financial payloads.
        break
    case .authentication(let status):
        // Update the session or action badge. Backend authorization is unchanged.
        break
    default: break
    }
}

try await call.start(CallRequest(
    businessSlug: "your-business",
    agentPublicId: "agt_your_agent",
    endUserContact: "caller@example.com",
    profile: CallerProfile(customerId: "YOUR_CUSTOMER_ID", phoneNumber: "YOUR_PHONE_NUMBER"),
    toolWaitSpeechMode: .toolSpecific
))

try await call.setMicrophone(enabled: false)
try await call.setMicrophone(enabled: true)
try await call.sendText("What services can you help me with?")
await call.end()
```

`start` requests microphone permission before dispatching a call. Add
`NSMicrophoneUsageDescription` to your app's Info.plist. Denied permission produces
`microphoneDenied` and does not start a server session. The wrapper never requests
camera access. LiveKit manages its audio session using its normal communication
profile, including echo cancellation, noise suppression and gain control.

Use `state` for the network lifecycle and `agentState` for the assistant's state.
`connected` alone does not prove the agent is ready. A 30-second agent-readiness
timeout closes an unanswered session. `transcripts` updates partial/final segments
in place and retains at most 500; `toolActivity` retains at most 100 calls, updating
each by call ID. Both are in-memory, cleared for a new call. Ended-call transcripts
remain available until the next call or until the object is released.

Call `end()` when leaving the call screen. It cancels pending connection work and
cleans up microphone/room resources. A second `start` is rejected while a call is
active. Failed call creation is not automatically retried. LiveKit handles
reconnection within an active call; the wrapper reports `reconnecting`.

The current backend emits legacy transcription segments as well as text streams;
this version consumes its segment events and sends chat on `lk.chat`. It forwards
only agent-originated tool/auth data events. These events do not authorize tools
and cannot bypass the backend's session and action voice checks. Chat-only tests
cannot pass those voice checks. Profile values are context, not authentication.

## Local Development and Verification

Set `allowsInsecureDevelopmentConnections: true` only for development HTTP/WS
endpoints. Do not ship that setting. Physical devices need a reachable dashboard
host and RTC server/media addresses; `localhost` on an iPhone refers to the phone.
Use HTTPS/WSS for device testing where possible. If using LAN HTTP, configure a
development-only ATS exception and local-network usage description in the host
app. This SDK does not weaken transport security settings globally.

```sh
swift build
swift test
# From sdks/ios, compile the library for an iOS Simulator without signing:
xcodebuild -scheme AttentiveVoice -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath .build/ios-simulator CODE_SIGNING_ALLOWED=NO build
```

The optional macOS diagnostic connects without microphone audio, sends a harmless
chat message, and requires agent transcripts plus non-silent decoded audio. It
creates a real call and can create a conversation recording. It never invokes a
banking tool intentionally, logs only event labels/counts (not credentials or
conversation text), and disconnects afterward.

```sh
ATTENTIVE_CALL_ENDPOINT=http://localhost:3000/api/public-agent/connection-details \
ATTENTIVE_ALLOW_INSECURE=1 \
ATTENTIVE_BUSINESS_SLUG=your-business \
ATTENTIVE_AGENT_ID=agt_your_agent \
ATTENTIVE_CALLER_CONTACT=sdk-test@example.com \
swift run attentive-smoke
```

Before release, test on a physical iPhone: microphone permission grant/denial,
speaker/receiver/Bluetooth routing, interruptions, both voice-auth checks, tool
events, network changes, repeat calls and disconnect during connection. CallKit,
background-call policy, Android, gRPC and a distributable SDK release are not part
of this first wrapper. No model settings, server startup scripts or existing web
caller logic are changed by this package.

See [the API contract](../../docs/attentive/call-api.md) for the existing POC
bootstrap's security limits. A custom `CallCredentialProvider` can later obtain
credentials from the customer's trusted backend without changing call controls.
