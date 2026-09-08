# RVC natural-conversation latency lab

This lab lives in `sales-girl-voice-agent` on the `speculative-generation` branch. It does **not** replace the production LiveKit worker (`python main.py` / `make run`). Production SIP and room dispatch stay on that worker.

This is the experimental speculative-generation pipeline for measuring the natural-conversation critical path using the Ascend realtime STT (`RSTT`), streaming LLM (`RLLM`), and streaming TTS (`RTTS`). Voice Lab loads each session-scoped agent configuration, supports read-only retrieval from only its attached knowledge bases, and exposes its configured ticket, email, and live-call hang-up actions.

## Ownership and release boundary

```mermaid
sequenceDiagram
    participant B as Browser mic/VAD playback
    participant C as One turn coordinator
    participant R as Qwen realtime (optional)
    participant F as Qwen batch final
    participant L as RLLM
    participant T as RTTS

    B->>R: continuous PCM16 at 16 kHz
    R-->>C: optional timestamped partials
    C->>L: stable partial starts speculation
    L-->>C: streamed tokens
    C->>T: first complete phrase immediately
    T-->>C: PCM held in memory
    B->>C: silence window declares speech end
    C->>F: locally buffered utterance WAV
    F-->>C: authoritative final transcript
    alt speculative input still valid
        C->>B: release held PCM
    else transcript changed
        C->>L: cancel and regenerate from final
        C->>T: synthesize final phrases
        C->>B: stream authorized PCM
    end
    opt LLM explicitly requests missing business knowledge
        C->>C: wait for committed final authority
        C->>B: release short "Let me check" acknowledgement
        C->>C: retrieve only attached knowledge bases
        C->>L: one system message with retrieved context
        L->>T: verified follow-up phrases
        T->>B: stream follow-up PCM
    end
    opt LLM selects a configured action
        C->>C: prepare validated action arguments
        C->>B: ask for explicit confirmation after final authority
        B->>C: separate committed yes/no turn
        alt caller confirms
            C->>C: call conversation-service API once
            C->>B: speak success only after API success
        else caller rejects or is unclear
            C->>B: cancel or repeat confirmation
        end
    end
    opt committed caller turn clearly closes a SIP call
        L-->>C: end_call function with no arguments
        C->>T: deterministic short goodbye
        T->>B: drain final goodbye completely
        C->>B: delete LiveKit room; SIP edge emits BYE
    end
    B-->>C: actual playback start/stop timestamps
```

The coordinator is the only application-turn owner. Qwen realtime supplies optional partial evidence for speculation, but its `transcription.done` is never accepted as the committed final. Local VAD buffers exactly the caller-owned utterance and sends it to Qwen's batch endpoint at end-of-speech; only that batch transcript may authorize playback. Because the deployed realtime and batch routes share one Qwen/vLLM EngineCore, the coordinator first closes and awaits the realtime socket, runs the authoritative batch request alone, and reconnects realtime after that request finishes. This prevents the two decode modes from overlapping without adding a fixed delay. Speculation may prepare LLM text and TTS audio early, but only comparison with the batch final authorizes browser playback. A changed partial cancels the active attempt and logs the exact reason, similarity, and discarded audio.

## Run

From the `sales-girl-voice-agent` repository, on `speculative-generation`:

```bash
python3 -m venv .venv-latency-lab
source .venv-latency-lab/bin/activate
pip install -r latency_lab/requirements.txt
python -m latency_lab.server
```

Dashboard Voice Lab (`rvc_style`) talks to `http://127.0.0.1:8010`. The production LiveKit agent is still `make run`.

Open `http://127.0.0.1:8010`, click **Start call**, have a short multi-turn conversation, then click **END CALL**. The RVC pipeline is unchanged; the browser shell reuses Helen's public-agent visual language and existing dashboard assets so UI differences do not affect the comparison. Open the transcript control during or after the call to view the conversation and download the **Latest report**.

### Increment 1: LiveKit transport only

The `feature/rvc-core-livekit-transport-v1` branch adds a manual LiveKit PCM
adapter around the same RVC coordinator. LiveKit subscribes to microphone audio
and publishes the authorized RTTS frames, but it does not own endpointing, turn
state, STT, LLM, TTS, speculation, or playback authorization. `AgentSession`,
knowledge, and tools remain outside this experiment.

