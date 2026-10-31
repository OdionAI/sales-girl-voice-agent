# Local Rust RVC latency optimization

The avoidable delay was discarded speculative model work, rather than the Rust audio enqueue path. The corrected native worker reduces response onset from **2.302 s to 1.983 s**, saving **319 ms (13.9%)** in a controlled local replay, three fresh calls per version. This is a local protocol/model-timing simulation; no production service was accessed or changed for this task. It is not a measured live-provider speedup.

## Why the previous end-to-end result did not improve

The saved real-provider experiment had median Rust response onset2.450 s versus Python2.240 s. Rust's first-PCM/enqueue path was already0.234 ms; the winning LLM request took1083 ms to first usable text, versus822 ms in Python. These are provider waits including transport, not isolated model-compute measurements. Milliseconds of orchestration savings cannot compensate for hundreds of milliseconds of variable provider delay.

More significantly, all three Rust calls and all three Python calls started their eventual winning LLM request **after** authoritative ASR. Their realtime text accumulated repeated hypotheses, such as `Oh.Oh...Please tell.Please tell me whether...`, while final ASR returned a clean sentence. Earlier speculative work was consequently cancelled instead of reused. `artifacts/rvc-latency-local/original-real-breakdown.json` derives this from the unchanged saved traces.

Two native defects explain this behavior under the repository's ASR wire contract:

1. `deployment/stable/npu/asr/connection.py` emits a standalone `language English<asr_text>` delta before each forced-language cumulative hypothesis. The old parser filtered its empty spoken text **before** clearing the previous hypothesis. Warmup text and subsequent re-decodes accumulated. The same defect exists in the pinned Python receiver.
2. If the last partial's180 ms stability window expired after server EOT, the coordinator refused to start it while final ASR was pending. That left the final-ASR wait unused even when a current partial was ready to prepare.

The complete original raw ASR wire stream was not captured. The explicit-marker defect is proven against the checked-in server contract and reproduced locally; observing that marker on the current live endpoint remains a future integration check. Production was not inspected to establish it.

## Changes

- Apply explicit hypothesis boundaries before discarding empty content. Respect language rejection, and preserve ordinary caller repetition. There is no heuristic deletion of repeated words or guessed sentence boundaries.
- Allow a stable partial from the current confirmed turn to prepare while authoritative ASR is pending. Keep the turn in `AwaitingFinal`, so this change grants no audio/tool authorization.
- Keep final ownership,0.92 matching, changed/stale-final rejection, model-selected speech gate, cancellation and interruption intact. VAD thresholds, silence/stability durations, codec framing, queue sizes, voice/seed and model limits are unchanged.
- Extend the existing local ASR fixture with deterministic LLM/TTS responses and reuse the existing caller, RTC server and analysis. The Rust runtime still invokes no Python worker/coordinator bridge.

## Local before/after

Baseline executable: commit `4f1fe35`, verified against its recorded binary SHA before copying. The optimized binary uses the same official SDKs, input WAV, configuration, local LiveKit1.13.6, trace mode and hardware. Three serial fresh calls per version, baseline block followed by optimized block. The fixture models the observed cumulative-boundary pattern and a late final hypothesis. Delays are fixed equally for both: ASR350 ms, LLM1000 ms, TTS550 ms. Output is a synthetic tone, not cloned-voice quality evaluation. All five provider URLs point to127.0.0.1; the only API key is a public fixture placeholder.

| Median, n=3 each | Before | After |
|---|---:|---:|
| Receiver PCM onset after estimated last voiced block | 2302 ms | 1983 ms |
| Authoritative final to first adapter enqueue | 1570 ms | 1248 ms |
| Winning LLM head start before final ASR | -0.5 ms | 320.8 ms |
| Calls reusing the winning speculative attempt | 0/3 | 3/3 |

The negative baseline head start means the request began after final ASR. The optimized head start explains the approximately320 ms reduction: useful model work overlaps final ASR instead of restarting afterward. Source metrics and ranges are in `artifacts/rvc-latency-local/before/metrics.json` and `after/metrics.json`; raw RTC/worker traces, receiver recordings and fixture events are adjacent.

The packaged runner was then executed end to end with a fresh fixture/server: three more baseline calls, three optimized calls and one interruption call completed. Receiver-onset medians were 2.291 s before and 1.992 s after. Optimized speculation was reused in 2/3 calls; one replay did not emit its full matching partial (fixture emission depends on the voiced-sample count after Opus), so the worker correctly discarded its incomplete hypothesis and generated from the authoritative final. This retained fallback is slower and prevents a universal speedup claim. `artifacts/rvc-latency-local/reproduction.json` records every result and raw-file hash; the independent verification directory remains `/private/tmp/sg-rvc-latency-reproduction`.

Small serial samples establish this exercised path, not p95, concurrency capacity or production timing. Provider delays, actual decoder boundaries and transcript differences can change the gain. Receiver PCM onset uses RMS80 and an estimated100 ms sender frame boundary; physical audible stop and underflows remain unmeasured. Business integrations, RNNoise and other exclusions in `RVC_REVIEW.md` remain outside the prototype.

## Validation and reproduction

44 native tests pass on macOS and Linux ARM64, including the existing speculation/interruption suite plus explicit-marker replacement, ordinary word repetition, unsupported-language rejection, late preparation with stale-final rejection, changed-final PCM isolation, and speculative speech-gate tools waiting for final ownership. Strict release clippy, formatting and locked release builds pass. The official-server Linux media/lifecycle smoke passes. One optimized local interruption call cancels the first answer and completes the second caller turn with its own authoritative final.

The packaged runner rejects occupied local ports, creates only its own fixture/server processes, and cleans them up. Reproduce with built baseline/optimized binaries and the unchanged Python checkout used solely by the caller/reference tools:

```sh
SG_RVC_PYTHON=/absolute/python scripts/compare-latency-local.sh \
 /absolute/baseline-rvc-worker /absolute/optimized-rvc-worker \
 /absolute/pinned-python-checkout /absolute/output-directory
```

The initial fixture setup disabled the greeting while the existing caller waits for greeting completion. It timed out before sending the test utterance; greeting was restored before the measured baseline. That setup probe is excluded from results.

The branch remains local and unpushed. No SSH connection, production provider inference call, deployment, restart or production configuration change was made during this optimization task. Stop for review before integration.
