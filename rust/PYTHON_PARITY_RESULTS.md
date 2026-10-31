# Local parity review — 31 October 2026

The native port covers the capabilities reached by Python's deployed `latency_lab.livekit_worker` through GitHub main `04bbb2c3cfef84e08ec5b3c21f6fa6dca0049aa3`. See `PYTHON_PARITY.md` for the feature matrix and historical checkpoints. This is a local review candidate; production release is a separate decision.

## What is ready to review

The caller runtime, RVC speculative generation, final validation, interruption, RTC/audio and provider clients execute in Rust. Idle check-ins/fallback/goodbye, contextual engagement, scoped tools/knowledge, banking/voice authorization, both separate passkey flows, directory/saved-contact email, languages/voices, persistence/recording/usage, SIP/outbound/human transfer, browser messages and post-call reports have native implementations. No Python worker or coordinator participates in a Rust call.

Business/authentication/recording/SIP tests use local fixtures and synthetic SDK participants. The microphone demo intentionally has no business-service credentials or banking/email/ticket permissions. It exercises the actual existing ASR, DeepSeek and Helen TTS services, local LiveKit and native caller orchestration.

## Native checks

- `cargo test --locked --release --all-features`: **226 library tests pass**, one explicit SDK integration excluded by default; the local HTTP-origin test also passes.
- `cargo test --locked --release --all-features official_sdk_delivers_standard -- --ignored`: **passes** against local LiveKit with three synthetic participants, covering standard transcription/text/state and private destination isolation.
- `cargo clippy --locked --all-features --all-targets -- -D warnings`, formatting and release binaries: **pass**.
- The last report-only refinement passes its three affected tests, strict Clippy and release build. A real-provider native call then passes and automatically writes a native Markdown report on close.
- Independent unchanged Python suites and exported vectors are recorded per checkpoint in the matrix. The latest provider/memory/metrics group passes 55 tests; separate directory, demo-WebAuthn and language/Pidgin groups pass 45, 17 and 42 respectively. Earlier banking, connector-passkey, engagement, usage and persistence evidence remains recorded there.

Builds use `CARGO_TARGET_DIR=target/python-parity`, the isolated Cargo home and official macOS ARM64 WebRTC archive. The original `target/release/rvc-worker` was not overwritten; its SHA-256 remains `aa71b5bf1f0bf6270494f3c7f4108c0f92da817488557f622550d624fb37f1bc`.

## Same-provider call comparison

The comparison checkpoint is `e030535`. Six serial local calls use the identical PCM fixture, caller, local LiveKit/Opus transport and existing ASR/DeepSeek/Helen configuration. Order: Rust, Python, Python, Rust, Rust-interrupt, Python-interrupt. The Python coordinator, providers and transport come from the unchanged detached reference; a separate local adapter supplies RTC/control plumbing. It is never a Rust runtime bridge.

All six calls complete with authoritative finals. Each interruption call records one barge-in cancellation followed by the new final/response. Neither physical speaker-stop latency nor provider underflow is exposed, so those measurements remain unavailable.

| Observed median, three initial responses per runtime | Rust | Python |
|---|---:|---:|
| Decoded receiver PCM onset after estimated sender end | 1,978 ms | 2,163 ms |
| Owned speech-end → ASR final | 297 ms | 304 ms |
| Winning LLM request → first text | 713 ms | 865 ms |
| Authoritative final → SDK enqueue | 1,324 ms | 1,522 ms |
| First generated PCM → SDK enqueue | 0.31 ms | 4.42 ms |
| Peak worker/process-tree RSS | 55.4 MB | 364.7 MB |
| Total worker CPU, including startup and teardown | 0.755 s | 3.602 s |

This small live sample does **not** establish a statistical latency improvement, p95, throughput or production capacity. The received-PCM boundary is not a physical loudspeaker measurement. The sender endpoint is estimated from the last voiced 100 ms block; RTC, VAD and transport contribute additional time. Resource totals include SDK/process startup, and process-tree RSS can count shared pages more than once. Active-period CPU differences are smaller than the total-process difference. Native/Python trace schemas also differ despite synchronous JSONL tracing on both sides.

The useful bottleneck finding is that the winning LLM requests begin just after authoritative final validation in these calls: there is no winning speculative head start in this sample. Native rejects changed partial hypotheses, preserving final authority. Around 0.3 s goes to owned ASR finalization, around 0.7 s to LLM first text, with speech text/TTS and RTC/endpointing consuming most of the remainder. PCM-to-SDK orchestration is already sub-millisecond in the native observations; the end-to-end path remains roughly two seconds. The earlier complete-hypothesis/flush experiment remains off because its measured result regressed.

