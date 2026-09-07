# Attentive iOS Transport Fork

## Checkpoint and Scope

Approved on September 7, 2026, in the SDK side conversation. Work stays on
`attentive-ios-sdk`, separate from `speculative-integration`.

Pre-fork implementation: `d7b639c37078bd1620567014ce66611f64be4ee7`.
Baseline verification: `swift test --package-path sdks/ios` passed 39 tests.
Unrelated local ASR edits are not part of this checkpoint or SDK work.

The approved approach is a maintained, Attentive-branded source fork of the
pinned Swift client. It is not a new streaming gateway or a new audio engine.
Keep `AttentiveVoice` and optional `AttentiveVoiceUI` as the customer API.

| Component | Version | Source revision |
| --- | --- | --- |
| Swift client | 2.16.0 | `79fb2beee98e45556bffebefa50b5d05c3382af1` |
| Native WebRTC package | 144.7559.11 | `46f2af86f06b9a8a9158d37cadda5cb5a214e4c4` |
| Native FFI package | 0.0.6 | `7c161254ce7cd55debc48023f69a917076b12a26` |
| SwiftProtobuf | 1.38.1 | `55d7a1cc5666b85c13464aea1c4b4a90feccb4c8` |

## Implementation Boundaries

- Vendor source as `AttentiveRTC`; remove the upstream Swift client package
  dependency and use an Attentive-named internal call adapter.
- Preserve capture, playback, echo processing, queues, reconnects and signaling.
  Keep generated protocol messages, wire identifiers and native ABI unchanged.
- Preserve licenses, copyright and attribution; mark modified upstream files.
  Record exact source revisions and a reproducible mechanical import procedure.
- Native precompiled media artifacts retain their original identifiers and
  checksums. A source fork is not a promise that provenance is undiscoverable.
- Binary distribution can hide implementation source from ordinary Xcode use;
  it cannot remove required notices or guarantee concealment under inspection.
  Do not publish repositories or change native binaries during this step.
- Do not change prompts, ASR/LLM/TTS selection, authentication, tools, UI,
  backend routes, services, servers or the transport's network topology.

## Verification and Rollback

Compare the fork byte-for-byte against the pinned source after the recorded
namespace substitutions. Run the existing Swift suite, add packaging-boundary
checks and build the sample for iOS. Unit/build checks are not a claim of live
banking, real microphone or latency acceptance.

Rollback should revert only the SDK fork change commit(s), not reset the shared
checkout. The checkpoint above retains the previous SDK and dependency pins.
Resolve packages and rebuild the sample after reverting. No server rollback is
needed because this work must not touch server state.

The streaming gateway, gRPC, Android SDK, publishing and physical-device
hardening remain on the [roadmap](ROADMAP.md).
