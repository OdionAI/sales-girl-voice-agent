# VAD noise gate + empty-final recovery — fix and test guide (2026-10-31)

Branch: `fix/rvc-vad-empty-final` (based on `origin/main` = deployed `04bbb2c`).
Scope: `sales-girl-voice-agent` → `latency_lab/` (the RVC LiveKit worker that serves
public calls). No dependency, contract, or config-format change.

---

## 1. What was wrong (plain language)

On the 11:04 Momo PSB call the agent went completely silent, and on the 11:01 call it
went deaf for ~40 seconds. The chain behind both:

1. **The agent judged "is the caller talking?" by loudness alone.** Anything louder
   than RMS 280 counted as speech. RNNoise already computes a proper speech
   probability per frame, but the code threw that number away. So a loud TV / music /
   room-noise burst (RMS ~1,700, speech-probability only ~0.1) was treated as talking.
2. **End-of-turn timers are very short** (240 ms to confirm speech, 300 ms of silence
   to end it), so a ~0.3 s noise blip became a ~0.6 s "turn". The speech-to-text
   returned **empty** for that clip.
3. **For English the empty result was never surfaced to the turn logic.** Only the
   pidgin voice path had a callback for "final unavailable." So the turn sat in
   `awaiting_final` and every word the caller said next was discarded
   (`decoder_replay_cannot_revise_turn_after_final_requested`) until a **20-second**
   safety timer fired. That timer expiry is the silence the caller hears.

Net: noise → false "speech" → too-short clip → empty transcript → 21 s deaf turn.

---

## 2. What changed (files)

| File | Change |
|---|---|
| `latency_lab/input_noise.py` | RNNoise path now exposes `last_speech_prob` (mean probability for the processed chunk). WebRTC-APM / disabled paths set it to `None`. `ListeningNoiseRoute` forwards it too. |
| `latency_lab/config.py` | New knobs: `speech_prob_min` (`RVC_LAB_SPEECH_PROB_MIN`, default **0.35**), `min_utterance_ms` (`RVC_LAB_MIN_UTTERANCE_MS`, default **0** = off), `min_voiced_frame_percent` (`RVC_LAB_MIN_VOICED_FRAME_PERCENT`, default **0** = off). |
| `latency_lab/pipeline.py` | `audio()` takes `speech_prob`; a frame must clear both the loudness floor **and** `speech_prob_min` to count as speech. `_speech_ended()` discards a below-floor buffer before ASR (when enabled). New `on_final_empty()` returns the turn to `idle`/listening and cancels the deadline. `RSTTClient` is constructed with `on_final_unavailable=self.on_final_empty`. |
| `latency_lab/providers.py` | `RSTTClient` accepts `on_final_unavailable`; `_transcribe_batch` returns whether it produced a final; the batch path (after the inference lock) and the realtime-final path notify the pipeline on an empty final via `_notify_final_unavailable()`. |
| `latency_lab/livekit_worker.py` | Passes `noise_filter.last_speech_prob` into `pipeline.audio(...)`. |
| `latency_lab/tests/test_vad_noise_gate.py` | New regression tests (8). |
| `latency_lab/KNOWN_ISSUES.md` | Resolved-issue entry with trace evidence. |

### Lock-safety note
`on_final_empty()` deliberately does **not** call `stt.recover()` (unlike the SBPN
`on_stt_final_unavailable()`). The batch path already reopens the realtime socket in
its `finally`; calling `recover()` from inside the batch inference lock would deadlock.

---

## 3. Latency impact (nothing added on the response path)

- Speech-probability gate: **free** — the number is already computed; no extra model
  work, and it only runs when a probability is supplied (RNNoise path).
- Empty-final recovery: **improves** the failure path; zero effect on normal turns.
- Min-utterance guard: **zero** on normal turns (only skips sub-floor blips).
- No change to `end_silence_ms` (still 300 ms), so no added per-turn wait; the
  LLM→TTS critical path (~530 ms first audio, ~190 ms batch ASR) is untouched.

