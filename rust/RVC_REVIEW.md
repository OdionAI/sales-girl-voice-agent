# Native Rust RVC one-call worker — review

This report records checkpoint `4f1fe35`. See [LATENCY_REVIEW.md](LATENCY_REVIEW.md) for the subsequent local latency fix and controlled before/after measurements.

The one-call RVC conversation core, provider adapters, worker lifecycle and media path run in Rust using official LiveKit SDKs and generated protocol messages. No Python worker/coordinator bridge is invoked. Production continues to use its existing worker; this isolated branch is ready for review only.

## Implemented behavior

| Area | Native implementation |
|---|---|
| Lifecycle | Local registration, bounded admission, reservation/assignment, termination, reconnect/drain, signal handling; capacity is restored before terminal job reporting to prevent a subsequent-dispatch race |
| Media | Official Rust RTC SDK; 16 kHz mono input in 100 ms blocks, 24 kHz output, 240 ms source queue, reliable local control/status data |
| Turn ownership | Same RMS280, minimum voiced240 ms, silence300 ms; short noise does not interrupt. The caller's complete owned PCM is retained for final ASR |
| Speculation | Same180 ms stable partial and eight-character minimum; streaming LLM and serial phrase TTS start early, with all PCM held until authorization |
| Authorization | Current awaiting-final turn only; normalized equality or Python SequenceMatcher-compatible similarity >=0.92; changed finals replace prepared work, stale finals cannot authorize |
| Generation | Streaming hidden-reasoning removal, reference phrase/token budgets, bounded FIFO and backpressure. Replacement and interruption abort old provider work; stale callbacks are invalidated |
| Speech gate | Structured model-selected gate, applied only after owned final. Listening-only input remains in memory; model-supplied resume response is spoken without a second model call. Timers cannot enable muted output |
| Conversation memory | Source-compatible grouped turns, interrupted/listening-only provenance, bounded prompt context and paired tool messages; stale-safe background summary compaction with the same prompt, budgets and validation |
| Idle response | Tool-free background decision using actual conversation context, bounded pre-synthesis and stale-result rejection. Proactive audio does not manufacture a caller turn or reopen muted output |
| ASR recovery | Independent bounded realtime queue, connection retry/backoff and replay; input/interrupt handling does not await realtime writes. HTTP-owned finals continue if realtime is unavailable. Failed final cancels prepared speech and allows another caller turn |
| Failure handling | No arbitrary realtime final is promoted to authoritative. Native provider timeouts, source-configured empty-response behavior, one graceful response for an authorized LLM outage, bounded task cleanup |

The scope is a generic English microphone call, including subsequent/interrupted turns. It uses the reference default persona, not a production business agent. Fixed local metadata, room/caller identities and loopback registration are required. Business credentials and tools are not loaded; unsupported tool requests are rejected.

This is **not full production feature parity**. Business tools and confirmations, knowledge retrieval, banking/passkeys, billing/capacity leases, conversation persistence/recording, SIP/transfer, RNNoise and multilingual routing remain outside this prototype. Idle decisions are implemented; background business-tool engagement is not. Whisper and uncorrelated realtime-final fallback are refused. Linux ARM64 is locally validated; the production architecture and long-call/concurrent behavior are not certified. No Python bridge bypasses these boundaries.

## Reference and providers

Behavioral baseline: unchanged PR139 checkout `d3f90112d00c38868748b3101ae156e70d299313`. Latest fetched dev `5428a2dadb1d3c9a11a2e7245a8ca85d1621e4d0` was merged into this isolated branch locally. Its intervening changes concern additional language/voice routing and documentation; the Python pipeline/provider/memory/engagement reference used here remains unchanged.

Earlier authorized read-only production inspection verified `pipeline.py`, `providers.py` and `config.py` against the pinned reference. Evidence: `artifacts/rvc-production-source-verification.json`. No production write, restart, deployment, live business call or financial operation was performed.

The preserved providers are Qwen3-ASR on node1, DeepSeek API `deepseek-flash` with thinking disabled and reasoning_effort none, and Qwen3-TTS English voice `helen-mavino-0030`, seed3536072912 and six initial codec frames. Only ASR/TTS gateway transport is forwarded through SSH to loopback. Node2 is not used. Keys remain in a mode0600 private file outside Git. Model services were not rewritten or restarted.

