# Gemini Live Minimal Experiment

Branch:
- `codex/gemini-live-minimal-experiment`

Purpose:
- Keep the stable cascade voice path intact.
- Add a tiny isolated Gemini Live runtime path that can be enabled with:
  - `VOICE_RUNTIME_MODE=gemini_live_minimal`

Reference repo:
- `/Users/woron/Documents/sales-girl/_generated_repos/project-gemini-flash-live/src/pipeline/live_client.py`

What the reference repo does well:
- Uses a very small Gemini Live config.
- Avoids extra realtime knobs.
- Sends one conversational prompt and reads audio/transcript back.
- Uses:
  - `response_modalities: ["AUDIO"]`
  - `output_audio_transcription: {}`
  - `speech_config.voice_config.prebuilt_voice_config.voice_name`
  - `system_instruction`

What this experiment uses:
- LiveKit `google.realtime.RealtimeModel`
- Minimal config only:
  - `model`
  - `voice`
  - `instructions`
  - `modalities=[AUDIO]`
  - `output_audio_transcription`

What is intentionally not included in the minimal mode:
- `enable_affective_dialog`
- `proactivity`
- `input_audio_transcription`
- explicit language override
- other extra realtime options that were part of the earlier crashing path

TTS notes:
- The stable non-Gemini path still uses the Odion cloned TTS implementation.
- The request shape follows the demo HTML contract:
  - `text`
  - `language`
  - `owner_id`
  - `voice_id`
  - `seed`
- Experiment values:
  - `ODION_TTS_EXPERIMENT_VOICE_ID=d270a5cec6914373b9deed1d1c3cbade`
  - `ODION_TTS_EXPERIMENT_OWNER_ID=mavinomichael@gmail.com`
  - `ODION_TTS_EXPERIMENT_SEED=0`

Rollback:
- Switch `VOICE_RUNTIME_MODE` back to `cascade`.
- Restart the voice worker.

Current blocker to watch:
- The earlier Gemini Live path was closing with `realtime_model_error` and `APIError('1011 None. Internal error encountered.')`.
- The minimal path is meant to test whether that failure was caused by the extra runtime options rather than Gemini Live itself.

Smoke test result:
- A direct `genai.Client(...).aio.live.connect(...)` call with the same model/voice family succeeded locally.
- It returned `145920` audio bytes and the transcript `Hello, I  am  your  restaurant  assistant.`, which suggests the Gemini model/config itself is healthy.
- Remaining thing to prove is the LiveKit worker wrapper path under a real widget/public-agent call.
