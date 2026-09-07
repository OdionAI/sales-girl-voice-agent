# RVC orchestration over LiveKit transport

Status: initial investigation followed by an isolated comparison implementation,
September 7, 2026. The findings below describe the pre-integration adapter.
See [LIVEKIT_COMPARISON.md](LIVEKIT_COMPARISON.md) for implemented changes,
startup, verification and remaining acceptance work. The original running apps
and remote model configuration were not changed. The previously deferred
premature-LLM-stop issue remains deferred; response-completion recovery is not
part of this work.

## Decision

Keep LiveKit for room membership, participant identity, worker dispatch, media
transport and client SDK connectivity. Run the current RVC ConversationPipeline
inside the dispatched worker as the only conversation/turn coordinator. Do not
put a second AgentSession STT/LLM/TTS pipeline around it.

```text
Existing caller UI / AttentiveVoice SDK
                 |
       Existing session bootstrap API
                 |
       LiveKit room + named worker dispatch
                 |
  Caller microphone track -> LiveKit input adapter
                 |
     One RVC ConversationPipeline per caller session
       VAD -> ASR -> speculative/final LLM -> phrase TTS
                 |
        Authorized audio -> LiveKit output track
                 |
          Caller speaker / existing visualizer

  RVC events -> adapter -> existing transcript, state,
                  tool activity and authentication UI

  Existing scoped tools / voice-auth / configuration services
            remain backend services, not new client APIs
```

The worker imports the RVC core directly. It should not send the room audio
through an additional WebSocket connection to the standalone port-8012 service.
The existing provider connections from RVC to ASR, LLM and TTS remain unchanged.

## What exists today

| Responsibility | Original LiveKit app, port 3000 | RVC comparison app, port 3003 |
| --- | --- | --- |
| Client media transport | LiveKit WebRTC audio tracks; WebSocket signaling | Custom WebSocket: binary PCM input, JSON/base64 PCM output |
| Input audio | Microphone track consumed by the backend | Browser AudioWorklet; 16 kHz mono PCM16, 1,600-sample / 100 ms batches |
| Playback | LiveKit client receives agent audio track | 24 kHz AudioWorklet with a 120 ms initial audio prebuffer |
| Call structure | LiveKit rooms, participants and named worker dispatch | One WebSocket creates one in-memory pipeline and trace; no equivalent room/SFU membership model |
| Conversation ownership | main.py constructs AgentSession, provider adapters and turn-handling options | ConversationPipeline owns energy VAD, endpointing, hypothesis validation, cancellation and audio release |
| Streaming | Framework/provider streaming support | LLM token streaming to phrase tokenizer; phrase TTS streamed as available |
| Early generation | LiveKit supports preemptive generation; behavior depends on session configuration | Stable partial can start LLM and TTS; held output is released only after authoritative-final validation |
| Banking | Existing worker's tools, voice checks, confirmation and UI events | Eight configured tools; signed caller context; SAW checks retained, Yinka's explicit exception retained |
| Recording/session persistence | Existing room-recording and conversation-service integration | Trace/report artifacts; not equivalent to the original dashboard recording lifecycle |
| Multiple participants | Rooms support multiple human/agent participants | Current conversation coordinator is single-caller oriented |
| Mobile integration | Existing Attentive wrapper and optional SwiftUI library use LiveKit | Custom browser transport does not automatically provide the same mobile SDK contract |

Current public callers were configured to use Whisper, Qwen LLM with thinking
disabled, and Qwen TTS. Differences in orchestration must be tested without
changing those providers, prompts, voice, tool policies or generation settings.
The old README describes earlier Qwen batch-final experiments; inspect current
config/providers and MAIN_APP_COMPARISON.md for the Whisper path.

### What speculation actually means

RVC conversation speculation prepares an answer from a stable ASR partial while
the caller may still be speaking. Final-transcript validation either releases
the prepared audio or cancels/regenerates it. Banking arguments require an exact
matching hypothesis rather than approximate transcript similarity. Tool effects
remain gated by authoritative final input and the existing authorization and
confirmation requirements. No speculative banking execution is proposed.

This is different from the model server's speculative token decoding (MTP).
Changing the caller transport does not require changing model-server decoding.