### Increment 2: agent config and explicit knowledge retrieval

The `feature/rvc-explicit-knowledge-v2` branch loads the agent and business
IDs encoded in the LiveKit room, fetches that exact agent's runtime config, and
uses only its attached `knowledge_base_ids`. Speculative generation always tries
the saved prompt and normal conversation first. It does not prefetch knowledge.
Only when the LLM explicitly returns a short, first-person lookup
acknowledgement does the coordinator wait for final turn authority, release one
of several rotating natural spoken acknowledgements, and start the scoped
retrieval. "Let me check that for you" remains the preferred routing signal,
while tightly scoped natural variants such as "One moment while I confirm that"
are accepted. Advice telling the caller to check something and completed-answer
phrases such as "I checked" or "I can confirm" do not trigger retrieval. The
model's routing phrase is not sent to TTS. The retrieved
context is merged into the existing system prompt for a second LLM answer, so
the Qwen gateway receives exactly one system message. Agents with no attached
knowledge bases never fall back to an account-wide search.

Saved agent instructions become the runtime prompt after a successful config
load, with an explicit knowledge-only capability boundary for this increment.
Configuration loading occurs at session startup and is reported separately
from turn latency.

The loader accepts the platform-standard `AGENT_CONFIG_SERVICE_BASE_URL` used
by the dashboard/runtime environment. `AGENT_CONFIG_API_BASE_URL`, when set,
overrides it for an isolated experiment. This keeps mapped SIP calls from
silently falling back to the lab prompt and losing their configured tools.

### Increment 3: confirmed internal actions

The current branch exposes native LLM function definitions only for an agent's
configured `internal://create-ticket` and `internal://send-email` tools. A stable
partial may prepare the function name and arguments, but it cannot execute an
API call. After that caller turn is committed, the coordinator asks a
deterministic confirmation question. Only an affirmative, separately committed
caller turn authorizes one request to the conversation service. Rejection
cancels the pending action, unclear evidence repeats the confirmation, and
spoken success is generated only from a successful API result.

Generic HTTP tools, transfers, and transactional banking actions remain outside
this increment. Trace events record tool preparation, final-authority waiting,
confirmation classification, API start/completion, elapsed time, and result
keys without recording email bodies or other argument values.

### Increment 4: AICC number routing and end-call

For a LiveKit SIP participant, the worker reads `X-Callee-Number` or
`X-Odion-Callee-Number`, falling back to the SIP phone/From identity used by the
existing AICC POC. `POC_OUTBOUND_NUMBER_AGENT_MAP` maps the normalized Nigerian
number to the saved business and agent configuration before the prompt and
tools load. Non-SIP browser participants cannot activate this map.

When that saved agent has active `builtin://end_call`, RVC exposes a native
zero-argument `end_call` function. Speculation may prepare it, but only the
committed caller closing turn authorizes it. A deterministic final-transcript
gate independently requires an explicit farewell, request to hang up, or clear
statement that the caller is finished; a mistaken LLM tool selection is vetoed
and regenerated as a normal response with `end_call` unavailable for that turn.
RVC then synthesizes one
deterministic goodbye, waits for LiveKit playout to drain, and deletes the room;
the existing LiveKit SIP edge is responsible for emitting BYE to AICC. Trace
events cover the map decision (only a phone suffix), authorization, goodbye
playout completion, and room deletion.

Once the remote participant, microphone track, and RVC output track are ready,
the worker proactively speaks one short greeting using the saved agent name.
This opening is recorded as `opening-0000`, adds only an assistant message to
conversation history, and does not create a fake caller transcript or change
the measured caller-turn response path. Set
`RVC_LAB_OPENING_GREETING_ENABLED=false` to disable it.

With the dashboard running on port 3000, start the transport worker:

```bash
python -m latency_lab.livekit_worker dev
```

Then call Helen at
`http://localhost:3000/zenith-bank/agt_zenith_helen_demo`. The default worker
name matches the dashboard's localhost dispatch contract. Override it with
`RVC_LAB_LIVEKIT_AGENT_NAME` if the dashboard is configured differently.

