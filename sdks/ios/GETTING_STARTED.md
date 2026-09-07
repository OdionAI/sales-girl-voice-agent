# Integrate Attentive Into Your iOS App

This guide covers the preview SDK. Use the installation instructions in the
README accompanying your package for its exact location and release status.
All deployment addresses below are placeholders.

## What You Integrate

The sample app is a consumer of the SDK, just like your app will be. Do not copy
its Xcode project, transport, caller panels or developer settings into your app.

| Your choice | Link these products | You provide |
| --- | --- | --- |
| Use the supplied caller screen | `AttentiveVoice` and `AttentiveVoiceUI` | Entry button, deployment configuration, caller context and branding |
| Build your own call screen | `AttentiveVoice` only | Controls, transcript/activity views and lifecycle handling |

Both options use the same `AttentiveCall`. Microphone capture, audio playback,
media transport and reconnection are inside the SDK. The server runs the agent,
models, turn detection, tools and live voice authentication. You do not need to
operate a transport account or write an audio streaming loop.

The current HTTP API starts a realtime call; it is not a raw-audio HTTP or gRPC
API. A gateway and gRPC are separate [roadmap](../../docs/attentive/ROADMAP.md) work.

## 1. Install and Configure

1. Obtain the approved SDK package and version from Attentive. Its root contains
   `Package.swift`. A binary distribution contains `Frameworks`; an internal
   source checkout contains `Sources` and `Vendor` under `sdks/ios`.
2. In Xcode, use **File > Add Package Dependencies**, select the approved remote
   repository and exact version, or **Add Local** for a supplied package folder.
   Add the products from the table to your app target. Do not add the whole
   voice-agent repository as a package: its manifest is nested.
3. Use Swift 6.1+ tooling. The core requires iOS 16; the supplied UI requires
   iOS 17. The examples using that UI below require iOS 17.
4. Add this entry to the host app's Info.plist (or its generated Info settings):

```xml
<key>NSMicrophoneUsageDescription</key>
<string>Use your microphone to speak with our support agent.</string>
```

5. Obtain the following from your Attentive deployment and your signed-in app
   session. Do not copy demo identities from the sample.

| Configuration | Source and meaning |
| --- | --- |
| Call endpoint | Reachable HTTPS `/api/public-agent/connection-details` URL |
| Enrollment endpoint | Optional HTTPS `/api/public-agent/voice-enroll` URL, when using built-in enrollment |
| Business slug and public agent ID | The existing agent configured in the dashboard |
| Caller contact | The caller's identity; for current voice enrollment use the same normalized email for enrollment and calls |
| Customer ID, account and phone | Optional bank context from your trusted app session; not proof of identity |

