# Initial TTS chunk experiment: 2 to 8 frames

Pre-change checkpoint: `cc907c2` on `wema-bank-poc-tools`. This already preserves
the 10-frame steady TTS chunks and 0.25 / 0.50-second LiveKit delay experiment.
The user approved changing only `ASCEND_TTS_INITIAL_CODEC_CHUNK_FRAMES=2` to `8`.
At creation of this note, the live worker still uses 2; apply/restart is pending
until the active call ends. No NPU service restart is needed for this request field.

## Scope

| Setting | Before | Trial |
| --- | ---: | ---: |
| Request initial codec frames | 2 (160 ms audio) | 8 (640 ms audio) |
| Server steady codec frames | 10 | unchanged |
| Playback startup buffer | 0 ms | unchanged |
| Odion ASR final silence | 0.25 s | unchanged |
| LiveKit minimum / maximum delay | 0.25 / 0.50 s | unchanged |

Provider, model, voice, authentication, tools, recording, endpoint and network
settings stay unchanged. Deepgram remains the selected ASR provider.

## Isolated request probes before applying

Identical greeting text and existing Helen voice, through the jumper. No saved
runtime configuration was changed for these probes. Measurements reflect delivery
timing, not a human assessment of audio quality. Inference is stochastic and the
audio durations vary. The shortfall estimates the startup delay required to avoid
running out of received PCM during immediate playback.

| Initial frames | First audio | First burst audio | First gap | Estimated shortfall |
| --- | ---: | ---: | ---: | ---: |
| 2, probe 1 | 0.333 s | 0.160 s | 0.506 s | 0.346 s |
| 8, probe 1 | 0.608 s | 0.640 s | 0.501 s | 0 s |
| 10, probe 1 | 0.718 s | 0.800 s | 0.493 s | 0 s |
| 2, probe 2 | 0.333 s | 0.160 s | 0.616 s | 0.456 s |
| 8, probe 2 | 0.689 s | 0.640 s | 0.583 s | 0 s |
| 10, probe 2 | 0.703 s | 0.800 s | 0.502 s | 0.340 s (later gap) |

Additional 8-frame probes:

| Text | First audio | Audio duration | Estimated shortfall |
| --- | ---: | ---: | ---: |
| Greeting | 0.643 s | 2.640 s | 0 s |
| Balance acknowledgement | 0.606 s | 1.840 s | 0 s |
| Data-plan explanation | 0.614 s | 11.920 s | 0 s |

Eight frames avoided modeled playback starvation across five probes, at the cost
of roughly 0.3 seconds additional latency before the first sound. Ten frames did
not consistently improve the result. These observations are not a guarantee under
load or variable networking; a real-call listening test remains necessary.

## Apply and rollback

Apply only the one initial-frame value in the local voice-worker `.env` and keep
the tracked experiment override file in sync. Validate the generated TTS request
contains `initial_codec_chunk_frames: 8`, then restart only the idle voice worker
using its existing command. Confirm a single worker registers and TTS synthesizes.
Do not restart the NPU service, LiveKit, dashboard or other services for this trial.

To undo only this experiment, restore `ASCEND_TTS_INITIAL_CODEC_CHUNK_FRAMES=2`
and restart the idle voice worker. Keep steady frames at 10 and the current turn
delays. The full earlier rollback, including the remote server YAML, remains in
`RESULTS.md`; it is not necessary for undoing this single request change.
