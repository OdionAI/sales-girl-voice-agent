# Local call ASR failure — 30 October 2026

Status: local guards implemented and tested offline; local demo stopped for review. No production service was changed or restarted by this work. No production inference was performed after stopping the demo.

## Observed failure

The first interactive call received six authoritative final transcripts. Three caller replies reached the local audio queue 1,540.725–1,984.132 ms after server end-of-turn detection (median 1,727.067 ms). This excludes the greeting and replies interrupted before audio. It does not measure physical speaker onset or the preceding silence detection interval.

Five confirmed caller interruptions cancelled an attempt: two after audio began and three during preparation. Local cancellation-to-next-turn acknowledgements ranged from 0.081 to 3.203 ms. These timings exclude speech confirmation and physical speaker stop. No cancelled attempt subsequently emitted a first-audio enqueue event.

All times below are Africa/Lagos:

- 18:04:15: last successful final transcript in the first call.
- 18:05:13: node1 ASR engine failed in `gpu_input_batch.add_request`: a 32,827-token prompt could not fit its 32,768-entry token buffer. The configured `--max-model-len` was 32,768.
- 18:05:33: the local trace began reporting realtime provider errors.
- 18:07:06 onward: final transcription requests failed too. Eight owned final-transcription failures were observed across the two interactive calls.
- 18:08:41: server logs recorded the same failure with a 32,974-token prompt. Container state later showed a restart at 18:08:45, restart count 17, and `unless-stopped` policy. No restart command was issued by this task.
- 18:10:56: server startup completed; a subsequent health probe returned 200.
- 18:12:05: the same 32,974-versus-32,768 failure recurred. Recovery was therefore not stable.
- 18:13:23: the local demo call was ended, followed by stopping its local launcher and owned processes.

Read-only inspection used container state and bounded server-log excerpts. The SSH tunnel remained connected. The model-list and websocket handshake endpoints could respond even while inference failed; those checks were insufficient evidence of ASR recovery. The screenshot also showed a muted microphone, but muting did not explain the server failure.

## Client defect and local change

The native coordinator previously forwarded every input frame to realtime ASR, including silence delivered by RTC while idle or muted. A realtime request could therefore remain active and accumulate work indefinitely without a caller utterance ending. The native client also reset recovery backoff on a successful websocket handshake, even if generation immediately reported a dead engine.

The oversized request is confirmed by server logs. The local idle-stream defect is confirmed by code inspection and regression tests. The available trace does not link a server request ID to the particular local websocket, so attribution of every server crash to this client remains unproven. The repeated failures are consistent with this exposure. Server-side rejection of oversized input remains a separate production issue.

Local Rust changes:

- Send candidate speech immediately, then retain active-utterance frames including endpointing silence. Suppress idle/muted silence before ASR; VAD and owned final PCM still process input normally.
- Limit one realtime request to 15 seconds of audio, including warmup. At the limit, close optional realtime speculation until the next owned final. Do not split the caller's turn, promote a partial to final, or discard owned final PCM. The 15-second value is a conservative client guard, not an assertion about a universal audio-to-token ratio.
- Preserve the full utterance for authoritative HTTP recognition. Long utterances can lose further speculative updates after the cap; their final transcript still determines the answer.
- Preserve increasing recovery delay across handshakes that immediately return inference errors. Successful inference resets it.

## Offline validation

`cargo test --release --all-features`: 47 library tests and one local-origin test passed. Strict all-feature/all-target release clippy passed, and formatting/diff checks passed.

New regression coverage uses only local fake ASR/LLM/TTS servers:

- Ninety seconds of idle PCM before a turn and ninety seconds after it produce zero idle ASR samples. Subsequent speech still prepares speculative TTS, holds it until the owned final, and supports a confirmed interruption during the next answer.
- A 25-second utterance never sends more than 15 seconds to any realtime socket. The full 25 seconds reach the owned final request, no final is manufactured at the cap, and realtime resumes for the next turn.
- A frontend that accepts websocket connections but rejects generation backs off instead of reconnecting twice per second.

Existing tests continue to cover final mismatch cancellation, stale-final rejection, speech gating, unsupported tools, short-noise rejection, held speculative audio, and interruption.

Private interactive traces and sanitized per-turn measurements remain under ignored `artifacts/mac-live-call/`. Test and clippy output are in ignored `artifacts/rvc-asr-guard-tests.log` and `artifacts/rvc-asr-guard-clippy.log`. No user transcript is included in this review.

The changed worker has not been rerun against production providers. A reviewed real-provider retest is still required; these offline results do not establish production recovery or a new end-to-end latency improvement.
