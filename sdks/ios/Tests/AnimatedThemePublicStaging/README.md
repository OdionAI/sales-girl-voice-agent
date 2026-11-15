# Internal public-agent staging call

This Debug-only simulator harness reuses the existing staging sample project generator and the SDK's unmodified `AttentiveCallerView`. It fetches Amaka's theme and language from the staging public resolver, then starts the same staging public-agent session used by the web caller with the microphone disabled. The caller UI is unchanged. It uses a generated test-only email and never prints it, tokens, transcripts, or audio. It logs sampled scalar output-energy levels and state, and ends after the first non-silent greeting or a timeout.

This is the public-agent SDK path, **not** the key-based `AttentiveAgentView` path. It does not validate calling-key or caller-token policy, nor prove anything about production SDK credentials.

Before launching, independently verify the staging dashboard, configuration, billing, media service, dispatch worker and agent tools are isolated from production and training, and that an ordinary public call to this test agent is authorized. If inference infrastructure is shared, obtain explicit approval for that use. A staging hostname alone is not evidence. A call creates a session and may consume staging credits. The test checks the returned media host before connecting, but this check alone cannot establish isolation.

From `sdks/ios`:

```sh
node Tests/AnimatedThemePublicStaging/create_harness.mjs
xcodebuild -project .build/animated-theme-public-staging-app/AttentiveQuickStart.xcodeproj \
  -scheme AttentiveQuickStart -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath .build/animated-theme-staging-build -configuration Debug \
  ENABLE_TESTABILITY=YES CODE_SIGNING_ALLOWED=NO build
xcrun simctl install <booted-simulator-udid> \
  .build/animated-theme-staging-build/Build/Products/Debug-iphonesimulator/AttentiveQuickStart.app
```

Once isolation has been confirmed, run the simulator with console logs captured to a private file:

```sh
SIMCTL_CHILD_ATTENTIVE_STAGING_VERIFIED=yes xcrun simctl launch --terminate-running-process \
  --console <booted-simulator-udid> ai.odion.attentive.animated-theme-public-staging
```

`STAGING_THEME` is read directly from the public resolver; `STAGING_MEDIA` confirms the returned signaling host matches the expected staging host. `STAGING_AUDIO` records Unix timestamp, uptime, agent state and scalar meter values; `STAGING_EVENT` timestamps state changes. These can be correlated with observed UI motion, but the markers alone do not prove audible sound or visual behavior. A test is incomplete if the call fails, times out, is silent, or cannot be observed. The app disconnects after 15 seconds of observing non-silent output; it closes itself after cleanup.
