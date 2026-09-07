# Wrapper Verification: 2026-09-06

## Passed

- `swift test -j 4`: 14 tests, zero failures. Also compiled the native diagnostic.
- `xcodebuild -scheme AttentiveVoice -destination 'generic/platform=iOS Simulator'
  -derivedDataPath .build/ios-simulator -clonedSourcePackagesDirPath .build
  -disableAutomaticPackageResolution CODE_SIGNING_ALLOWED=NO -jobs 4 build`:
  BUILD SUCCEEDED (arm64 and x86_64 simulator).
- The existing dashboard caller page returned HTTP 200; the voice worker health
  endpoint returned OK; local LiveKit was listening on port 7880.
- Native diagnostic call at 17:09 WAT: the existing call-start endpoint returned
  HTTP 200. LiveKit recorded Swift SDK 2.16.0 joining, with both subscriber and
  publisher ICE/peer connections reaching `connected`.
- The wrapper closed the unanswered call after its 30-second agent timeout.

## Dispatch Recovery and Native Call

The first live test did not receive an agent greeting or audio. At 17:09:47 WAT,
LiveKit logged `failed to send job request` with
`no servers available (received 1 responses)` for the configured room agent.
No agent joined. This is a backend dispatch-availability failure despite the
process health endpoint responding; health alone is not a successful call test.

With user approval, only the local voice worker was restarted, preserving its
existing environment and launch command. It registered successfully at 17:18 WAT.
The dashboard, LiveKit server, model endpoints/settings, auth gates, tool logic
and server startup scripts were not changed or restarted.

The subsequent native diagnostic **passed**: call creation, agent greeting,
chat reply, final transcripts and non-silent decoded audio. Its first attempt
sent chat during the protected greeting and was rejected by the existing backend
(`This generation handle does not allow interruptions`). The diagnostic now
waits for a final greeting transcript and listening state before sending; no
backend interruption policy was changed.

## iOS Sample

- `Examples/AttentiveSample/script/build_and_run.sh`: BUILD SUCCEEDED, installed
  and launched `ai.odion.attentive.sample` on iPhone 17 Pro / iOS 26.5 Simulator.
- Native XCUITest for caller details and activity view passed.
- Opt-in XCUITest real chat-only call passed at 17:27 WAT: start, greeting,
  non-silent received audio, typed message, reply transcript and end call.
- `swift test -j 4` rerun: 14 tests, zero failures.
- Screenshot inspected for the actual native screen, not a browser/mockup.
- Both UI tests passed together at 17:28 WAT, including landscape layout and
  chat-composer screenshots. No calls were left active after the tests.
- The strengthened live UI test passed again at 17:30 WAT, waiting for the
  complete final reply transcript and return to listening before ending.

See the sample README for repeatable commands. Live UI tests require
`TEST_RUNNER_ATTENTIVE_LIVE_UI_TEST=1` in the xcodebuild process environment.
They deliberately leave the microphone off and do not invoke banking tools.

Physical iPhone microphone input, barge-in, Bluetooth/audio routing, voice
verification and privileged tool execution are NOT yet verified end to end
through the native wrapper. Badges display backend events; a prefilled profile
does not pass authentication. Unit tests cover event mapping/lifecycle, not model
accuracy or authentication quality. This preview does not include CallKit,
background calls, a production credential gateway or gRPC.

The local backend also logged recording/session metadata persistence errors
after a diagnostic call. This is recorded as an existing integration issue;
successful native audio/chat does not prove dashboard recording availability.
One native test greeting identified SAW as a Fidelity Bank assistant despite the
Wema public agent target. This is an observed backend response inconsistency,
not a claim about its root cause. No agent prompt or routing was edited here.

## Public Caller SwiftUI Port

The sample presentation now follows the existing public caller page instead of
the previous navigation/tab layout. Only sample views, assets, UI tests and docs
were changed in this UI pass. `SampleModel`, the wrapper, dashboard, backend,
model settings, authentication gates and tool execution were not modified.

- Build succeeded on iPhone 17 Pro / iOS 26.5 Simulator.
- Both targeted UI tests passed at 18:07 WAT, with zero failures: pre-call form,
  My Wema open/close, phrase selection, settings, landscape and a real chat-only
  call with greeting, non-silent received audio, final reply and end call.
- In-call profile and phrase controls were verified disabled.
- Inspected start, call-stage, bank-panel, transcript and keyboard screenshots.
- The initial test failure was an accessibility identifier inherited by child
  elements. Removing the container identifier fixed the test; the call itself
  was already connected with audio. No service restart was needed.
- Banking transactions and voice verification were not exercised by these tests.
  The sample continues to display real SDK events; it does not simulate success.

## Audio-Reactive Avatar