Whisper frequently emits its useful text only after final commit. In the
inspected user trace 22285d8039b9, there were 13 generation_start events and zero
speculation_start events; 86 post-commit partials were ignored. The RVC pipeline
was fast without early generation in that call. Preserve the observed behavior
first, and measure speculation starts, accepted attempts and discarded work
separately. Do not assume the strategy's name proves speculation was active.

## Existing adapter: useful but incomplete

latency_lab/livekit_worker.py already provides LiveKitPCMTransport and a named
worker entrypoint, using raw rtc.AudioStream and rtc.AudioSource. It imports the
same ConversationPipeline without creating AgentSession. It also publishes
transcriptions, clears its source queue on interruption and handles end-call.

Its five transport unit tests passed in this investigation. They use a fake
audio source and do not prove WebRTC latency, browser/iOS playback, banking,
voice-authentication parity or reconnection behavior.

Before routing Wema or Yinka to it, address these observed gaps:

1. Runtime overrides: it builds LabConfig directly rather than applying the
   public caller's resolved per-session overrides. That can select different
   ASR/LLM/TTS defaults from the working port-3003 route.
2. Trusted caller context: ConversationPipeline is constructed without
   session_identity. BankingTools requires that identity, so merely loading
   the saved tools is insufficient for banking to work. Carry the existing
   trusted business, agent, customer profile and voice policy through scoped
   dispatch, bound to the intended room and caller. Room membership is not
   successful speaker verification.
3. UI events: LiveKitPCMTransport.send handles audio/transcription/control, but
   does not forward odion.tool.activity or odion.auth.*. Preserve those event
   contracts and existing agent-readiness/state behavior for web and iOS.
4. Startup: the entrypoint awaits the complete opening greeting before it
   creates and consumes the microphone stream. The input pump must be active
   independently of greeting playback; test early user speech and interruption.
5. Lifecycle: carry across disconnect cleanup, cancellation, text input where
   supported, session persistence and recording hooks. Do not make database,
   telemetry or recording operations block each incoming/outgoing audio frame.
6. Dispatch: use an isolated worker name for the experiment. Its current
   default resembles the existing worker's name; do not register both under
   the existing call route and let calls be assigned unpredictably.
7. Participant selection: the adapter waits for one participant. Bind input
   and private events to the intended caller, not whichever observer joins
   first. Multi-human assistance/handoff needs an explicit policy before use.

## Latency risks and controls

There is no justified zero-overhead guarantee. WebRTC introduces media framing,
encoding/decoding, network routing and jitter buffering; the existing WebSocket
path already introduces input batching, TCP behavior and a playback prebuffer.
The net difference must be measured on the same devices and network. Room
creation/connection is a startup cost, not an operation repeated every turn.

| Risk | Integration rule |
| --- | --- |
| Two endpointing/turn controllers | RVC remains the sole turn owner; no additional AgentSession/VAD finalization delay |
| Extra proxy/process hop | Instantiate RVC in the LiveKit worker; do not chain room -> standalone RVC WebSocket -> room |
| Double input batching | Convert received media to 16 kHz mono once and pass promptly; do not add another 100 ms accumulator after the adapter's existing 100 ms AudioStream frames |
| Output queue/backpressure | Measure actual queue depth and capture-frame blocking; do not wait for the full utterance before publishing |
| Double playback buffering | Use normal LiveKit audio playback, not the old RVC AudioWorklet prebuffer in addition to WebRTC playback |
| Slow auxiliary work | Run telemetry/persistence independently with bounded work and cleanup; retain required tool-authentication gates |
| Late interruption | Cancel the RVC attempt and pending generation, clear unsent PCM and the LiveKit source queue, and measure remaining client-side audio |
| Network placement | Keep worker/media/model routing comparable; test mobile and relay routes separately from localhost |

The existing LiveKit adapter uses queue_size_ms=240. This is a queue capacity,
not proof that every response waits a fixed 240 ms. A large queue can hold a
backlog when generation is faster than playback. It is also not the client's
WebRTC jitter buffer. Clearing the server queue cannot recall audio already
sent to the receiver. Keep the current setting for the first comparison and
tune only from measured queue/underrun/interruption evidence.

The current direct-browser playback prebuffer is 120 ms of accumulated audio,
not necessarily 120 ms of wall time if TTS produces a burst.

