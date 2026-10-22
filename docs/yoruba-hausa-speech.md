# Yoruba and Hausa calls

Save the agent language as Yoruba or Hausa with Nigerian accent and the matching
Yoruba or Hausa voice. The RVC worker resolves the saved language for web, SDK and
SIP calls. A signed Voice Lab language override can select a different language.

| Language | ASR | TTS model | Public TTS endpoint |
| --- | --- | --- | --- |
| Yoruba (yo/yor) | SBPN multilingual | coqui-yoruba-vits | http://102.88.137.124:8080/tts-yoruba/v1/audio/speech |
| Hausa (ha/hau) | SBPN multilingual | coqui-hausa-vits | http://102.88.137.124:8080/tts-hausa/v1/audio/speech |

Both use the existing SBPN WebSocket and complete-turn recovery adapter. The
LLM model/credentials are retained. A language instruction applies to normal
answers, tool responses and knowledge follow-ups, including native spelling and
spoken numbers. Direct openings, goodbyes and lookup acknowledgements are localized.

TTS requests use stream=true and response_format=pcm. The server synthesizes
sentences before delivering PCM chunks; this is sentence streaming. Audio is
24 kHz mono signed PCM16, already at the call transport's sample rate. The client
checks headers, preserves odd network byte boundaries and rejects empty/truncated
audio. Requests are split losslessly at 140 characters; cancellation stops future
requests. The service's existing seed and speed defaults are retained.

English, French, Qwen voices and the Ejiro Pidgin adapter keep their existing
routes. SBPN chooses the recognized language automatically; agent selection
determines the response language and TTS route. Recognition and pronunciation
quality require evaluation with native speakers beyond integration smoke tests.
