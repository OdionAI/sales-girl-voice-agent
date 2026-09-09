# Attentive iOS Sample

This is the advanced diagnostic/regression app for the released API. New customer
integrations should use the [minimal key-based app](../QuickStart/README.md) once
its API and compatible SDK release are deployed. Customers do not need to copy
this app's `SampleModel`, runtime settings or developer diagnostics.

Native SwiftUI caller app consuming the published binary `AttentiveVoice` and
`AttentiveVoiceUI` products at exact version `0.1.0-staging.2`. Requires Xcode
26.6 / Swift 6.3.3, GitHub read access to `OdionAI/attentive-ios-sdk`, an installed
iOS Simulator runtime for simulator tests, and the reachable public backend. The app
targets iOS 17+; the underlying wrapper supports iOS 16+.

## Run

From the repository root:

```sh
sdks/ios/Examples/AttentiveSample/script/build_and_run.sh
```

The script selects a booted iPhone simulator, or the first available iPhone,
builds, installs and opens the sample. Set `ATTENTIVE_SIMULATOR_ID` to select a
specific simulator. It stops only the previous sample app, never backend services.
It does not start the dashboard, worker or LiveKit; those must already be running.
Build products go into `sdks/ios/.build/sample-app`, not source control.

Alternatively open `AttentiveSample.xcodeproj`, select the `AttentiveSample`
scheme and an iPhone simulator, and Run. Its remote Swift package reference points
to `https://github.com/OdionAI/attentive-ios-sdk.git`, pinned to
`0.1.0-staging.2`; it does not consume local implementation source. Sign into
GitHub in Xcode with repository access. The scripts use system Git credentials
and a separate `.build/sample-dependencies` cache. The app does not import
LiveKit directly. Future changes under `sdks/ios/Sources` will not appear in
this app until a new SDK version is published and the package pin is updated.

The default target is the Lagos Wema agent `agt_73099afb71` at
`https://attentive.odion.ai/api/public-agent/connection-details`, business
`wema-bank-poc-local`. The clean binary package is `0.1.0-staging.2`, commit
`893878b`. The runtime frameworks have Attentive names; documentation remains
outside the distributed package.
The public deployment has no enrollment route yet, so this sample passes `nil`
enrollment to the caller view for that host. Server voice checks and auth badges
are unchanged. Never embed the jump API key or point the SDK at jump API ports.
Opening the app normally uses this public endpoint; caller details remain
launch-time inputs. To clear a previously saved Debug LAN target, launch once
with `ATTENTIVE_LOCAL_DEVICE=0` or an explicit public endpoint.

Open the top-right
My Wema menu before starting to set the customer ID and phone. Call settings
inside that panel provides the optional account and connection configuration.
Use the caller identity enrolled in the existing backend for voice verification.
Profile values provide context; entering them does not authenticate a caller.

When targeting a local deployment with enrollment, the pre-call form checks
voice enrollment for the caller email and offers
**Record my voice** / **Re-record my voice**, matching the web caller. Recording
lasts eight seconds; a countdown and Cancel control are shown. Start Call and
identity edits are locked during capture/upload. The same normalized email is
sent with the call. A phone-only caller identity cannot enroll with the current
email-keyed backend; use an email for voice-authenticated Wema testing.

The recording is temporary, deleted after capture, and sent only when you
explicitly choose to record. The microphone/audio session is released before
the call begins. Backgrounding the app cancels enrollment. Re-recording replaces
the existing backend voiceprint, so use your own voice. Existing enrollment is
reused without re-recording. Neither enrollment nor a prefilled customer ID is
a successful Session or Action voice-authentication check.

Optional launch environment variables (not committed customer data):

| Variable | Purpose |
| --- | --- |
| `ATTENTIVE_CALL_ENDPOINT` | Existing call bootstrap endpoint |
| `ATTENTIVE_BUSINESS_SLUG` | Dashboard business slug |
| `ATTENTIVE_AGENT_ID` | Public agent ID |
| `ATTENTIVE_CALLER_CONTACT` | Caller email/phone identity |
| `ATTENTIVE_CUSTOMER_ID` | Bank customer ID |
| `ATTENTIVE_PHONE` | Default own-line phone number |
| `ATTENTIVE_ACCOUNT` | Optional selected bank account |

These variables are forwarded by the script. Xcode users can set them in the
scheme's Run environment. Caller details are kept in memory, not stored on disk
by the sample. The backend may record calls according to its existing settings.

## Experience

- SwiftUI port of the **public web caller**, not Voice Lab: full-screen light
  stage, original circular avatar, pre-call contact card and bottom call dock.
- Hamburger/X floating My Wema panel; chat opens a separate transcript panel.
  Only one panel is displayed at a time. Both respect the keyboard/safe area,
  and long content scrolls. All dock controls have 44-point tap targets.
- Start/end calls; mute/unmute microphone.
- Live conversation text and typed messages. Wait for the opening greeting before
  sending chat: the current backend protects that greeting from interruption.
