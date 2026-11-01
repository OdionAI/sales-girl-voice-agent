# SalesGirl Rust feasibility checkpoint

Stopped at native SDK capability gaps at the user's explicit request. This is an inventory, tested authorization-gate experiment and unverified SDK probe source. It is **not a runnable one-call voice service**, a full port, or an approved migration. No Python bridge was added.

- Worktree: `/Users/mavinomichael/PycharmProjects/Odion/sales-girl-voice-rust-prototype`
- Branch: `codex/sg-voice-rust-prototype`
- Source baseline: `d3f90112d00c38868748b3101ae156e70d299313`, latest merged PR #139. User accepted GitHub source instead of live production verification.
- Isolated Python source: `/private/tmp/sg-voice-python-reference-d3f90112`
- Changes exist only under `rust/`; production and shared checkout source are untouched.

Read [INVENTORY.md](INVENTORY.md) for source anchors, hashes, the preservation map, and implemented/deferred matrix.

## Runnable core checks

```sh
cd /Users/mavinomichael/PycharmProjects/Odion/sales-girl-voice-rust-prototype/rust
cargo fmt --check
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
cargo run --locked --release --bin gate_replay
```

Five Rust invariant tests pass. They cover authorization before speculative release, stale/cancelled callbacks, ordered chunks and separate generation/playout completion, listening-only input retention, bounded capacity and unsupported tools. The gate does not implement ASR, transcript matching, end-of-turn detection, model decisions, an RTC playback queue or business behavior. Capacity exhaustion cancels the entire experimental attempt with an explicit error; chunks are never silently coalesced or dropped. This difference needs policy review before integration.

## SDK findings and stop boundary

Official LiveKit crates are pinned to `livekit = 0.10.0` and `livekit-api = 0.10.0`, with Cargo.lock retained. `src/bin/rtc_smoke.rs` is an **uncompiled, unexecuted draft** of a loopback-only two-participant SDK probe. It must be compiled/fixed/tested before use. The official build requires a 277,130,371-byte WebRTC archive; its download was stopped at the requested review boundary. A local LiveKit server was not started. Room connection, PCM, data events and cancellation therefore remain unproven in Rust here.

Two native compatibility gaps block the requested equivalent one-call worker:

1. RTC room membership/server APIs do not implement Python livekit-agents WorkerOptions/JobContext job registration, admission, dispatch and shutdown.
2. libwebrtc 0.3.55 NativeAudioSource exposes capture_frame/clear_buffer but no Python AudioSource.wait_for_playout equivalent. Capture completion cannot be called remote audible completion. The reference also tracks attempt-scoped playback acknowledgements.

The user chose to stop at these gaps rather than retain a Python bridge. No replacement transport, handcrafted codec, sequential pipeline, hardcoded speech phrases, or business/authentication bypass was introduced. All business tools in the core fail closed as Unsupported. No production dispatch name or credentials are accepted by the draft probe.

A future **local-only** probe, after compilation/validation, would use `livekit-server --dev --bind 127.0.0.1 --config-body 'port: 17880'` and `cargo run --locked --features rtc --bin rtc-smoke`. These are prospective commands, not verified results.

## Validation evidence

- `artifacts/fmt-check-final.log`: fmt check passed.
- `artifacts/cargo-test-final.log`: 5 tests passed, replay binary/doc tests passed.
- `artifacts/clippy-final.log`: default-feature/all-targets clippy passed with warnings denied. RTC feature is excluded and unverified.
- `artifacts/python-first-failure.log`: despite the historical filename, test_lab completed: 48 tests, 12 pre-existing skips, no failures (36 executed).
- `artifacts/python-focused-complete.log`: 3 focused tests passed (post-final partial ownership, short-noise interruption protection, odd PCM byte carry).
- `artifacts/python-engagement-first-failure.log`: the unchanged baseline's engagement suite fails on its first error/failure; no test altered. Broader combined attempts were terminated when a speculative-tool fixture remained pending. Their incomplete logs are retained.
- `artifacts/python-transport.log`: blocked at import by missing webauthn in the existing test environment. No claim of transport parity.
- `artifacts/python-test-environment.txt`: actual dependency versions used. Existing environment was read-only; an attempted separate environment download was stopped. Production parity of the full dependency set was not established.

## Measurements and limits

`artifacts/rust-replay.csv` retains 31 raw samples of 10,000 synthetic gate turns each; first sample is warmup, 30 measured. Same in-memory workload each turn: input counter, 4 held chunks of 240 samples, no release before authority, authorize, ordered drain and generation/playout completion. Release build, no network/model calls, no child processes inside the benchmark. This measures only the independent Rust gate, not the SalesGirl worker.

Warm median: **354.2 ns/synthetic turn**, range **332.8–658.1 ns**. `/usr/bin/time -l` reports peak process RSS **1,982,464 bytes**; raw CPU/resource data in `artifacts/rust-replay-resource.txt`, machine identity in `artifacts/machine.txt`. These are process-wide microbenchmark resources, not steady/idle/per-call service measurements. Binary startup cache state was uncontrolled; this is not a cold-start benchmark.

All ASR-final, LLM-first-token, TTS-first-PCM, audible TTFA, interruption-to-stop, underflow/gap, per-call/idle CPU, steady RSS and native/child-process service measurements are **unmeasured**, represented by null in `artifacts/measurement-summary.json`. No Python-versus-Rust service comparison, model inference improvement or performance speedup is claimed. Provider settings/prompts were not changed or probed; input recordings and matched provider test configuration were not established before the stop boundary.

## Resume prerequisites

Resolve worker dispatch/lifecycle and attempt-scoped playout completion using official SDK APIs, or separately authorize a measured hybrid bridge. Then implement/verify the PCM→existing-ASR→existing-LLM→existing-TTS→PCM slice with the original speculative/commit/cancellation coordinator contract. Run the full preserved Python and Rust parity suites, a unique local worker/room, and serial matched-provider trials before drawing performance conclusions. Deferred business features remain unsupported.