## Measurement corrections before comparison

The port-3003 UI currently fills several first-audio metrics on receipt of the
first tts_chunk, before the AudioWorklet reports started. Those numbers are not
listener-audible latency. The report's user_speech_end timestamp is emitted
after the endpointing silence window, so measuring from it excludes that wait.
The existing LiveKit playback acknowledgment is observed at the server after
the client sends it, so it also includes the acknowledgment return path.

Use a shared event contract with session/turn/attempt IDs and these boundaries:

1. Last voiced input sample in the test utterance.
2. RVC endpoint decision and ASR final received.
3. LLM request, first token and first speakable phrase.
4. TTS request, first PCM and authorized output release.
5. Transport enqueue and actual client render/audio-energy onset.
6. User interruption onset and last audible agent sample afterward.

Compute listener-side speech-end-to-response using a common client monotonic
clock or a calibrated external recording. Use server monotonic times for
server stages. Do not subtract unsynchronized client/server wall clocks.
Report first audio, first meaningful answer, tool acknowledgment and actual
tool-result speech separately. An opener must not hide a slower real answer.

## Rollout and acceptance

1. Checkpoint the working repositories, relevant local configuration privately,
   and endpoint/settings hashes. Keep ports 3000 and 3003 as rollback baselines.
2. Finish the adapter under a distinct dispatch name and opt-in test route.
   Preserve saved agents, prompts, model requests, voice checks and tool policy.
3. Compare three paths: existing LiveKit orchestration; RVC over direct
   WebSocket; the same RVC over LiveKit. The second-versus-third comparison
   isolates transport integration; first-versus-third compares orchestration.
4. First run a transport-only prerecorded-audio test without model inference,
   then repeated full-pipeline tests using identical audio, agent, providers,
   endpointing and tool-result fixtures. Alternate runs to reduce cache/load
   bias; separate warm calls, cold startup and concurrency.
5. Record median and p95 heard-response delay, startup, interruption delay,
   underruns, failures, transcript fidelity and speculative acceptance/waste.
   Agree a latency budget before promotion, rather than declaring zero overhead
   from one run. Example review targets, not promises: <=50 ms median and
   <=100 ms p95 added transport-only delay in the controlled comparison.
6. Verify SAW's two voice checks and Yinka's existing exception; all eight tools;
   customer/own-line prefills; no effects from cancelled speculation; explicit
   confirmation; bank activity and badges; reconnect/end-call cleanup; web and
   physical-iPhone mute, interruption and transcript behavior; recordings.
   Use fixtures for transactions and do not execute money movement for a test.
7. Promote only when measured latency and feature parity pass. Rollback is a
   session-routing change for new calls, not an emergency model/server restart.

No API or iOS SDK rewrite should be required for the transport experiment if
we preserve the existing connection contract and event formats. Mobile UI
parity still needs verification. Attentive streaming gateway, gRPC and Android
SDK remain later roadmap items; do not add them to this latency comparison.

## Source map

- Original orchestration: ../sales-girl-voice-agent/main.py,
  _build_session_for_language and session lifecycle/recording helpers.
- RVC orchestration: pipeline.py audio, on_partial, on_final,
  _stream_llm_and_tts, _commit_attempt_text and _cancel_attempt.
- Direct transport/bootstrap: server.py and session_identity.py.
- Banking parity: banking_tools.py and action_tools.py.
- Experimental room adapter: livekit_worker.py and tests/test_livekit_transport.py.
- Browser transport and metrics:
  ../sales-girl-dashboard-speculative-main/app/_components/public-agent/use-rvc-call-session.js
  and public/rvc-lab/playback-worklet.js.

Official references checked September 7, 2026:

- [LiveKit signaling and media protocol](https://docs.livekit.io/reference/internals/client-protocol/)
- [Rooms, participants and media tracks](https://docs.livekit.io/transport/media/)
- [Raw media processing and dispatched programmatic participants](https://docs.livekit.io/transport/media/raw-tracks/)
- [AudioSource queue and playout semantics](https://docs.livekit.io/reference/python/livekit/rtc/audio_source.html)
- [AgentSession orchestration](https://docs.livekit.io/agents/logic/sessions/)
