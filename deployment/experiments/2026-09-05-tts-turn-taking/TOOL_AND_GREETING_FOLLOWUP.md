# Tool and greeting follow-up

## Checkpoint before the prompt correction

- Branch: `wema-bank-poc-tools`; code/runtime checkpoint: `d83fb46`.
- Keep TTS steady chunks at 10, initial chunks at 8, ASR silence at 0.25 s,
  and LiveKit endpointing at 0.25-0.50 s. Do not change providers or auth gates.
- No dashboard changes or remote service changes are part of this correction.
- Revert the subsequent prompt-only diff to return to this checkpoint. No remote
  rollback is needed for a prompt-only change. Earlier tuning rollback is in
  `RESULTS.md` and `INITIAL_CHUNK_8.md`.

## Observed failures

On 2026-09-05 at 01:44 local time, all seven Wema HTTP tools were registered.
The first balance request produced an assistant response containing `[amount]`
without invoking a tool. The repeat request also produced a placeholder.
At 01:46:04 the same call invoked `wema_get_balance`; both voice checks passed
and the connector returned HTTP 200. The panel still opens on the public caller
page. This is evidence of skipped model tool invocation, not removed tool code.
It does not independently prove browser delivery of every historical event.

An isolated Qwen probe using the saved Wema instructions, current spoken style,
configured HTTP schemas, temperature 0 and thinking disabled reproduced a
balance acknowledgement with no function call. No banking tool was executed by
these probes. Appending explicit Wema lookup requirements produced the correct
function calls for balance, recent history and a paraphrased balance question;
a greeting did not invoke a tool. These are small-sample checks, not a guarantee
of model compliance.

The next call's first 54-character TTS request generated more than 15 seconds of
audio before cancellation when the caller hung up. Server logs show one synthesis
request, not repeated call startup. Three direct greeting probes with the same
voice and initial chunk setting produced 8.16, 7.28 and 4.00 seconds of audio, with
no modeled delivery underrun. This points to a separate synthesis-quality issue;
duration alone does not prove repeated words. Do not claim it is fixed by the
prompt correction or change the server sampling configuration without approval.

## Narrow correction

Append Wema-specific live-data requirements after context/style composition,
only for enabled Wema tools. Require actual lookup before answering, preserve
tool-enforced voice authorization, prohibit placeholder results and preserve
the normal handling of blocked/failed/needs-input tool outputs. Do not force
tool choice for every utterance or change transaction execution behavior.
