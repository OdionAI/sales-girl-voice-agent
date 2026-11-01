# Parallel transfer guard: local review, 2026-11-01

This is an opt-in Rust implementation on `codex/stream-transfer-guard`. Production, staging, shared branches and deployment configuration were not changed. The new `transfer_streaming_replies` setting defaults to **false**.

## Behavior

The existing guard waits for the entire tool-capable model response, even for ordinary speech. The opt-in path concurrently requests:

1. A tool-routing decision using the original instructions and tools, plus an empty `rvc_answer_normally` marker. `tool_choice=required` forces an explicit decision.
2. An answer-only draft with no tools, preserving the business and language instructions.

Draft text stays held until routing ends with `finish_reason=tool_calls` **and** `[DONE]`, and the complete response validates. An ordinary marker releases buffered text and subsequent tokens immediately; the answer need not reach EOF. Any advertised action, including one after an ordinary marker, discards the draft. Only the actual action and its continuation context enter the existing coordinator. Routing narration and the internal marker never become speech.

Unknown actions, invalid/duplicate/missing ordinary markers, truncated routes and unexpected actions in the answer-only stream fail closed. Buffers are bounded. Dropping the parent future or its output receiver cancels both owned requests; no background generation is detached.

The existing coordinator still owns RVC speculation, independent authoritative-final validation, TTS/audio authorization, human transfer cues, exactly-once side effects, interruption and SIP cleanup. Speculation can prepare this generation early but cannot authorize speech or a transfer. The opt-in path applies only to non-thinking DeepSeek, with an advertised transfer tool, speech enabled and no directory tools. Other configurations use the existing guard.

Routing and response metrics are labelled separately. Routing tokens cannot replace spoken-response TTFT metrics; usage observations still include both requests.

The [DeepSeek API contract](https://api-docs.deepseek.com/api/create-chat-completion) allows one or more calls under `required`. This implementation therefore validates the full routing response rather than releasing speech on the first function name. It does not rely on an undocumented parallel-tool switch.

## Measured result

Eight observations per variant, alternating ABBA four times, using the same short synthetic Yoruba question, full saved Balogun staging-cloned instructions and tools, existing ASR/DeepSeek/TTS, and 150 ms end silence. Calls ran serially on this Mac. The endpoint is **first local audio enqueue**, not sound heard on a phone or browser. The local sink simulates queue consumption; LiveKit, browser buffers and device playback are excluded.

| Observation | Existing guard | Parallel routing |
|---|---:|---:|
| Median silence to local audio enqueue | 2,312 ms | 1,948 ms |
| Slowest observed / nearest-rank sample p95 | 2,906 ms | 2,945 ms |
| Mean hold after first response text | 662 ms | 315 ms |
| Mean ASR after VAD commit | 245 ms | 231 ms |
| Mean TTS request to first PCM | 274 ms | 171 ms |
| LLM requests per ordinary generation | 1 | 2 |
| Mean provider-reported input tokens | 7,781 | 15,394 |
| Mean cached input tokens | 7,552 | 15,104 |
| Mean output tokens | 54 | 63.6 |

Median improvement: **364 ms / 16%**. TTS began before the answer stream completed in **7/8** candidate calls, compared with **0/8** baseline calls. All 16 returned Yoruba responses identifying Balogun.

There is **no demonstrated tail improvement**. The slow candidate had a 2,266 ms routing request despite earlier draft text, producing a 2,945 ms enqueue latency. Provider first-response time and routing completion remain bottlenecks. TTS variability and naturally varying answer lengths also affect this small sample, so the entire median difference cannot be attributed solely to guard code. Eight samples of one question do not establish a production percentile or broad conversational quality.

The cost is an extra LLM request for each eligible generation, including speculative attempts. Observed input tokens nearly doubled, mostly through cache hits. Cancelled requests may not return complete token usage; accounting marks those observations partial rather than estimating them. The setting remains off pending review of this tradeoff.

A separate follow-up ABBA check after cancellation/telemetry hardening (two observations per variant) recorded medians of 2,455 ms versus 1,972 ms. This confirmation is separate from the eight-per-variant batch above.

Raw timing rows and provider usage counts are in [the sanitized measurements](transfer-guard-20261101.json). Private configuration, instructions, provider credentials, full traces and audio remain outside the repository.

## Correctness and provider checks

- 253 Rust library tests passed; one remained ignored. Existing coordinator scenarios now exercise both legacy and opt-in routing for held speculative audio, changed finals, short noise versus sustained interruption, interrupted connection cues, late answered bridges and unchanged speculative transfer-plan reuse.
- Deterministic streaming barriers prove that speech waits for the complete route, then can arrive before answer EOF. Late conflicting actions suppress all drafts/preambles. Truncation (including `finish_reason=length`), unknown actions, ambiguous markers and cancellation release no speculative speech.
- A local provider-backed Balogun call was interrupted during playback. The worker recorded `user_barge_in`, cancelled the old attempt and completed the next turn.
- A synthetic Yoruba human request traversed ASR, speculative/final reconciliation, tool routing and the localized connection cue, then emitted **one** bridge handoff intent. The local harness has no SIP client and returned a controlled failure instead of dialling a human. Actual SDK dial/cleanup behavior remains covered by the fake SDK integration tests; this was not a real AICC call.

## Reproduce locally

Build `rvc-transfer-review` with the `rvc` feature (or `--all-features`) using the existing pinned `RNNOISE_MODEL_PATH` setup. Supply a private config with resolved Balogun context, saved instructions plus conversation rules, provider credentials and a reachable speech-gateway tunnel. The runner applies the language profile. Input is mono 16 kHz PCM16; credentials and config contents are never command-line arguments.

```sh
python3 rust/bench/transfer_review.py /absolute/path/rvc-transfer-review \
  /private/path/config.json /private/path/question.pcm /private/path/results --blocks 4

/absolute/path/rvc-transfer-review /private/path/config.json \
  /private/path/question.pcm /private/path/interruption stream-interrupt

/absolute/path/rvc-transfer-review /private/path/config.json \
  /private/path/human-request.pcm /private/path/handoff stream-human
```

The Python file only launches the native Rust test executable and summarizes measurements. The Rust coordinator performs ASR, LLM, TTS, speculation, validation and interruption handling. The review executable never creates a platform writer or SIP bridge.