The existing bootstrap is a POC contract, not yet a production-authenticated
customer API. See the [security boundary](../../docs/attentive/call-api.md#deployment-and-security-boundary).
Never embed model keys, transport server secrets or long-lived service tokens.
Do not enable insecure connections or copy the sample's LAN forwarding policy
into a released app. A real phone must be able to reach both the API and realtime
media service; `localhost` refers to the phone itself.

## 2. Retain One Session

This small host-owned object keeps the call, mutable request and optional
enrollment together. It does not implement audio processing or tool execution.
Copy it into your app, or use the same ownership in your existing app model.

<!-- snippet: session -->
```swift
import AttentiveVoice
import Combine
import Foundation

@MainActor
final class CustomerVoiceSession: ObservableObject {
    let call: AttentiveCall
    let enrollment: VoiceEnrollment?
    @Published var request: CallRequest

    init(callEndpoint: URL, enrollmentEndpoint: URL?, request: CallRequest) throws {
        call = try AttentiveCall(endpoint: callEndpoint)
        self.request = request
        if let enrollmentEndpoint {
            enrollment = VoiceEnrollment(
                provider: try HTTPVoiceEnrollmentProvider(endpoint: enrollmentEndpoint),
                recorder: IOSVoiceEnrollmentRecorder()
            )
        } else {
            enrollment = nil
        }
    }
}
```

Keep identity and profile stable for the duration of a call. Do not replace the
session on view updates. The supplied UI locks its edits while calling or
recording enrollment; the rest of the host app must respect that lock too.

## 3A. Embed the Supplied Caller UI

Copy this screen. Its existing Start Call button starts the microphone and call;
do not also call `start()` when the screen appears.

<!-- snippet: caller-ui -->
```swift
import AttentiveVoiceUI
import SwiftUI

@available(iOS 17, *)
@MainActor
struct CustomerCallerScreen: View {
    @ObservedObject var session: CustomerVoiceSession
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            AttentiveCallerView(
                call: session.call,
                request: $session.request,
                configuration: CallerUIConfiguration(
                    title: "Talk to customer care",
                    agentName: "Assistant",
                    profileTitle: "My account",
                    showsProfile: true,
                    toolDisplayNames: [
                        "wema_get_balance": "Check balance",
                        "wema_get_transactions": "Transaction history"
                    ]
                ),
                enrollment: session.enrollment
            )
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Close", systemImage: "xmark") { dismiss() }
                }
            }
        }
    }
}
```

The supplied view includes mute/end/chat controls, live transcripts, a profile
panel, tool activity/results and Session/Action badges. Labels only affect
presentation: `agentName` does not rewrite the saved agent prompt, and
`toolDisplayNames` does not register new tools. Configure the real tools and
agent identity in the dashboard. Set `showsProfile: false` for a non-bank caller.
See [all UI configuration options](CALLER_UI.md#configuration-and-ownership).

Here is an entry button for an existing SwiftUI screen. The session is created
only on a deliberate tap and retained for the presented caller screen. Setup
errors are displayed rather than crashing the app.

<!-- snippet: entry-button -->
```swift
import AttentiveVoice
import SwiftUI

@available(iOS 17, *)
@MainActor
struct CustomerSupportButton: View {
    let callEndpoint: URL
    let enrollmentEndpoint: URL?
    let initialRequest: CallRequest
    @State private var session: CustomerVoiceSession?
    @State private var showsCaller = false
    @State private var showsError = false
    @State private var errorMessage = ""

    var body: some View {
        Button("Call support", systemImage: "phone") {
            do {
                session = try CustomerVoiceSession(
                    callEndpoint: callEndpoint,
                    enrollmentEndpoint: enrollmentEndpoint,
                    request: initialRequest
                )
                showsCaller = true
            } catch {
                errorMessage = error.localizedDescription
                showsError = true
            }
        }
        .disabled(showsCaller)
        .fullScreenCover(isPresented: $showsCaller) {
            if let session { CustomerCallerScreen(session: session) }
        }
        .alert("Call unavailable", isPresented: $showsError) {
            Button("OK", role: .cancel) { }
        } message: {
            Text(errorMessage)
        }
    }
}
```

For example, insert the result of this function into an existing screen, passing
the signed-in customer's context. Replace the deployment placeholders before
testing. It does not start a call or enroll a voice until the caller chooses to.

<!-- snippet: configure-entry -->
```swift
import AttentiveVoice
import SwiftUI

@available(iOS 17, *)
@MainActor
func customerSupportEntry(email: String, profile: CallerProfile?) -> CustomerSupportButton {
    CustomerSupportButton(
        callEndpoint: URL(string: "https://your-service.example.com/api/public-agent/connection-details")!,
        enrollmentEndpoint: URL(string: "https://your-service.example.com/api/public-agent/voice-enroll")!,
        initialRequest: CallRequest(
            businessSlug: "YOUR_BUSINESS_SLUG",
            agentPublicId: "YOUR_AGENT_PUBLIC_ID",
            endUserContact: email.trimmingCharacters(in: .whitespacesAndNewlines).lowercased(),
            profile: profile,
            toolWaitSpeechMode: .toolSpecific
        )
    )
}
```

Pass `enrollmentEndpoint: nil` if enrollment is handled elsewhere. That hides
the enrollment flow, not backend authentication. With the current API, a
customer ID alone does not select the reference voiceprint: the email must match.
An enrollment recording is explicit and replaces any previous reference.

Removing the caller view ends the call by default, including an in-progress
connection, and cancels enrollment. Reopening creates a fresh session. Do not
add a second teardown owner unless deliberately changing this lifecycle.

## 3B. Use Your Own UI Instead

Skip the `caller-ui`, `entry-button` and `configure-entry` snippets and do not
link `AttentiveVoiceUI`. Keep one `CustomerVoiceSession` in your host, then pass
its `call` and current `request` to your screen. The following minimal controls
show the core integration; style and extend them using your app's design system.

<!-- snippet: custom-controls -->
```swift
import AttentiveVoice
import SwiftUI

@MainActor
struct CustomerCallControls: View {
    @ObservedObject var call: AttentiveCall
    let request: CallRequest
    @State private var operationInProgress = false
    @State private var errorMessage: String?

    private var canStart: Bool { [.idle, .ended, .failed].contains(call.state) }
    private var isActive: Bool { [.connecting, .connected, .reconnecting].contains(call.state) }

    var body: some View {
        VStack {
            Text(call.state.rawValue)
            Text(call.agentState.rawValue)
            Button {
                Task { await perform { try await call.start(request) } }
            } label: {
                Label("Start call", systemImage: "phone")
            }
            .disabled(!canStart || operationInProgress)

            Button {
                Task { await perform { try await call.setMicrophone(enabled: !call.microphoneEnabled) } }
            } label: {
                Label(call.microphoneEnabled ? "Mute" : "Unmute",
                      systemImage: call.microphoneEnabled ? "mic" : "mic.slash")
            }
            .disabled(call.state != .connected || operationInProgress)

            Button(role: .destructive) { Task { await call.end() } } label: {
                Label("End call", systemImage: "phone.down")
            }
            .disabled(!isActive)

            if let message = errorMessage ?? call.lastError?.localizedDescription {
                Text(message).foregroundStyle(.red)
            }
        }
        .onDisappear { Task { await call.end() } }
    }

    private func perform(_ operation: () async throws -> Void) async {
        guard !operationInProgress else { return }
        operationInProgress = true
        errorMessage = nil
        defer { operationInProgress = false }
        do { try await operation() }
        catch is CancellationError { }
        catch { errorMessage = error.localizedDescription }
    }
}
```

`start()` returns after joining and publishing, not after the agent greeting.
Check `agentState` as well as `state`. Handle failures after start using
`lastError`/`.failure`, not only the error from `start()`. There is a 30-second
agent-readiness watchdog. End remains available during connection so the user
can cancel. Do not automatically retry failed call creation or replay a tool.

For your own enrollment UI, use `session.enrollment`, call `refresh(email:)`
for status, and call `record(email:)` only after explicit user action. Disable
call start during recording/upload, cancel enrollment on dismissal/background,
and never capture enrollment concurrently with the call. The minimal controls
above do not implement an enrollment screen.

## 4. Observe Conversation and Tools

These properties/events are available in either UI option. UI observers must
observe `AttentiveCall` directly; an outer model holding a `let call` does not
automatically republish the call's updates.

| Need | SDK surface |
| --- | --- |
| Network and assistant status | `state`, `agentState` |
| Current microphone state | `microphoneEnabled` |
| Transcript | `transcripts`, or `.transcript` from `onEvent`; upsert by ID, do not append each partial as a new utterance |
| Tool execution visibility | `toolActivity`, or `.toolActivity`; upsert by activity ID, display `toolName`, `event`, `status` and optional `payload["result"]` |
| Voice badges | `sessionAuthentication`, `actionAuthentication`, or `.authentication` |
| Chat input | `try await call.sendText(text)` when connected and the agent is ready |
| Speaking avatar | Observe `call.agentAudioLevel.energy` in a small dedicated view |
| Errors | `lastError` and `.failure`; show bounded messages, not raw backend responses |

`onEvent` is one main-actor callback. Keep its work short and use weak captures
when capturing its owner. The provided caller UI does not replace it. Tools run
on the backend; activity is observational and contains no client execution API.
A missing event is not permission to invent a successful balance or transaction.
Do not treat an HTTP 200 or a green badge as proof of transaction success.

Profile prefills and enrollment status do not grant authorization. For the Wema
policy, the backend still requires both live voice checks for protected tools.
Keep the microphone available so those checks can run. Chat-only tests cannot
establish a voice match. Do not log tokens, voice samples, full transcripts,
customer identifiers or banking results to analytics by default.

## 5. Verify Before Shipping

- Grant and deny microphone permission; no permission must mean no dispatched
  microphone call. Handle permission recovery in the host app.
- Start, mute, unmute, send chat, interrupt, end and cancel during connection.
- Verify actual speech input/output, repeated calls and missing-agent errors.
- Verify profile context, enrollment, both auth states and tool activity with an
  approved staging identity. Never use a real money transfer as a smoke test.
- Check audio routes, Bluetooth, incoming phone interruptions, network changes,
  background/foreground and screen dismissal on supported physical devices.
- Check the avatar/resources and VoiceOver/Dynamic Type in the final host app.

Background calling and CallKit are not implemented promises in this preview.
Choose and test your app's background policy rather than simply enabling an
entitlement. No end-to-end banking or background guarantee follows from a build.

The snippets are compile-checked against both the source and binary packages
as part of release verification. That compilation does not replace live-call
acceptance in your app.

Next: [SDK distribution process](DISTRIBUTION.md),
[full UI reference](CALLER_UI.md), [API contract](../../docs/attentive/call-api.md),
and [copyable coding-agent instructions](INTEGRATION_AGENT.md).