---

## 4. Tests

Local, from the worktree root:

```bash
python3 -m pytest latency_lab/tests/test_vad_noise_gate.py -q     # new: 8 pass
python3 -m pytest latency_lab/tests/ -q                            # full: 585 passed, 12 skipped
```

The 8 new tests assert:
- a loud frame with low speech probability does **not** open a caller turn;
- a loud frame with high probability **does**;
- with no probability (APM/off) behaviour is unchanged (energy fallback);
- a below-floor committed buffer is discarded before ASR (no `commit`);
- a sufficient buffer still commits;
- `on_final_empty()` returns to listening, clears the turn, cancels the deadline;
- `RSTTClient` notifies on an empty batch final and does not on a successful one.

---

## 5. Recommended deploy env (only what is not already the code default)

The fix is active once this image runs: `RVC_LAB_SPEECH_PROB_MIN` defaults to 0.35 in
code and RNNoise is already on (`RVC_INPUT_NOISE_SUPPRESSION=1`, `...=rnnoise`). No env
change is strictly required. Optional tuning, to apply only if a live call still shows
an issue:

| Var | Current | Suggested | Why |
|---|---|---|---|
| `RVC_LAB_MIN_UTTERANCE_MS` | (unset → 0) | `150` | Safety floor: drop <150 ms garbage that slips past the prob gate. |
| `RVC_LAB_STT_BATCH_TIMEOUT_SECONDS` | `20` | `5` | Empty finals now recover instantly; a shorter deadline only bounds a truly hung request. |
| `RVC_LAB_END_SILENCE_MS` | `300` | leave `300` | Do NOT lengthen: the prob gate removes the blip problem without adding per-turn delay. |
| `RVC_LAB_SPEECH_RMS` | `280` | leave `280` | Raising it risks missing soft-spoken callers; the prob gate is the right lever. |

---

## 6. How to validate with a real call (after owner-approved deploy)

Re-test the **exact** failing situation, not just any call (an easy call also passes
on the old code):

1. Start a call to the MoMo PSB / Yinka agent, let the greeting play.
2. Introduce background noise (TV / music / talking nearby) **or** speak in short
   bursts with ~0.3–0.5 s pauses between them.
3. Expected (fixed): the agent keeps listening and answers; if a burst produced no
   words it drops that turn and re-listens within well under a second — it does **not**
   go silent for 20 s.
4. Then complete a normal task (e.g. "I want a debit card" then "I was debited but the
   recipient wasn't credited") and confirm it still responds and can create a ticket.

In the per-call trace (`/app/latency_lab/artifacts/reports/<trace>.md`, filename is the
12-hex trace id printed on the worker's stdout as `RVC LiveKit latency report: …`):

- **Pass signals:** no long gap between `user_speech_end` and the next assistant audio;
  no `stt_final_timeout` on a turn; if a turn did hit an empty final you will see
  `stt_final_empty_recovered` (turn returned to listening) instead of a 21 s stall;
  `input_noise_filter_metrics` shows the low `speech_prob` windows were not treated as
  speech.
- **Still-broken signal:** `decoder_replay_cannot_revise_turn_after_final_requested`
  followed ~20 s later by `stt_final_timeout` on the same turn.

---

## 7. Rollback

Revert the image to the previous deployed tag (`attentive/voice:ci-04bbb2c3…`). No DB
migration, no config format change, so rollback is image-only.

---

## 8. Not done here (deliberately)

- Not deployed. This is the implement → test → owner-approval step; a Lagos deploy runs
  from `main` via GitHub Actions (`deploy-lagos.yml`), and this is a behavior change
  that needs your sign-off plus a validation call.
- `end_silence_ms` left alone; `speech_rms` left alone (see §5).
- Native Rust worker parity: this fix is for the Python `latency_lab` worker that is
  live today; the Rust worker cutover is separate.
