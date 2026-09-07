# Attentive Integration Roadmap

Updated: 2026-09-07. SDK resume branch: `attentive-ios-sdk`.
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
| Human and coding-agent integration instructions | Complete locally | `CALLER_UI.md` and `INTEGRATION_AGENT.md`; local-package instructions and explicit release limitations |

## Follow-Up Work

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
default. The SDK package release, streaming gateway, gRPC, Android SDK and human
and coding-agent documentation remain planned work, not cancelled.

### Delivery Queue

These are recorded deliverables, not a command to implement them all in the
current change. Implement and verify them separately, preserving existing calls.

| Order | Deliverable | Scope and release gate | Status |
| --- | --- | --- | --- |
| 1 | Distributable iOS package | Select/approve package repository, publish a version, validate remote SwiftPM installation, pin dependencies, preserve notices, test documentation snippets and an external consumer app | Planned |
| 2 | Production call API boundary | Versioned session API, tenant/agent/caller identity binding, scoped short-lived credentials, enrollment protection, rate/payload/session limits, sanitized events/errors; retain both voice checks and transaction confirmation | Planned |
| 3 | Attentive streaming gateway | API-only audio input/output without a customer-side LiveKit dependency; initially bridge to existing internal rooms/workers; preserve auth, tools, transcripts, interruption and recording association | Planned; design and latency benchmark first |
| 4 | gRPC audio API | Bidirectional native/server audio and events, protobuf contract, generated clients, deadlines, backpressure, cancellation, safe reconnect and examples; share gateway internals | Planned |
| 5 | Browser streaming API | WebSocket audio/event adapter, capture/playback example, queue flushing and reconnect behavior; official gRPC-Web does not provide bidirectional streaming | Planned |
| 6 | Web SDK and optional web UI | Headless Attentive controls/events plus reusable caller UI; continue supporting current public caller/dashboard routes | Planned |
| 7 | Android SDK and sample | Kotlin headless wrapper, optional native caller UI, enrollment, auth badges, tool activity, chat, lifecycle/audio routing/permission tests and integration examples | Planned |
| 8 | Mobile release hardening | Physical iPhone/Android testing, Bluetooth/speaker routing, interruptions, background policy and optional CallKit/platform calling integration; no untested background-call promises | Planned |
| 9 | Published developer documentation | Human quickstarts/reference/API collection, machine-readable contracts, coding-agent prompt, tested snippets, version compatibility, troubleshooting and migration guidance for every shipped platform | Initial iOS guides complete locally; publishing planned |
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
- No customer must operate a LiveKit account. A wrapper may contain LiveKit as a
  disclosed dependency; the proposed gateway keeps its protocol server-side.
- Keep the existing dashboard, model choices, turn-taking parameters, voice-auth
  gates, tools and server startup configurations unchanged during extraction.
- Gateway latency and audio quality must be measured, not assumed better.
- Package/transport work does not fix the separate intermittent runtime issues.
- Infrastructure changes, repository publication and deployment need an explicit
  scope and rollback plan. No remote server changes are included here.

See [architecture and alternatives](integration-architecture.md),
[current API contract](call-api.md), and
[verification](../../sdks/ios/VERIFICATION.md).
