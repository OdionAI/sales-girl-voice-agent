# Hausa Research Speaker Selection

CLEAR Global TWB Voice Hausa 1.0 is CC-BY-NC-4.0. This change enables explicitly
opted-in noncommercial listening/end-to-end research, not commercial-license
clearance. `RVC_LAB_ENABLE_HAUSA_TWB_VOICES` defaults to false. Enable it only on
the agreed research worker after the backend cutover has been verified. Pair it
with the dashboard build flag `NEXT_PUBLIC_ENABLE_HAUSA_TWB_VOICES=true`.
The user approved all accounts, including MoMo, Wema and new accounts, for this
research pilot. The Lagos GitHub release enables the worker flag when the
repository variable `ENABLE_HAUSA_TWB_VOICES=true` is explicitly set.

The config service's existing `tts_voice_id` persists `haisha`, `adamu` or
`muhammad`. The runtime context now retains that saved identifier. An explicit
Voice Lab speaker override takes precedence; otherwise the saved agent speaker
is retained. Legacy/generic Hausa selectors use Aisha only when opted in. With
the flag off, old settings keep the previous Hausa route/payload; a saved new
research speaker fails closed rather than switching to a different speaker.

Every speech chunk, including later conversation turns, sends the same matching
`voice` and `speaker_id` pair to the existing `/tts-hausa/v1/audio/speech` route:

| Voice | Native speaker |
| --- | --- |
| `haisha` | `spk_f_1` |
| `adamu` | `spk_m_1` |
| `muhammad` | `spk_m_2` |

The first speaker is displayed as **Aisha**; `haisha` is retained as the stable
backend identifier, not the visible name.

Payload: model `hausa`, PCM streaming, `stream_format=audio`, speed 1, seed 17.
The HTTP client retains its 24 kHz mono PCM16 validation, cancellation and
140-character chunking. There is no fallback to another speaker after an error.
SBPN ASR, the LLM and all other language adapters stay unchanged.

Silent regression test (loopback HTTP stub, no model inference or audio player):

```bash
python -m unittest latency_lab.tests.test_speech_languages latency_lab.tests.test_agentic.LoadAgentRuntimeContextTest latency_lab.tests.test_agentic.RoomAgentContextTest latency_lab.tests.test_pidgin
```
