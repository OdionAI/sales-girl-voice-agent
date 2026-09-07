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