The reference uses Python3.12.7, `livekit1.1.17` and `livekit-agents1.8.0`; Rust uses official `livekit0.10.0`, `livekit-api0.10.0`, and `livekit-protocol0.8.2`. See `artifacts/rvc-production-runtime-versions.json`. The official WebRTC/codec sources were not forked. This compares these implementations and SDKs, not languages in isolation.

## Refreshed local comparison

Six serial calls, three per implementation, order Rust/Python/Python/Rust/Rust/Python. Fresh workers, one active call, zero prewarmed Python jobs, same Mac, LiveKit1.13.6 server/caller, input WAV, framing, queue and effective provider configuration. Both synchronously trace per event; schemas/counts differ. Python includes its active child and resource tracker.

| Median (n=3 each) | Rust | Python |
|---|---:|---:|
|Peak aggregate worker RSS|48.6 MiB|344.7 MiB|
|Idle RSS|12.2 MiB|148.2 MiB|
|Active CPU, percent of one core|4.90 %|6.21 %|
|Idle CPU, 2 s window|0.40 %|0.66 %|
|Receiver PCM onset after last voiced block|2450 ms|2240 ms|
|ASR final after server EOT|364 ms|378 ms|
|Winning LLM request to first usable text|1083 ms|822 ms|
|First TTS PCM to adapter enqueue|0.234 ms|4.148 ms|
|Peak process count|1|3|

The native worker has lower local resource overhead and a smaller first-PCM/enqueue interval in this slice. **End-to-end response onset is not faster in this repetition:** Rust2.450 s versus Python2.240 s. Winning DeepSeek first-text times also differ (1083/822 ms). Prior repetitions changed the ordering; varying model/WAN timing, hypotheses and generated answers prevent a reliable speedup claim. No p95, concurrency capacity, long-call endurance, model-inference gain or language-only speedup is established.

The adapter interval has different SDK frame/callback boundaries and is not an audible-onset ratio. Aggregate RSS double-counts shared pages across Python processes; it is not unique physical RAM. Receiver onset uses decoded PCM RMS80 and an estimated100 ms sender block boundary, not a physical loudspeaker. Underflows and physical audible stop remain unmeasured. Raw calls, traces, recordings, process samples and ranges are in `artifacts/rvc-comparison-review/metrics.json` and adjacent folders. Previous checkpoint values remain in `artifacts/RVC_PREVIOUS_CHECKPOINT_RESULTS.md`; they are not substituted for the refreshed measurements.

One additional real-provider interruption call per implementation confirmed user_barge_in cancellation and completion of the subsequent caller turn. Rust cancel-to-new-turn after local-clear acknowledgment was 0.156 ms; Python cancel-to-transport-clear was 4.151 ms. These endpoints differ, so no cancellation speed ratio or physical audible-stop claim follows. Evidence: `artifacts/rvc-interruption-review/diagnostics.json`, caller recordings and raw traces.


## Validation and remaining review

- 40 native Rust tests pass on macOS and Linux ARM64. Coverage includes official worker wire/lifecycle, bounded playout, reference text/matching vectors, ASR handoff, held speculation, changed/stale finals, interruption/noise, output gate, idle-response cancellation, background compaction, bounded recovery replay, final failure followed by a healthy caller turn, and cancellation of replaced provider work.
- Independent memory expectations come from the untouched Python class: five conversation plans, 45 budget/exclusion contexts and five post-compaction results. Re-export is byte-identical. Invalid/stale summaries cannot overwrite the conversation.
- 80 unchanged Python memory, engagement and empty-generation checks pass; earlier selected35 reference checks remain in the preceding checkpoint. These tests describe the reference contracts, not parity for excluded production features.
- Rust formatting, all-feature/all-target release clippy with warnings denied, locked optimized binaries, Python harness syntax and shell syntax checks pass. Logs: `artifacts/rvc-*-review.log` and `artifacts/rvc-python-expanded-reference-tests.log`.
- The local non-root Linux container passes native dispatch, bidirectional audio/data, held/stale audio checks, interruption, termination and active-job shutdown against official LiveKit1.13.6. `artifacts/linux-capacity-race-before.json` retains the failure that prompted restoring capacity before terminal status; the corrected wire test and real-server smoke pass. Linux artifacts are named `linux-*-review.*`.

`README.md` documents local reproduction. `scripts/verify-linux-local.sh` owns only its test container, publishes no host ports and cleans up on exit. Python under `bench/` supplies an independent reference/caller/export harness, never the Rust runtime. Historical feasibility documents and previous metrics are retained separately.

No GitHub push/merge or production change was made. Stop here for code and evidence review before any production integration.
