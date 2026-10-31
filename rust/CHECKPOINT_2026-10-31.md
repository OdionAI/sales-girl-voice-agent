# Rust voice prototype checkpoint — 31 October 2026

This records the reviewed starting point before experimenting with earlier complete speculative hypotheses. Runtime source at this checkpoint is `632ea67`; all orchestration is native Rust. The local demo uses the existing ASR, DeepSeek and named Helen TTS services. No production deployment, service restart or configuration change has been made.

The worker retains speculative LLM/TTS preparation, owned authoritative HTTP final recognition, the existing 0.92 final matching rule, stale-attempt rejection, model-selected speech gating and confirmed interruption cancellation. Idle audio is filtered before realtime ASR; each realtime request is limited to 15 seconds of audio, while owned final PCM is retained. Failed inference backs off even when websocket handshakes succeed. Forty-seven library tests and one local-origin test passed at this checkpoint, along with strict release clippy and a locked release build.

The latest measured call supplied seven replies with audio after restoring the local ASR/TTS tunnel. Greeting and proactive speech are excluded. Median server-declared end of speech to first local audio enqueue was 1,717 ms (range 1,427–1,944 ms). This excludes the configured 300 ms silence wait and unmeasured physical speaker/RTC delay.

| Phase | Median ms |
|---|---:|
| Server EOT to authoritative ASR final | 277 |
| Winning LLM request to first usable text | 700 |
| First usable text to first TTS request | 241 |
| First TTS request to first PCM | 542 |
| First PCM to local enqueue | 0.361 |

These medians are not additive because work can overlap. One sequential reply measured 1,779 ms after server EOT: 277 ms ASR, 1.8 ms dispatch, 700 ms LLM, 257 ms phrase accumulation, 542 ms TTS and 0.4 ms enqueue. Adding the nominal silence policy explains roughly 2.08 seconds before playback transport.

Only two of the seven replies reused speculative work. Five started their winning LLM request after final ASR. Three speculative attempts were discarded because the hypothesis was an incomplete prefix of the final transcript; two other replies had no speculative attempt ready. The matching rule must not be relaxed to accept incomplete caller requests.

The next experiment should deliver a more complete, current-turn speculative hypothesis sooner while retaining independent final validation, cancellation, idle filtering and the realtime audio limit. The running demo must remain unchanged while local changes are tested. Provider keys and user transcripts remain outside Git; raw interactive traces and sanitized per-reply analysis are in the ignored local artifacts directory.

No new latency improvement is claimed by this checkpoint. See `ASR_REVIEW.md`, `LATENCY_REVIEW.md` and `RVC_REVIEW.md` for earlier evidence and prototype limitations.
