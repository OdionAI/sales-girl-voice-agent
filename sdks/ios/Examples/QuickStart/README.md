# Minimal supplied-UI app

`AttentiveExampleApp.swift` is the entire customer app entry point. It deliberately
has no sample model, server URL, business slug, test profile or developer flags.
Create an API key under Dashboard > Deploy > API & SDK and replace `YOUR_API_KEY`
and `YOUR_AGENT_ID`. Use SDK `0.1.0-staging.3` or a compatible newer version, with
the key API deployed and enabled. Add `NSMicrophoneUsageDescription` to your target.

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
configured it shows the invalid-key message; that is not a live-call test.

See [the integration guide](../../ACCOUNT_KEYS.md) for headless use and authenticated
callers. Neither this README nor the example source is part of the binary bundle.
