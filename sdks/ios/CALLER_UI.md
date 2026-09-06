# Optional iOS Caller UI

`AttentiveVoice` is the headless SDK (iOS 16+).
`AttentiveVoiceUI` is the optional SwiftUI library (iOS 17+).
The latter depends on the former, never the reverse. Neither product changes the
backend's model, turn-taking, authentication or tool settings.

## Install Locally

This is a preview, not a published package release. Add the `sdks/ios` directory
as a local Swift package in Xcode. Link `AttentiveVoice` for your own UI; also link
`AttentiveVoiceUI` for the provided caller view. Do not enter the repository root
as a remote SwiftPM URL: its manifest is in a subdirectory.

Both products use the same core `AttentiveCall`. Do not create one connection for
your own UI and another for the bundled view. The sample app is an external
consumer of the two products, not an implementation to copy into your target.

## Add the Caller to Your App

The host owns and retains `AttentiveCall` and its request. A containing app model
can publish the call as in the sample. Here is the view composition:

```swift
import AttentiveVoice
import AttentiveVoiceUI
import SwiftUI

@available(iOS 17, *)
struct CustomerCallScreen: View {
    @ObservedObject var call: AttentiveCall
    @Binding var request: CallRequest

    var body: some View {
        AttentiveCallerView(
            call: call,
            request: $request,
            configuration: CallerUIConfiguration(
                title: "Talk to Customer Care",
                agentName: "Assistant",
                profileTitle: "My account"
            )
        )
    }
}
```

Create the call and request on the main actor in your host model:

```swift
let call = try AttentiveCall(
    endpoint: URL(string: "https://your-service.example.com/api/public-agent/connection-details")!
)
let request = CallRequest(
    businessSlug: "your-business",
    agentPublicId: "agt_your_agent",
    endUserContact: "caller@example.com"
)
```

These are placeholder identifiers; obtain real values and the service endpoint
from your deployment. Add `NSMicrophoneUsageDescription` to the host's Info.plist.
Do not embed a provider key, LiveKit API secret or a long-lived privileged token.
The current POC bootstrap still needs the hardening described in the
[API contract](../../docs/attentive/call-api.md) before external production use.

The view does not start a call on appearance. Its Start Call button uses the
latest bound request. It defaults to enabling the microphone, while
`microphoneOnStart: false` enables a chat-only diagnostic. Chat cannot pass voice
authentication. It does not require voice enrollment to start a call; the
backend remains responsible for deciding which actions are allowed.

## Configuration and Ownership

| Input | Meaning |
| --- | --- |
| `call` | One retained headless core instance; shared with any host observers |
| `request` | Binding to caller/agent/language/profile/wait-phrase settings; the UI edits contact and enabled profile fields before a call |
| `configuration` | Presentation labels, optional profile fields, tool display names, accent color and avatar |
| `enrollment` | Optional `VoiceEnrollment`; omit to use your own enrollment screen |
| `microphoneOnStart` | Initial microphone choice; subsequent mute/unmute uses the core |
| `onSettings` | Optional closure to open host-owned settings; no developer connection form is shipped inside the UI |

`CallerUIConfiguration` includes `title`, `agentName`, `profileTitle`,
`customerIDLabel`, `activityTitle`, `emptyActivityText`, `showsProfile`,
`showsToolWaitSelection`, `toolDisplayNames`, `accentColor`, `avatar` and
`endsCallOnDisappear`. A custom avatar is a SwiftUI `Image`; by default the UI
loads the existing caller image from its own package resource bundle. It does
not depend on assets in the host app.

Changing display names does not change the actual agent prompt/name. Tool labels
map backend tool identifiers to readable UI text; they do not define or execute
tools. Generic defaults contain no Wema branding. The existing Wema context wire
contract remains available for bank callers; this extraction does not invent a
generic bank profile API or fetch an account number.

Request/profile editing is locked while a call or enrollment capture/upload is
active. Do not replace `call` or `enrollment` objects during those operations, or
change caller identity from another host screen mid-call. Replace dependencies
only between calls. Keep client-side edits separate from server authorization.

By default, removing the caller view cancels enrollment and ends its call,
including a pending connection. Set `endsCallOnDisappear: false` only when the
host intentionally retains the call elsewhere and will call `await call.end()`
itself. Backgrounding cancels pre-call enrollment; it is not a complete background
calling/CallKit policy. Presenting host settings is not permission to change an
active call's identity.

## Enrollment and Live Events

Create `VoiceEnrollment` using `HTTPVoiceEnrollmentProvider` and
`IOSVoiceEnrollmentRecorder`, as shown in the [core README](README.md), then pass
it to the view. The current backend enrolls by normalized email. The call form
normalizes email contact before starting, matching the enrollment identity.
Recording or re-recording is explicit and can replace a voiceprint. Never use
synthetic test audio to overwrite a real caller's enrollment.

The UI reads published core state without replacing `call.onEvent`, so the host
can keep its own event listener. It displays both Session and Action badges,
tool activity/results, transcript revisions and agent audio energy. Backend
failures remain failures, even if one badge is verified. The view never runs
banking actions or authorizes them from a badge.

The avatar follows decoded audio amplitude, with no ring or periodic pulse.
Only its small view observes high-frequency audio energy. Reduce Motion keeps
it stationary. The caller screen is not continuously republished for each audio
frame. Do not persist transcript/tool payloads in analytics by default.

## Use Your Own UI Instead

Import only `AttentiveVoice` and use `start`, `end`, `setMicrophone`, `sendText`,
published state and `onEvent`. The [core quickstart](README.md#call-from-your-ui)
uses this route. You do not need `AttentiveVoiceUI`, its assets or its view model.
The `attentive-smoke` executable is a headless consumer of the same core.

## Verify an Integration

- Compile a host that imports only core, and a host using both products.
- Check generic branding and the configured bank variant on portrait/landscape.
- Confirm the avatar resource loads from the package, not an app copy.
- Test call start/end/mute/chat, denied permission, enrollment cancellation and
  removing the screen during connection.
- Verify real microphone audio, both voice checks, tool activity and recordings
  against your staging backend. Chat-only tests do not prove these paths.
- Test reconnects, repeated calls and audio routing on physical devices.

See the [sample](Examples/AttentiveSample/README.md),
[coding-agent handoff](INTEGRATION_AGENT.md), [verification](VERIFICATION.md) and
[follow-up roadmap](../../docs/attentive/ROADMAP.md).
