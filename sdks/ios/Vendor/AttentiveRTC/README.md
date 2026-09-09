# AttentiveRTC Internal Transport

A namespace-only source fork of the pinned LiveKit Swift client 2.16.0. It is
an implementation target of `AttentiveVoice`, not a public SwiftPM product or
a new streaming gateway. The separate native media binaries are not forked.

## Kept Intact

Capture/playback, echo processing, media codecs, packet handling, reconnects,
queues, room behavior, generated protobufs, native ABI symbols and signaling
parameters use the pinned implementation. SDK version reporting remains 2.16.0
for server compatibility. There is no extra network hop or audio buffer.
Error types/domains and protocol strings are intentionally not relabeled.

## Mechanical Changes

- Swift module/source directory: `LiveKit` -> `AttentiveRTC`.
- SDK utility type: `LiveKitSDK` -> `AttentiveRTCSDK` (including log queue labels).
- Objective-C helper module/class: `LKObjCHelpers` -> `AttentiveObjCHelpers`.
- Generated Swift FFI wrapper module: `LiveKitUniFFI` -> `AttentiveMediaBindings`.
  Its generated source and native ABI are unchanged.
- Main SDK filenames use `AttentiveRTC`; modified source files carry a notice.
- Upstream DocC pages and upstream tests/examples are not imported. The complete
  runtime sources are retained, including capabilities unused by our audio UI.
- The root package supplies exact binary targets and the pinned Apple protobuf
  dependency instead of fetching the upstream Swift source packages.
- Transport and bindings retain their upstream Swift 6 mode; the customer
  wrapper and optional UI retain their existing Swift 5 mode.
- Source licenses/notices are also copied to the transport resource bundle.

`upstream.json` records source commits, native URLs and archive checksums.
`inventory.json` records each imported file's original and transformed SHA-256.
Copyright headers, Apache LICENSE, NOTICE, broadcast attribution and dependency
licenses are preserved. Native artifacts must retain their bundled licenses too.

## Reproduce / Check

Use a directory containing Git clones named `client-sdk-swift`,
`livekit-uniffi-xcframework`, `webrtc-xcframework`, and `swift-protobuf`, with the
commits in `upstream.json` available. SwiftPM caches may contain these from the
pre-fork build, but a fresh post-fork build will not fetch the first three.
The importer reads committed Git objects, not working-tree contents. Run a Swift
build first to populate the native artifact cache. The WebRTC binary's license is
read from that checksum-verified artifact and checked against its recorded hash.
If using a different cache location, pass the binary's LICENSE path as a third
argument after the checkout directory.

From the SDK directory:

```sh
node script/vendor_transport.mjs --check /path/to/pinned-checkouts
node script/verify_transport.mjs
swift test
```

`--write` imports missing files and refuses to overwrite a changed source file.
An upstream update requires reviewing pin changes and mechanical import rules in
a separate worktree. Do not edit generated protobufs or native binaries to make
names disappear. Keep an auditable fork diff and retest every update.

## Distribution Limit

This is a source distribution for local SDK development. Native binary targets
still use `LiveKitWebRTC` and `RustLiveKitUniFFI`, including artifact URLs and
internal symbols. Upstream names remain in legal notices and compatibility code.
Customers must only integrate the supported `AttentiveVoice` and
`AttentiveVoiceUI` interfaces, not import this target or its dependencies.

A customer-facing compiled XCFramework release is separate follow-up work.
It can conceal implementation source in ordinary Xcode browsing, but not make
upstream provenance undiscoverable. Rebuilding/rebranding the native libraries
requires its own compatibility, signing and license audit. This change does not
do that, publish a release, or claim latency or device acceptance.
