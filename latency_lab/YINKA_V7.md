# Saved Yinka voices

The RVC worker and direct WebSocket server resolve the agent's saved
`tts_provider: odion_tts`, `tts_voice_id: yinka-v7-test` into the Yinka profile
after loading the business-scoped agent configuration. Every synthesis request
in that session uses the selected voice. Other saved voices keep their existing
behavior. No database migration is needed.

The profile uses `/tts-yinka-v7/v1/audio/speech` on the existing NPU jumper,
model `yinka-v7-test`, voice `yinka`, task `CustomVoice`, seed `1`, initial codec
chunk size `6`, and a `4096`-token ceiling. Audio remains streamed PCM.
`non_streaming_mode: false` selects the model's text-streaming prompt layout;
it does not turn off audio streaming. Sentence punctuation still flushes
speech, but Yinka does not split a clause just because it reached 24 words.

Explicit session selections take precedence over saved voices. Setting
`tts_model: Qwen3-TTS` switches back to the existing 1.7B route and clears Yinka
settings. An explicit TTS URL or voice continues to override the profile.

## Production rollout

Deploy this change to the **current RVC worker release**, retaining any later
production fixes. Do not replace a newer production image with an older main
branch image. The legacy orchestrator in `main.py` is not changed by this PR.

Before enabling the dashboard's `NEXT_PUBLIC_ENABLE_YINKA_V7=true` flag:

1. Confirm the active production RVC host, image, and deployment route.
2. Verify the Yinka models endpoint from that worker's network.
3. Start a public web or SDK call with an isolated test agent saved with the
   Yinka fields above, without runtime overrides. Check request model, voice,
   task, seed, and audio streaming, then listen for a complete long response.
4. Switch the test agent back to its previous voice and repeat the call.

The existing deployment workflow targets the old Huawei SSH route and legacy
services. It is not the deployment mechanism for the current RVC stack. Use
the existing `[skip deploy]` merge marker for a code-only promotion until the
current worker route is confirmed. Do not repoint secrets or restart unrelated
services to make that legacy job pass.

Run the RVC suite with:

```sh
python -m pip install -r latency_lab/requirements.txt
python -m unittest discover -s latency_lab/tests -p 'test_*.py'
```
