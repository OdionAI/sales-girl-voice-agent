"""Language-specific speech routes for the shared SBPN recognizer."""
from __future__ import annotations

from dataclasses import replace

import aiohttp

from .pidgin import SBPN_MODEL, SBPN_URL, SBPN_WS_URL, apply_pidgin_stack, is_pidgin, split_text, LANGUAGE_RULES


LANGUAGES = {
    "yo": {"name": "Yoruba", "model": "coqui-yoruba-vits", "voice": "yoruba",
           "url": "http://102.88.137.124:8080/tts-yoruba/v1/audio/speech",
           "opening": "Ẹ káàbọ̀. Báwo ni mo ṣe lè ràn yín lọ́wọ́ lónìí?",
           "goodbye": "Ẹ ṣé gan-an. Ó dàbọ̀.",
           "acknowledgement": "Ẹ jọ̀ọ́, ẹ fún mi ní àkókò díẹ̀ láti ṣàyẹ̀wò rẹ̀."},
    "ha": {"name": "Hausa", "model": "coqui-hausa-vits", "voice": "hausa",
           "url": "http://102.88.137.124:8080/tts-hausa/v1/audio/speech",
           "opening": "Sannu da zuwa. Ta yaya zan iya taimaka muku yau?",
           "goodbye": "Na gode sosai. Sai an jima.",
           "acknowledgement": "Don Allah, ku ba ni ɗan lokaci in duba muku."},
}


def language_code(value):
    language = str(value or "").strip().lower()
    if is_pidgin(language):
        return "pcm"
    if language in {"yo", "yor", "yoruba", "yorùbá"}:
        return "yo"
    if language in {"ha", "hau", "hausa"}:
        return "ha"
    return ""


def uses_sbpn(language):
    return bool(language_code(language))


def apply_language_stack(config, context=None, identity=None):
    overrides = (identity or {}).get("runtime_overrides") or {}
    selected = (overrides.get("tts_language_hint") or config.speech_language_override
                or getattr(context, "tts_language_hint", "") or config.tts_language)
    profile = LANGUAGES.get(language_code(selected))
    if profile is None:
        return apply_pidgin_stack(config, context, identity)
    return replace(config, tts_language=profile["name"], tts_model=profile["model"],
                   tts_voice=profile["voice"], tts_url=profile["url"], tts_ref_audio="",
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


class CoquiTTSClient:
    """Yoruba/Hausa sentence streams already use the call's 24 kHz PCM format."""
    def __init__(self, config, session):
        self.config, self.session, self.usage = config, session, None

    async def stream(self, text):
        async def chunks():
            for part in split_text(text, limit=140):
                if not part.strip():
                    continue
                async with self.session.post(self.config.tts_url, json={
                    "model": self.config.tts_model, "voice": self.config.tts_voice,
                    "input": part, "response_format": "pcm", "stream": True,
                }, timeout=aiohttp.ClientTimeout(total=60)) as response:
                    response.raise_for_status()
                    if (response.content_type != "audio/pcm" or
                        response.headers.get("X-Audio-Sample-Rate") != "24000" or
                        response.headers.get("X-Audio-Channels") != "1"):
                        raise ValueError("Yoruba/Hausa TTS must stream 24 kHz mono PCM16")
                    pending = b""
                    received = 0
                    async for block in response.content.iter_chunked(9600):
                        received += len(block)
                        if received > 30 * 24000 * 2:
                            raise ValueError("Yoruba/Hausa audio exceeds the 30-second segment budget")
                        pending += block
                        length = len(pending) // 2 * 2
                        if length:
                            yield pending[:length]
                            pending = pending[length:]
                    if not received or pending:
                        raise ValueError("Yoruba/Hausa returned empty or truncated PCM16")
                    if self.usage is not None:
                        self.usage.tts_accepted(part)
        return 24000, chunks()