- Session and action authentication badges driven by backend events.
- Expandable tool activity with the original structured results.
- Pre-call caller profile and tool-specific/LLM-generated waiting phrase selection.
- Settings locked during calls; results remain visible until the next call.

The UI package's `CallerTheme.swift` maps the existing web call-surface tokens.
Its bundled `Resources/Assets.xcassets/CallerAvatar.imageset/caller-avatar.jpg` is the unchanged JPEG
embedded in the dashboard's `public/assets/AI-avatar-2.svg`, center-cropped by
SwiftUI just as the web SVG does. No new avatar or image service is introduced.
The avatar has no outer ring or repeating pulse. While the agent is speaking,
its scale follows decoded agent audio energy (RMS plus peak), using the web's
bounded energy range, 0.25 scale gain and spring settings. Quiet speech produces
smaller movement than loud speech; silence returns to rest. Reduce Motion keeps
the avatar stationary. This is amplitude-driven, not an exact copy of the web's
seven-band frequency analysis.

The dock exposes only capabilities already supported by the wrapper: microphone,
chat, settings and end call. Web camera/casting placeholders are not reproduced
as nonfunctional buttons. This sample does not add automatic account lookup or
client-side transaction authorization.
The two authentication badges continue to reflect the existing backend checks.

Allow microphone access to speak. The simulator uses the Mac's audio devices;
macOS may also ask for microphone permission. The `--chat-only` script argument
starts with the microphone disabled for automated testing, but the app can enable
it later. Chat alone cannot satisfy voice authentication.

## Boundaries

`SampleModel` owns one `AttentiveCall` and the deployment/profile configuration.
`CallScreen` embeds the public `AttentiveCallerView` with a request binding,
enrollment and sample-supplied Wema labels. `CallerSettings` is the sample's
developer configuration form, not part of the UI library. The sample keeps its
own core event listener for bounded diagnostics; the UI does not replace it.
The wrapper handles session bootstrap, microphone permission,
WebRTC audio, WebSocket signaling, reconnect events, chat and event mapping.
The existing agent retains VAD, turn detection, STT, LLM, TTS, authentication and
tool execution. No model fallback or server configuration is introduced here.
This is not yet a gRPC client, CallKit integration or background-call product.
Use `--generic-ui` to inspect the library's unbranded defaults with no enrollment
control or bank profile fields; this only changes presentation, never backend
authorization. See the [UI integration guide](../../CALLER_UI.md).

