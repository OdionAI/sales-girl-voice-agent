# Native worker and playout layers — review checkpoint

**Historical checkpoint:** the subsequent native RVC core/provider implementation and real-provider comparison are recorded in [RVC_REVIEW.md](RVC_REVIEW.md). Deferred core/provider entries below describe the earlier checkpoint, not the current one-call slice.

Scope: resolve the two missing supporting layers in the isolated Rust experiment, using official LiveKit SDK/protocol components, with local verification and no Python bridge. Baseline remains PR #139 (`d3f90112d00c38868748b3101ae156e70d299313`). This is not authorization to replace or deploy the production RVC runtime.

## What retains the RVC design

`ConversationPipeline` and the existing RVC worker are unchanged. The native layers provide call-lifecycle and audio-source boundaries for a later port of that coordinator. They do not replace speculative generation with a sequential pipeline. The gate still requires explicit final authorization before release, rejects stale attempt IDs, preserves ordered audio, and keeps input independent from speech gating. A combined gate-to-playout test checks those invariants across both Rust modules.

ASR/LLM/TTS provider streaming, the complete turn coordinator, prompts, languages/voices, business tools, identity/authentication, billing, persistence, recording and SIP remain deferred. The local integration handler accepts only a fixed synthetic metadata scope and uniquely named test rooms; it cannot run a real SalesGirl business call. Production dispatch names and non-loopback worker URLs are unavailable in this harness. Unsupported resume/recording/other job scopes are declined rather than approximated.

## 1. Worker call assignment and lifecycle

`src/worker.rs` implements the missing worker-control glue using:

- Official `livekit-protocol = 0.8.2` generated WorkerMessage/ServerMessage types and prost 0.14.4 encoding.
- Official `livekit-api = 0.10.0` token construction and server APIs. Synthetic dispatch creation/deletion uses its AgentDispatchClient.
- A standard tokio-tungstenite WebSocket client for `/agent`; media connections remain in official `livekit = 0.10.0`.

Implemented: worker registration and agent identity; bounded call capacity with slots reserved before assignment; assignment timeouts; exact offered/assigned job-scope correlation; rejection of unreserved/duplicate/expired/changed assignments; running and final job status; heartbeats and bounded sends; control reconnect with a fresh admission epoch; job cancellation/termination; graceful draining and active-job shutdown. Jobs receive their original metadata, assigned token, room and participant identity. Tokens are not included in debug events or error messages.

A bounded history of 4,096 accepted job IDs survives control reconnects to prevent repeated execution. Once exhausted, the worker advertises full and needs a fresh worker lifetime. Reservations count toward capacity. The local prototype allows 1–16 jobs and defaults to one; cancellation cleanup has a two-second deadline. Resume/migration of active jobs is unsupported and declined. Control loss cancels and cleans up jobs before re-registering; it does not keep serving an orphaned call. These are explicit prototype operational policies, not a claim of parity with every Python Agents feature.

This is our native compatibility layer built against the published protocol, not an upstream Rust Agents framework. It still needs wider server/version, soak and crash-recovery testing before production consideration.

## 2. Attempt-scoped local audio queue completion

`src/playout.rs` wraps the official NativeAudioSource through a small AudioSink adapter. It owns ordered source writes and monotonic queue-duration accounting, exposing explicit begin, authorize, push, finish, cancel, speech-decision and shutdown operations.

- An unauthorized attempt cannot submit PCM. A stale attempt cannot publish audio or complete a newer attempt.
- PCM uses a bounded FIFO with backpressure; payload size is checked before enqueue. Valid speech chunks are never coalesced as obsolete rendering frames.
- A control channel has priority over PCM. Cancellation clears the source and invalidates outstanding completion waiters.
- Native capture is split into pieces of at most 10 ms. The SDK's clear_buffer releases a blocked callback on a later audio tick. The adapter waits for that single callback before reuse, preventing a multi-piece SDK future from enqueueing later stale chunks. A callback that cannot clean up within 100 ms closes the adapter; it does not permit unsafe reuse.
- End-of-generation waits for the estimated local source queue deadline. It is separate from provider completion. The final partial 10 ms frame is completed with zero padding only, preserving every supplied sample and adding less than 10 ms of trailing silence. Queue timing includes that padding.
- Listening-only is an authorized typed speech decision; this component has no input/ASR stop operation and performs no phrase matching.

`LocalQueueDrained` deliberately means the local queue timing boundary used by Python AudioSource.wait_for_playout. It is not confirmation from the caller's loudspeaker. Remote audible completion and audible interruption latency require receiver observation and remain unmeasured. The local smoke independently checks nonzero PCM arriving at a second participant.

## Local verification and artifacts

Reproduction: `./scripts/verify-local.sh` from `rust/`. It owns a server bound to loopback only, on TCP 17880/17881 and UDP 17882, and stops only that process. It refuses occupied ports. Dependencies/builds are pinned in Cargo.lock. Tests use LiveKit server 1.13.6 and public development credentials; no production environment or provider endpoint is loaded.

The official native WebRTC release (`webrtc-a545de4`, mac-arm64-release) is 277,130,371 bytes. SHA-256 `153a1f1a268b812b4a501deda6505037f29481691efec52be817f560ff09e206` matches GitHub's release asset digest. See `artifacts/native-asset.json` and `native-asset-verification.json`. SDK transport/codec source was not forked or rewritten.

Validation completed successfully:

| Check | Result / evidence |
|---|---|
| cargo fmt --check | Passed; artifacts/native-fmt-final.log |
| cargo test --locked --all-features | 19 passed; artifacts/native-tests-final.log |
| cargo clippy --locked --all-features --all-targets -- -D warnings | Passed; artifacts/native-clippy-final.log |
| All native binaries build | Passed; artifacts/native-build-final.log |
| Official local server registration, assignment, termination, second job and active-job shutdown | Passed; artifacts/control-smoke-final.log |
| Assigned native RTC room join, caller microphone subscription, output PCM reception, reliable bidirectional data events | Passed; artifacts/native-smoke-final.log |
| No speculative output before authorization, stale-output rejection after cancellation, input continues while speech is disabled | Passed in core/adapter tests and native media smoke |
| Packaged local runner and owned-server cleanup | Exit 0; artifacts/verify-local-final.log; test ports released |

The native media smoke received 73,760 nonzero microphone samples and 9,600 nonzero output samples at the receiver. It observed one reliable caller data event plus its worker reply. Its one diagnostic sample recorded a 113.317 ms wait at local finish and a 0.268 ms local cancellation acknowledgement. These are functional diagnostics, not a latency benchmark or remote audible-stop measurement. Raw JSON is retained; missing audible completion remains null. No real provider or full conversational call was tested.

The machine/source/environment records and earlier Python baseline results remain available from the first checkpoint. The earlier incomplete native-build logs are historical evidence and do not override this follow-up. Machine-readable final evidence is in artifacts/native-verification-summary.json.

## Stop boundary

No production access, account edits, real calls, shared-model probes, service restarts, pushes, merges or deployments. Stop for review after local verification. Resolving these two layers does not complete the deferred conversation/provider port or establish a Python-versus-Rust service performance comparison.
