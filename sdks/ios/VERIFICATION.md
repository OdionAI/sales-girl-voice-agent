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

## End-to-End Blocker

The same live test did not receive an agent greeting or audio. At 17:09:47 WAT,
LiveKit logged `failed to send job request` with
`no servers available (received 1 responses)` for the configured room agent.
No agent joined. This is a backend dispatch-availability failure despite the
process health endpoint responding; health alone is not a successful call test.

No dashboard, voice worker, LiveKit, NPU model settings, auth gates, tools or
startup scripts were changed or restarted during this SDK implementation.
Approval was requested to restart only the local voice worker with unchanged
settings before repeating the real conversation test.

Real agent audio, chat response, physical iPhone microphone input, barge-in,
Bluetooth/audio routing, voice verification and privileged tool execution are
NOT yet verified end to end through the native wrapper. Unit tests cover event
mapping and lifecycle behavior, not the models' behavior or authentication quality.

## Reproducibility

LiveKit is pinned to 2.16.0 and dependency revisions are in `Package.resolved`.
SwiftPM's initial binary download stalled locally. The two official release
archives were fetched separately and their SHA-256 values matched the upstream
package manifests before placing them in the local SwiftPM artifact cache:

- WebRTC 144.7559.11: `07c5caf718058af3c528dcabd257298c40e5a8527e4fb9f47c48336ba5899853`
- UniFFI 0.0.6: `0d3f2ce159a224c728f8b131068d53bbf9b13d968cda0edc68a6a2290f2651ed`

No dependency sources/manifests were patched, no TLS verification was disabled,
and no binary archives or credentials are committed to this repository.