Local evidence, ignored rather than committed:

- `artifacts/python-parity-live-comparison/experiment.json`, `metrics.json`, caller/worker traces and received audio.
- `artifacts/python-parity-final-smoke/00-rust/`: final candidate's successful real-provider call and automatic `reports/AJ_*.md`.

## Silence and provider resilience

Native first passed a real-provider call followed by check-in, actual check-in playout, a 30-second grace period, goodbye playout and room deletion. The Python comparison initially exposed missing room-close plumbing in the test adapter; that adapter now passes the same handler used by the deployed entry point. The reference policy itself was not edited.

During further review the existing `/asr-rt` realtime and chat routes began returning HTTP 502, while its batch transcription route still returned valid text. One Python attempt therefore has no authoritative final and is **not** counted as a successful call. Its idle events alone are not proof of an answered caller turn.

A separate local profile clears only the chat-final override and uses the same healthy ASR service's owned batch-final endpoint. Both native and unchanged Python then complete a validated caller turn, check in, play goodbye and disconnect. Evidence is in `artifacts/python-parity-live-idle-batch/`. This verifies idle lifecycle, not a new latency benchmark. No production route/service was restarted or reconfigured. Realtime speculation remains implemented and final-protected; it cannot provide a head start while that upstream realtime route is unavailable.

## Filter/ASR check

`bench/review_noise.py` independently runs the unchanged Python filter and the native offline filter executable. A clean phrase and a deterministic synthetic 8 dB white-noise variant each pass unfiltered, Python/native APM and Python/native RNNoise paths. All ten existing-ASR transcripts agree with their unfiltered service responses, with no filter errors. Native retains exact input duration, including the final partial/resampler tail. Report: `artifacts/python-parity-noise-review/report.json`.

This is a small synthetic check, not a human-labelled word-error-rate corpus, proof of bit-identical waveforms or broad language/noise quality validation. The reference C RNNoise/resampler and Rust RNNoise/SDK resampler are different implementations.

## Local demo

Candidate: **http://localhost:17901/**. The old demo at 17900 stays available. The candidate's local shell supervisor runs only the native browser server and native worker, registering another one-call worker after each completed call. Config/credentials remain in a mode-600 private file outside Git. Logs, traces, PID files and automatic reports are under `artifacts/python-parity-review-17901/`.

The local review profile uses the existing healthy batch ASR final, DeepSeek and Helen; check-in eligibility is 10 seconds after quiet/actual playout, plus generation/playout time. Hangup grace is 30 seconds after check-in playout. It enables the local end-call action but no business APIs.

Suggested microphone review:

1. Start a call, ask a question and interrupt the reply with a different question. Verify old speech stops and the new question is answered.
2. Stay quiet after a response until the check-in. Answer it and verify the pending goodbye/hangup is cancelled.
3. On a separate quiet interval, allow the check-in and grace period to finish. Verify goodbye completes before the call closes; then start another call.

`bench/start_parity_review.sh PRIVATE_CONFIG_JSON LIVEKIT_SDK_UMD_JS [PORT [LOG_DIRECTORY]]` restarts the local review when needed. Config, SDK and log paths must be absolute. It refuses an occupied port and does not stop another demo. Build the parity release binaries first. The supervisor PID is in its log directory; sending TERM to that PID stops its owned native children.

## Deliberate compatibility differences and remaining review

- Disabled configured tools stay disabled; generic ambiguous confirmations have a bounded re-ask count.
- Unknown submissions remain unknown and block automatic retry. Mail acceptance is not claimed as inbox delivery, and a pending passkey/bank result is not claimed executed.
- Demo WebAuthn uses the library's valid opaque 16-byte UUID user handle instead of Python's 32-byte handle; credentials remain call-scoped and cannot authorize real banking.
- Native preserves the RNNoise/resampler tail and yields unfinished ASR handshakes to later owned commands. These fix observed reference/prototype failure cases without weakening final validation.
- Thinking-enabled synthetic tool exchanges include required empty `reasoning_content`; ordinary memory, storage, reports and browser telemetry omit private reasoning.

Physical Apple/device-sheet review, real language/voice intelligibility, intended-environment business/voice/passkey calls, directory/mail delivery, recording/storage, SIP/outbound/transfer and Linux packaging/resource/rollback validation remain gates before any production release. Local service fixtures cannot prove those external integrations. Production has not been changed, and nothing has been pushed or deployed.
