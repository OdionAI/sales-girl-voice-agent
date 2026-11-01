"""Language-specific speech routes for the shared SBPN recognizer."""
from __future__ import annotations

from dataclasses import replace

import aiohttp

from .pidgin import SBPN_MODEL, SBPN_URL, SBPN_WS_URL, apply_pidgin_stack, is_pidgin, split_text, LANGUAGE_RULES
from .providers import is_wema_voice


LANGUAGES = {
    "yo": {"name": "Yoruba", "model": "coqui-yoruba-vits", "voice": "balogun",
           "url": "http://102.88.137.124:8080/tts-yoruba/v1/audio/speech",
           "opening": "Ẹ káàbọ̀. Báwo ni mo ṣe lè ràn yín lọ́wọ́ lónìí?",
           "goodbye": "Ẹ ṣé gan-an. Ó dàbọ̀.",
           "inactivity_checkin": "Ṣé ẹ ṣì wà níbẹ̀? Ṣé mo tún lè ràn yín lọ́wọ́?",
           "inactivity_goodbye": "Mi ò gbọ́ ìdáhùn yín, nítorí náà màá parí ìpè yìí báyìí. Ó dàbọ̀.",
           "acknowledgement": "Ẹ jọ̀ọ́, ẹ fún mi ní àkókò díẹ̀ láti ṣàyẹ̀wò rẹ̀."},
    "ha": {"name": "Hausa", "model": "coqui-hausa-vits", "voice": "hausa",
           "url": "http://102.88.137.124:8080/tts-hausa/v1/audio/speech",
           "opening": "Sannu da zuwa. Ta yaya zan iya taimaka muku yau?",
           "goodbye": "Na gode sosai. Sai an jima.",
           "inactivity_checkin": "Kuna nan kuwa? Akwai wani abu da zan taimaka muku?",
           "inactivity_goodbye": "Ban ji amsarku ba, zan kawo ƙarshen kiran yanzu. Sai an jima.",
           "acknowledgement": "Don Allah, ku ba ni ɗan lokaci in duba muku."},
    "ig": {"name": "Igbo", "model": "igbo-sorotts-three-voices-v1", "voice": "chinedu",
           "url": "http://102.88.137.124:8080/tts-igbo/v1/audio/speech",
           "request_language": "ig",
           # CPU research endpoint emits PCM only after generating each sentence.
           "request_timeout_seconds": 1800,
           "opening": "Ndeewo. Kedu ka m ga-esi nyere gị aka taa?",
           "goodbye": "Daalụ nke ukwuu. Ka ọ dị.",
           "inactivity_checkin": "Ị ka nọ ebe ahụ? Ọ dị ihe ọzọ m nwere ike inyere gị aka?",
           "inactivity_goodbye": "Anụghị m azịza gị, ya mere m ga-akwụsị oku a ugbu a. Ka ọ dị.",
           "acknowledgement": "Biko, chere obere oge ka m lelee ya."},
}

# CLEAR Global TWB Voice Hausa 1.0 is CC-BY-NC-4.0, for opted-in research only.
HAUSA_TWB_VOICES = {"haisha": "spk_f_1", "adamu": "spk_m_1", "muhammad": "spk_m_2"}
ABDUL_MODEL = "hausa-ljspeech-vits-epoch271"
ABDUL_CHECKPOINT = "a92c761456510945bd1a556987c673bc7768546f97436eb840bf6e06b4f64dc4"
HAUSA_LEGACY_VOICES = {"", "default", "hausa", "musa", "coqui-hausa-vits", "hausa-twb-voice-1.0"}
# SoroTTS is CC-BY-NC-SA-4.0: these speakers are for noncommercial evaluation.
IGBO_VOICES = {"chinedu": "ibo1", "eze": "ibo2", "ada": "ibo3"}
IGBO_LEGACY_VOICES = {"", "default", "igbo", "igbo-sorotts-three-voices-v1",
                      "qwen-igbo-cleaned5h-epoch20", "qwen-igbo-epoch20"}


def igbo_voice(value):
    selected = str(value or "").strip().lower()
    if selected in IGBO_LEGACY_VOICES:
        return "chinedu"
    for name, speaker in IGBO_VOICES.items():
        if selected in {name, speaker}:
            return name
    raise ValueError("Unknown Igbo voice selection")


def hausa_voice(value):
    selected = str(value or "").strip().lower()
    if selected in HAUSA_LEGACY_VOICES:
        return "haisha"
    for name, speaker in HAUSA_TWB_VOICES.items():
        if selected in {name, speaker}:
            return name
    raise ValueError("Unknown Hausa voice selection")


def language_code(value):
    language = str(value or "").strip().lower()
    if is_pidgin(language):
        return "pcm"
    if language in {"yo", "yor", "yoruba", "yorùbá"}:
        return "yo"
    if language in {"ha", "hau", "hausa"}:
        return "ha"
    if language in {"ig", "ibo", "igbo"}:
        return "ig"
    return ""


def uses_sbpn(language):
    return bool(language_code(language))


