# Local voice-match threshold experiment

Date: 2026-09-06. User explicitly requested a more permissive threshold after
the running ECAPA service was confirmed to use 0.40, not the numpy fallback's
0.85 default.

| Setting | Previous | Experiment |
| --- | --- | --- |
| `VOICE_AUTH_COSINE_THRESHOLD` | `0.40` | `0.20` |

This is a cosine-similarity cutoff, not a calibrated accuracy percentage.
Lowering it increases false-accept risk. This local experiment is not a
recommendation for production banking authentication.

## Scope

- Local voice-worker `.env` and voice-auth sidecar launch environment only.
- The worker sends the configured cutoff in its sidecar verification requests.
  The sidecar also uses 0.20 for requests without an explicit cutoff.
- Both the initial session check and fresh per-tool action check remain
  mandatory. Speech-quality checks, enrollment, retries and all tool gates are
  unchanged. Low-quality/non-speech audio is not automatically accepted.
- Repository defaults stay at 0.40 for ECAPA. No prompt, provider, UI, network,
  NPU service or bank API changes are included.
- Restart only the local voice worker and sidecar after confirming no active
  LiveKit calls. No iPhone rebuild is required for this backend setting.

## Rollback and restart

Set `VOICE_AUTH_COSINE_THRESHOLD=0.40` in the local worker `.env` and sidecar
launch environment, then reload those two processes when no calls are active.
The sidecar launcher does not load `.env`; pass its override explicitly:

```sh
VOICE_AUTH_COSINE_THRESHOLD=0.20 .local/voice-auth-venv/bin/python scripts/run_voice_auth_http.py
```

For rollback, use `0.40` instead. Preserve all other current launch settings.
Do not start a second sidecar on an occupied port or restart unrelated services.

Private pre-change `.env` and process launch-state snapshots are kept under
`.artifacts/voice-auth-threshold-20260906/`. They may contain credentials and
must not be committed or uploaded. Prefer changing only the threshold during
rollback rather than overwriting later unrelated configuration changes.

## Verification

- Confirmed zero LiveKit rooms immediately before reload.
- Local sidecar PID `19851` -> `99659`; ECAPA ready at 22:50:21 WAT.
- Local worker PID `84179` -> `99661`; registered at 22:50:23 WAT as
  `AW_HabyVk3CCH5U` with the existing agent name and local LiveKit URL.
- Live sidecar verification returned `threshold: 0.2`. The worker comparison
  path also returned 0.2 for both an unenrolled diagnostic identity and the
  enrolled caller with synthetic silence; neither was accepted. No enrollment
  was created or modified, and no live session authorization was granted.
- All 15 voice-auth/observer tests and 13 dynamic HTTP-tool tests passed with
  `PYTHON_DOTENV_DISABLED=1`. Production defaults and gate code are unchanged.
- Successful recognition of the caller's actual voice still requires a new
  call; this change does not establish that the underlying capture/match issue
  is resolved.
