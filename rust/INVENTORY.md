# Inventory and preservation contract

**Historical checkpoint:** the subsequent native RVC core/provider implementation and real-provider comparison are recorded in [RVC_REVIEW.md](RVC_REVIEW.md). Deferred core/provider entries below describe the earlier checkpoint, not the current one-call slice.

This is the original source inventory. Native worker and queue-layer follow-up status is recorded in [NATIVE_GAPS.md](NATIVE_GAPS.md); the original deferred rows below describe the full conversation runtime, not the newly tested infrastructure subset.

Baseline: `d3f90112d00c38868748b3101ae156e70d299313`, latest merged PR [#139](https://github.com/OdionAI/sales-girl-voice-agent/pull/139), fetched 2026-10-30. User approved latest GitHub PR instead of production inspection. This is a source baseline, not a claim about the running container. Branch `codex/sg-voice-rust-prototype` starts at this same commit. Python reference is an isolated detached checkout at `/private/tmp/sg-voice-python-reference-d3f90112`.

11,907 top-level latency_lab Python lines; 39 test modules, 8,906 test lines. Source hashes and exact symbol lines are in `artifacts/source-manifest.json`. No vendor, UI or SDK source was translated.

| Module | Lines |
|---|---:|
| `pipeline.py` | 3026 |
| `livekit_worker.py` | 818 |
| `providers.py` | 1327 |
| `agentic.py` | 476 |
| `config.py` | 400 |
| `speech_languages.py` | 193 |
| `engagement.py` | 467 |
| `memory.py` | 261 |
| `input_noise.py` | 181 |
| `action_tools.py` | 522 |
| `banking_tools.py` | 381 |
| `session_identity.py` | 46 |
| `conversation_persist.py` | 484 |
| `usage.py` | 123 |
| `passkey.py` | 341 |
| `passkey_demo.py` | 244 |
| `sip_bootstrap.py` | 163 |
| `sip_headers.py` | 30 |
| `outbound.py` | 77 |
| `aicc_transfer.py` | 40 |

## Contract map

All paths below are relative to the service root at the pinned baseline.

| Contract | Source anchors | Prototype status |
|---|---|---|
| Microphone PCM and local speech/end-of-turn ownership | pipeline.py:628 `audio`, :713 `_speech_started`, :801 `_speech_ended`, :834 `_final_timeout`; livekit_worker.py audio subscription | Deferred; Rust gate receives typed events, does not detect speech |
| Stable partials start preemptive LLM/TTS | pipeline.py:908 `on_partial`, :981 `_stable_after_delay`, :1130 `_start_attempt` | Deferred orchestration; speculative hold gate implemented independently |
| Only locally requested authoritative final validates held audio | pipeline.py:1002 `on_final`, :1075 `_respond_to_final`; providers.py `RSTTClient.commit`, `_run_authoritative_chat`, `_run_authoritative_batch` | Gate accepts authorization event only; transcript ownership, similarity and banking strict matching deferred. No automatic authorization |
| Cancel stale hypotheses/tasks, clear playback on interruption | pipeline.py:2864 `_cancel_attempt`; livekit_worker.py:113 `send` | Rust generation IDs reject stale callbacks; SDK source clearing probe; end-to-end cancellation deferred |
| Ordered phrases/chunks, synthesis lock, held release | pipeline.py:2555 `_stream_llm_and_tts`, :2685 `_synthesize_phrases`, :2763 `_release_held`, :2841 `_send_audio` | Bounded FIFO PCM gate implemented; phrase tokenizer and provider streaming deferred |
| Generation end differs from playback completion | livekit_worker.py:113 `send` calls Python AudioSource.wait_for_playout; :195 `_finish_playout`, :299 browser ack | Explicit separate completion event in core; Rust RTC receiver/audible completion unimplemented |
| Listen-only keeps input and memory; model-driven speech tools | pipeline.py:1002 `on_final`, :1195 `_apply_speech_control`; engagement.py `Engagement`; tests/test_engagement.py speculative gate tests | Authorized typed speech decision and independent input counter tested; model/tool integration deferred, no phrase matching |
| Provider settings, prompts and voice/language routes | config.py `LabConfig`, agentic.py `AgentRuntimeContext`, speech_languages.py `apply_language_stack`, providers.py RSTT/RLLM/RTTSClient | Deferred unchanged reference; no replacement prompts/models/voices |
| Recovery, bounded reconnect buffer and ASR fallback | providers.py RSTTClient; input_noise.py InputNoiseFilter/ListeningNoiseRoute | Deferred; no claim that raw PCM counter supplies ASR or noise filtering |
| Knowledge, memory compaction, conversation retention | agentic.py KnowledgeClient, memory.py ConversationMemory, conversation_persist.py ConversationStore | Deferred, inaccessible in prototype |
| Tool schema, confirmations, banking authorization | action_tools.py ActionToolExecutor, banking_tools.py BankingTools, pipeline.py _run_banking_tools | All tool requests return Unsupported and cancel the attempt; no financial execution |
| Signed caller/session identity and passkeys/voice auth | livekit_worker.py resolve_dispatch, session_identity.py verify_session_token, passkey.py PasskeyFlow, passkey_demo.py | Deferred; prototype cannot accept production dispatch/session credentials |
| Usage, billing, persistence, recording | usage.py UsageCollector/UsageReporter; conversation_persist.py; livekit_worker.py entrypoint | Deferred, no service calls |
| SIP, outbound claim, AICC handoff | sip_bootstrap.py, sip_headers.py, outbound.py, aicc_transfer.py | Deferred; no SIP/outbound interface |
| Worker registration/job admission/shutdown | livekit_worker.py entrypoint/main, Python WorkerOptions/JobContext | Rust RTC SDK is not a Rust Agents worker; compatibility unproven, blocking full slice |

## Craft reference observations (read-only)

FigCraft docs/architecture.md separates headless engine from adapters and enforces layer dependencies; retained/full parity tests compare effects. EffectCraft crates/engine/src/lib.rs:628/654 routes commands through checked registry entries with bounded journals. FilmCraft crates/engine/src/commands.rs defines typed command metadata and enablement. PhotoCraft book/src/architecture/command-engine.md requires graceful failure for malformed inputs. Adopted: small core/adapter/service boundary, generation-scoped commands, bounded work, invariant tests. No UI/rendering stack copied; valid speech chunks remain FIFO, never latest-frame coalescing.

## Initial SDK gaps (addressed locally in the native follow-up)

Official source: https://github.com/livekit/rust-sdks (livekit and livekit-api pinned to 0.10.0; transitive versions in Cargo.lock). The SDK exposes room RTC/audio/data and server APIs. It does not establish Python WorkerOptions/JobContext lifecycle parity. NativeAudioSource exposes capture_frame and clear_buffer; no wait_for_playout equivalent is exposed in libwebrtc 0.3.55's native audio source. Capture completion cannot be reported as remote audible completion. A temporary Python worker/transport bridge would retain Python process/native overhead and must be measured as a hybrid. No such bridge or sequential substitute has been installed. The two supporting layers have now been implemented and verified locally in the native follow-up; see NATIVE_GAPS.md. The full coordinator/provider port and one-call/provider benchmark remain deferred. No Python bridge was added.