Replaced the sample's constant speaking pulse and outer ring with passive agent
audio metering. The existing remote audio observer now calculates RMS/peak energy
at up to 25 Hz without retaining or changing PCM. Only the avatar observes the
meter; call controls, transcripts and tool/auth views are not invalidated by each
audio sample. The avatar uses a bounded scale and spring, rests during silence,
and stays still with Reduce Motion enabled. No microphone, playback, backend,
model, authentication, tool or server configuration was changed.

- `swift test -j 4`: 21 tests passed at 18:36 WAT. Added coverage for quiet/loud
  energy differences, silence, float/int16/int32 PCM, interleaved stereo,
  unchanged buffers, invalid samples, meter expiry, isolated publication and
  call-end/stale-event cleanup.
- Native iPhone 17 Pro / iOS 26.5 build and build-for-testing succeeded.
- `git diff --check` passed.
- Installation was initially deferred to leave an active caller session
  undisturbed. After the user requested a rebuild/relaunch, the updated app was
  rebuilt, installed and launched at 19:41 WAT. The Start Call screen was visually
  confirmed on iPhone 17 Pro / iOS 26.5, with the caller launch configuration
  preserved. Live speech-reactive animation verification is pending the user's
  call test; no backend services or model settings were changed.
- The opt-in live UI test can capture twelve greeting-stage screenshots with
  `TEST_RUNNER_ATTENTIVE_AVATAR_CAPTURE=1` plus
  `TEST_RUNNER_ATTENTIVE_LIVE_UI_TEST=1` for amplitude/scale inspection.

## Voice Enrollment and Local Call Recovery

The native sample now uses the existing web enrollment API, without changing
the dashboard route or backend authentication policies. The SDK adds a separate
pre-call enrollment controller and an iOS recorder: eight seconds of 16 kHz,
mono, 16-bit PCM WAV, submitted as multipart `email` and `audio`. Email is
normalized consistently for both enrollment and call creation. Starting a call
and editing the identity/settings are disabled during capture/upload; cancelling
or backgrounding stops capture, removes its temporary file and restores the
pre-call audio-session category. Enrollment does not set Session/Action badges
to verified or authorize tools. Those still consume backend events.

Verification on 2026-09-06 (WAT):

- `swift test -j 4`: 30 tests passed at 20:03, including nine new enrollment
  tests for the HTTP contract, identity handling, failures, cancellation and
  stale responses.
- Native caller-details/layout UI test passed at 19:49, including portrait,
  landscape, floating panel, settings and phrase controls.
- Native enrollment validation/cancellation UI test passed at 19:50. It started
  capture for a disposable test email and cancelled before upload. A subsequent
  GET confirmed that identity remained unenrolled; no enrollment temp directory
  remained. No user's existing voiceprint was replaced by automated testing.
- After local transport recovery, the real chat-only UI test passed at 20:04:
  call start, complete greeting, non-silent audio, typed message, final reply,
  listening state and clean hang-up. In-call settings remained locked. Result:
  `/tmp/attentive-enrollment-live-2004.xcresult`.
- Twelve real-call avatar screenshots show no outer ring, changing sizes during
  speech and a resting size after speech. The colored-avatar pixel bounds vary
  across frames rather than following a fixed repeating animation.
- Rebuilt, installed and relaunched on iPhone 17 Pro / iOS 26.5 at 20:04.
  The final screen at 20:05 shows the configured caller's existing enrollment
  from a read-only server check. Record/re-record is available before a call.

The user's 19:42 call failure was separate from enrollment: the worker received
the job but its RTC connection timed out. Local LiveKit was still advertising
`192.168.100.231` after the Mac changed networks. With user approval, local
LiveKit was restarted with the then-current Wi-Fi address `192.168.100.241`.
The local worker also required a same-command/environment restart after its
reconnect retry budget was exhausted. A restart attempt initially omitted the
configured LiveKit credentials and produced HTTP 401; the existing `.env`
credential pair was restored, and the worker registered at 20:03:18. No keys
were rotated. An earlier UI retry while LiveKit was offline failed; the later
20:04 test above passed.

Final runtime settings are unchanged except for the advertised node IP:
LiveKit HTTP bind `127.0.0.1:7880`, RTC TCP `7881`, UDP `7882`, Redis
`127.0.0.1:6380`; worker command `.venv/bin/python main.py dev`, connecting to
`ws://127.0.0.1:7880`. For future restarts, the existing
`deployment/stable/local/start-livekit.sh` loads the credential pair and detects
the active default-interface address unless `LOCAL_LIVEKIT_NODE_IP` is set.
Check the advertised address again after a network change. No NPU/jumper
service, model parameter, dashboard source, tool logic or authentication gate
was changed in this recovery.

