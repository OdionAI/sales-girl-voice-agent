# RVC conversation memory

## Before this change

Rollback checkpoint: `b09e3dc` on `speculative-integration`.
Caller text and tool results were committed with the completed assistant answer.
Cancelling that answer could discard already-final caller text. The LLM received
only the last four saved caller turns; there was no running summary.

Scope: the isolated RVC/LiveKit comparison worker. No changes to remote inference
servers, model selection, audio timing, saved agent prompts, voice authentication,
the original workers, or the SDK. The original trace remains the source of record.

## Implementation plan

1. Retain final caller text immediately, once per turn, including chat input.
   Cancel speculative output without cancelling confirmed conversation history.
   Retain completed tool results before speaking their explanation.
2. Keep recent exact exchanges and a bounded running summary. Keep consecutive
   caller fragments together; never compact a pending response or split a native
   tool call/result pair. Existing application-owned pending actions and prepared
   previews remain authoritative, not the generated summary.
3. Run one asynchronous compaction task per session at configurable intervals,
   initially 60 seconds, preserving at least eight recent exchanges. Use the
   existing NPU LLM without tools or TTS. Snapshot an older completed prefix and
   replace only that prefix after a valid summary returns. New turns must survive.
4. Summaries are untrusted reference data, not instructions or authentication.
   Preserve corrections and uncertainty. Never infer transaction confirmation.
5. Bound foreground context even when summarization fails; report omissions in
   context and telemetry. Test message retention, fragmentation, tool-result
   integrity, concurrent updates, cancellation, summary failure and long calls.

An async task avoids blocking the call event loop; it does not eliminate shared
NPU contention. Compaction must yield to caller activity and not delay a response.
No server restart or server configuration change is needed. Reload only the
isolated hybrid worker after the call has ended and tests pass.

## Rollback

Before deployment, record the final code/config changes below. To disable only
LLM compaction, use the documented feature flag and reload the isolated worker.
For a full rollback, run that worker from checkpoint `b09e3dc` in a separate
worktree with its existing private environment. Do not reset other worktrees or
restart LiveKit, the original voice worker, or shared inference services.

## Implemented behavior and settings

| Control | Previous | Current default |
| --- | --- | --- |
| Confirmed caller retention | At completed response | Immediately, once per turn |
| Interrupted caller fragments | Could be discarded | Retained in order as one exchange group |
| Tool outcome retention | At completed spoken response | Before cancellable completion notifications |
| Foreground history | Four saved caller turns | Summary plus exact recent history within budget |
| `RVC_LAB_MEMORY_COMPACTION_ENABLED` | None | `true` |
| `RVC_LAB_MEMORY_COMPACTION_INTERVAL_SECONDS` | None | `60` |
| `RVC_LAB_MEMORY_RECENT_EXCHANGES` | None | `8` minimum exchanges kept out of compaction |
| `RVC_LAB_MEMORY_CONTEXT_TOKENS` | None | `6000` estimated conversation tokens |
| `RVC_LAB_MEMORY_SUMMARY_TIMEOUT_SECONDS` | None | `15` |

These are worker environment settings, not caller-controlled overrides. Environment
changes require reloading only the hybrid worker. Setting compaction to `false`
disables background requests while keeping the history correctness fix.

The session owns one interval task and at most one compaction request. Requests
start only while idle, with no pending confirmation/prepared preview. Caller
speech, chat, or foreground generation cancels the summary request without awaiting
it on the response path. Clear-history and disconnect cancel background work;
snapshot checks reject stale results. There is no extra request per response.
The HTTP client cancellation cannot guarantee immediate cancellation inside the
remote inference server. Measure shared-NPU contention under concurrent calls.

Compaction reuses `RLLMClient` and the session's existing NPU model/endpoint. It
requests up to 768 output tokens at temperature 0, with no tools and no TTS.
Normal replies keep their existing 180-token cap and temperature 0.35. A validated
JSON summary contains topics, facts, corrections and open questions. It is supplied
as explicitly untrusted reference data, never as a system instruction or tool result.
Current application-owned pending action and prepared preview are pinned separately.
Saved business prompts and authentication decisions are not modified.

The budget estimator uses serialized UTF-8 bytes divided by three; it is an estimate,
not the remote model tokenizer. The 6000-token limit applies to summary/history,
excluding the system prompt, tool definitions, pinned task state and current input.
Context selection normally keeps whole exchanges; if one exchange exceeds the
budget, it keeps only whole messages/native call-result pairs and explicitly marks
omissions. It never truncates account digits or supplies a partial tool JSON result.
On summary timeout/invalid JSON, the previous summary and original history stay
intact. Foreground selection remains bounded; raw session history can still grow
when compaction repeatedly fails. Original call traces remain under existing policy.

## Verification (2026-09-07)

- `python -m unittest discover -s latency_lab/tests`: 126 tests, 114 passed,
  12 existing skips. New tests cover fragment retention, duplicate finals,
  speculative cancellation, interrupted tool notification, bounded context,
  interval scheduling, single-flight requests, timeout/invalid summaries,
  concurrent appends, clear-history, stale snapshots and authentication isolation.
- Real NPU replay of the reported fragmented budget produced: "Did you mean three
  thousand five hundred naira?" No banking action was executed by this probe.
- A real NPU summary of the older four exchanges of a 12-exchange conversation took
  3.81 seconds. Eight exact exchanges remained. A follow-up correctly recalled the
  corrected 3500-naira budget, monthly MTN plan preference and unresolved transfer
  complaint. This is one sample, not a latency guarantee.
- Hybrid live-call trace `27bcda19f763`: all four budget fragments survived rapid
  chat interruptions; the agent confirmed and recalled 3500 naira. MTN plan tool
  activity was emitted, and the text-only caller correctly failed voice checks.
  No generation errors or invariant violations occurred. This test does not prove
  spoken-ASR accuracy or successful biometric authentication.
- Audio-only smoke trace `87ec42fa902f`: the native LiveKit caller received 698
  audio frames, including 670 nonzero frames, with no receiver errors. Both smoke
  rooms were disconnected/deleted after testing. Only the isolated worker was
  reloaded; shared services and all private environment values were unchanged.
