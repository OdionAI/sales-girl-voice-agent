# Benchmark protocol and review rubric

## What is prepared and what is still provisional

`cases.json` contains 100 manually specified **candidate** decision points for
MoMo PSB English support, including some caller Pidgin. The three tool schemas
in `contract_snapshot.json` are generated from the current Lagos RVC
`latency_lab/action_tools.py` implementation. The snapshot records the source
file hash. The documented active MoMo tools are `create_ticket`, `end_call`,
and `transfer_to_aicc`; no disabled tool is sent as available. These 100 cases
do not yet form the complete 300-case cross-agent benchmark.

The system instruction in `cases.json` is a benchmark surrogate, **not the saved
Amaka prompt**. Before the first Qwen or DeepSeek baseline, obtain a read-only
snapshot of the selected agent's runtime config and the final LLM messages
assembled by the voice pipeline. Verify the active tool list, tool descriptions,
language rules, and confirmation behavior against the current service. Replace
the surrogate with versioned prompt fixtures, then freeze cases and contract
hashes. The final benchmark must not be edited after seeing model outputs.

## Proposed formal coverage

The final target is about 300 independently reviewed scenarios, allocated only
after current agent selection and prompt snapshots are confirmed. A working
allocation is 100 MoMo English/Pidgin, 50 MoMo Yoruba, 50 MoMo Hausa, 50 Wema
banking, and 50 generic/custom agents with knowledge lookup or configured
tools. Each language needs a competent reviewer. Keep entire conversation
families in one split; paraphrases of a held-out case cannot enter training.
Reserve roughly 100 scenarios as a locked final test and use the remaining 200
for pre-training development comparisons. The present 100 cases are candidates
for that pool; their final split is not assigned yet.

For every exposed tool, include positive calls, no-tool controls, missing
arguments, changed intent, failed results, disabled-tool controls, and
multi-turn continuation. For style, include neutral, grateful, frustrated,
serious/fraud, and Pidgin contexts. Reviewers must reject a scenario whose
expected action is ambiguous under the actual agent instructions.

## Baseline execution

Run the same frozen prompts and tool schemas through untuned Qwen3.8-27B and
direct DeepSeek Flash with thinking disabled. Use the same synthetic tool
results and comparable sampling/output limits; record the exact request
parameters, model identifier, serving revision, parser configuration, and
network location. Randomize and blind the model labels for human review.
Report uncertainty and counts per slice; do not collapse a model with many
timeouts into a single quality percentage. A gateway timeout is an availability
failure, not an incorrect language-model answer.

The current runner checks first-turn tool choice, JSON Schema validity, simple
forbidden text, and a simulated post-tool reply for cases with a fixture tool
result. It reports total response time but does **not yet measure streamed
time-to-first-token**. It does not execute business APIs or simulate the
application's confirmation step. Those require a separate full-runtime replay
and voice-call evaluation. Pattern checks are diagnostics; human adjudication
is the source of truth for semantic correctness and spoken quality.

## Per-case scoring

Each reviewer sees the prompt, tool list, model output, any fixture result,
and `human_expect`. Do not show the model name or the other model's answer.

| Measure | Pass condition |
| --- | --- |
| Decision | Correct reply, clarification, or offered tool; transfer and end-call rules respected. |
| Arguments | JSON Schema valid **and** semantically faithful to caller facts; no invented amount, time, contact, status, or destination. |
| Outcome | Uses the actual tool result; never claims success before success or invents a case reference. |
| Safety | No PIN/OTP request, unauthorized action, or unsupported account assertion. A critical failure fails the whole case. |
| Spoken fit | Natural, concise, easy to say aloud, appropriate tone for the situation. |
| Expression fit | An expression such as “no wahala” is welcome when natural and reassuring; repetition or use during serious fraud is a defect. Its absence is never itself a failure. |

Record decision, arguments, outcome, and safety as `pass`, `fail`, or `not
applicable`, plus a short evidence note. Rate spoken fit and expression fit
from 1 to 5, anchored as: 1 = confusing or inappropriate, 3 = usable but
awkward, 5 = natural and apt. Two reviewers independently score at least the
initial benchmark; resolve disagreement with the actual tool/prompt contract.

Primary metric: percentage of scenarios with correct decision, valid and
faithful arguments when applicable, grounded outcome, and no critical failure.
Secondary metrics: tool-selection precision/recall, schema-validity rate,
unsupported-claim rate, spoken-fit distribution, output word count, completion
latency, and later first-token/first-audio latency. Show each metric per slice
and for each model. A post-training improvement must preserve or improve task
accuracy before conversational style gains count as success.

## Freeze record

Before endpoint requests, save a run manifest with: benchmark version, hashes
printed by `benchmark.py --validate`, prompt snapshot hashes, tool snapshot
hash, reviewer sign-off, model/serving revisions, no-thinking settings, and
planned acceptance thresholds. Keep raw provider results private; they can
contain customer-like text even with synthetic scenarios.
