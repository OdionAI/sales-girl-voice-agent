# Local call recording

The existing LiveKit Egress flow records the room's mixed audio. No TTS, ASR,
LLM, tool or authentication behavior is changed. Text sent in call chat is not
spoken by the caller, but the agent's audible replies are recorded.

## Start

Docker Desktop must be running. In the voice-agent `.env` set:

```dotenv
LIVEKIT_RECORDING_ENABLED=true
LIVEKIT_RECORDING_STORAGE_PROVIDER=local
LIVEKIT_RECORDING_LOCAL_OUTPUT_DIR=/out
LOCAL_LIVEKIT_REDIS_HOST=127.0.0.1:6380
```

Run `bash deployment/stable/local/start-recording.sh` from the voice-agent repo.
This creates a private, git-ignored `.local/recording` directory, starts a
dedicated Redis on loopback port 6380, and starts Egress with health port 7981.
Egress reads the existing LiveKit keys without putting them in tracked files.
The output directory is mounted at `/out` in Egress.

Set `LOCAL_RECORDING_DIRECTORY` in the conversation-service `.env` to the
absolute path of the voice-agent repo's `.local/recording/audio` directory.
Restart conversation-service and the voice worker. Restart local LiveKit using
`deployment/stable/local/start-livekit.sh` so it shares Egress's Redis.
Do these restarts only when no call is active.

Keep the dashboard, public caller URL, model endpoints and microphone settings
unchanged. Start and end a test call, then open Dashboard > Conversations. Allow
time for the session cleanup and Egress finalization before refreshing playback.

## Storage and access

Audio stays on this computer; it is not uploaded to cloud storage. Conversation
records contain `local-recording:///...` references. The existing authenticated
recording endpoint checks the session's business ownership before serving the
file, and confines file resolution to `LOCAL_RECORDING_DIRECTORY`. GCS remains
the default provider when local recording is not configured.

Recordings are not deleted automatically. They may contain personal financial
information. Do not commit, publish, or share the `.local/recording` directory.
Docker removal does not remove the host audio files. Back up audio and the
conversation database together if needed. Earlier unrecorded audio cannot be
recreated from transcripts.

## Stop

Set `LIVEKIT_RECORDING_ENABLED=false` and restart the voice worker first.
Remove `LOCAL_LIVEKIT_REDIS_HOST` and restart idle LiveKit before stopping Redis:

```sh
RECORDING_RUNTIME_DIR="$PWD/.local/recording" docker compose -p sg-recording \
  -f deployment/stable/local/recording-compose.yaml down
```

Do not stop Redis while LiveKit is configured to use it. No remote NPU/jumper
services need to be restarted for recording.
