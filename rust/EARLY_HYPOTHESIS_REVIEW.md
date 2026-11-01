# Earlier completed ASR hypothesis experiment

**Disposition: keep disabled for the current providers.** The implementation preserves final validation and passes the local correctness checks. A controlled model-delay replay improved, but neither real-provider trial established a response-latency improvement. The running Mac demo retains the checkpoint binary and configuration.

## Checkpoint and scope

`d1c961e` checkpoints the state before this experiment; runtime source was `632ea67`. The experiment is isolated on `codex/sg-rvc-early-hypothesis`, reconciled with current `dev`. No push, deployment, production service restart or production model configuration change was made. Real-provider measurements used serial synthetic-input calls to the existing ASR, DeepSeek and Helen TTS services, in separate rooms on the local RTC server. The active browser room was not changed.

The Rust worker performs all orchestration. Existing Python tools act only as the test caller and offline provider fixture; no Python worker/coordinator bridge was added. The official LiveKit SDK and protocol dependencies are unchanged.

## What changed

The checkpoint adapter closes realtime ASR at confirmed EOT before asking HTTP ASR for the authoritative final. It can discard an unsent realtime tail and never asks that stream to flush its final hypothesis.

The opt-in `stt_speculative_flush_ms` setting now permits one bounded flush per caller turn. On the first quiet input frame of a confirmed utterance, the adapter sends queued audio through that snapshot's sequence fence and sends the existing protocol's `input_audio_buffer.commit` with `final:true`. A nonempty `transcription.done` can immediately prepare speculative LLM/TTS work, without an additional stability wait. This is a completed recognition generation, not proof that the caller has finished speaking.

The independent HTTP final remains the **only** authority to release prepared PCM or apply a tool. Its owned utterance PCM, existing 0.92 matching rule, stale-final rejection and speech-gate behavior are unchanged. No missing words are guessed and no matching threshold is lowered.

If speech resumes, the coordinator invalidates the candidate revision and cancels its unapproved generation. Realtime ASR reopens with the complete owned prefix, subject to the same 15-second request limit. Only one early flush is attempted per turn, so normal pauses cannot cause repeated flush/replay cycles. Oversized replay prefixes disable optional realtime preparation rather than bypass the limit; the full PCM still reaches final recognition.

The final version preempts the flush immediately for resumed speech/recovery/closure. Once EOT is confirmed, it grants at most 80 ms of additional optional wait, within the overall configured budget, before the normal socket handoff and authoritative final request. A missing, empty, stale or failed completion falls back to final recognition. Normal transport/close time is separate from that optional wait bound.

**Default is zero/off.** No TTS phrase sizes, initial codec framing, voice/seed, endpointing thresholds, stability duration, LLM settings, or production routing were changed.

## Measurements

A bounded ASR-only probe using the existing synthetic input returned a flushed completion in 257 ms and the HTTP final in 264 ms; the texts matched. This showed why flushing only after EOT would offer little head start. The implementation instead overlaps flushing with the existing silence wait.

The comparisons used the same input WAV, voice, seed, provider settings and caller/Opus transport, with three interleaved normal calls per variant and an experimental interruption call. The only configuration difference was `stt_speculative_flush_ms: 0` versus `350`; binaries differed as documented by SHA256 in the measurement artifact.

| Final implementation, median | Checkpoint | Experiment |
|---|---:|---:|
| Local model-delay replay: EOT to audio enqueue, n=3 each | 1,773 ms | 1,556 ms |
| Replay calls reusing speculative work | 0/3 | 3/3 |
| Existing real providers: EOT to audio enqueue, n=3 each | 1,611 ms | 1,774 ms |
| Real calls reusing speculative work | 0/3 | 0/3 |

The controlled replay saved 217 ms on its exercised path. It is a simulation, not a live-provider gain. In the final real-provider trial, two flushes timed out and one was superseded by resumed speech; none supplied an accepted completed hypothesis. The median was 164 ms slower, so the results do not justify enabling this tactic for the current services.

An earlier implementation without the 80 ms final-handoff cap also failed to improve the real sample: 1,495 ms before versus 1,598 ms after. It reused speculation in 2/3 calls, but only one came from an accepted completed flush; the other reused a later ordinary partial after replay. A timeout postponed final recognition by about 150 ms, motivating the final cap. These results are retained, not replaced by the favorable replay.

One baseline call in the initial simulation produced an unexpected second caller turn. It remains recorded as an outcome but is excluded from that trial's primary latency medians (n=2 baseline, n=3 experiment). All normal calls in the final simulation and final real comparison had one caller turn and are included.

All four interruption calls completed, each recording one confirmed barge-in cancellation and no subsequent first-audio enqueue from that cancelled attempt. This measures worker behavior, not physical speaker-stop latency.

Times start at **server-declared EOT** and exclude the configured 300 ms silence policy and unmeasured RTC/device playback. Samples are small and serial; no p95, statistical-significance, concurrency or production-speedup claim is made. Model first-output/phrase timing and live provider workload vary across calls.

Flushing may perform more recognition work than aborting a partial stream. A false-pause replay can also add ASR work. The client-side timing window is therefore not a free improvement in model throughput. The real endpoint did not consistently complete within that useful window.

## Validation and reproduction

54 library tests plus the local-origin test passed, along with strict locked release clippy across all features/targets, formatting/diff checks, and the locked release build in `target/early-hypothesis`. New checks cover held speculative audio, independent-final mismatch isolation, revision rejection after speech resumption, full-prefix replay, opt-in/budget validation, bounded fallback, EOT preemption, and replay respecting the realtime audio limit. Existing interruption, gate, stale-final and idle-audio tests continue to pass.

The public, transcript-free summary is `artifacts/early-hypothesis-results.json`. Raw simulated traces/receiver PCM remain locally under `artifacts/early-hypothesis-local*`; real-provider traces are private and ignored under `artifacts/early-hypothesis-real*`. Keys and private configurations remain outside Git. The running demo binary's SHA256 still matches the checkpoint baseline and its flush setting remains zero.

The replay extends the existing fixture and reuses the existing caller and analyzer. With the normal loopback RTC development server already running, reproduce into a new directory:

```sh
SG_RVC_PYTHON=/absolute/test-python scripts/compare-early-hypothesis-local.sh \
  /absolute/checkpoint-rvc-worker /absolute/experimental-rvc-worker \
  /absolute/new-output-directory
```

The runner refuses an occupied fixture port, uses only loopback provider URLs and a public fixture key, creates separate named local rooms/workers, and stops only its own fixture. It leaves the browser demo and existing RTC server running. Real-provider trials reused `bench/run_calls.py` with mode0600 private derived configs; only the flush setting differed.

The experiment is saved for review rather than enabled. The evidence says that this provider's completed-stream latency and pause behavior make early flushing unreliable; relaxing final validation would conceal that problem rather than solve it.
