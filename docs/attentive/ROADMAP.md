# Attentive Integration Roadmap

Updated: 2026-09-08. SDK resume branch: `attentive-ios-sdk`.
Checkpoint before optional UI extraction: `7db19c6`.
The checkpoint includes the existing caller/enrollment implementation, tests,
runtime verification notes and architecture plan. It is not a claim that the
known intermittent STT/DNS or TTS issues are fixed.

## Current Delivery

| Work | Status | Completion evidence |
| --- | --- | --- |
| Checkpoint the current implementation | Complete | Commit `7db19c6`; 30 baseline Swift tests passed |
| Separate headless `AttentiveVoice` and optional `AttentiveVoiceUI` | Complete locally | Separate SwiftPM products, unchanged core/transport sources; 38 Swift tests passed |
| Make sample consume the public UI product | Complete locally | No copied caller panels, theme or avatar in the app target; live chat call UI test passed |
| Configure branding and retain enrollment/auth/tool UI | Complete locally | Four iOS UI tests passed, covering Wema/generic layout, enrollment cancellation and live call controls; live voice checks still require manual testing |
| Human and coding-agent integration instructions | Published for iOS staging | Quickstart, UI guide and coding-agent instructions in `OdionAI/attentive-ios-sdk`; both documented consumers compile from exact remote tag `0.1.0-staging.1` |
| Attentive-branded Swift transport fork | Implemented locally | Pinned `AttentiveRTC` source, unchanged public products, reproducible namespace-only import and preserved notices; see `IOS_TRANSPORT_FORK.md` |

## Follow-Up Work

### SDK Side-Conversation Resume

The SDK work resumed independently from checkpoint
`d7b639c37078bd1620567014ce66611f64be4ee7` on `attentive-ios-sdk`. Pre-fork scope
and rollback were committed as `dd128c6`. The RVC integration remains in its own
worktree; this SDK change does not merge it or change backend services.

The approved local fork keeps the existing WebRTC implementation as internal
`AttentiveRTC`. No new audio engine or gateway hop is introduced. Customer APIs
remain `AttentiveVoice` and optional `AttentiveVoiceUI`. Native artifact names and
required credits remain visible. The staging XCFramework distribution hides
implementation source; public interfaces, dependencies, resources and notices
were audited. Signing and physical binary acceptance remain release gates.
Do not claim all upstream traces can be removed. Native media rebuild/rebranding
remains separate, uncompleted work.

The fork implementation is checkpointed at `38efc97`. Its signed physical-iPhone
sample was installed and launched on September 7; the user reported that it
works. This is a manual call smoke confirmation, not full mobile release
acceptance. The customer quickstart now explains how an existing app consumes
core or optional UI, and `sdks/ios/DISTRIBUTION.md` records the binary-build,
package-hosting, customer-install and rollback process. No production API or
gateway was deployed as part of these documentation and packaging steps.

