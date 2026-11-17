# Internal animated-theme staging call

This Debug-only harness runs `AttentiveAgentView` with the actual SDK configuration and call path, but injects an explicitly supplied staging service. The customer API and production endpoint are unchanged. It starts with the microphone disabled, prints only sanitized configuration/call/audio markers, and ends the call after non-silent assistant output or a 75-second timeout. It does not intentionally invoke tools or banking actions; use only a staging agent without those capabilities.

Before running, independently verify the service is an isolated staging deployment, that its agent has an animated theme saved, and that the agent and API key belong to staging. A staging-labelled hostname is a guard against accidents, not proof of environment isolation. The harness accepts only HTTPS, an exact confirmed hostname, and `/api/sdk/v1/`; it will not issue a request on invalid configuration.

From `sdks/ios`, build the source package into the generated, separate sample:

```sh
node Tests/AnimatedThemeStaging/create_harness.mjs
xcodebuild -project .build/animated-theme-staging-app/AttentiveQuickStart.xcodeproj \
  -scheme AttentiveQuickStart -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath .build/animated-theme-staging-build -configuration Debug \
  ENABLE_TESTABILITY=YES CODE_SIGNING_ALLOWED=NO build
xcrun simctl install <booted-simulator-udid> \
  .build/animated-theme-staging-build/Build/Products/Debug-iphonesimulator/AttentiveQuickStart.app
```

Create a private local JSON file with permissions `0600` (never commit or print it):

```json
{
  "baseURL": "https://your-confirmed-staging-host/api/sdk/v1/",
  "confirmedHost": "your-confirmed-staging-host",
  "apiKey": "<staging calling key>",
  "agentID": "<staging agent ID>",
  "customerID": "<optional staging customer ID>",
  "callerToken": "<optional staging caller token>"
}
```

The host must be `salesgirlstaging.odion.ai`, have a DNS label `staging`, start with `staging-`, or end with `-staging`. Remove the optional fields when they are not needed. The runner requires explicit confirmation and launches the app with credentials in its process environment; it stores the raw simulator log in a private temporary file and prints only the `STAGING_` markers.

```sh
python3 Tests/AnimatedThemeStaging/run.py --configuration /path/to/private.json \
  --simulator <booted-simulator-udid> --staging-verified
```

Observe the UI while the call is active. `STAGING_THEME` confirms the key retrieved by the SDK, `STAGING_MICROPHONE=off` confirms the input is disabled, and `STAGING_ASSISTANT_OUTPUT=non_silent` confirms decoded assistant output. This is not a listening assessment or confirmation of acoustic output on a physical device. A timeout or missing marker is a test failure, not proof of a successful call.

For a purely local visual inspection, launch the installed app with a synthetic theme and energy level:

```sh
SIMCTL_CHILD_ATTENTIVE_THEME_PREVIEW='animated:#2F6FED:grain' xcrun simctl launch \
  <booted-simulator-udid> ai.odion.attentive.animated-theme-staging
```

The preview is labelled `PREVIEW · NO CALL`; it has no SDK client, sends no request, and uses synthetic energy. It does not verify staging or real assistant audio.
