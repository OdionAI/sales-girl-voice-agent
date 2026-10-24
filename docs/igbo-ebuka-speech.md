# Igbo calls with Ebuka

Select **Igbo**, **Nigerian**, and **Ebuka** in the agent's voice settings.
The saved voice model is `qwen-igbo-cleaned5h-epoch20`; `igbo` is the TTS
service's voice ID, not the agent's display name. `ig`, `ibo`, and `Igbo` all
resolve to the Igbo route. Existing agent names and other language routes are
unchanged.

| Stage | Route |
| --- | --- |
| ASR | SBPN multilingual, `ws://102.88.137.124:8080/asr-sbpn/v1/realtime` |
| LLM | Existing configured LLM, with Igbo response instructions |
| TTS | `http://102.88.137.124:8080/tts-igbo/v1/audio/speech` |

The shared SBPN adapter retains complete-turn recovery. The worker applies the
saved language for public web, dashboard, SDK, and SIP calls. Signed Voice Lab
language overrides use the same routing. Openings, acknowledgments, normal
answers, tool/knowledge follow-ups, and goodbyes use Igbo.

TTS sends the canonical model, `voice: "igbo"`, `language: "ig"`, `stream: true`,
and `response_format: "pcm"`. The service also accepts `model: "igbo"`.
Audio must be 24 kHz, mono PCM16LE. Requests are split losslessly at 140
characters, below the service's 480-character limit, and streamed immediately.
The service's fixed seed 17 and speed 1 are retained; no inference settings or
model process are changed by this integration.

Verify the saved agent route without TTS overrides, and test a spoken Igbo turn
through SBPN and the existing LLM. Confirm nonempty streaming audio and retain
RNNoise on the worker. Test data must not authorize any real financial action.
Integration checks do not certify recognition accuracy, pronunciation, or
native-language conversation quality; those need separate listening review.
