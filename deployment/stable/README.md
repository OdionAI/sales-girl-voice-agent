# Stable Voice Stack Snapshot

This directory is the secret-free recovery bundle for the low-latency stack
validated on 2026-09-02. It captures the running Voice Lab pipeline at voice
agent baseline commit `a498bfe15e629538efd3741f621db5eaabe01b14`.

The tested path was:

```text
browser -> local LiveKit 1.13.6 -> sales-girl voice worker
        -> jumper Nginx :8080
        -> Qwen3-ASR realtime :8093 (NPU 3)
        -> Qwen 3.8 27B :8095 (NPUs 1,2)
        -> Qwen3-TTS :8091 (NPU 0, cached Helen voice)
```

No live service was restarted or reconfigured while this snapshot was made.

## Snapshot Contents

- `npu/`: the active startup scripts, ASR vLLM overlays, ASR patch, and TTS
  deploy configuration, converted into deterministic replay copies.
- `images/`: Dockerfiles pinned to the exact base-image digests. The ASR image
  bakes in the two modified vLLM files.
- `gateway/`: the active Nginx gateway server blocks.
- `manifests/`: container settings, image IDs, source commits, Python package
  locks, model file inventory, and SHA-256 identities.
- `scripts/`: guarded install, restore, image mirror/export, startup, and
  verification tools.
- `probes/`: direct inference probes for LLM, realtime ASR, and streaming TTS.
- `local/`: foreground launchers for local LiveKit and the voice worker.
- `voice-agent.env.template`: the exact non-secret Voice Lab runtime profile.
- `VERIFICATION.md`: the test commands, live probe results, and known baseline
  ASR quality observations captured with this bundle.

The committed Nginx copy differs from the captured file only by removed trailing
whitespace. Both the original active-file hash and normalized-copy hash are in
`manifests/runtime-baseline.json`; the exact raw file remains in the private
snapshot archive identified by `manifests/private-artifacts.sha256`.

## Deliberately Not In Git

GitHub is not the backup location for image archives, model weights, secrets,
or biometric voice material.

- The three Docker images total about 20 GB. Their immutable registry digests,
  sizes, source repository commits, and derivative Dockerfiles are committed.
  Use `scripts/mirror-images.sh` when a private Docker registry is available,
  or `scripts/export-images.sh` for private offline archives.
- The mounted model directories total tens of gigabytes. Every model file is
  inventoried and all metadata and weight files are checksummed. Mirror the
  directories to private object/model storage with paths preserved.
- The Helen `.safetensors` profile contains raw biometric audio. It is excluded
  by `.gitignore`. A private local copy captured with this snapshot is at the
  path recorded in `manifests/voice-profile.json`; only its identity and restore
  procedure are committed.
- LiveKit and provider credentials remain in local `.env` files only.

## Verified Runtime Profile

### Realtime ASR

- Endpoint: `ws://102.88.137.124:8080/asr-rt/v1/realtime`
- Model: `Qwen3-ASR`
- Transport: WebSocket, mono PCM16 at 16 kHz, 100 ms client chunks
- NPU: `3`; upstream port: `8093`
- Decode mode: cumulative; cadence: `0.8s`; maximum retained audio: `30s`
- Forced language: `English`
- Client finalizer: `0.7s` silence, `0.2s` minimum speech, VAD threshold `0.5`

The two ASR overlays are required. They preserve audio arriving while a decode
is in flight, cumulatively re-decode the utterance, force the English prompt,
and emit hypothesis boundaries. The voice-agent adapter then removes protocol
markers and rejects stray non-Latin hallucinations. Running the unmodified base
image reproduces the earlier dropped-word and Chinese/Arabic transcript bugs.

### LLM

- Endpoint: `http://102.88.137.124:8080/qwen38-standard/v1/chat/completions`
- Served model: `qwen3.8_27b`
- Model path: `/data/models/Qwen3.8-27B-w8a8`
- NPUs: `1,2`; tensor parallel size: `2`; upstream port: `8095`
- Temperature: `0`; thinking enabled by default
- Prefix caching and three MTP speculative tokens enabled

### TTS

- Endpoint: `http://102.88.137.124:8080/tts/v1/audio/speech`
- Served model: `Qwen3-TTS`
- Model path: `/data/models/Qwen3-TTS-12Hz-1.7B-Base`
- NPU: `0`; upstream port: `8091`
- Cached ICL voice: `helen-mavino-0030`
- Request: PCM stream, English, `Base`, `x_vector_only_mode=false`, initial
  codec chunk frames `2`
- Voice adapter framing: 24 kHz, 80 ms frames, 2048-byte HTTP reads, no initial
  client buffer

### Turn Handling

- minimum endpointing delay: `0.45s`
- maximum endpointing delay: `0.9s`
- minimum interruption duration: `0.1s`
- AEC warmup: `0.1s`

