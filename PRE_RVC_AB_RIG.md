# Pre-RVC vs RVC A/B rig (local)

Purpose: run the pre-RVC answering runtime (`main.py`, LiveKit `AgentSession`) against
the *current* repo code and the real agent configs, without touching staging,
production, or the RVC worker.

Bottom line of the architecture difference:

| | pre-RVC | RVC (what is live) |
|---|---|---|
| Entrypoint | `python main.py dev` | `python -m latency_lab.server` (node B) / `/usr/local/bin/rvc-worker` (Lagos, Rust) |
| Turn taking | LiveKit `AgentSession` (silero VAD + endpointing) | `latency_lab` pipeline owns VAD, endpointing, speculation, cancellation |
| Tools | `agent/dynamic_tools.py` + system tools | `latency_lab/action_tools.py` + Rust tools |
| Feature gates | none of `voice_auth`, passkey, capacity admission | all of them live in `latency_lab/` |

Both read the same `agent-config-service` and write the same
`conversation-service`, so agents do not need to be duplicated between them.

## Worktree / branch

- Worktree: `_wt_pre_rvc_livekit`
- Branch: `experiment/pre-rvc-livekit-ab` (cut from `origin/main`, never pushed)
- Historical snapshot (optional, for the literal pre-RVC code): tag
  `livekit-full-pre-rvc-v1` @ `400b4d3`, and `PRE_RVC_LIVEKIT_BASELINE.md`.

## Rig pieces

1. Local service stack (SQLite, ports 8090/8091/8092/8094/8095)
   `tools/pre_rvc_rig/run_services.sh`
2. LiveKit server: `livekit-server --dev --bind 127.0.0.1` (devkey/secret, ws://127.0.0.1:7880)
3. Agent seed: `tools/pre_rvc_rig/seed_agent.py`
   Copies a real business + agent (+ versions, tools, KB links) out of the
   `.rescue/` staging pg_dump into the local DBs. Edit `BUSINESS_SLUG` to pick a
   different customer.
4. Worker: `.env` in this worktree + `python main.py dev`
   Registered as `sales-girl-agent-en-pre-rvc` on port 8188, so it can never steal
   jobs from `sales-girl-agent-en` / `-staging`.
5. Caller: `tools/pre_rvc_rig/drive_call.py` — creates the room, dispatches the
   pre-RVC worker, publishes WAV files as the microphone, records the agent audio.

## Known local-only failures (not product behaviour)

- Billing: the business needs a wallet row or the worker fails closed with
  "Insufficient wallet balance". Insert one into the billing DB, e.g.
  `INSERT INTO wallets (id,business_id,currency,balance_kobo,created_at,updated_at)`
  with a 32-char hex business id.
- Billing heartbeat returns 404 against the local billing service; harmless here.
- Knowledge retrieval is empty unless the knowledge DB is seeded too.

## Model stack used

- LLM: node1 `qwen3.8-27b` through the existing tunnel `http://127.0.0.1:40013/v1`
- STT/TTS: Deepgram (`nova-3` / `aura-asteria-en`) so no Huawei-hosted TTS is needed.
  Swap `VOICE_AGENT_STT_PROVIDER` / `VOICE_AGENT_TTS_PROVIDER` to `odion_*` when
  testing against the production voice stack.

## Measurements from the first verified run

Two caller turns, Deepgram STT/TTS, qwen3.8-27b:

- endpointing (silero VAD): ~302 ms
- LLM TTFT: 796 ms (turn 1), 592 ms (turn 2)
- TTS TTFB: 305 ms
- first audible reply ≈ endpointing + LLM + TTS ≈ 1.8–2.1 s
- observed breakages: greeting truncated by barge-in ("Good day, Tunde. Welcome"),
  second utterance split into two committed turns, one LLM request cancelled at
  3.0 s with 0 tokens

## Voice pinning (fixed speaker, every turn)

Qwen3-TTS samples a new speaker embedding per request unless a `seed` is sent, so the
clone drifts turn to turn. Pinned here with:

```
VOICE_AGENT_TTS_MODE=cloned_voice
ASCEND_TTS_VOICE=wema-en-ng-female
ODION_TTS_EXPERIMENT_SEED=3536072912      # sha256("helen-mavino-0030")[:8] as int
STRICT_ODION_CLONE_CONSISTENCY=true
```

`VOICE_AGENT_TTS_MODE` alone does nothing: an empty caller `tts_mode` normalises to
`"auto"`, and `main.py` prefers that value at the read site (`_build_tts_engine`), so
the env never applies and the default-voice branch runs with `seed=None`. The local
patch makes `"auto"` count as unset at that read site.

Verify by log, not by ear: `TTS request -> ... seed=3536072912` (no `seed=None`), and
two seeded synthesis calls must return byte-identical audio while two unseeded ones
differ.
