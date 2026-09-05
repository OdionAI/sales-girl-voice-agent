# Applied experiment: 2026-09-05

Pre-change voice-agent checkpoint: `13b4f0739d5ed629743224a6ee2d8e7e36ee1e2e`.
Branch: `wema-bank-poc-tools`. No push or stable-branch promotion performed.

## Actual changes

| Setting | Before | Applied |
| --- | --- | --- |
| Remote TTS `codec_chunk_frames` | 25 | 10 |
| Local `ODION_STT_REALTIME_ENDPOINTING_SILENCE_SECONDS` | 0.50 s | 0.25 s |
| Local `TURN_MIN_ENDPOINTING_DELAY` | 0.30 s | 0.25 s |
| Local `TURN_MAX_ENDPOINTING_DELAY` | 0.65 s | 0.50 s |

Only these three local `.env` keys changed, verified against its private backup.
No voice-agent Python code changed for this experiment. Stable defaults/templates
remain unchanged. `voice-agent-overrides.env` contains the reproducible, non-secret
experiment overrides: apply them to the worker environment before starting it.

The 0.60 -> 0.50 s historical tune was the Odion ASR final-silence gate, not the
LiveKit minimum delay. Current calls still select Deepgram Nova-3. Therefore the
Odion 0.25 s value applies only when Odion ASR is selected; LiveKit's 0.25 / 0.50 s
delays affect the current Deepgram session. No ASR provider was switched and no
remote ASR inference cadence was modified. Faster endpointing can split natural
pauses or digit sequences; validate those in a real call before promoting.

The remote active YAML differs from the backup on exactly one line. New SHA-256:
`fcfb8a4ba1febdc39b5057eafffdf00794ac44ee27c237709952c5056deb210c`.
The startup script, left context (72), capture sizes, request initial frames (2),
voice, model, PCM format, playback buffer (0), engine code and image are unchanged.

## Timing evidence

Same input and voice for every probe:
"Hello, this is SAW. How can I help you today?"
Generation remains stochastic, so total audio duration varies. Tests were serial,
not concurrent. Bursts group network reads within 20 ms; they are not HTTP frame
sizes. `startup_buffer_needed_s` estimates the buffer needed to avoid starvation
in immediate real-time PCM playback, not a measurement of perceived sound quality.

| Measurement | First audio | First inter-burst gap | Estimated startup buffer needed | Total request | Audio duration |
| --- | ---: | ---: | ---: | ---: | ---: |
| Before, inside TTS container | 0.360 s | 1.190 s | 1.030 s | 1.859 s | 2.640 s |
| After, first cold request | 37.321 s | 0.372 s | 0.212 s | 38.906 s | 2.800 s |
| After, warm container probe 1 | 0.340 s | 0.507 s | 0.347 s | 2.041 s | 2.800 s |
| After, warm container probe 2 | 0.323 s | 0.507 s | 0.347 s | 2.218 s | 3.120 s |
| After, warm container probe 3 | 0.317 s | 0.503 s | 0.343 s | 2.074 s | 2.880 s |
| After, through jumper from local agent machine | 0.343 s | 0.642 s | 0.484 s | 1.960 s | 2.560 s |

Steady audio bursts dropped from 2.0 s to 0.8 s, as intended. Warm first gaps
inside the container dropped about 58%, but they still exceed the 0.16 s initial
audio supply. The change improves the gap; it does NOT eliminate playback
starvation with the unchanged zero-buffer setting. No further buffer/initial-frame
tuning was applied. The cold request is reported separately, not hidden in a warm
average. Subsequent probes succeeded before handing the service back for testing.

Probe source: `probe_tts.py`. On the remote node, warm JSON results are in the
private checkpoint directory as `after-warm1.json` through `after-warm3.json`.
To repeat locally:

```sh
.venv/bin/python deployment/experiments/2026-09-05-tts-turn-taking/probe_tts.py
```

## Service lifecycle and validation

The existing systemd unit `qwen-tts.service` automatically restarted the TTS
process after its verified process group received SIGTERM. This supervision was
discovered during the restart. A second launch by this experiment was terminated
with SIGTERM, leaving only the managed API process (PID 16959 in the container)
and its two stages. Future restarts MUST go through the existing systemd unit.
No unit or startup script was edited. Managed-process environment matched the
pre-change `/proc/101/environ` snapshot exactly, with no differing keys.

The `tts` container's start time remains `2026-08-26T08:22:00.087012041Z`, and its
image ID is unchanged. No server, container, network, port, SSH, ASR, LLM,
LiveKit, authentication sidecar, dashboard or conversation-service restart occurred.

The idle local voice worker was stopped with SIGINT and restarted using its
original command and local service endpoints. It registered as
`AW_3WQgCxn9iigM`, same agent name and local LiveKit URL. Its configured delays
and Deepgram provider were checked after applying the overrides.

Verification:

- No LiveKit rooms and no queued/running TTS requests before changing the service.
- Remote TTS health HTTP 200 and four warm synthesis requests completed.
- LLM model discovery HTTP 200, model `qwen3.8_27b`.
- Existing NPU ASR WebSocket connected; no inference configuration changed.
- Public caller page HTTP 200 and local voice worker registered.
- Isolated voice-agent suite: 175 tests passed before and after changes.
- Human listening, full voice conversation and digit/pause tolerance remain to be
  validated by the user. No claim that the audio artifact is fully resolved.

## Rollback without unrelated changes

On node1, when there are no calls or TTS requests:

```sh
cp /data/scripts/dwt/qwen3-tts/checkpoints/2026-09-05-before-chunk10/qwen3_tts_fidelity.yaml /data/scripts/dwt/qwen3-tts/qwen3_tts_fidelity.yaml
sha256sum /data/scripts/dwt/qwen3-tts/qwen3_tts_fidelity.yaml
systemctl restart qwen-tts.service
journalctl -u qwen-tts.service -n 30 --no-pager
```

Expected restored YAML hash:
`e373aa188b88daf39a81a477f046df917b9bfdda419f881d2d04c5875bd72f09`.
Wait for health and a successful warm PCM probe. Do not start an additional
detached TTS process. This leaves Docker, the node and all other services running.

Restore just these three local values, then restart only the idle voice worker:

```dotenv
ODION_STT_REALTIME_ENDPOINTING_SILENCE_SECONDS=0.5
TURN_MIN_ENDPOINTING_DELAY=0.3
TURN_MAX_ENDPOINTING_DELAY=0.65
```

Original private local `.env` backup:
`.local/checkpoints/2026-09-05-tts-turn-taking/.env.before`.
Original private remote backup:
`/data/scripts/dwt/qwen3-tts/checkpoints/2026-09-05-before-chunk10/`.
Do not commit either private environment backup, reset the entire worktree, or
restore other repositories to reverse this experiment.
