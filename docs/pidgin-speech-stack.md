# Nigerian Pidgin speech stack

Saved agent `tts_language_hint` values `Nigerian Pidgin`, `Pidgin`, `pcm`, or `pd`
select SBPN ASR and Ejiro TTS in the RVC pipeline, for web, SDK and SIP calls.
The dashboard saves `tts_provider=odion_tts`, `tts_voice_id=mms-pcm-ejiro-3h`.
The existing LLM endpoint, model and credentials are unchanged. A per-call system
instruction requires Nigerian Pidgin for the LLM's spoken output, including
knowledge followups. Other languages retain their existing speech providers.

- ASR: `http://102.88.137.124:8080/asr-sbpn/v1/audio/transcriptions`, multipart
  model `ogunlao/SBPN_multilingual_base`. Language detection is automatic.
- TTS: `http://102.88.137.124:8080/tts-ejiro/v1/audio/speech`, model
  `mms-pcm-ejiro-3h`, voice `ejiro`, response format `wav`.
- Neither endpoint currently requires authentication. Do not attach credentials
  from other speech providers to these requests. They currently use plaintext
  HTTP; deployment operators should restrict access or provide TLS when needed.

These are **batch endpoints**, not streaming model APIs. VAD gathers caller
audio; turns longer than 29 seconds are sent in consecutive bounded recordings
and joined into one final transcript. There are no speculative ASR partials.
TTS splits replies losslessly into at most 240-character requests, preferring
sentence and word boundaries. Each 16 kHz mono PCM16 WAV is validated and
resampled to the existing 24 kHz call transport before playback. The next chunk
is synthesized sequentially; some delay between chunks is expected.

Voice Lab's explicit `tts_language_hint` override takes precedence over saved
agent language. Selecting English again restores the normal provider path.
Changing language on a saved agent takes effect on the next call.

Roll out the worker before the dashboard. Test a saved, isolated Pidgin agent
without speech overrides and verify both `stt_batch_final` and `tts_first_audio_chunk`
events, audible audio and Pidgin answers. Preserve existing worker environment,
Compose overlays, images and other agents; do not use the legacy Huawei workflow
for the Lagos RVC worker. Rollback is the previous worker image and dashboard release.

Checks: `python -m unittest discover -s latency_lab/tests`.
