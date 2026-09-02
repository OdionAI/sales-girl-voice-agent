# Runtime Tuning Log

Change one control at a time and validate both latency and complete transcription
before promoting an experiment into the stable baseline.

## 2026-09-03 Local Voice Lab

| Control | Previous / stable | Active local value | Result |
| --- | ---: | ---: | --- |
| Qwen thinking | enabled or provider default | disabled | Lower LLM latency; retained as stable behavior. |
| ASR language | automatic / unconstrained | English | Prevented unrelated-script hypotheses and improved English recognition. |
| ASR cumulative decode cadence | earlier `0.4s` trial | `0.8s` | `0.4s` degraded/cut off recognition; `0.8s` restored stable transcription. |
| LiveKit minimum endpointing delay | `0.45s` | `0.30s` | Reduced perceived response latency in Voice Lab; promoted to stable. |
| LiveKit maximum endpointing delay | `0.90s` | `0.65s` | Reduced the upper wait bound; promoted with the minimum delay. |
| Client ASR final-silence gate | `0.70s` | `0.60s` | Reduced turn latency without observed transcription regression; promoted to stable. |
| Client PCM upload chunk | `100ms` | `100ms` | Unchanged. |
| Client minimum speech | `0.20s` | `0.20s` | Unchanged. |
| Client VAD activation threshold | `0.50` | `0.50` | Unchanged. |
| Minimum interruption duration | `0.10s` | `0.10s` | Unchanged and already at the configured lower bound. |

The `0.30s` / `0.65s` endpointing experiment was accepted and promoted to the
runtime defaults and stable environment templates before beginning another
turn-taking experiment.

The `0.60s` ASR final-silence experiment was accepted and promoted to the
runtime default and stable deployment templates.

## 2026-09-03 0.50s Silence Tune

| Control | Previous value | Current stable value | Result |
| --- | ---: | ---: | --- |
| Client ASR final-silence gate | `0.60s` | `0.50s` | Reduced latency without observed loss of final words or pause tolerance; promoted to stable. |

All other turn-handling and ASR cadence controls remain at the stable values
listed above.