The raw trace and Markdown report use the same artifact directories as the
browser baseline. LiveKit runs additionally record track readiness, first input
PCM, output-frame enqueue time, queue depth, interruption clears, and playout
queue completion. The Helen browser sends a reliable `rvc_latency_ack` data
message when remote audio energy crosses its playback threshold. Reports then
separate RVC→LiveKit enqueue time from LiveKit enqueue→browser-audible time;
without that acknowledgment, the report labels the boundary as enqueue rather
than claiming the audio was already audible.

The committed-final path no longer depends on realtime `transcription.done`.
Healthy microphone PCM is still offered directly to the realtime websocket so
stable partials can start speculation, but websocket errors or closure only
disable that optional acceleration. At local end-of-speech, RVC quiesces that
socket and awaits its receive loop before issuing the batch request, so the two
routes never infer concurrently on the shared EngineCore. Local VAD independently
buffers the confirmed caller utterance, wraps its PCM16 as mono 16 kHz WAV, and
posts it to `RVC_LAB_STT_BATCH_URL`; realtime reconnects after the batch request
completes. The default batch deadline is eight seconds, with the application
final timeout one second beyond that provider deadline. Realtime finals are
logged as non-authoritative and ignored. See
[`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) for the remaining provider-side EngineCore
investigation.

If the gateway is only reachable through a tunnel, override the three endpoint variables in `.env.example` before launching. Environment variables are read directly; the lab deliberately does not load Helen's environment.

The controlled TTS experiment requests `initial_codec_chunk_frames=16`. This asks the
gateway for enough initial PCM to clear the unchanged 120 ms browser prebuffer and
continue playing without the startup underrun seen with the gateway's smaller default.
The lab's default system prompt makes Helen a concise Zenith Bank Nigeria customer-service
agent with a small embedded FAQ for USSD, account opening, loans, digital channels, and
official support. A successfully loaded saved agent prompt replaces this fallback, and
attached knowledge retrieval can ground the turn. The lab still has no account access or
transaction tools beyond configured support tickets and outbound email. A
configured SIP agent may also end its current call after a committed closing
turn. `RVC_LAB_SYSTEM_PROMPT` can override the fallback prompt without changing
the measured pipeline.

The LLM client retries gateway 502/503/504 and pre-token connection failures on
a bounded 0.3/1.0/2.0-second schedule. Healthy requests take the original
single-request path with no added delay. If a committed turn still cannot reach
the LLM after those retries, the agent speaks a deterministic temporary-service
message instead of leaving the caller in silence; traces distinguish the
provider error, degraded response, and completed fallback playout.
The current end-of-turn experiment uses 300 ms of silence instead of 500 ms. When
`RVC_LAB_TTS_REF_AUDIO` and its exact `RVC_LAB_TTS_REF_TEXT` are supplied, the lab
uses fixed ICL Base synthesis rather than generating a new VoiceDesign speaker.
The lab defaults to the gateway-cached ICL clone `helen-mavino-0030`.
`RVC_LAB_TTS_VOICE` can select a different voice enrolled once through the
gateway's `/v1/audio/voices` endpoint. Named voice requests do not resend the
reference WAV or transcript for each phrase.

## Trace and report artifacts

- Raw append-only trace: `latency_lab/artifacts/traces/<session-id>.jsonl`
- Shareable report: `latency_lab/artifacts/reports/<session-id>.md`
- Latest report endpoint: `http://127.0.0.1:8010/reports/latest`

Every event contains a sequence number, UTC wall time, monotonic nanoseconds, session ID, turn ID, attempt ID, state transition, reason, and event-specific data. The report includes the complete event chart, a chronological waterfall, speech-end-to-playback latency, cancellation/restart counts, invariant checks, and the largest measured critical-path gap.

## What this proves—and what it does not

A good result proves the clean prepare-early/release-after-commit architecture works with the selected model endpoints and browser buffer. It does not prove Helen's LiveKit, retrieval, or production transport path is fixed. That architecture should only be transferred into Helen after repeated lab turns show correct finals, zero state violations, uninterrupted playback, and a stable latency distribution.
