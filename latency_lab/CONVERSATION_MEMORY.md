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
