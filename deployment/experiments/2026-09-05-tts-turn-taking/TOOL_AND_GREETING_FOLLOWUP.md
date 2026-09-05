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

## Verification and deployment

- `PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m unittest discover tests`: 180
  tests passed, including prompt scope, enabled lookup names and existing
  authorization/tool-activity tests.
- A second live Qwen probe using the implemented helper returned the correct
  balance/history tool calls. With simulated blocked tool outputs, it requested
  new speech for voice verification instead of inventing account information.
  These probes do not execute bank operations or validate a complete voice call.
- No LiveKit rooms were active before the local voice worker restart. At 01:57:29
  local time it registered as `AW_mNwBgmcJwtJd`; health on port 8188 returned OK.
- The dashboard code, voice auth gates, model providers and runtime audio/turn
  settings were not changed by this fix.

## Isolated synthesis follow-up (not enabled for calls)

Two direct TTS requests with initial frames 8 produced over 20 seconds of audio
for a single greeting and were stopped by the diagnostic client. Deepgram
transcription of that generated audio included "Good good this the the the" and
repeated "so", "we" and "good". This confirms repetition in the source audio
without the browser, LiveKit, or tool calls. A single initial-2 control completed
in 4.88 audio seconds; this small sample does not establish that initial-8 alone
causes repetition.

The deployed engine supports `non_streaming_mode` independently of HTTP
`stream`. In `prompt_embeds_builder.py`, `_generate_icl_prompt` uses the full
text conditioning during prefill when this flag is true. It does not require
turning off streaming PCM delivery. The Base model defaults to false.

Three isolated requests adding only `non_streaming_mode: true` while retaining
initial frames 8, `stream: true`, and `stream_format: audio` produced:

| Probe | First audio | Audio length | Total delivery | Modeled underrun |
| --- | --- | --- | --- | --- |
| 1 | 0.691 s | 3.68 s | 2.658 s | 0 s |
| 2 | 0.626 s | 3.44 s | 2.475 s | 0 s |
| 3 | 0.627 s | 3.44 s | 2.446 s | 0 s |

ASR transcripts contained a single greeting in each case, without the repeated
words above. ASR rendered proper names imperfectly; this is not a listening test
or proof of universal audio quality. Await approval before enabling this option
for real calls, then test longer responses and interruptions as well.

Remote startup/YAML hashes match the pre-investigation state. No server edits,
container changes, service restarts, or port changes were made.
