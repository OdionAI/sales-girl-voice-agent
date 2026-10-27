# Model Text And Transfer Release Boundaries

The existing llm_first_token trace event is emitted by the pipeline after its
transfer-preamble guard. It is not raw provider TTFT for agents with
transfer_to_aicc enabled. This change measures both boundaries without changing
prompts, model parameters, tool choices or transfer outcomes.

## New Trace Events

- llm_provider_request_start: one per actual provider HTTP attempt, correlated
  by request_id, turn, attempt and phase. Retry attempts have distinct IDs.
- llm_provider_first_text: first usable spoken text from RLLMClient, before the
  transfer guard, with provider_ttft_ms. This excludes hidden reasoning and is
  not a first-header/metadata timer. Tool-only replies have no invented TTFT.
- llm_provider_request_complete: completed indicates receipt of DONE, not a
  successful financial operation. total_ms is client stream wall time and may
  include downstream consumption/backpressure; retry backoff is excluded.
  input_tokens/output_tokens/cached_tokens appear only when supplied by the
  provider as nonnegative integers. Missing cache evidence is not zero or a hit.
- llm_transfer_guard_decision: suppressed, buffered_events and text_buffer_ms
  report the actual decision and time from the first buffered text to decision.
  A provider exception or cancellation releases no buffered speech.

Caller-scoped LiveKit metrics use llm_first_token only for the new pre-guard
provider measurement. Pipeline release is separately named llm_text_released
with post_guard_ms. The existing JSONL event name remains for trace compatibility;
its semantics have not silently become raw provider timing. The transport only
forwards allowlisted numeric/status fields, never prompts, tool arguments or
credentials. Context is iterator-scoped so background/overlapping calls do not
inherit another turn's timing identity.

## Why Incremental Transfer Release Is Not Enabled

The current provider contract permits content followed by a transfer tool call
in the same response. Tool calls are assembled by the client at completion.
Neither a timer, a sentence boundary, nor absence of a tool in the first chunks
can prove that a later transfer decision will not invalidate that speech.
Regression tests pause after a complete Hausa transfer sentence, then append a
late transfer call: no text is released before or after that decision. They also
cover failure, cancellation and preservation of ordinary text/other tools.

Accordingly, full-response behavior is retained. No bounded early-release design
is safe under the current contract. Simply deleting the guard, scanning for
English transfer phrases, or imposing a timeout would weaken handoff correctness.

A future incremental design needs an explicit, irrevocable per-turn decision
from the model/provider: either an action decision with no caller speech, or a
speech decision whose remaining stream cannot request an action. That requires
a validated protocol or a separate decision pass and before/after latency
evaluation. It must preserve natural-language intent, all languages, barge-in,
speech gates and every existing tool/security rule. It is not deployed here.
