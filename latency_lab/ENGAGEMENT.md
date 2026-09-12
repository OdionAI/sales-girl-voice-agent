# Contextual engagement

Status: opt-in local evaluation. `RVC_LAB_ENGAGEMENT_ENABLED=false` by default.

The timer decides when to reconsider; the model decides whether and what to
say; the RVC runtime decides whether it is still safe to play it. LiveKit remains
PCM/data transport. No `AgentSession` orchestration or additional model/provider
is introduced.

## Behavior

- A committed request can start its tool while its model-generated acknowledgement
  is still being synthesized/played. Speculation cannot execute tools.
- Pending tools get bounded, tool-free model reconsideration opportunities. Fast
  tools need no filler. Updates may describe only actual pending work/results.
- Idle gaps can trigger one contextual check-in, or silence. There is no canned
  progress/idle utterance. Invalid output and timeouts produce no optional audio.
- Quiet means listening-only, not input suppression. Final ASR text is published
  and persisted immediately, before response authorization. Partials still appear.
  Quiet-period speech remains in memory as listening-only reference, not requests
  or confirmations to execute later. Long listening periods can be compacted with
  that provenance; normal context-size limits still apply.
- A lightweight model preference review authorizes responses off the ASR receive
  loop. It can suppress even the initial acknowledgement of a silence request.
  New speech can interrupt a pending decision; stale responses cannot start.
  While quiet is pinned, background dialogue and ambiguous speech preserve quiet.
  Natural resume requests work without a fixed wake phrase, unless the caller
  explicitly specified one. This is contextual filtering, not speaker identity
  detection or acoustic noise suppression. Microphone mute still stops input.
- Quiet is stored outside compacted chat history. Time passing cannot clear it.
  `until_user` suppresses check-ins; `until_result` suppresses pending updates.
- One speech lock prevents overlapping synthesis/playback streams. Optional
  phrases are buffered and checked again against turn/revision/user speech before
  playback. A result arriving during preparation discards the stale update.
- Barge-in cancels speech, not an already-dispatched request. Results remain in
  memory. Pending requests are not duplicated. Unexpected execution exceptions
  pin an unknown outcome and block automatic retries/new dispatch in that session.
  Existing connector-specific failure handling and confirmation rules are unchanged.
- A progress segment draining is not the end of the enclosing tool turn. Idle
  updates create assistant-only history, not fabricated customer turns.

The Wema balance loop also supports the real account-list response (`needs_input`
with valid owned account entries). It permits bounded sequential lookups rather
than stopping after four rounds. If the model promises the next balance but omits
its function call, a bounded read-only continuation decision can select a missing
requested account. It cannot select a transaction tool or replay an account already
returned through that continuation. Auth failures and invalid lists still stop.

## Isolated run

Create an isolated checkout from the reviewed RVC base. Install the pinned runtime:

```bash
python3 -m venv .venv
.venv/bin/pip install -r latency_lab/requirements.txt -c latency_lab/constraints-livekit.txt
```

Use the existing approved service/model/LiveKit environment. Do not put credentials
in this document, shell history, trace attachments, or Git. Select a **unique**
worker name and configure the local dashboard to dispatch that exact name:

```bash
export RVC_LAB_ENGAGEMENT_ENABLED=true
export RVC_LAB_LIVEKIT_AGENT_NAME=rvc-contextual-engagement-local
export RVC_LAB_LIVEKIT_WORKER_PORT=8193
.venv/bin/python -m latency_lab.livekit_worker start
```

Never register the experimental worker under a production worker name. Models,
TTS settings, caller identity, confirmations and saved voice-auth policy must stay
the same as the baseline. The local dashboard must honor the existing voice-auth
setting for both RVC transport modes; do not turn off server authentication to test.

Defaults in `.env.example`: first pending opportunity 0.3 s, later opportunities
8 s, idle 18 s, decision/audio preparation deadline 6 s, at most 3 pending checks
per operation and 1 idle check per caller turn, 8 banking rounds (clamped 4-12).
These are reconsideration thresholds, not guaranteed speech deadlines. The caller
preference review adds a model request per committed utterance and must finish
before response release. Reply preparation now overlaps that review using RVC's
existing speculative buffer, including during quiet mode. Audio, assistant
transcripts and tool execution remain blocked until approval. A rejected or stale
review discards the prepared response. This can spend LLM/TTS work on replies that
will never play; measure contention, usage and response latency before enabling
broadly. It is not a zero-latency guarantee. A new committed utterance cancels an
older pending preference review rather than waiting behind it.

