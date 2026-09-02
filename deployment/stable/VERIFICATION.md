# Snapshot Verification

Verification performed on 2026-09-02 without restarting or reconfiguring any
jumper, NPU, LiveKit, voice-agent, or dashboard service.

## End-to-End QA

The local Voice Lab call completed through the full cascade with the Nia fixed
customer-care script before this snapshot was created. The worker received
microphone audio, produced realtime transcripts, sent final turns to Qwen 3.8
27B, returned cached-Helen TTS audio, and accepted barge-in.

Observed ASR quality notes from the fixed script included occasional phonetic
substitutions (`order`/`other`, `Lekki`/`Lakey`, and `parcel`/`password`) and one
sentence split at a pause. Those are model-quality baseline observations, not
missing audio or transport failures.

## Automated Checks

```text
Voice-agent unit tests: 85 passed
Command: PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m unittest discover tests

Dashboard focused ESLint: passed
Command: npx eslint app/internal/voice-lab/voice-lab-page-client.jsx

Layer Lab TTS metric tests: 2 passed
Command: npm run test:layerlab-tts

Shell syntax, Python syntax, JSON parsing, and TTS YAML parsing: passed
Git whitespace check: passed
Credential-pattern audit of staged files: passed
Tracked recovery files over 50 MiB: none
Private Helen voice SHA-256 verification: passed
```

The first unisolated unit-test invocation loaded the developer's active `.env`
through `python-dotenv`. Six legacy-default TTS assertions therefore inherited
the stable Ascend cached-voice mode and 80 ms framing. The clean CI-equivalent
run above disables dotenv loading and passes all 85 tests. No test was removed
or skipped.

## Direct Live Probes

```text
Gateway /health: {"status":"ok"}
LLM: returned "I am reachable."
TTS: returned 226,560 bytes of streamed PCM
ASR WebSocket: connected and returned a non-empty English hypothesis
```

The ASR smoke probe used a macOS synthetic voice and returned only the opening
phrase, `Hello from.` It proves WebSocket/session/audio/event reachability, not
recognition quality. Use the recorded fixed QA script and compare the expected
text turn by turn for acceptance testing; do not tune server cadence from a
single synthetic sample.