These values were validated together. Change one variable at a time and replay
the fixed QA script before adopting another stable snapshot.

## Cold Recovery

Run these steps only on a replacement host or during an approved maintenance
window. The scripts refuse to replace existing containers, and the gateway
installer never reloads Nginx, but model processes consume NPUs immediately
when started.

1. Check out branch `stable` in this repository and in
   `OdionAI/sales-girl-dashboard`.
2. Restore `/data/models/Qwen3-ASR-1.7B`,
   `/data/models/Qwen3-TTS-12Hz-1.7B-Base`, and
   `/data/models/Qwen3.8-27B-w8a8` from private storage with paths preserved.
3. Pull the exact image digests from `manifests/node1-images.json`, or retag
   their private mirrors. For a self-contained ASR image, build from the repo
   root on Linux/ARM64:

   ```bash
   docker build -f deployment/stable/images/asr.Dockerfile -t odion-asr:stable .
   docker build -f deployment/stable/images/tts.Dockerfile -t odion-tts:stable .
   docker build -f deployment/stable/images/llm.Dockerfile -t odion-llm:stable .
   ```

4. Review and install the `/data` scripts. The first command is a dry run:

   ```bash
   deployment/stable/scripts/install-node1-assets.sh
   sudo deployment/stable/scripts/install-node1-assets.sh --apply
   ```

5. Create only missing containers. To use derivative images, pass the matching
   `ASR_IMAGE`, `TTS_IMAGE`, or `LLM_IMAGE` environment variable:

   ```bash
   deployment/stable/npu/tts/create-container.sh
   deployment/stable/npu/llm/create-container.sh
   deployment/stable/npu/asr/create-container.sh
   ```

6. If the ASR container uses the original base digest rather than the derived
   image, apply the overlays before starting it:

   ```bash
   deployment/stable/scripts/apply-asr-overlays.sh --apply asr
   ```

7. Retrieve the Helen profile from private storage and restore it. The script
   enforces the captured SHA-256 before copying anything:

   ```bash
   deployment/stable/scripts/restore-helen-voice.sh --apply /private/path/helen-mavino-0030_user-approved-2026-08-25_1787674731.safetensors
   ```

8. Verify image/model presence. Full weight verification is read-only but
   reads roughly 40 GB:

   ```bash
   deployment/stable/scripts/verify-node1.sh
   deployment/stable/scripts/verify-node1.sh --weights
   ```

9. Confirm NPUs `0` through `3` are free, then start the three missing
   services without touching listeners already present:

   ```bash
   deployment/stable/scripts/start-node1-services.sh
   ```

10. On the jumper, compare the committed gateway config, review the dry-run
    message, and install only in a maintenance window. The installer backs up
    the target and runs `nginx -t`; it does not reload Nginx:

    ```bash
    deployment/stable/scripts/install-gateway-config.sh
    sudo deployment/stable/scripts/install-gateway-config.sh --apply
    ```

    At capture time, `/etc/nginx/sites-enabled` also contained a backup config
    symlink, so `nginx -t` printed duplicate/conflicting-server warnings while
    still succeeding. Remove or disable that extra file deliberately before a
    future reload; do not automate the change during an active test.

11. Run all three direct probes before connecting LiveKit:

    ```bash
    deployment/stable/probes/llm.sh
    deployment/stable/probes/tts.sh
    .venv/bin/python deployment/stable/probes/asr-realtime.py /path/to/mono-16k.wav
    ```

## Local Voice Lab Replay

Copy `voice-agent.env.template` to `.env`, then replace only secret
placeholders. Recreate the tested Python environment from the lock if exact
parity is required:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r deployment/stable/manifests/voice-agent-pip-freeze.txt
```

Start local LiveKit and the worker in separate terminals:

```bash
deployment/stable/local/start-livekit.sh
deployment/stable/local/start-voice-worker.sh
```

The matching dashboard `stable` branch documents its own environment and start
command. Voice Lab must explicitly dispatch
`sales-girl-agent-en-pre-rvc-helen-cached-fast`; the dashboard, worker, and
LiveKit server must use the same key/secret pair.

## Image and Source Identity

`manifests/node1-images.json` records immutable image digests and exact vLLM,
vLLM Ascend, and vLLM Omni Git revisions. `manifests/*-pip-freeze.txt` captures
the complete Python environment inside each running container. Docker image
digests are the primary byte-for-byte identity; source commits explain the
contents but are not substitutes for those digests.

After a private Docker Hub namespace is supplied, run `mirror-images.sh` on
Node 1 so the multi-gigabyte layers move registry-to-registry without a Mac
download/upload round trip. Build and push the derivative ASR image as well so
the two critical overlays cannot be omitted during recovery.