The user then approved the private `OdionAI/attentive-ios-sdk` distribution
repository. Prerelease `0.1.0-staging.1` now delivers compiled core and optional UI
XCFrameworks with native dependencies, public interfaces, resources, notices and
file checksums. Core-only and optional-UI documentation examples compile against
the binaries. On September 8 (WAT), repository access was resolved and the private
[prerelease](https://github.com/OdionAI/attentive-ios-sdk/releases/tag/0.1.0-staging.1)
was published at package commit `6c9d1131a7754293666ea986f1f8cf180846ec49`.
Fresh remote core/UI consumers compiled and downloaded checksums matched.
Physical-device binary acceptance remains pending. No backend or phone app was
changed for packaging. Keep the release tag immutable; future updates follow
`dev` -> PR -> `staging` in the standalone distribution repository.

### SDK Pause for Speculative Harness Evaluation

The user paused SDK development to evaluate the engineer's
`speculative-generation` branch, reference commit `8a29091`, in an isolated
worktree. The current SDK, optional UI, physical-device setup, Wema routing
fix and test coverage are checkpointed on `attentive-ios-sdk` before that work.
Keep the existing SDK checkout and running services separate from the lab.

Resume this roadmap after evaluating whether the lab's coordinator can retain
its measured latency over LiveKit transport without LiveKit owning application
turns. Do not replace the SDK transport or remove the roadmap deliverables below.
The frontend counterpart is `sales-girl-dashboard` branch `voice-lab-preemptive`
at `c665713`; preserve our existing caller controls while comparing harnesses.

Checkpoint verification on September 7: 183 Python tests passed with dotenv
disabled for tests; 39 Swift package tests passed. Full mobile banking/auth and
audio-quality acceptance is still incomplete, not implied by these unit tests.
See `WEMA_MOBILE_TOOL_RECOVERY.md` for the saved-prompt restoration and
`VOICE_AUTH_THRESHOLD_EXPERIMENT.md` for the local 0.40 -> 0.20 experiment and
rollback. Secrets, voiceprints, local databases, recordings and build products
remain private/ignored; the local threshold is documented, not a new production
default. The iOS staging package and initial integration guides are now published;
streaming gateway, gRPC, Android SDK and wider documentation remain planned work,
not cancelled.

### Delivery Queue

These are recorded deliverables, not a command to implement them all in the
current change. Implement and verify them separately, preserving existing calls.

| Order | Deliverable | Scope and release gate | Status |
| --- | --- | --- | --- |
| 1 | Distributable iOS package | Select/approve package repository; build/validate compiled distribution; publish a version; validate remote SwiftPM installation, notices, tested snippets and an external consumer app | Private `0.1.0-staging.1` published; remote core/UI builds and checksums passed; physical binary call test pending |
| 2 | Production call API boundary | Versioned session API, tenant/agent/caller identity binding, scoped short-lived credentials, enrollment protection, rate/payload/session limits, sanitized events/errors; retain both voice checks and transaction confirmation | Planned |
| 3 | Attentive streaming gateway | API-only audio input/output without a customer-side LiveKit dependency; initially bridge to existing internal rooms/workers; preserve auth, tools, transcripts, interruption and recording association | Planned; design and latency benchmark first |
| 4 | gRPC audio API | Bidirectional native/server audio and events, protobuf contract, generated clients, deadlines, backpressure, cancellation, safe reconnect and examples; share gateway internals | Planned |
| 5 | Browser streaming API | WebSocket audio/event adapter, capture/playback example, queue flushing and reconnect behavior; official gRPC-Web does not provide bidirectional streaming | Planned |
| 6 | Web SDK and optional web UI | Headless Attentive controls/events plus reusable caller UI; continue supporting current public caller/dashboard routes | Planned |
| 7 | Android SDK and sample | Kotlin headless wrapper, optional native caller UI, enrollment, auth badges, tool activity, chat, lifecycle/audio routing/permission tests and integration examples | Planned |
| 8 | Mobile release hardening | Physical iPhone/Android testing, Bluetooth/speaker routing, interruptions, background policy and optional CallKit/platform calling integration; no untested background-call promises | Planned |
| 9 | Published developer documentation | Human quickstarts/reference/API collection, machine-readable contracts, coding-agent prompt, tested snippets, version compatibility, troubleshooting and migration guidance for every shipped platform | Initial iOS guides published with staging package; other platforms planned |
| 10 | Cross-transport regression suite | Compare direct WebRTC and gateway latency; test real microphone, auth failure/retry and both checks, safe tool calls, recording, network changes and repeated calls; no automatic financial-action replay | Planned |

`attentive-api` is the API workstream and `attentive-ios-sdk` is the iOS
workstream. Select a branch for Android when starting it; do not silently switch
or merge the active working tree. Do not change the current SDK's transport as
part of adding a gateway. The independent WebRTC-edge option remains an
alternative, not an additional committed deliverable.

## Boundaries

- API-only customers implement device capture/playback; SDK customers use our
  implementation. The caller UI remains optional in both product positioning
  and package dependencies.
- No customer must operate a LiveKit account. The source fork retains its
  attribution and protocol; the proposed gateway keeps that protocol server-side.
- Keep the existing dashboard, model choices, turn-taking parameters, voice-auth
  gates, tools and server startup configurations unchanged during extraction.
- Gateway latency and audio quality must be measured, not assumed better.
- Package/transport work does not fix the separate intermittent runtime issues.
- Infrastructure changes, repository publication and deployment need an explicit
  scope and rollback plan. No remote server changes are included here.

See [architecture and alternatives](integration-architecture.md),
[current API contract](call-api.md), and
[verification](../../sdks/ios/VERIFICATION.md).
