# Wema mobile tool recovery, 2026-09-06

## Failure and scope

The physical iPhone used the existing public agent `agt_59a007e81e` under
`wema-bank-poc-local`, named SAW. Its September 6 call loaded saved prompt
version 10 (6,520 characters), registered the seven Wema HTTP tools and passed
session voice authentication. Qwen replied that it was checking the balance but
never invoked the function; no action check or tool-activity event followed.

Version 10 replaced the detailed Wema instructions with a generated generic
customer-service prompt on September 5. Its configured-tool text contained
"account balance", triggering legacy Fidelity domain detection. That path
added Fidelity identity instructions and attempted Fidelity ops preloads.
The version record alone does not identify who saved it.

With user approval:

- Restored the exact saved instructions from version 9 as new version 11.
  SHA-256: `b3591c5a2ae3848915e78d42b5074367719e7e9bd904f881c7a748483b6283fe`.
- Cleared version 10's generic `training_prompt`; version 9 had no training
  prompt. All eight tool definitions, identity and TTS settings were verified
  unchanged through the runtime-config API after the update.
- Wema tool names select the existing generic/dashboard-configured runtime
  before legacy domain keyword heuristics. Explicit business-ID mappings retain
  precedence; actual legacy Fidelity tool routing is unchanged.

No SDK transport, playback, tool executor, bank API, model configuration or
voice authentication gate was changed. The UI is a passive observer of real
backend events, not a source of authorization or tool execution.

## Rollback

Before edits, a private SQLite backup and copies of edited source files were
saved under `.artifacts/wema-mobile-recovery-20260906/` (gitignored). The directory
is private because the configuration database can contain connector credentials.
Do not upload that database or embed credentials in a recovery note.

For prompt rollback, use the existing scoped agent-config PATCH endpoint to
create another version with version 10's `instructions` and `training_prompt`.
Do not overwrite the whole live database or delete newer versions. Preserve all
other fields. Code rollback removes the four-line Wema classifier guard and
restarts only the local voice worker with its existing environment and command.

## Verification

- `PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m unittest discover -s tests`:
  183 tests passed. The same suite with the local NPU experiment `.env` loaded
  has six TTS expectation failures; disabling dotenv affects tests only, not the
  running worker or its provider selection.
- Prompt regressions cover Wema balance/transaction wording and a Fidelity
  transfer destination without identity injection, preservation of runtime tool
  guidance, no Fidelity ops preload, and unchanged explicit/legacy routing.
- Native UI test added:
  `testLiveBankLookupShowsBackendActivityWithoutBypassingVoiceAuth`, opt-in via
  `TEST_RUNNER_ATTENTIVE_BANK_UI_TEST=1`. It sends a balance request through the
  real call, using a separate unenrolled test identity and no microphone. It
  expects a real tool result with `voice_not_recognized`, never a bypass or a
  successful bank API read. The failure is displayed as `Failed`, matching the
  current backend's `status: failed` contract.
- First real native attempt at 22:29 WAT invoked `wema_get_balance` with `{}`
  and reached the existing auth gate. A separate DNS failure resolving
  `api.deepgram.com` closed the call before UI verification completed. Router
  DNS `192.168.100.1` timed out while a direct `1.1.1.1` query resolved it.
  No DNS change was made at that point; permission was requested separately.
- Successful physical-iPhone balance retrieval after both voice checks and
  visual verification of its bank result remain required before claiming full
  end-to-end completion.
- The local voice worker was reloaded at 22:32 WAT after confirming zero rooms:
  PID `26631` -> `84179`, registered worker `AW_JYR6KPvZiBKb`. Its original argv
  and all environment entries were copied intact via macOS process arguments;
  it still uses the same `.env`. Other services were not restarted.
- Swift test/build verification also covers activity start-to-completion row
  replacement, result preservation and no client-side auth grant. These are
  unit tests, not a claim that a real balance was retrieved.
- DNS permission is pending. Original Wi-Fi setting is automatic (no manually
  configured servers), using router `192.168.100.1`. The proposed temporary
  override is `1.1.1.1`, `1.0.0.1`; restoring automatic would use
  `networksetup -setdnsservers Wi-Fi Empty`. Do not apply either command without
  the user's approval. No network setting was changed during this fix.
