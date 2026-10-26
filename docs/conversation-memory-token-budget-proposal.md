# Proposal: budget-triggered memory for a live call

Status: **proposal only**. This document does not describe the currently deployed
memory algorithm and does not authorize a deployment.

## Goal

During one call, an unfinished request must remain available even after the
caller changes topics. For example, if the caller asks for airtime, then asks
about a different service, the agent must still know that the airtime request
is open when the caller returns to it. A finite LLM context cannot contain an
arbitrarily long transcript verbatim, so the worker must preserve the complete
record separately and send a compact, checked representation on each request.

## Proposed logic

1. Keep every committed caller turn, assistant reply, and tool result in the
   session ledger with stable turn IDs. Compaction must not delete the raw
   record. The existing conversation-service transcript can remain the durable
   record; the worker should also retain or reload the source turns needed
   during the active call.
2. Maintain a structured list of **open caller requests** independent of the
   prose summary. Each item contains its source turn IDs, the caller's exact
   request, known details, unresolved details, status, and any tool outcome.
   Update it only from committed turns and actual tool results. Do not mark an
   airtime request complete merely because the caller starts another topic.
3. Before each LLM request, estimate the history payload using the same
   accounting used to enforce the configured history budget. Reserve room for
   the current utterance, current task state, tool schemas, and response.
   With the present 6,000-token history budget, begin compaction when the
   projected history reaches **4,500 estimated tokens** (75%). Do not use a
   60-second timer as the trigger.
4. Compact the oldest completed exchange groups first. Keep the current turn,
   pending confirmations, and at least the latest four complete exchange
   groups verbatim. Merge older exchanges with the previous summary. Require
   the new summary to retain every still-open request and its source turn IDs;
   validate that requirement against the structured open-request list before
   accepting it. Keep uncertain values explicitly uncertain.
5. Build each LLM request from the system instructions, structured open
   requests and current task state, the validated older summary, recent
   verbatim exchanges, and the current caller turn. The structured open
   requests have reserved space and cannot be dropped by ordinary history
   trimming. If a topic reopens, retrieve its original source turns or exact
   tool result from the session ledger and include them within the budget.
6. If compaction fails or the result omits an open request, keep the existing
   summary and raw turns, retry once, and use a bounded fallback that still
   includes the open-request list plus the newest exchanges. Never silently
   convert an omitted amount, recipient, account number, authentication state,
   or confirmation into a fact. Ask the caller to clarify or retrieve the
   original turn before taking an action.
7. Record the history estimate, compaction trigger, compacted turn IDs,
   summary validation result, and any retrieval or fallback in the call trace.
   This makes forgotten-context incidents diagnosable.

## Airtime example

- Turn 1: caller says, "I want to buy airtime for my number." The open-request
  list records `airtime`, source turn 1, with amount still missing.
- Turns 2–10: caller asks unrelated questions. The raw turns remain in the
  ledger. Once projected history reaches 4,500 estimated tokens, the worker
  summarizes older completed exchanges. The airtime request remains pinned
  as open even if turn 1 is no longer sent verbatim.
- Turn 11: caller says, "Let's do that airtime now." The worker includes the
  pinned request and retrieves turn 1 if needed. It asks for the missing
  amount; it does not invent one or treat the earlier request as confirmation
  to charge.
- After an actual tool result or explicit cancellation, the request changes
  to completed or cancelled. Its source turns remain in the ledger for audit.

## Acceptance checks for later implementation

- A long, topic-switching call preserves an earlier unfinished request after
  several compactions and retrieves its original turn when resumed.
- A failed or malformed summary cannot erase an open request.
- The current utterance, protected task state, and recent exchanges fit the
  model's real context window, including tool schemas and response reserve.
- Exact amounts, destinations, and authorizations always come from committed
  caller input or tool results, never from an unverified summary.
- Explicit `clear_history` ends the current memory context and clears the
  structured open-request list; a normal topic change does neither.
