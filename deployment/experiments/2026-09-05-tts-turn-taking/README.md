# Pre-experiment checkpoint: TTS chunking and turn taking

Captured on 2026-09-05 before changing any live service or latency setting.
The commit that introduces this directory, `13b4f07`, is the voice-agent rollback checkpoint.
This is an experiment, not a promotion to the stable deployment defaults.
See `RESULTS.md` for applied values and verification after the checkpoint.

## Requested changes and actual baseline

| Control | Observed baseline | Requested experiment | Scope |
| --- | --- | --- | --- |
| TTS `codec_chunk_frames` | 25 (2,000 ms audio) | 10 (800 ms audio) | TTS connector configuration |
| Request `initial_codec_chunk_frames` | 2 (160 ms audio) | unchanged | Voice-agent TTS request |
| Server default initial frames | 1, overridden by request | unchanged | TTS connector configuration |
| TTS left context | 72 frames | unchanged | Preserve decoder context |
| TTS playback startup buffer | 0 ms | unchanged | Voice agent |
| Odion ASR final-silence gate | 0.50 s | 0.25 s | Inactive with current Deepgram selection |
| LiveKit minimum endpointing delay | 0.30 s | 0.25 s | Active turn handling |
| LiveKit maximum endpointing delay | 0.65 s | 0.50 s | Active turn handling |
| Minimum interruption duration | 0.10 s | unchanged | Active turn handling |

Current local STT selection is `VOICE_AGENT_STT_PROVIDER=deepgram`, model
`nova-3`. Its installed plugin defaults `endpointing_ms` to 25 ms; this is a
different control from the 0.50-second Odion silence setting. Do not silently
switch providers or change multiple turn-handling controls to satisfy the request.
After clarification the user requested the Odion silence reduction and also
authorized reducing LiveKit's delays. Both changes are recorded in `RESULTS.md`.

LLM remains Qwen, with thinking disabled. TTS remains Qwen3-TTS Base using
`helen-mavino-0030`, English, PCM streaming. No provider fallback is introduced.
No auth, tool, prompt, recording, microphone, dashboard, port, firewall, SSH,
Docker image, driver, model weight, or server boot configuration changes belong
to this experiment.

## Measured baseline

A direct request to `http://127.0.0.1:8091/v1/audio/speech` inside the `tts`
container, with text "Hello, this is SAW. How can I help you today?", returned:

- First 160 ms PCM at 0.352 s.
- Next 2,000 ms PCM at 1.540 s: a 1.188 s inter-burst gap.
- Last PCM at 2.096 s; total audio duration 3.04 s.
- With immediate playback, the first gap implies about 1.028 s starvation.

This bypasses the jumper and LiveKit. Stage input processing releases the initial
two frames, then waits for 25 additional frames before its next steady decode.
There is no configured 1.1-second sleep. The polling sleep is 0.01 s.

Repeat the same request after warm-up. Capture first audio, inter-burst gaps,
audio delivered per burst, required startup buffer, total time and audio duration.
Listen for chunk-boundary artifacts. Do not declare the break fixed from a better
first-byte number alone, or promote settings until a real call is tested.

## Remote runtime identity

- Jumper: `102.88.137.124`, SSH port `29321`; node1: `10.205.50.7`.
- Docker container: `tts`, running with `bash` as PID 1. Do not restart it.
- Image ID: `sha256:acba66221a39170cbc405c7e7aa69b73b1905e04ef92f04943a61bb3878d6761`.
- `/data` is a bind mount from node1 `/data`.
- Model: `/data/models/Qwen3-TTS-12Hz-1.7B-Base`.
- Working directory: `/vllm-workspace/vllm-omni`.
- Engine Git revision: `3f4abedbda281302b28f7885c270fce3e342c924`.
- Engine has an existing modified `vllm_omni/deploy/qwen3_tts.yaml` plus unrelated
  dataset/backup files. The active config is the external file below; leave those
  engine working-tree files untouched.
- Config: `/data/scripts/dwt/qwen3-tts/qwen3_tts_fidelity.yaml`.
- Startup: `/data/scripts/dwt/qwen3-tts/start_tts.sh`.
- Observed TTS launcher PID 95, API PID 101, process group 95. These are historical
  identifiers, not safe targets to reuse without checking the current process tree.
- Device selection: `ASCEND_RT_VISIBLE_DEVICES=0`; both stages use NPU 0.

The active YAML is byte-for-byte identical to `tts-before.yaml` in this directory
and to `deployment/stable/npu/tts/qwen3_tts_fidelity.yaml` at checkpoint time.
`start-tts-before.sh` is the exact remote startup file, not the parameterized
redeployment wrapper elsewhere in the repository.

