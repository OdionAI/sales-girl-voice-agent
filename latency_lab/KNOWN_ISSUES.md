# RVC latency-lab known issues

## Ascend RSTT can fail a session or restart its EngineCore

- Status: committed-final dependency removed from realtime; client-side
  realtime/batch overlap is now prevented. Provider repair and redundant
  deployment remain open.
- First confirmed trace: `f037b4452aea` on 2026-08-26.
- User-visible symptom: after several successful turns, the agent detects speech but stops
  transcribing and therefore produces no response.
- First provider failure: the RSTT WebSocket sometimes emits neither transcript deltas nor
  `transcription.done` after the application sends a final commit. The coordinator correctly
  declares `stt_final_timeout` after five seconds.
- The rotation/replay containment experiment was retired after traces showed it
  introduced duplicate audio and a reconnect race without repairing EngineCore.
  Realtime Qwen is now optional and supplies only partials for speculation.
  Local VAD owns the utterance buffer and the Qwen batch endpoint supplies the
  sole committed transcript, so a missing realtime `transcription.done` cannot
  block the LLM response.
- Impact in the confirmed trace: turns 2, 4, 8, and 9 timed out; no recovery completed. Later
  speech reached VAD, but no usable final transcript reached the LLM.
- Evidence:
  - `latency_lab/artifacts/traces/f037b4452aea.jsonl`
  - `latency_lab/artifacts/reports/f037b4452aea.md`

Later evidence in trace `5cb35b9ef494` shows an explicit Ascend
`processing_error`, followed by a websocket close and three HTTP 502 handshake
responses. The batch-authoritative boundary prevents that sequence from
blocking committed transcription, but
the upstream EngineCore stack trace, process/NPU memory state, worker restart,
and gateway readiness behavior still require provider-host observability. A
second independent ASR worker is also still required before a single provider
process failure can be treated as fully redundant.

Trace `d4cfc6bc043f` exposed a second, concrete failure mode. The realtime
WebSocket and `/audio/transcriptions` batch request overlapped on the same
Qwen/vLLM process; immediately after the batch request returned, EngineCore
asserted in `async_scheduler.py` on `request.num_output_placeholders >= 0` and
the process restarted. RVC now uses one inference-mode lock: at local
end-of-speech it closes and awaits the realtime receiver, runs the authoritative
batch request alone, then reconnects realtime. Trace events
`stt_inference_handoff_start`, `stt_realtime_quiesced`, and
`stt_realtime_resume_complete` validate this ordering on every turn. This
contains the observed concurrency trigger, but it is not a substitute for an
upstream vLLM fix or a second ASR replica.

## Loud background noise mistaken for speech; an empty ASR final froze the turn ~21 s

- Status: FIXED on branch `fix/rvc-vad-empty-final` (2026-10-31); pending review/deploy.
- Confirmed traces: `f6eae00fb7ca` (11:04 call — total silence) and
  `71e9dd9c7d63` (11:01 call — ~40 s dead air), business `f2ff0fe3…` (MoMo PSB),
  agent Yinka.
- Symptom: caller speaks, the agent goes silent and "cannot hear" them; the caller
  repeats 2–3 times. On a fresh call the greeting is cut off ~1.4 s in and the
  agent never replies until the session ends.
- Root-cause chain:
  1. The VAD was a loudness gate only (`speaking = rms >= speech_rms(280)`); the
     RNNoise per-frame speech probability was computed but discarded, so loud
     non-speech (RMS 1,600–1,800 with speech_prob 0.08–0.19 in the trace) counted
     as speech.
  2. `min_speech_ms=240` + `end_silence_ms=300` turned a ~0.3 s noise blip into a
     ~0.6 s "turn"; the batch ASR returned `EmptyTranscript`.
  3. For English (`RSTTClient`) an empty authoritative final was NOT reported to
     the pipeline (only the pidgin/SBPN client had `on_final_unavailable`), so the
     turn stayed in `awaiting_final`; every later word was dropped
     (`decoder_replay_cannot_revise_turn_after_final_requested`) until
     `stt_final_timeout` (20 s + 1 s) fired.
- Fix (this branch):
  - `input_noise.py`: expose `last_speech_prob` from RNNoise per processed chunk.
  - `pipeline.audio`: when a speech probability is supplied, also require
    `speech_prob >= RVC_LAB_SPEECH_PROB_MIN` (default 0.35).
  - `pipeline._speech_ended`: optional min-utterance guard
    (`RVC_LAB_MIN_UTTERANCE_MS` / `RVC_LAB_MIN_VOICED_FRAME_PERCENT`, default off)
    discards a near-empty buffer before ASR.
  - `providers.RSTTClient`: track the batch result and call `on_final_unavailable`
    (a new lock-safe `pipeline.on_final_empty`) when the final is empty, so the
    turn returns to listening immediately instead of waiting out the deadline.
  - Tests: `latency_lab/tests/test_vad_noise_gate.py`.
- Evidence: `latency_lab/artifacts/traces/f6eae00fb7ca.jsonl`,
  `latency_lab/artifacts/traces/71e9dd9c7d63.jsonl`.
