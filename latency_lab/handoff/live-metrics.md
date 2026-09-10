# Hybrid LiveKit metrics

The hybrid worker publishes `odion.voice_lab.metric` JSON on the reliable
`odion.voice_lab.metrics` LiveKit data topic, addressed only to the dispatched
caller. The existing dashboard call hook consumes this contract. No public
metrics port or browser credentials are required.

## Boundaries

- STT delay: server VAD turn commit to accepted final transcript. It excludes
  the preceding silence window and is not microphone-to-transcript latency.
- LLM TTFT: each LLM stream request to first text delta, scoped by attempt and
  phase. Tool execution and prior generations are not included.
- TTS TTFA: each phrase request to first nonempty PCM chunk.
- Server first audio: accepted final transcript to first authorized PCM frame
  enqueued into LiveKit. Speculative buffered audio is not counted until release.
- TTS RTF: completed phrase stream wall time / generated audio duration. This
  includes output backpressure; it is not a model-only GPU throughput benchmark.
- Browser first audio remains unavailable: enqueueing server PCM does not prove
  browser playback or speech-end-to-audible latency.

Missing clocks remain null. Opening greetings are excluded from caller-turn
averages. Cancelled/incomplete streams do not produce completion/RTF samples.
Trace payloads are allowlisted; prompts, tool arguments/results and credentials
are never forwarded. Queues and correlation state are bounded; slow or failed
metric delivery cannot block audio. Closing a call removes its trace listener
and cancels its publisher after a bounded drain.

## Verification and rollout

1. Run `python -m unittest discover -s latency_lab/tests` in the worker environment.
2. Build the worker from the intended deployed source. For an existing deployment
   with additional patches, apply only this change after `git apply --check`;
   do not replace the source tree with an older checkout.
3. Keep the prior image and source backup for rollback. Wait for callers to leave
   before recreating only the voice container, with no dependent-service restart.
4. Start a new Production call in Voice Lab. Speak a complete phrase, wait for the
   response, and confirm a retained turn plus STT/LLM/TTS/server-first-audio samples.
   Text chat can verify packet forwarding but cannot verify STT delay.
5. Complete a phrase to verify RTF, then interrupt another response and repeat.
   Ending the call logs published, failed and dropped telemetry counts.

Existing calls do not acquire new worker code. Trace files from old calls remain
available, but this change does not backfill them into the live panel or central
conversation metrics storage.