| File | SHA-256 |
| --- | --- |
| Active YAML | `e373aa188b88daf39a81a477f046df917b9bfdda419f881d2d04c5875bd72f09` |
| Remote startup | `5a8a3fc5262c7173061baa0e3b5e69bf73ef6c09bdb200e9952483dae2984c8a` |
| Engine `model_executor/stage_input_processors/qwen3_tts.py` | `849d39749c549612cd3c1a639e6f50772b0abceb989c5c5f799e3d451669e874` |
| Engine `model_executor/models/qwen3_tts/qwen3_tts_code2wav.py` | `149756de792fb683e09bcfb04aacd5ee705e0f7e6cfefdb3a9afec16468664d7` |

## Backup and restart procedure

IMPORTANT correction discovered during application: node1 already manages this
process through `qwen-tts.service`, with `Restart=always` and `RestartSec=10`.
Its unchanged definition is captured in `qwen-tts.service.before`. Use the managed
procedure below, not a detached second launch. The initial manual stop caused
systemd to restart TTS automatically; a duplicate launched by this experiment
was stopped, leaving only the systemd-managed instance. No unit was modified.

Before the first service change, create a root-only backup directory on node1:
`/data/scripts/dwt/qwen3-tts/checkpoints/2026-09-05-before-chunk10` (mode 700).
Copy the current YAML, startup file and `/proc/<verified-api-pid>/environ` there,
preserving the YAML/startup and saving the environment as `environ.before`
(mode 600). Save `/proc/<verified-api-pid>/cmdline` as `cmdline.before` too.
Never commit the environment: it may contain credentials. The startup environment
includes CANN 9.0.0 paths and must be preserved; a fresh generic shell is not an
equivalent restart. Avoid `bash -lc`: login initialization hung during inspection.

Locally, copy `.env` to the private, git-ignored directory
`.local/checkpoints/2026-09-05-tts-turn-taking/.env.before` before changing it.
Do not restore the whole file over later unrelated edits; restore only the
specific latency variables from it.

Apply only `codec_chunk_frames: 25` -> `10` in a candidate YAML, review its diff,
then copy it over the active path. Leave the committed `tts-before.yaml` intact.
Do not overwrite a live file whose checksum changed since the checkpoint.

For both future apply and rollback:

1. Ensure no calls or TTS requests are active. Check `systemctl cat
   qwen-tts.service` and the current process tree inside `tts`. Verify the unit
   still targets only the TTS container/process and matches the captured unit.
2. Restart only that unit with `systemctl restart qwen-tts.service`. Do not run a
   second detached `docker exec` launch: the unit owns process supervision. Do
   not restart Docker, the container, the server, or any other service.
3. Check `systemctl status qwen-tts.service` and `journalctl -u qwen-tts.service`.
   Model initialization takes minutes; unit "active" alone does not mean the
   HTTP API is ready. Confirm exactly one TTS API process and both model stages.
4. Wait for the TTS API health endpoint and a successful PCM synthesis request.
   Check logs for initialization/decoder errors and repeat the timing probe.
5. Verify LLM, ASR, local LiveKit and the registered voice worker remain healthy.

## Rollback

Copy `tts-before.yaml` from the checkpoint back to the active YAML path and verify
the SHA-256 above. Restart only the TTS process with the same procedure. The image,
engine and weights are unchanged by this experiment, so no image rollback or
server restart is needed. If the remote copy is lost, recover the committed YAML
and startup from this checkpoint commit; do not use a mutable branch tip.

For a turn-taking rollback, restore only the changed variable in local `.env`:
`TURN_MIN_ENDPOINTING_DELAY=0.3` or
`ODION_STT_REALTIME_ENDPOINTING_SILENCE_SECONDS=0.5`, according to which was changed.
If maximum endpointing was changed, restore `TURN_MAX_ENDPOINTING_DELAY=0.65` too.
Restart only the idle local voice worker with its existing local service endpoints
and model selection. Do not restart LiveKit or the dashboard for this setting.
Verify exactly one worker registers before starting another call.

## Local code checkpoint verification

`PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m unittest discover tests`:
175 tests passed before any experiment edits. Plain discovery loads the local
NPU `.env` and produces 2 failures and 4 errors in default-TTS tests; disabling
dotenv isolates the tests without changing the running worker or its environment.

The checkpoint commit includes the already-present voice-auth gating, fixed and
LLM-generated waiting speech, local recording integration and their tests.
These are existing changes, not changes introduced for the latency experiment.
Secrets, recordings, model weights, virtual environments and `.idea` are excluded.

Other repositories are not touched by this experiment and have existing local
changes which this voice-agent checkpoint does not commit:

- Dashboard: `wema-bank-poc-tools`, HEAD `e536808a6392d93ede4fd703996f87385f9e5ec2`.
- Conversation service: `codex/promote-dev-to-staging`, HEAD
  `2df5f45c4cb016070548aab4dea6aa6651fd7747`.

This checkpoint is not a new full-machine/image backup. It preserves everything
this scoped experiment is permitted to change, and identifies the unchanged
runtime needed to reverse it.
