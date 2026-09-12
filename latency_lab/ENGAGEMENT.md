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
- A lightweight model preference review runs alongside ordinary turns. The next
  turn observes that review before proceeding. While quiet is pinned, incoming
  speech must be judged directed to the assistant before replies or tools start.
  Background dialogue and ambiguous speech preserve quiet. This is contextual
  filtering, not speaker identification or acoustic noise suppression.
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
preference review adds a model request per committed utterance; measure contention
and usage alongside normal response latency before enabling broadly.

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
4. Ask the agent to stay quiet until another question. Wait beyond the idle timer,
   then play unrelated dialogue. Neither the timer nor that dialogue should resume it.
5. Address the assistant explicitly and ask a new question. Verify it resumes.
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

## Release gate

Do not deploy this checkout over a running image without comparing its source
and release manifest. The inspected Lagos runtime contains additional SIP/AICC
handoff and closing-intent changes not present in the Git base used here. Preserve
and reconcile those first. The Git `dev` branch and RVC `main` history also differ
substantially; a promotion must not sweep unrelated changes into the old stack.
No deployment targets, secrets, live services, SDKs, or dashboard layout are changed
by this feature.