### Per-call A/B comparison

On the isolated dashboard, set `PUBLIC_AGENT_ENGAGEMENT_PREVIEW_ENABLED=true` and
restart its development server. Keep `RVC_LAB_ENGAGEMENT_ENABLED=true` on the
isolated voice worker. Open the local caller page and change the **Contextual
engagement** checkbox before starting a call:

- Checked: quiet/listening control, contextual tool updates and idle checks on.
- Unchecked: baseline response path, without the additional caller preference
  review or contextual engagement scheduler. Deterministic quiet/listening
  enforcement is also off; the normal agent prompt still applies.

End the call before changing modes. Use the same model, voice, caller and test
phrases in both calls. The checkbox is hidden during calls and outside an opted-in
development preview; production ignores this public override. The signed session
passes `engagement_enabled=false` (boolean or string) to opt out. It cannot enable
an operator-disabled worker flag. Existing clients that omit it keep the worker's
configured behavior. This is an operator A/B control, not a model-callable tool,
so conversation content cannot accidentally switch the test mode.

Rollback: stop only the isolated worker, or set the feature flag false before
starting new sessions. Do not restart an unrelated live service. A process-level
flag change does not rewrite an already-running session.

## Verification

```bash
.venv/bin/python -m unittest discover -s latency_lab/tests
git diff --check
```

Coverage includes concurrent tool/speech lifetimes, no speculative execution,
barge-in retention, no duplicate pending requests, uncertain outcomes, stale
LLM/TTS cancellation, bounded decisions, quiet after history compaction, background
dialogue while quiet, explicit resume, five sequential balances, read-only
continuation, failed authentication, and old LiveKit segment completion races.

Manual acceptance sequence:

1. Use a dedicated local worker and the existing authorized Wema test caller.
2. Ask: "Check all five account balances one by one. Retrieve my account list
   first, then report each balance before checking the next."
3. Verify one account-list lookup and five actual balance results. Spoken promises
   alone do not count. Check audio continuity, barge-in, and final playout drain.
4. Ask the agent to keep listening silently until told it can talk. Speak 3 facts,
   wait beyond the idle timer, then play unrelated dialogue. All recognized speech
   must remain visible in the transcript without replies or new tool execution.
5. Say "Okay, now you can talk. What did I just tell you?" Verify it recalls those
   facts. Repeat while a preference review is slow and with ordinary resume
   paraphrases. Repeat with an explicit wake phrase and respect that condition.
6. Use controlled delayed read-only work to exercise progress updates; short bank
   responses may never require a timed update. Test result-arrival/caller-speech races.

Local evaluation on 2026-09-12:

- Five-account web test completed: 6 distinct retained tool results (list + 5
  balances), 0 generation errors, final playout drain after 78.36 s. Initial
  acknowledgement reached LiveKit enqueue in 1.175 s. No banking mutation was tested.
- This was a text-submitted request in a real web call with configured LLM/TTS and
  banking services, not a clean microphone-input test. The trace had no browser
  audible-playback acknowledgement; enqueue is not proof of what the user heard.
- A controlled test with the configured LLM/TTS produced a contextual update while
  a read-only test operation was pending, stayed quiet past 20 s, rejected four
  movie-style utterances, then accepted an explicit request to resume.
- Repeated after the caller-turn gate: again 6 distinct results and 0 generation
  errors. First audio enqueue was 3.793 s and final drain 125.74 s; the model chose
  a longer response with a repeated summary. These runs do not establish latency
  equivalence or an immediate-acknowledgement guarantee.
- In that updated web call, a quiet request was pinned, the supplied movie dialogue
  was rejected as unaddressed, and no reply/tool followed. The subsequent explicit
  request to resume was accepted. These were text inputs through the call's same
  committed-turn path. Repeat the acoustic movie/microphone test before rollout.
- Regression suite: 225 tests run, 12 skipped, no failures. The additional dashboard
  saved-policy test passed. No SDK build or production deployment was performed.

