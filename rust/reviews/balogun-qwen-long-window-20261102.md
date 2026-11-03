# Balogun: longer background Qwen audio

## Deployment and failed-call findings

Read-only inspection on 2026-11-02 found production healthy at
`1306fd56fb6380e1125942c955d2e3163481afa5`, with the feature enabled and
Balogun included in the Qwen scope. This repair has not been deployed.

The most recent saved Balogun call found was `AJ_9DMHBjeSu7y4`,
16:00:48–16:03:40 UTC. It has zero parallel-Qwen events and ended nine seconds
before that deployment completed at approximately 16:03:49 UTC. Its recording
failed, so there is no audio evidence to recover. It cannot validate the
currently deployed parallel implementation. No later saved Balogun call was
found in the read-only recheck.

PR #197's native CI finished with 265 passed, 7 failed and 1 ignored. Its
provider/tool fixtures still described the retired capture action and an
incorrect native Yoruba PCM response contract. This change updates those
fixtures and adds coverage of the replacement behavior.

## Runtime changes

SBPN remains authoritative for conversational meaning, normal turn boundaries,
and RVC speculation. Qwen does not perform speculative decoding. A separate
caller-only recorder collects longer audio across short primary boundaries and
pauses, closing after 1 second of silence, with a 30-second resource cap.
Each completed window produces one ordinary hinted Qwen ASR request.

Results are correlated to immutable window IDs and their actual owning primary
turns. Out-of-order completions cannot overwrite another window. Ordinary SBPN
replies do not wait for Qwen. Numeric finals wait for the relevant longer
window; any SBPN-only speculative numeric answer is replaced before release.
The model may read the existing result with `get_secondary_number_transcript`;
this never starts or flushes recording. Failure returns an unavailable result
rather than guessed digits. Model references are untrusted user/tool data,
not system instructions. Only numeric Qwen output is added to ordinary context.

The LLM chooses the complete number using
`propose_transaction_phone_number`. Rust requires an exact match to an observed
Qwen digit run (or literal typed digits), preserves leading zeros, renders each
digit in Yoruba itself and requests caller confirmation. A correction replaces
the earlier candidate. Existing confirmation requirements for ticket writes
remain. Human handoff and interruption can cancel pending model/tool work;
shutdown aborts background ASR jobs. Other agent profiles remain outside scope.

## Local tests with existing providers

The native coordinator was exercised locally against the existing SBPN, Qwen,
DeepSeek and Balogun TTS services, through a local SSH-forwarded gateway. No
production container, database, agent configuration or provider configuration
was changed. Inputs were synthetic speech and non-customer numbers.

- Leading-zero test: SBPN omitted a zero; Qwen returned `00123456789`. The LLM
  selected that exact string, and native Yoruba readback began “odo odo ọ̀kan”.
- Paused-digit test: 8.355 seconds of audio contained 10 pauses longer than
  150 ms and shorter than 1 second. One complete Qwen number window retained
  `00123456789` despite short primary boundaries.
- Correction: a Yoruba correction followed by English digits produced
  `00987654321`; readback began “odo odo mẹ́sàn-án”. It was not appended to
  the earlier number.
- The combined complaint/paused-number/correction run completed 3 Qwen requests
  with 0 failures and selected/read back both exact candidates. Qwen requests
  took approximately 489, 634 and 763 ms after window submission. Number
  replies intentionally include the 1-second quiet window plus ASR/model/TTS
  time; this is an accuracy tradeoff, not a claim of lower digit-readback latency.

An earlier provider run exposed an ungrounded free-form Yoruba readback that
changed zeros to ones. The structured selection and deterministic native
readback were added in response, and the tests above passed afterward. One
intermediate run had a transient TTS HTTP failure; the immediate health check
and subsequent complete provider runs passed.

These provider tests use a local audio sink. They validate ASR/model/TTS behavior,
not browser, network RTC or audible end-to-end latency. Human noisy-room testing
and real browser interruption testing remain review steps before release.
Raw transcripts, credentials and provider traces are kept outside the repository.

## Validation

Local validation completed on the final runtime implementation:

- All-feature release native library suite, serial: **276 passed, 0 failed,
  1 ignored** (317.66 seconds).
- All-feature release Clippy for library and binaries with `-D warnings`: passed.
- Release worker build with production `rvc` features (without `local-call`): passed.
- Lagos deployment contracts: **19 passed**.
- Cargo formatting and Git whitespace checks: passed.
- Local UI health endpoint passed; the Rust worker registered with local LiveKit.

Regression coverage includes long windows, out-of-order results,
ordinary speculative replies while Qwen is pending, missing/failed ASR recovery,
leading-zero grounding, incorrect-candidate rejection and human-transfer
cancellation. Production remains on its inspected revision until a reviewed
release through the normal pipeline.

## CI follow-up

The first Linux native run passed 275 tests but failed
`completed_hypothesis_mismatch_never_releases_prepared_wrong_audio` while
waiting for speculative TTS. Its trace showed `stt_speculative_flush_timeout`
and an authoritative ASR final request; no completed hypothesis or model attempt
had started. The test queued the final commit immediately after starting the
optional flush, so the production 80 ms final-handoff grace could legitimately
expire on the runner. The final fixture was intentionally held, so there could
be no TTS to satisfy the test.

Both completed-hypothesis validation tests now send one silence frame, observe
prepared TTS, then send the remaining silence and release the independent final.
This exercises the intended match/mismatch branches without racing optional
flush cancellation. The mismatch assertion also requires nonempty corrected
audio. No runtime timeout or safety assertion was relaxed. The separate bounded
flush-timeout and final-handoff timing tests remain unchanged.
