# Minimal supplied-UI app (unreleased API)

`AttentiveExampleApp.swift` is the entire customer app entry point. It deliberately
has no sample model, server URL, business slug, test profile or developer flags.
Replace the two placeholders only after the key API is deployed and a compatible
SDK version is installed. Add `NSMicrophoneUsageDescription` to your app target.

Maintainers can build this source version without altering the existing binary
sample or app installed on a phone:

```sh
node sdks/ios/script/create_quickstart_project.mjs
```

Open the generated `sdks/ios/.build/quickstart/AttentiveQuickStart.xcodeproj` and
run scheme `AttentiveQuickStart` on a simulator. This uses a local source package,
has a distinct bundle ID, and intentionally ships no real calling key. Until
configured it shows the invalid-key message; that is not a live-call test.

See [the integration guide](../../ACCOUNT_KEYS.md) for headless use and authenticated
callers. Neither this README nor the example source is part of the binary bundle.