Listening-only correction on 2026-09-12:

- The earlier unaddressed-turn test incorrectly expected discarded history. Quiet
  now gates output/actions after transcript retention, not incoming speech. The
  preference review also no longer blocks ASR from receiving another utterance.
- An actual-provider test fed synthesized caller speech through the configured
  streaming ASR and pipeline. The agent stayed silent while retaining a green
  notebook, Friday meeting, and 5 guests, stayed silent beyond the idle timer, then
  recalled all 3 details on natural-language permission to resume. This verifies
  ASR/model/TTS behavior with synthetic speech, not a physical microphone or what
  a caller hears over WebRTC.
- Regression suite: 234 tests run, 12 skipped, no failures. Coverage includes
  immediate publication/persistence, recognition during delayed reviews, quiet
  partials, interruption, shutdown, recall context, and compaction provenance.
- The updated web caller also retained all 3 facts and replied on "Okay, now you
  can talk" with "Your notebook is green, the meeting is on Friday, and there will
  be 5 guests." The trace recorded 0 audio chunks and 0 tool starts between the
  silence and resume requests. This was a text-input web-call test in addition
  to the synthetic-speech ASR test above. Physical microphone acceptance remains
  for the caller to repeat; browser-visible transcript is not proof of audibility.
- Restored the stopped loopback-only service tunnel. LiveKit was still trying to
  dispatch to an unreachable old local worker registration, so the local worker
  and dashboard now use the fresh name `rvc-silent-listening-local`. No shared
  LiveKit service, production worker, or deployment configuration was restarted.

Latency correction on 2026-09-13:

- Earlier traces showed about 1 second of caller preference review before reply
  generation began; one background review took 5.437 seconds. New resume requests
  no longer queue behind such an old review. Trace decisions now include their
  turn ID and review duration.
- Three samples per mode using the same configured LLM/TTS measured median time
  from committed text to the first server audio chunk as follows:

  | Mode | Before | After |
  | --- | ---: | ---: |
  | Engagement off, ordinary reply | 1.263 s | 1.230 s |
  | Engagement on, ordinary reply | 2.136 s | 1.868 s |
  | Engagement on, natural resume and recall | 2.092 s | 1.955 s |

  This is a small local sample, not an acoustic/WebRTC end-to-end latency claim.
  The resume improvement is modest and about 0.7 seconds of overhead remains
  compared with the ordinary baseline. Two concurrent model requests still use
  shared serving capacity; the optimization does not remove the review request.
- Regression suite: 238 tests run, 12 skipped, no failures. Additional coverage
  checks cancellation of slow reviews, buffered reply reuse, no quiet-mode audio
  or assistant transcript leaks, no premature tool dispatch, baseline bypass,
  and per-session opt-out without enabling a disabled master flag.
- Dashboard hybrid tests: 45 passed. Production build and scoped lint passed
  (3 pre-existing image warnings). The checkbox was checked in mobile and desktop
  layouts, is absent during calls, and both settings reached the local worker.
- Web-call verification with typed input: off returned the baseline answer; on
  retained the notebook, meeting and guest count while quiet, then recalled all
  3 on "Okay, now you can talk." There were 0 audio chunks during the quiet turns,
  0 tool starts and 0 generation errors. Resume reached the first server audio
  chunk in 2.517 s in this full configured-agent call, distinct from the smaller
  benchmark above. Microphone was muted for controlled submissions; incidental
  pre-mute speech means this is not a clean acoustic latency measurement.
- Two repeated synthetic-speech ASR checks did not finish: a short sentence
  failed the existing 240 ms continuous-speech gate and never produced a final
  transcript. Earlier utterances were retained silently. No VAD threshold was
  weakened to make this pass. Physical microphone acceptance and this short-speech
  edge case remain unverified by the current change.

## Release gate

Do not deploy this checkout over a running image without comparing its source
and release manifest. The inspected Lagos runtime contains additional SIP/AICC
handoff and closing-intent changes not present in the Git base used here. Preserve
and reconcile those first. The Git `dev` branch and RVC `main` history also differ
substantially; a promotion must not sweep unrelated changes into the old stack.
No deployment targets, secrets, live services, SDKs, or production dashboard layout are changed
by this feature.
