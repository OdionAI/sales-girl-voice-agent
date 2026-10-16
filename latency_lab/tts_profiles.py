"""Named model routes shared by saved agent voices and Voice Lab overrides."""

TTS_MODEL_PROFILES = {
    "yinka-v7-test": {
        "tts_url": "http://102.88.137.124:8080/tts-yinka-v7/v1/audio/speech",
        "tts_voice": "yinka",
        "tts_task_type": "CustomVoice",
        "tts_language": "Auto",
        "tts_seed": 1,
        "tts_initial_codec_chunk_frames": 6,
        "tts_max_new_tokens": 4096,
    },
    "Qwen3-TTS": {
        "tts_url": "http://102.88.137.124:8080/tts/v1/audio/speech",
        "tts_voice": "helen-mavino-0030",
        "tts_task_type": "Base",
        "tts_language": "English",
        "tts_seed": None,
        "tts_initial_codec_chunk_frames": 6,
        "tts_max_new_tokens": None,
    },
}
