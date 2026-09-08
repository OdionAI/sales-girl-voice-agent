# Numeric TTS cutoff (2026-09-07)

Pre-change checkpoint: `5116d77` on `speculative-integration`.
Scope: the isolated hybrid worker. No remote server, private environment,
model, voice, saved prompt, timing, authentication, tool or SDK changes.

## Evidence

Call trace `ce5c4e03a01e` contains complete assistant text but incomplete spoken
account lists. The request estimated speech duration from whitespace words, so
each ten-digit account counted as one word. Three list phrases reached their
audio token limit without interruption or a generation error:

| Phrase | Previous token limit | Received audio |
| --- | ---: | ---: |
| Here are your accounts, followed by five accounts | 64 | 5.04 seconds |
| Here they are one by one, followed by five accounts | 74 | 5.84 seconds |
| Let me read them all again, followed by five accounts | 89 | 7.04 seconds |

At the current model's observed 12.5 audio tokens per second, these durations
equal `(limit - 1) / 12.5`. LiveKit drained the audio it received; there was no
caller interruption during those three turns.

## Change

`providers.tts_token_budget` retains the existing duration estimate and counts
each numeric digit as a potential spoken word. This is only an output-token
allowance: the input text is not altered, leading zeros are preserved, and
currency amounts are not rewritten as identifiers. The minimum 24 and maximum
360 tokens remain. Ordinary nonnumeric phrases keep their original budgets.
The existing `tts_request_start` trace now includes `max_new_tokens`.

The reported five-account phrase now allows 299 tokens instead of 74. Same-server
request-only comparison, same text/voice and four initial codec frames:

| Budget | Received audio | First chunk | Total generation time |
| --- | ---: | ---: | ---: |
| 74 | 5.84 seconds | 0.475 seconds | 4.001 seconds |
| 299 | 17.28 seconds | 0.514 seconds | 11.629 seconds |

These are single-request observations, not latency guarantees. A higher ceiling
allows completion; it does not require generating all the permitted tokens.

## Verification and limits

- Unit suite: 129 tests, 117 passed and 12 existing skips. Added regression
  coverage for account/phone digits, unchanged normal speech budgets, the safety
  cap, exact request text and voice/stream settings, PCM framing and release.
- Repeated request through the modified client generated 16.80 seconds. Existing
  Whisper websocket transcription reached the fifth account, including its final
  digits. One middle account differed in that ASR round trip, so this is evidence
  of completion, not proof of digit-perfect pronunciation or transcription.
- Reloaded only the idle hybrid worker. Native LiveKit smoke trace `ae1df41d8102`
  confirms the new budget telemetry, greeting and completed response playback,
  with 17.31 seconds of received response audio and no receiver/generation errors.
  The LLM declined the text-only request to repeat the account list; the saved
  prompt was not changed to force it. Numeric-list completion was verified in
  the direct TTS test, not this end-to-end call. The smoke room was removed.
- Very long numeric phrases can still exceed the unchanged 360-token ceiling.
  This fix does not add arbitrary-length speech segmentation, change the LLM's
  output cap, or repair independently truncated LLM sentences.

## Rollback

Run only the isolated worker from checkpoint `5116d77` in a separate worktree,
using the existing private environment, or reverse this narrow request-budget
change after review. Reload it after calls end. No inference-server state needs
restoration. Do not reset other worktrees or restart shared services.