Remaining manual verification: an actual caller recording a full enrollment
and passing the live Session/Action speaker checks, followed by a privileged
tool. The automated live call deliberately uses text and does not prove those
checks or microphone audio quality. The backend again introduced itself as
Fidelity Bank in the Wema-targeted test; the existing prompt/config inconsistency
noted above remains outside this native UI change.

## Reproducibility

### Subsequent Voice Call Failure (20:06 WAT)

The caller's next microphone-enabled call connected but ended at 20:06:16.
Worker logs for job `AJ_gDyjDJ4ZWk73` identify an unrecoverable STT error after
Deepgram retries: `ClientConnectorDNSError` resolving `api.deepgram.com`.
The backend then deleted the room. This was not the iOS agent-start watchdog
or a voice-auth rejection. The auth observer had attached and published pending
Session/Action statuses; seven Wema tools were registered.

Subsequent diagnostics reproduced intermittent DNS failures: three authenticated
Deepgram WebSocket handshakes succeeded, but later fresh HTTP requests failed
at name resolution. The active Wi-Fi resolver `192.168.100.1` timed out on a
direct DNS query; `1.1.1.1` and `8.8.8.8` returned answers. No system DNS settings,
provider, credentials, retry policy or server configuration were changed. Local
LiveKit and the worker remain listening with the correct current node address.
The successful chat-only test above does not establish reliable microphone/STT
operation under this network condition.

The same call's first 41-character TTS request reported 16.61 seconds of audio
before STT failure cancelled synthesis. This is consistent with the previously
documented intermittent source-synthesis repetition, but duration alone does not
prove its cause in this call. A separate unchanged initial-8 greeting probe then
produced 2.64 audio seconds in 1.945 seconds with no modeled delivery underrun.
Attempts to transcribe the probe and actual recording were blocked by the same
DNS failure. The recording exists locally, but metadata persistence failed.
Do not mark the stammering resolved from the successful short probe.

Earlier isolated full-text-conditioning results and the still-pending approval
to enable `non_streaming_mode: true` for calls are documented in
`deployment/experiments/2026-09-05-tts-turn-taking/TOOL_AND_GREETING_FOLLOWUP.md`.
That option has not been enabled; HTTP PCM streaming remains unchanged.

At 20:12 WAT, with explicit approval, a bounded DNS experiment temporarily set
this Mac's Wi-Fi DNS to `1.1.1.1` and `8.8.8.8`. Five fresh authenticated Deepgram
WebSocket connections succeeded (0.74-1.43 seconds). The diagnostic sent only
synthetic silence, not a caller recording, and did not enroll/authenticate anyone.
The original setting had no manual DNS entries. A `finally` cleanup restored
`networksetup -setdnsservers Wi-Fi Empty`; both `networksetup` and `scutil --dns`
confirmed automatic DNS via `192.168.100.1` again. No application code, services,
router settings or model parameters changed. This supports the DNS diagnosis,
but is not an end-to-end voice-call test or a permanent fix; the original DNS
failure may recur now that the experiment has been reverted.

LiveKit is pinned to 2.16.0 and dependency revisions are in `Package.resolved`.
SwiftPM's initial binary download stalled locally. The two official release
archives were fetched separately and their SHA-256 values matched the upstream
package manifests before placing them in the local SwiftPM artifact cache:

- WebRTC 144.7559.11: `07c5caf718058af3c528dcabd257298c40e5a8527e4fb9f47c48336ba5899853`
- UniFFI 0.0.6: `0d3f2ce159a224c728f8b131068d53bbf9b13d968cda0edc68a6a2290f2651ed`

No dependency sources/manifests were patched, no TLS verification was disabled,
and no binary archives or credentials are committed to this repository.

## Optional Caller UI Extraction (2026-09-06)

Pre-extraction checkpoint: `7db19c6`. Branch: `attentive-ios-sdk`.

The package now exports headless `AttentiveVoice` and optional `AttentiveVoiceUI`.
The sample consumes `AttentiveCallerView` instead of owning copied caller views.
Branding, caller request and optional enrollment are supplied by the host. The
sample retains its developer settings; the library has generic defaults and a
bundled avatar resource. Core, transport, agent, dashboard, model parameters,
authentication policy and running service configurations were not changed.

Verification completed on iPhone 17 Pro / iOS 26.5 Simulator:

- `swift test -j 4`: 38 tests passed, including eight new UI-control tests.
  These cover host event callback preservation, unchanged request forwarding,
  microphone/chat controls, error handling, enrollment cancellation, pending
  call cancellation on dismissal, and explicit call retention on dismissal.
