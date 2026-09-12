# Hybrid Call History and Private Recordings

The LiveKit hybrid worker saves finalized caller transcripts, assistant responses,
session lifecycle and recording status through the conversation service. Live
transcription packets alone are not durable call history. Banking authorization
still uses the original signed caller claims, not the history identity alias.

Email callers use the web identity channel; phone callers use voice. Opaque SDK
subjects use tenant/session-scoped aliases accepted by the conversation service.
Messages use the canonical conversation-session UUID. Dashboard transcript queries
must use that UUID so the service can also resolve historical client-session IDs.

## Recording Contract

Recording is explicitly enabled with `LIVEKIT_RECORDING_ENABLED=true`. Calls use
audio-only room composite Egress. No microphone/TTS frame performs filesystem or
database I/O. The recorder runs separately from ASR, LLM and TTS.

The local provider requires:

```dotenv
LIVEKIT_RECORDING_ENABLED=true
LIVEKIT_RECORDING_STORAGE_PROVIDER=local
LIVEKIT_RECORDING_LOCAL_DIRECTORY=/recordings
LIVEKIT_RECORDING_FILE_PREFIX=livekit-recordings
LIVEKIT_RECORDING_FORMAT=mp3
```

The conversation service requires `LOCAL_RECORDING_DIRECTORY=/recordings` and the
same volume mounted read-only. It must include the private local-recording route
and its tenant/path validation. No schema migration is needed on Lagos: recording
metadata columns and session events already exist there.

Only completed Egress output with file metadata becomes `available`. Pending,
disabled and failed capture are not represented as playable recordings. The
stored URI is `local-recording:///livekit-recordings/<business-uuid>/<file>.mp3`.
Browsers use authenticated dashboard proxy routes, never that URI or a public
storage URL. Cross-business access and traversal are rejected by the service.

## Lagos Rollout

Apply the overlay to the current release, preserving other on-box patches. Do not
replace the whole source checkout or reuse the legacy node-B deployment workflow.
Merge backend PRs with `[skip deploy]`; deploy Lagos separately.

1. Back up the running voice/conversation image IDs, source files and compose files.
   Use SQLite's backup API for the live conversation database, not a raw file copy.
2. Pull the digest-pinned image in `recording-compose.yaml`. Its Egress UID/GID is
   1001/0; verify this with `docker run --rm --entrypoint id <image>` after upgrades.
3. Copy `recording-compose.yaml` to the release root. Run on the VM as root:

   ```sh
   python3 sources/voice/latency_lab/handoff/prepare_recording.py "$PWD" \
     --egress-uid 1001 --egress-gid 0
   docker compose -p attentive-huawei -f compose.yaml -f recording-compose.yaml config --quiet
   docker compose -p attentive-huawei -f compose.yaml -f recording-compose.yaml up -d --no-deps egress
   ```

   The preparer reads the existing worker's LiveKit credentials without printing
   them, uses loopback LiveKit/Redis, and writes private config files. It is specific
   to the Lagos ports 17880/6380. Do not expose Redis or recording storage publicly.
4. Apply the reviewed voice and conversation changes, run focused tests in the
   built images, and confirm there are no active calls before reloading services.
   Use both compose files for rebuilds/recreates so the recording settings remain.
5. Merge the dashboard PR via Odion Chrome; confirm Vercel production is READY on
   the merged SHA. No recording credentials go into browser or SDK configuration.
6. Start an actual call using an email or SDK caller, speak at least two turns,
   interrupt one response, then end the call. Confirm the canonical session has
   the finalized caller messages and a completed recording. Check playback and
   seeking in the dashboard, unauthorized rejection, and cross-business rejection.

## Operational Boundaries

- This does not invent recordings for past calls. Capture begins when Egress joins;
  startup before that point is not recorded. Generated text is not proof of audio
  being heard; interrupted responses must be labelled accordingly.
- The local disk is private storage, not replicated object storage. Include it in
  approved backups and set an account retention policy before extended production
  use. Monitor disk space; do not let recordings exhaust the app VM's disk.
- Egress is limited to two CPUs and 3 GiB here. The audio preflight is not a
  concurrency/capacity guarantee. Its video-capacity warning does not establish
  audio failure; use actual audio job status and output to verify recording.
- Conversation writes use a bounded in-memory queue and bounded retries. A worker
  crash or prolonged service outage can still lose unpersisted messages. Failed
  starts and exhausted retries are traced; do not claim complete audit retention.
- If finalization remains processing or the worker dies during cleanup, inspect
  the session's Egress event and final Egress status before repairing metadata.
  Never mark a file available merely because an incomplete file exists.
- Apply the business's recording notice, access and retention requirements when
  enabling capture. Administrative access does not make recording URLs public.

References: [self-hosted Egress](https://docs.livekit.io/transport/self-hosting/egress/),
[audio-only composite](https://docs.livekit.io/transport/media/ingress-egress/egress/composite-recording/),
[local file output](https://github.com/livekit/egress/blob/v1.14.1/pkg/pipeline/sink/uploader/uploader.go).
