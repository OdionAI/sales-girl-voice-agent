# Distributing the Attentive iOS SDK

## Clean Package Update

Version `0.1.0-staging.5` is the public runtime-only package. See its
[release notes](https://github.com/OdionAI/attentive-ios-sdk/releases/tag/0.1.0-staging.5).
It contains `Package.swift`, `.gitattributes`, `ThirdPartyNotices`, and four
XCFrameworks: AttentiveVoice, AttentiveVoiceUI, AttentiveMedia, AttentiveBindings.
There are no Markdown files, sample apps or internal agent instructions.
Build metadata and checksums are generated outside the package; all guides remain
in the internal source repository. Native names are private packaging names,
not a claim that upstream symbols or required attribution are hidden.
This is a packaging-only prerelease, not a new runtime build or backend rollout.
Only the distribution repository has a new parentless history. Maintainers must
replace old distribution clones, not merge the old history back. Update pinned
dependencies and regenerate `Package.resolved` through Xcode. Previously fetched
copies and GitHub caches cannot be recalled by rewriting repository refs.

## Current Staging Release

- Published version: `0.1.0-staging.5`.
- Package commit: `d5d8b99c6d96987e9fb62deae28773b2e58bdd1a`.
- Package tree: `5b8314497fa57b204c8c371c1328233db545d215`.
- Build: `node script/build_binary_release.mjs` from `sdks/ios`.
- Output: `.build/binary-release/package`, a standalone root Swift package.
- Verify documented consumers:
  `node script/check_documentation.mjs .build/binary-release/package`.
- Verify remote installation:
  `node script/check_documentation.mjs https://github.com/OdionAI/attentive-ios-sdk.git 0.1.0-staging.5`.
  Also verify with credentials disabled and fresh consumer/build directories.
- Both core-only and optional-UI documentation consumers compile against the
  binaries, without access to implementation source.
- Artifacts: separate `AttentiveVoice` and `AttentiveVoiceUI` XCFrameworks plus
  pinned native frameworks, resources, privacy manifests and required notices.
- Distribution uses path-based binary targets carried in the public Git repository.
  No GitHub credentials are required to install it.
- Builds use Xcode 26.6 / Swift 6.3.3, iPhone arm64 and Simulator arm64/x86_64.
  Core requires iOS 16, optional UI iOS 17. Older compiler compatibility and
  App Store readiness are not claimed. Frameworks are ad-hoc signed staging
  artifacts; app signing remains the host application's responsibility.
- API-key calling encapsulates the endpoint. The advanced endpoint-based API
  remains supported. Publication does not enable backend routes or prove a live call.

The build script archives core first, then compiles UI against that binary core,
so UI does not embed a second copy of the call engine. Only public Swift
interfaces are distributed. Resource bundles are materialized inside their
frameworks, and native relative symlinks are preserved. A file-size, source-leak
and external-symlink audit runs before writing `SHA256SUMS` and `BUILD_INFO.json`.
The `--ui-only` option reuses an existing locally built core/native artifact set;
use a full build for a fresh release.

The public distribution currently contains `dev` and the new release tag only.
Do not move the published tag or overwrite its artifacts. Before a new release,
update the version in the build metadata and release README, rebuild, verify,
then create a new tag. Keep implementation repositories private. Never publish
internal build metadata, deployment credentials or customer data in this repository.

## Publish a Library, Deploy a Service

The SDK is not a server that customers run. It is a library built into their
iOS app. We publish a versioned package; Xcode downloads it at build time, and
the customer ships their app normally. Updating the SDK requires a customer
dependency update, rebuild and app release, not a runtime SDK download.

Our call API, realtime infrastructure, workers, models and tools are separate
running services. They must remain available at reachable production addresses.
Publishing a package does not deploy those services or make a Mac's localhost
accessible to customers. Customers continue configuring agents in our dashboard.

| Deliverable | What the customer receives | Current status |
| --- | --- | --- |
| SDK | `AttentiveVoice`, optional `AttentiveVoiceUI` | Local source package works; user confirmed the updated physical-iPhone sample works |
| Sample | Small app demonstrating public library consumption | Exists; developer LAN configuration is not production configuration |
| Documentation | Human quickstart, reference, coding-agent prompt and version notes | Separate documentation site at https://docs.odion.ai |
| Binary package | Compiled implementation installed through SwiftPM | Public prerelease; validate clean remote builds and release checksums |
| Customer API deployment | Secure call bootstrap and reachable realtime service | Current POC contract exists; production hardening remains a release gate |

## Recommended Customer Delivery

For the requirement not to distribute implementation source, publish a
**binary Swift Package backed by XCFrameworks**, not a source checkout of the
voice-agent repository. Apple's supported packaging flow combines compiled
platform variants and distributes them through SwiftPM.
[Binary packages](https://developer.apple.com/documentation/xcode/distributing-binary-frameworks-as-swift-packages),
[XCFramework creation](https://developer.apple.com/documentation/xcode/creating-a-multi-platform-binary-framework-bundle).

The public staging package uses this layout:

```text
Dedicated SDK distribution repository
  Package.swift                 # At repository root; exposes the two public products
  .gitattributes
  ThirdPartyNotices/
  Frameworks/
    AttentiveVoice.xcframework
    AttentiveVoiceUI.xcframework
    <required native dependency frameworks>
```

The implementation has Swift, Objective-C, WebRTC and Rust/native dependencies;
the binary package includes its required runtime frameworks and resource bundles.
Simply zipping the sample `.app` or our `Sources` directory is not an SDK release.

Only customer-facing code should use the two public modules. Compiling the
implementation keeps source out of Xcode's source tree, but does not guarantee
undetectable upstream technology. Required attribution, native symbols and
protocol behavior remain discoverable. Preserve license/NOTICE files and privacy
manifests. The internal build downloads pinned native artifacts from upstream;
the generated customer package includes those binaries without upstream downloads.

## Preview Installation Today

Customers can install the exact public version below without GitHub sign-in.
An approved preview consumer can also receive the generated binary-package folder
and add it as a **local package**. It contains
compiled implementation, public interfaces and notices, not implementation source.
The original internal source preview remains an alternative for maintainers.
Do not hand out the whole voice-agent repository just to install an SDK.

The internal source preview must preserve `Package.swift`, `Package.resolved`, `Sources`,
`Vendor`, notices and integration docs. It still needs network access to resolve
its pinned dependencies; it is not an offline distribution. Exclude `.build`,
signing profiles, `.env`, private identities, recordings and internal server data.

## Release Process

### 1. Choose the Distribution Locations

Approve a dedicated repository under our organization with `Package.swift` at
its root. Separately approve HTTPS artifact storage and access policy. Neither
a Docker registry nor an App Store SDK listing is needed for SwiftPM delivery.

A private Git repository protects its manifest/docs, not a public artifact URL
referenced by it. Apple's URL-based binary-package flow expects a reachable
archive. Do not assume signing into GitHub in Xcode authenticates arbitrary
binary downloads. For restricted artifacts, validate a supported authenticated
download setup on a clean customer machine/CI, or deliver the XCFrameworks in an
access-controlled package using path-based binary targets. Avoid expiring URLs
in immutable release manifests. Never put storage credentials in `Package.swift`.
[Apple binary-package delivery](https://developer.apple.com/documentation/xcode/distributing-binary-frameworks-as-swift-packages).

### 2. Build the Release Artifacts

Pin the SDK source commit, toolchain and all native dependencies. Create real
framework/archive targets for distribution without changing the call engine.
The release script now performs this packaging; the ordinary sample Debug build
is not a substitute for it.

Build Release archives for physical iOS and iOS Simulator; choose and document
the architectures actually supported. Use `BUILD_LIBRARY_FOR_DISTRIBUTION=YES`
for Swift library evolution and `SKIP_INSTALL=NO` for the framework archive,
then combine variants with `xcodebuild -create-xcframework`. Keep device and
Simulator variants separate. Validate signing for the resulting SDK artifacts.
[Apple framework archive instructions](https://developer.apple.com/documentation/xcode/creating-a-multi-platform-binary-framework-bundle).

Audit the generated `.swiftinterface` files: no unsupported internal transport
types should leak into the customer's public API. Confirm required modules are
available to the compiler without exposing implementation source, with no
duplicate native symbols. Include avatar assets, privacy manifests and notices;
do not assume SwiftPM's development resource-bundle paths will survive binary
packaging unchanged. Keep debugging symbols privately for support as appropriate.

### 3. Produce the SwiftPM Manifest

The current release uses path-based binary targets within the public Git
repository, pinned by the version tag. Separate release asset `RELEASE_SHA256SUMS` is an additional audit record;
SwiftPM does not automatically check that file. Preserve framework symlinks and
`.gitattributes` so Git does not rewrite packaged bytes.

For a future URL-based distribution, publish archives at immutable versioned
URLs and compute each archive checksum:

```sh
swift package compute-checksum /path/to/AttentiveVoice.xcframework.zip
```

That URL-based manifest uses binary targets whose names match the compiled modules,
URLs and checksums. It must expose the two public products and link all required
dependencies/resources. Core must remain usable without the optional UI. Test
the complete graph before publishing; a two-line binary-target declaration
alone would not prove this package works. SwiftPM verifies downloaded archives
against the declared checksums.
[Apple checksum and manifest guidance](https://developer.apple.com/documentation/xcode/distributing-binary-frameworks-as-swift-packages).

### 4. Verify as a New Customer

Use a clean consumer outside the development checkout with no local-package
overrides or pre-existing build caches. Install the exact candidate artifacts.
Build one app with core only and another with both products. Build, archive,
sign and run on a real iPhone and supported Simulator variants. Check the
published source/interface visibility, package assets and notices.

Run the [customer acceptance checks](GETTING_STARTED.md#5-verify-before-shipping).
Specifically retain voice-auth enforcement, event routing and the same backend
agent identity. Benchmark audio/interruption behavior against the source build;
do not infer parity solely from compilation. Test downloads on customer CI,
including artifact access controls if used.

### 5. Publish a Version

After approval, tag the distribution manifest with a semantic version and
publish matching artifacts, release notes and versioned documentation. SwiftPM
uses semantic-version Git tags to resolve dependency versions.
[SwiftPM version selection](https://docs.swift.org/swiftpm/documentation/packagemanagerdocs/addingdependencies/).

Keep the source commit and internal build records privately. Publish artifact hashes, supported OS/toolchain versions,
backend compatibility and known limitations. Breaking public API changes need
a documented migration. Do not overwrite artifacts under an existing version.

### 6. Customer Installation After Publication

1. Give the customer the public SDK repository URL, tested version, API key,
   agent ID and signed-caller integration contract for customer-specific tools.
2. They use Xcode's **Add Package Dependencies**, enter that repository URL,
   choose the agreed version and link core plus optional UI.
3. They follow [GETTING_STARTED.md](GETTING_STARTED.md) inside their existing app.
   No separate LiveKit package setup, model credentials or server work is part
   of the normal SDK integration.
4. They run staging acceptance tests, then release their own signed app.

For package-manifest consumers:

```swift
// In the customer's Package.swift dependencies:
.package(url: "https://github.com/OdionAI/attentive-ios-sdk.git", exact: "0.1.0-staging.5")
// In its target dependencies, use the published package identity:
.product(name: "AttentiveVoice", package: "attentive-ios-sdk")
.product(name: "AttentiveVoiceUI", package: "attentive-ios-sdk") // Optional
```

### 7. Updates and Rollback

Customers commit their resolved dependency version, read migration notes and
test before upgrading. Keep older artifacts available so an app can rebuild
against its previous known-good version, starting with the clean distribution baseline.
A released mobile app needs an app
update to replace its embedded SDK; changing a package tag cannot patch an
already installed app. Backend rollouts and rollbacks are independent and must
honor the advertised client contract.

## Backend Gate Is Separate

Do not expose the current public POC bootstrap as a production banking API just
because the SDK is packaged. Before external production use, complete tenant,
agent and caller-session binding, profile ownership validation, protected
enrollment, scoped short-lived credentials, rate limits and payload limits.
Retain server-side live voice checks and transaction confirmation. Audit the
existing experimental auth settings; SDK packaging must not silently carry a
test threshold into a production policy.

The public `CallCredentialProvider` extension point allows an app-specific trusted
credential flow without changing audio controls. Its production HTTP contract
and enrollment equivalent still need an agreed deployment implementation. SDK
access control is not runtime call authorization. See the [API security notes](../../docs/attentive/call-api.md#deployment-and-security-boundary).

## Remaining Release Gates

- Physical-phone live-call test of the published binaries against local staging.
- Installation on a separate customer's machine/CI with that customer's access.
- Production identity/enrollment, publisher signing and App Store requirements
  before moving beyond the staging preview.

Next step: switch a test app to the exact remote package and verify calls on the
physical iPhone. This does not require replacing transport or building the planned
gateway/gRPC/Android SDK first.
