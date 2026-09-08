# Handoff verification, 2026-09-08

This verifies the deployment materials, not a new latency or full banking acceptance result.

## Passed

- Created a new Python 3.12.7 virtual environment on macOS arm64, independently of
  the running hybrid venv. Installed every version in `hybrid-python-freeze.txt`.
- Fresh hybrid venv: `pip check` passed; 133 unit tests, 121 passed and 12 existing skips.
- Fresh hybrid venv: `verify_deployment` passed against the local dashboard env,
  the pinned dashboard defaults and completed trace `76b94b1d2e15`.
  Verified thinking disabled, Whisper, TTS codec frames 4, silence 300 ms, SAW and eight tools.
- Dashboard's six focused Whisper/policy/activity tests passed at `c14ad11`.
- All three service patch files passed `git apply --check` against archives of the pinned
  clean source revisions. Applied only to temporary copies, not running checkouts.
- Recreated the separate platform dependencies from pinned service requirements,
  matching the captured distribution-version inventory. `pip check` passed.
- Patched clean config source: all six `test_conversation_flow.py` tests passed.
- A further clean migration check found missing `agents.theme_key` and
  `agent_versions.training_prompt`. Added `agent-config-schema-parity.patch`;
  its regression test plus the six config-flow tests passed. Migration to
  `20260908_0004` succeeded and all ORM columns are present in the fresh SQLite DB.
- Imported the exact private SAW export into that migrated clean config database:
  all eight tools and their descriptions were preserved, and the saved prompt hash
  matched `b3591c5a2ae3848915e78d42b5074367719e7e9bd904f881c7a748483b6283fe`.
  This exercised the config API locally, without calling any banking endpoint.
- Patched clean conversation source: all 13 local recording/session timing tests passed.
- Clean SQLite migrations completed for auth and conversations too; their mapped
  columns match the migrated schemas.
- Existing banking and ECAPA environments both passed `pip check`; their version
  snapshots are included separately from the hybrid snapshot.
- Running hybrid worker's HTTP health endpoint returned 200.

## Limits and a deployment hazard

The old `/tmp/odion-local-services-venv` still has distribution directories but is
missing entrypoint/source/metadata files. Trying to reuse it for new tests fell
back to unrelated global packages and failed. A NEW isolated platform venv passed
the tests above. `platform-python-freeze.txt` is an inventory recovered from the
remaining versioned directory names, not a recovered byte-for-byte installation.
Do not use that temporary directory as a deployment dependency or repair it in place.
No running service was stopped or restarted during this work.

Banking and ECAPA snapshots were inspected and dependency-checked in their existing
environments; fresh full ECAPA/model enrollment and banking installations were not
retested. The complete platform/business restore, browser enrollment, authenticated
banking, new mobile deployment, Linux wheel install and new NPU host rebuild were
not executed as part of this documentation task. Private database/enrollment data
and remote model/server state are not in Git. Follow every CLEAN_DEPLOY gate on the
receiving machine and report untested gates explicitly.

No source in the running hybrid pipeline/providers/transport was edited. The new
preflight utility is offline tooling and is never imported by the call worker.