def apply_language_stack(config, context=None, identity=None):
    overrides = (identity or {}).get("runtime_overrides") or {}
    selected = (overrides.get("tts_language_hint") or config.speech_language_override
                or getattr(context, "tts_language_hint", "") or config.tts_language)
    profile = LANGUAGES.get(language_code(selected))
    if profile is None:
        config = apply_pidgin_stack(config, context, identity)
        selected_language = str(selected or "").strip().lower().replace("_", " ")
        has_runtime_voice = bool(overrides.get("tts_voice_id") or overrides.get("tts_voice"))
        saved_voice = str(getattr(context, "tts_voice_id", "") or "").strip()
        if (
            not has_runtime_voice
            and selected_language in {"", "en", "english", "american english", "nigerian english"}
            and is_wema_voice(saved_voice)
        ):
            # English voice selection is persisted with the agent config. Apply
            # only the supported Wema cached ICL IDs here; other agent voice
            # routing and explicit signed session overrides keep their current
            # behavior.
            return replace(config, tts_voice=saved_voice, tts_ref_audio="", tts_ref_text="")
        return config
    model, voice = profile["model"], profile["voice"]
    if language_code(selected) == "ig":
        saved = (getattr(context, "tts_voice_id", "")
                 if language_code(getattr(context, "tts_language_hint", "")) == "ig" else "")
        configured = str(config.tts_voice or "").strip().lower()
        if configured not in IGBO_VOICES and configured not in IGBO_VOICES.values():
            configured = ""
        voice = igbo_voice(overrides.get("tts_voice_id") or overrides.get("tts_voice") or saved or configured)
    if language_code(selected) == "ha":
        saved = (getattr(context, "tts_voice_id", "")
                 if language_code(getattr(context, "tts_language_hint", "")) == "ha" else "")
        configured = str(config.tts_voice or "").strip().lower()
        if configured not in HAUSA_TWB_VOICES and configured not in HAUSA_TWB_VOICES.values() and configured != "abdul":
            configured = ""
        requested = overrides.get("tts_voice_id") or overrides.get("tts_voice") or saved or configured
        if str(requested or "").strip().lower() == "abdul":
            model, voice = ABDUL_MODEL, "abdul"
        else:
            chosen = hausa_voice(requested)
            if config.hausa_twb_voices_enabled:
                model, voice = "hausa", chosen
            elif str(requested or "").strip().lower() not in HAUSA_LEGACY_VOICES:
                raise ValueError("Hausa research voices are not enabled")
    return replace(config, tts_language=profile["name"], tts_model=model,
                   tts_voice=voice, tts_url=profile["url"], tts_ref_audio="",
                   stt_model=SBPN_MODEL, stt_ws_url=SBPN_WS_URL, stt_batch_url=SBPN_URL,
                   stt_batch_timeout_seconds=120.0)


def language_rules(language):
    code = language_code(language)
    if code == "pcm":
        return LANGUAGE_RULES
    profile = LANGUAGES.get(code)
    if not profile:
        return ""
    return f"""

CALL LANGUAGE: {profile['name']} ({code}). Speak natural {profile['name']} throughout
this call. Give greetings, tool acknowledgements, progress updates, answers and
goodbyes in {profile['name']}, even when business instructions, retrieved information
or tool results are in English. Use the language's correct native spelling and
diacritics. Preserve names, amounts, account numbers and factual meaning. Spell
spoken numbers in {profile['name']} words; read identifiers digit by digit without
losing zeros. Keep numeric tool arguments as numbers and tool names and JSON keys
unchanged. Use short, complete spoken sentences and ask one question at a time.
Keep all existing safety and business rules. Do not invent facts.
"""


class NativeLanguageTTSClient:
    """Native-language HTTP streams use the call's 24 kHz mono PCM format."""
    def __init__(self, config, session):
        self.config, self.session, self.usage = config, session, None

    async def stream(self, text):
        is_abdul = language_code(self.config.tts_language) == "ha" and self.config.tts_voice == "abdul"
        async def chunks():
            for part in split_text(text, limit=140):
                if not part.strip():
                    continue
                payload = {
                    "model": self.config.tts_model, "voice": self.config.tts_voice,
                    "input": part, "response_format": "pcm", "stream": True,
                }
                profile = LANGUAGES.get(language_code(self.config.tts_language), {})
                if is_abdul:
                    payload.update(model=ABDUL_MODEL, voice="abdul", speaker_id="abdul",
                                   language="ha", stream_format="audio", speed=1, seed=17)
                elif language_code(self.config.tts_language) == "ha" and self.config.hausa_twb_voices_enabled:
                    selected = hausa_voice(self.config.tts_voice)
                    payload.update(model="hausa", voice=selected, speaker_id=HAUSA_TWB_VOICES[selected],
                                   stream_format="audio", speed=1, seed=17)
                if profile.get("request_language"):
                    payload["language"] = profile["request_language"]
                async with self.session.post(self.config.tts_url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=profile.get("request_timeout_seconds", 60))) as response:
                    response.raise_for_status()
                    if (response.content_type != "audio/pcm" or
                        response.headers.get("X-Audio-Sample-Rate") != "24000" or
                        response.headers.get("X-Audio-Channels") != "1"):
                        raise ValueError("Native-language TTS must stream 24 kHz mono PCM16")
                    if is_abdul and any(response.headers.get(key) != value for key, value in {
                        "X-Model": ABDUL_MODEL, "X-Voice": "abdul", "X-Speaker-ID": "abdul",
                        "X-Checkpoint-SHA256": ABDUL_CHECKPOINT,
                    }.items()):
                        raise ValueError("Abdul TTS returned an unexpected voice or checkpoint")
                    pending = b""
                    received = 0
                    async for block in response.content.iter_chunked(9600):
                        received += len(block)
                        if received > 30 * 24000 * 2:
                            raise ValueError("Native-language audio exceeds the 30-second segment budget")
                        pending += block
                        length = len(pending) // 2 * 2
                        if length:
                            yield pending[:length]
                            pending = pending[length:]
                    if not received or pending:
                        raise ValueError("Native-language TTS returned empty or truncated PCM16")
                    if self.usage is not None:
                        self.usage.tts_accepted(part)
        return 24000, chunks()


CoquiTTSClient = NativeLanguageTTSClient
