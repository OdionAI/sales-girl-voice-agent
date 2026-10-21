# Nigerian Pidgin speech stack

Saved agent `tts_language_hint` values `Nigerian Pidgin`, `Pidgin`, `pcm`, or `pd`
select SBPN ASR and Ejiro TTS in the RVC pipeline, for web, SDK and SIP calls.
The dashboard saves `tts_provider=odion_tts`, `tts_voice_id=mms-pcm-ejiro-3h`.
The existing LLM endpoint, model and credentials are unchanged. A per-call system
instruction requires Nigerian Pidgin for the LLM's spoken output, including
knowledge followups. Other languages retain their existing speech providers.

- ASR: `ws://102.88.137.124:8080/asr-sbpn/v1/realtime`, model
  `ogunlao/SBPN_multilingual_base`. Language detection is automatic. Bounded
  recovery uses `/asr-sbpn/v1/audio/transcriptions` multipart uploads.
- TTS: `http://102.88.137.124:8080/tts-ejiro/v1/audio/speech`, model
  `mms-pcm-ejiro-3h`, voice `ejiro`, `stream=true`, `response_format=pcm`.
- Neither endpoint currently requires authentication. Do not attach credentials
  from other speech providers to these requests. They currently use plaintext
  HTTP; deployment operators should restrict access or provide TLS when needed.

Both endpoints support streaming as of 2026-10-21. SBPN emits revisable buffered
prefixes: use its complete `text`, never blindly concatenate `delta`. Existing
VAD owns turn boundaries and final commit. Idle silence does not fill the bounded
provider buffer. A disconnect, missing final, or turn over 29 seconds falls back
to bounded batch uploads of the complete turn, joined into one final transcript.
This is buffered prefix re-decoding, not causal/cache-aware ASR.

TTS splits replies losslessly into at most 240-character requests, preferring
sentence and word boundaries. Streaming response metadata is validated; a stateful
resampler converts incoming 16 kHz mono PCM16 to the existing 24 kHz call transport
without waiting for the whole response. Partial PCM samples are carried between
network chunks. Empty, truncated or oversized audio is rejected. Model-level
streaming uses overlap-context vocoder windows, not incremental text input.

Voice Lab's explicit `tts_language_hint` override takes precedence over saved
agent language. Selecting English again restores the normal provider path.
Changing language on a saved agent takes effect on the next call.

Roll out the worker before the dashboard. Test a saved, isolated Pidgin agent
without speech overrides and verify both `stt_realtime_final` and `tts_first_audio_chunk`
events, audible audio and Pidgin answers. Preserve existing worker environment,
Compose overlays, images and other agents; do not use the legacy Huawei workflow
for the Lagos RVC worker. Rollback is the previous worker image and dashboard release.

Checks: `python -m unittest discover -s latency_lab/tests`.