- Xcode build succeeded. All four UI tests passed at 21:42 WAT: Wema profile,
  activity/settings and portrait/landscape layout; enrollment cancellation;
  generic caller presentation; and a real chat-driven call with greeting,
  non-silent decoded agent audio, chat reply, locked in-call fields and hangup.
- Visually inspected Wema/generic panels, auth badges, call stage and avatar.
  The original avatar asset is unchanged and renders from the library bundle.
  The existing audio-energy animation remains; no ring or looping pulse added.
- Auth statuses and tool events remain backend-owned. The UI observes published
  state without replacing the host's `onEvent` callback or granting tool access.

Local evidence (not committed build artifacts):

- Unit log: `/tmp/attentive-ui-extraction-unit.log`
- Build log: `/tmp/attentive-ui-extraction-build.log`
- UI/live-call log: `/tmp/attentive-ui-extraction-final.log`
- Result bundle: `/tmp/attentive-ui-extraction-final.xcresult`
- Screenshots: `/tmp/attentive-ui-extraction-final-images/`

The live test was deliberately chat-only. It does not prove microphone/STT
reliability, successful voice enrollment, either real speaker check, privileged
bank transactions or resolution of the earlier TTS artifacts. The existing
Fidelity Bank greeting on the Wema-targeted backend remains unchanged. A full
real-voice test and physical-device testing remain release gates.

For rollback, inspect a separate worktree at checkpoint `7db19c6` or revert the
specific extraction commit after reviewing subsequent changes. Do not reset an
active dirty worktree. There is no server-state rollback for this extraction:
no service, DNS, port or model configuration was touched. Gateway, gRPC and
Android work are tracked separately in `docs/attentive/ROADMAP.md`.

## Physical iPhone Setup (2026-09-06)

With explicit user approval, added a sample-only Debug LAN mode and a temporary
Mac-side signaling forwarder. The physical app still embeds `AttentiveVoiceUI`
and targets `wema-bank-poc-local` / `agt_59a007e81e`. No core SDK, UI library,
dashboard, agent, model, voice-auth gate or existing service was changed.

- Signed arm64 Debug build succeeded using the user's Apple development account.
  `codesign --verify --deep --strict` passed. Installation on the connected
  iPhone 16 Pro Max / iOS 26.6.1 succeeded.
- First launch was denied by iOS with a development-profile trust error; the
  user then explicitly trusted their profile on the phone. A rebuild, reinstall
  and configured launch succeeded at 22:17 WAT.
- Six simulator UI/policy tests ran: five passed, the opt-in live-call test was
  skipped, and none failed. New tests cover explicit private-endpoint opt-in and
  loopback-only signaling remapping, with no production URL changes.
- Direct LAN checks returned HTTP 405 for GET on the POST-only call API, and
  HTTP 200 for the forwarded LiveKit root.
- The caller started a physical-device call at 22:17:47, job `AJ_5AbixzUoLJ67`.
  The backend received a spoken balance request and generated a spoken reply.
  A device screenshot at 22:18:33 shows the active caller UI, enabled microphone,
  supplied profile details and a passed Session authentication badge. Action
  authentication remained pending and no banking activity was displayed at that
  observation. Do not treat this as proof of completed privileged-tool execution
  or of long-call audio quality. The user's call was not restarted or ended.
- The first pre-call screenshot showed an enrollment-status network error,
  although the LAN API returned `enrolled: true` and the subsequent live Session
  check passed. The timing relative to iOS's first Local Network permission was
  not captured; this transient pre-call error needs a separate retest.

Test setup at 22:16 WAT: Mac API `192.168.100.241:3000`; separate Node forwarder
PID `75213`, binding `192.168.100.241:7880` to `127.0.0.1:7880`. Original LiveKit
PID `26785` and voice worker PID `26631` were left running. LiveKit already
advertised the same LAN RTC address. No DNS, firewall, router, server binding or
model/startup parameter was modified. Local HTTP/WS is unencrypted and is only
for this approved trusted-LAN test.

Evidence: `/tmp/attentive-physical-signed-build.log`,
`/tmp/attentive-device-policy-tests.xcresult`, and
`/tmp/attentive-local-signaling.log`. The forwarder logs only its address/PID,
not call payloads or credentials. Build products/profiles remain ignored.
The final build/install/launch log is `/tmp/attentive-iphone-install-launch.log`.
All 38 core/UI-control Swift tests also passed in
`/tmp/attentive-device-core-tests.log`.

To undo networking, verify PID `75213` still belongs to
`forward_local_signaling.mjs`, then send it SIGTERM. Do not kill LiveKit, the
worker or the dashboard. Close the sample and remove `ATTENTIVE_LOCAL_DEVICE`
from its launch environment. Device reproduction and the foreground forwarder
command are in `Examples/AttentiveSample/README.md`.
