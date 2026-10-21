# Minimal supplied-UI app

`AttentiveExampleApp.swift` is the entire sample app entry point. It has no sample
model, server URL, business slug or built-in customer profile. Configure
`ATTENTIVE_API_KEY` and `ATTENTIVE_AGENT_ID` in your local Run scheme using a key
created under Dashboard > Deploy > API & SDK. For a customer-specific call, also
set `ATTENTIVE_CUSTOMER_ID` and a fresh `ATTENTIVE_CALLER_TOKEN` from your backend.
The ID is passed directly to `AttentiveAgentView`; there is no profile-entry menu.
Without an ID/token the sample makes a general call, if allowed by the key.

Caller tokens expire after five minutes; the environment is only a local sample
convenience. In a real app, replace this callback with your existing authenticated
backend client, returning a fresh token per call. Never put a server identity key
in Xcode, the app, or a shared scheme. A raw customer ID plus a publishable key
does not authorize access to a customer's banking records.

Resolve the latest compatible stable SDK release, then pin its exact tag. Add `NSMicrophoneUsageDescription` to
your target. The account and three-dot menus are hidden; mute, transcript and end
call remain available.

The Start call button belongs to this app. It presents `AttentiveAgentView` in a
full-screen cover, which starts once and dismisses after End call. Pass
`startsAutomatically: false` only when you want the SDK's built-in start button.
The generated demo enables the Audio background mode for an active call; your
app should enable and test that mode only if it needs background calling.

Maintainers can build this source version without altering the existing binary
sample or app installed on a phone:

```sh
node sdks/ios/script/create_quickstart_project.mjs
```

To verify a built binary package instead, pass its directory as the first argument
to the same script. The generated app and test credentials are never distributed.

Open the generated `sdks/ios/.build/quickstart/AttentiveQuickStart.xcodeproj` and
run scheme `AttentiveQuickStart` on a simulator. This uses a local source package,
has a distinct bundle ID, and intentionally ships no real calling key. Until
configured, tapping Start call shows an invalid-key message; that is not a live-call test.

See [the integration guide](../../ACCOUNT_KEYS.md) for headless use and authenticated
callers. Neither this README nor the example source is part of the binary bundle.
