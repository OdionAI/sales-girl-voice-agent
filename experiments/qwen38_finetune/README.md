# Qwen3.8-27B voice fine-tuning experiment

This experiment targets **non-thinking text replies and structured tool calls** for
SalesGirl's voice runtime. It does not change live routing, prompts, or tools.

## Current checkpoint (2026-11-16)

- A 14-case, synthetic, read-only starter benchmark and Chat Completions runner
  are available. These cases exercise the bench plumbing; they are **not** a
  statistically useful quality benchmark or a production-ready test set.
- Both configured local Huawei routes (`qwen3.8_27b` at the dashboard's Qwen URL
  and `deepseek_v4_flash` at the voice repo's Ascend URL) timed out on `GET
  /models` and on one synthetic `POST /chat/completions` case from this machine.
  There are **no model-quality scores yet**. The latter route is Huawei MaaS,
  not the direct `deepseek-flash` API used by the current Lagos Voice Lab.
- `latency_lab/providers.py` sends `chat_template_kwargs.enable_thinking=false`
  to Qwen-compatible endpoints. It does not copy successful caller-facing raw
  output into traces. The conversation transcript alone therefore cannot
  reconstruct complete supervised tool-call examples; data collection must
  preserve the request's actual tool schemas, assistant tool calls, tool
  results, and spoken continuation in an approved, redacted capture or replay.

## Phases and gates

1. **Freeze the contract.** Record the exact Qwen model revision, serving
   endpoint, no-thinking request field, tool parser, runtime prompt revision,
   active tool schemas, and the DeepSeek teacher endpoint. Confirm a parsed
   no-thinking tool call on each endpoint. Record hardware and access route.
2. **Build the benchmark.** Expand the starter set to about 300 distinct
   scenarios, stratified by agent/business, tool, language, missing information,
   success/failure, and no-tool decisions. Reserve roughly 100 as a locked
   final test; use the rest for development comparisons. Keep all turns of a
   conversation in one split, and hold out businesses where possible.
3. **Run untuned baselines.** Replay the same cases through direct
   `deepseek-flash` and untuned Qwen 3.8 27B, both with thinking off, identical
   tool schemas and mock tool results. Measure correct action, valid arguments,
   grounded follow-up, word count, time to first token, and completion time.
   Have two blinded reviewers score accuracy, spoken naturalness, and whether
   expressions such as “no wahala” fit the moment. Do not score slang frequency.
   Resolve disagreements with a rubric and examples.
4. **Prepare data.** Use corrected real call traces where permitted, plus
   synthetic scenarios grounded in current SalesGirl tool contracts. Generate
   multiple candidates, review and deduplicate them, redact customer data,
   and retain provenance. Include no-tool, clarification, wrong-tool, tool-error,
   and recovery cases. Never train on the locked benchmark or chain-of-thought.
   Preserve business knowledge as retrieved context rather than memorized facts.
5. **Train nested QLoRA experiments.** Start with 500, then 1,500, then 5,000
   accepted examples, reusing the same held-out set and recording the exact
   dataset/model/framework revisions. Use `Qwen/Qwen3.8-27B`, template
   `qwen3_8`, and no-thinking targets. Validate tokenized tool calls and a
   two-update real-model smoke run before the full first run. The output is an
   adapter; reload it before evaluation.
6. **Compare and expand only where useful.** If the 1,500-example checkpoint
   shows a meaningful gain, continue to 5,000. If the 5,000-example learning
   curve still rises on important failure categories, consider 8,000–12,000
   accepted examples. Otherwise improve coverage or tool/runtime behavior,
   not just volume. Compare the tuned adapter in the intended serving precision
   against both original baselines.
7. **Voice Lab and limited rollout.** Check actual ASR, interruptions, tool
   execution, TTS, and first-audio latency. Promote only when task completion
   improves without unauthorized tool actions or unacceptable latency.

The quantity targets above are **experiment sizes, not guaranteed thresholds**.
Research shows that around 1,000 carefully selected instruction examples can
move behavior, and smaller high-quality tool datasets can outperform noisier
larger ones. SalesGirl's many tools, languages, and agent configurations may
need more. The 500 → 1,500 → 5,000 learning curve will tell us. Sources:
[LIMA](https://arxiv.org/abs/2305.11206),
[tool-use data quality study](https://aclanthology.org/2024.emnlp-main.285/),
[QLoRA](https://arxiv.org/abs/2305.14314).

## Proposed first data budget

For the first formal run, aim for **1,500 accepted training examples** plus
the independent 300-case benchmark. Generate about **3,000 candidate examples**
to allow for rejection and deduplication; the actual yield must be measured.
For the first 1,500 accepted examples, target approximately 450 direct spoken
replies, 550 complete successful tool cycles, 300 clarification/no-tool cases,
and 200 tool failure/recovery cases. Cross these with actual tools, businesses,
and languages rather than treating these counts as rigid quotas. Review every
example in the first 500 and audit a stratified sample of later batches.

Each accepted record needs: source and reviewer status, redacted caller text,
the exact available tool schemas and agent instructions, assistant response or
structured call, synthetic or observed tool result, and the grounded follow-up.
Keep IDs and review labels out of the model prompt. Use a canonical source JSONL
and convert to the selected trainer's format; inspect rendered tokens before
training. [ms-swift's agent format](https://github.com/modelscope/ms-swift/blob/main/docs/source_en/Customization/Custom-dataset.md#agent-format)
and [LlamaFactory's ShareGPT format](https://github.com/hiyouga/LlamaFactory/blob/main/data/README.md)
show the required role mapping.

## Running the starter benchmark

Validate locally:

```bash
python3 experiments/qwen38_finetune/benchmark.py --validate
```

Set a key in the environment without putting it on the command line. Run each
model with the same cases, temperature, and timeout. `--base-url` may be a `/v1`
base or a full `/chat/completions` URL.

```bash
python3 experiments/qwen38_finetune/benchmark.py \
  --base-url "$MODEL_BASE_URL" --model "$MODEL_NAME" \
  --api-key-env MODEL_API_KEY --provider qwen-compatible \
  --output /private/tmp/qwen-baseline.jsonl
```

Use `--provider deepseek-api` only for the direct DeepSeek API; use `generic`
for an OpenAI-compatible MaaS gateway until its accepted request fields are
verified. `--ids warm_01 --timeout 5` makes a one-case connectivity smoke run.
The runner never executes tools: it supplies fixture tool results for the
follow-up turn. Output JSONL may contain model-generated text and should be
stored privately. Automatic checks are intentionally narrow. Review answers
manually for factual correctness, whether a clarification is needed, and
whether the language sounds natural when spoken.

## External training starting point

The maintained [LlamaFactory Qwen3.8 workflow](https://github.com/hiyouga/LlamaFactory/blob/main/.agents/skills/llamafactory-qwen38-sft/SKILL.md)
selects the [CUDA 4-bit QLoRA YAML](https://github.com/hiyouga/LlamaFactory/blob/main/examples/train_qlora/qwen3_lora_sft_otfq.yaml)
or [Ascend QLoRA YAML](https://github.com/hiyouga/LlamaFactory/blob/main/examples/train_qlora/qwen3_lora_sft_bnb_npu.yaml).
Copy one into a pinned training checkout; change its model, template, dataset,
output, and no-thinking settings deliberately. Use its dependency, smoke-test,
adapter-reload, and held-out evaluation gates. The actual accelerator and
network route are still to be selected.

## Immediate access/setup gap

Provide a reachable inference route for the official `Qwen/Qwen3.8-27B`
checkpoint (or identify the exact revision of the served model) and for the
direct `deepseek-flash` endpoint, with credentials available as environment
variables. Also choose the isolated training accelerator and approved redacted
data location. The existing local Huawei URLs cannot currently be benchmarked
from this machine; do not interpret those timeouts as model failures.
