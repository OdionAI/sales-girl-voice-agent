# Native Rust RVC one-call prototype

The local worker now runs RVC orchestration in Rust: microphone/VAD ownership, stable-partial speculative LLM/TTS generation, authoritative-final validation, held audio release, cancellation, interruption, the model-driven speech output gate, bounded conversation context, background memory compaction, idle response decisions, and ASR recovery. It uses the official LiveKit Rust RTC SDK, API client and generated worker protocol. Its runtime starts no Python process. Existing ASR, DeepSeek API and TTS services remain the model providers.

The latest local latency optimization and before/after measurements are in [LATENCY_REVIEW.md](LATENCY_REVIEW.md). The earlier implementation and real-provider comparison are retained in [RVC_REVIEW.md](RVC_REVIEW.md). That report defines the tested generic English one-call slice, its remaining production gaps, metrics and raw evidence. Production has not been changed. Nothing has been pushed, merged into a shared branch, or deployed.

- Current optimization branch: `codex/sg-rvc-latency-local`; base prototype checkpoint: `4f1fe35`.
- Behavioral reference: PR #139, `d3f90112d00c38868748b3101ae156e70d299313`. Latest fetched dev `5428a2dadb1d3c9a11a2e7245a8ca85d1621e4d0` was reconciled locally; the compared Python checkout remains pinned and unchanged.
- Rust worker: `src/bin/rvc_worker.rs`; coordinator/providers/text: `src/rvc/`.
- Local-only worker registration and bounded playout: `src/worker.rs`, `src/playout.rs`.
- Python scripts under `bench/` are independent reference/export/caller/test tools. The Rust worker does not invoke them.

## Local validation

```sh
cargo fmt --check
cargo test --locked --release --all-features --all-targets
cargo clippy --locked --release --all-features --all-targets -- -D warnings
cargo build --locked --release --features rvc --bin rvc-worker
```

Native builds need LiveKit's official WebRTC archive. The verified local build uses `LK_CUSTOM_WEBRTC=.../rust/target/native-archive/mac-arm64-release`. Archive provenance/hash is in `artifacts/native-asset-verification.json`; SDK/codec source was not forked.

`./scripts/verify-local.sh` independently owns a loopback LiveKit server and tests dispatch/media lifecycle using synthetic data. It refuses occupied ports and stops only its own server. `local-livekit.yaml` enables loopback ICE candidates so both official SDKs can connect without exposing the test server.

## Linux container validation

```sh
./scripts/verify-linux-local.sh
```

This builds the non-root local image, runs all native library tests, and checks dispatch, bidirectional media, cancellation, termination and active-job shutdown against an official LiveKit server. It publishes no host ports and removes only its own server container. The validation machine is Linux ARM64 inside Docker on this Mac; AMD64 is not validated. The builder uses the official pinned Rust image and LLVM21 from the signed official LLVM repository, required by LiveKit's bundled C++ headers. The runner contains native executables and shared libraries, with no Python interpreter or worker bridge.

The image is deliberately confined to the same local test registration/profile. A private config must be mounted with permissions readable by the selected container user; use a writable trace directory such as `/tmp` for local experiments. No production image is published.

## Speak through this Mac

The live browser caller is documented in [MAC_CALL.md](MAC_CALL.md). It runs the Rust worker and RTC locally while using the existing model services.

## One-call comparison

Build the release worker first. Use a private configuration file exported from the pinned reference with the effective provider settings. Keep API credentials outside Git with file permissions0600. `bench/export_reference.py` can export default fixture settings, or accept a private JSON `environment` mapping as its third positional argument. It refuses platform service credentials. The local tested transport forwards only ASR/TTS gateway traffic through an existing SSH tunnel; DeepSeek remains its configured HTTPS endpoint.

A Python3.12 environment with pinned-reference dependencies and the production-matched LiveKit packages is needed **for the separate Python baseline and test caller**. See `artifacts/rvc-production-runtime-versions.json`. Use:

```sh
SG_RVC_PYTHON=/path/to/python scripts/compare-rvc-local.sh \
  /absolute/private-config.json /absolute/pinned-python-checkout /absolute/output
```

This starts a fresh local worker for each serial test call, with one active call and no prewarmed Python job pool. Both receive the same locally synthesized test WAV,16 kHz mono PCM input,24 kHz mono PCM output,100 ms input frames and240 ms source queue. Test credentials are public loopback development credentials. Only fixed test metadata and `sg-rvc-*` rooms/caller identities are accepted.

Measured receiver PCM onset is not a physical audible-onset measurement. Local queue drain/cancellation are also distinct from sound at a caller's speaker. Missing observations remain null.

[NATIVE_GAPS.md](NATIVE_GAPS.md), [INVENTORY.md](INVENTORY.md) and [CHECKPOINT_1.md](CHECKPOINT_1.md) retain the earlier infrastructure/feasibility checkpoints; RVC_REVIEW is the current scope and result.

### Explicit operator startup under parity review

The default command still uses fixed loopback credentials and one-call dispatch. The separate `rvc-worker --platform OPERATOR_JSON TRACE_DIRECTORY` entry point reads a private JSON object with `worker`, `voice`, optional `sip`, and optional `capacity` fields. `voice` has the same native provider/coordinator configuration as the local harness; `worker` supplies LiveKit URL/API credentials, agent name, concurrency and drain policy. Platform services are required. SIP routing must explicitly pin trunks and a room prefix. Recording API credentials come from the operator's LiveKit configuration. Never commit a populated private operator file.

This path remains under local parity review: unsupported configured capabilities fail closed, and the outstanding gates in `PYTHON_PARITY.md` still apply. It has not been registered against production. New release binaries use `target/python-parity`; the existing demo binary remains unchanged.

Optional native directory settings live under `voice.directory`: `endpoint`, `token`, `contacts_db`, `zepto_key` and `from_address`. The operator selects the Google Apps Script endpoint and local contacts database; neither comes from caller/model data. The database uses the existing `caller_contacts` schema and a transactional `directory_email_outbox`. Keep it on persistent private storage; losing the outbox would lose restart deduplication. `allow_loopback` and `mail_url` exist for isolated local fixtures and stay unset for real mail. Directory-enabled profiles require a fresh read before provider facts and a separate committed read-back approval before emailing an alternate address.

The optional `voice.passkey_demo` requires explicit `business_ids`, `agent_ids` and `subjects` allowlists; all default empty. Its `rp_id`, `origin` and `timeout_seconds` default to docs.odion.ai, its HTTPS origin and 90 seconds. This separate simulated WebAuthn test uses call-scoped credentials and cannot authorize a banking operation. Device-sheet QA requires the trusted app/domain; the loopback voice page is not that relying-party origin.

The standard browser wire check is explicitly local: with the existing loopback LiveKit server running, execute `cargo test --release --all-features official_sdk_delivers_standard -- --ignored`. It creates a uniquely named room with synthetic participants and no model, financial, email, outbound or trunk operation. It verifies transcript/text/state and private recipient isolation.