Insecure HTTP/WS is enabled only for HTTP targets in Debug simulator builds, or explicitly in the
Debug-only physical-device LAN test mode below. Release builds require reachable
HTTPS/WSS endpoints; a phone cannot use the Mac's `127.0.0.1`.
Select a signing team and confirm the backend's reachable media addresses
before testing a device. The local-network ATS entry follows Apple's
[NSAllowsLocalNetworking documentation](https://developer.apple.com/documentation/bundleresources/information-property-list/nsapptransportsecurity/nsallowslocalnetworking);
there is no global arbitrary-load exception.

## Physical iPhone on the Local Network

The same app consumes `AttentiveVoiceUI`; no separate caller implementation or
backend is needed. Sign in to Xcode, connect/unlock the iPhone, trust the Mac and
enable Developer Mode. Keep the phone and Mac on the same trusted Wi-Fi network.
Allow Local Network and Microphone access when the app asks.

Normal device integrations use HTTPS/WSS. For an explicitly approved local Debug
test, set `ATTENTIVE_LOCAL_DEVICE=1` and use the Mac's RFC1918 IPv4 address at port
3000 (original agent) or 3004 (comparison agent) as `ATTENTIVE_CALL_ENDPOINT`.
This mode is compiled out of Release. Only
`ws://127.0.0.1:7880` (or equivalent loopback host) in returned credentials is
mapped to the API's private host; room, token, URL path/query and all other
signaling endpoints remain unchanged. The core SDK and dashboard are untouched.
Local testing is unencrypted; use only a trusted LAN, never a public network.

When local LiveKit binds signaling only to loopback, run this separate temporary
forwarder from the repository root (replace the example private address):

```sh
node sdks/ios/Examples/AttentiveSample/script/forward_local_signaling.mjs 192.168.1.10
```

It binds only that explicitly supplied, locally assigned private IP on port 7880
and pipes TCP to `127.0.0.1:7880`, without logging traffic. Existing LiveKit and
web callers keep using loopback. It neither restarts services nor changes DNS,
firewall, router settings or model parameters. LiveKit must already advertise the
correct LAN media address and expose its RTC ports; the forwarder covers signaling
only. If media addresses need changing, request approval separately.

Then build/install/launch (supply your own device/team/caller values):

```sh
export ATTENTIVE_DEVICE_ID='YOUR_PHYSICAL_DEVICE_UDID'
export ATTENTIVE_DEVELOPMENT_TEAM='YOUR_APPLE_TEAM_ID'
export ATTENTIVE_CALL_ENDPOINT='http://192.168.1.10:3000/api/public-agent/connection-details'
export ATTENTIVE_LOCAL_DEVICE=1
export ATTENTIVE_CALLER_CONTACT='your-enrolled-email@example.com'
export ATTENTIVE_CUSTOMER_ID='YOUR_CUSTOMER_ID'
export ATTENTIVE_PHONE='YOUR_PHONE_NUMBER'
bash sdks/ios/Examples/AttentiveSample/script/build_and_run_device.sh
```

For the original local agent, explicitly set `ATTENTIVE_AGENT_ID=agt_59a007e81e`;
the app now defaults to the public Lagos comparison agent. Customer details
remain launch-time inputs, not committed defaults. End and close any current
call before rerunning; the script does not
terminate an existing app/call. These inputs are not persisted: after force
quitting, relaunch with the script or Xcode Run environment. No voiceprint is
created/replaced automatically, and no call starts on launch.

Rollback: stop only the forwarder with Ctrl-C, or send SIGTERM to its reported
PID after verifying the command with `ps`. Close the physical app and remove
`ATTENTIVE_LOCAL_DEVICE` from the launch environment. No existing server state
was changed. If the Mac changes networks, stop the old forwarder and recheck both
signaling and RTC addresses before retesting. Do not reuse a stale LAN address.

### RVC + LiveKit Comparison Agent

The published SDK also accepts the comparison backend's existing call credentials.
No SDK release, alternate audio engine, prompt edit or authentication bypass is
required. These are two explicit launch configurations for the same sample:

| Target | Call endpoint path / port | Wema public ID on this local database |
| --- | --- | --- |
| Original | `:3000/api/public-agent/connection-details` | `agt_59a007e81e` |
| RVC + LiveKit | `:3004/api/public-agent/rvc-session` | `agt_73099afb71` |

The comparison backend must already be running with the comparison public ID in
its server-side LiveKit allowlist. A standalone RVC WebSocket session is not a
compatible SDK bootstrap response. Verify that the resulting room starts with
`rvc-livekit-` and uses worker `rvc-livekit-comparison`.

If the comparison dashboard is loopback-only, expose only its existing HTTP
listener on this Mac's private LAN address using a second temporary forwarder:

```sh
node sdks/ios/Examples/AttentiveSample/script/forward_local_signaling.mjs 192.168.1.10 3004
```

Keep the existing 7880 signaling forwarder running; do not start a duplicate.
An explicitly approved local-device launch now saves only its endpoint, business
slug, agent ID and Debug local-device permission in the sample's preferences.
Opening the installed app from the Home Screen therefore retains the comparison
route instead of reverting to loopback. Caller details, tokens and voice samples
are not saved by this mechanism. A new explicit endpoint replaces this setup;
an endpoint without local-device opt-in or `ATTENTIVE_LOCAL_DEVICE=0` clears it.
Release builds ignore these preferences and continue requiring secure endpoints.
The iPhone must still share a reachable local network with the Mac.

The optional port argument permits only 7880 and 3004. This forwards the call
bootstrap/enrollment API, not audio through another orchestration layer. Both
servers and the old caller remain unchanged.

With the device/team/caller variables above set, launch the comparison:

```sh
export ATTENTIVE_CALL_ENDPOINT='http://192.168.1.10:3004/api/public-agent/rvc-session'
export ATTENTIVE_AGENT_ID='agt_73099afb71'
bash sdks/ios/Examples/AttentiveSample/script/build_and_run_device.sh
```

Replace the example LAN address and use the existing enrolled caller email.
The comparison bootstrap currently supplies bank context from its server-side
prefill settings; it does not apply the sample's editable `wemaContext` or
`toolWaitSpeechMode`. Voice checks and banking execution remain backend-owned.
Do not interpret chat-only testing as successful voice authentication or assume
all comparison-backend features have native parity.

To restore the old target, close/end the sample call and relaunch with the
original endpoint and ID from the table. The SDK package pin and original app
defaults are unchanged. Stop only the optional port-3004 forwarder when finished.

## Tests

From `sdks/ios` (replace the simulator ID):

```sh
xcodebuild -project Examples/AttentiveSample/AttentiveSample.xcodeproj \
  -scheme AttentiveSample -destination 'id=YOUR_SIMULATOR_ID' \
  -derivedDataPath .build/sample-app -clonedSourcePackagesDirPath .build/sample-dependencies \
  -scmProvider system \
  -parallel-testing-enabled NO CODE_SIGNING_ALLOWED=NO test
```

The UI test checks the start screen, floating menu, waiting-phrase selection,
caller settings and portrait/landscape layouts. The live test is
skipped by default. Prefix the command with the environment variable
`TEST_RUNNER_ATTENTIVE_LIVE_UI_TEST=1` to opt in: it starts a real chat-only call, waits for
the greeting, verifies non-silent received audio, sends a harmless message,
verifies the reply transcript, and ends the call. It can create a backend
conversation/recording; it does not intentionally invoke banking tools.
Also set `TEST_RUNNER_ATTENTIVE_AVATAR_CAPTURE=1` to attach a sequence of call-stage
screenshots during the greeting for visual inspection of audio-driven movement.

See [verification](../../VERIFICATION.md) for the latest results and remaining
physical-device/voice-auth tests.
