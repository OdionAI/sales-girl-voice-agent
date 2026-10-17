# Noise control and response sensitivity

These controls require this SDK source revision and the matching
`rvc-livekit-comparison` worker. Older published SDK binaries do not contain this
API. An SDK update alone does not update the worker.

The supplied caller's **Listening settings** button exposes:

- **Noise control:** Standard or Noisy environment.
- **Response sensitivity:** Responsive, Balanced or Strict.

The controls are available before and during a call. Before a call, changes are
saved on the call object. During a call, the UI shows Applying until the worker
acknowledges them. If the worker rejects a change, the previous settings remain
active and the UI offers a retry. Pause speaking before changing settings: an
active speech candidate/utterance rejects changes to avoid resetting a filter
halfway through a word. Settings do not change microphone permission or mute.

## Supplied UI

```swift
import AttentiveVoiceUI

AttentiveAgentView(
    apiKey: "YOUR_API_KEY",
    agentID: "YOUR_AGENT_ID",
    audioInput: .noisyEnvironment
)
```

The default is Standard noise control with Balanced sensitivity. A host using
`AttentiveCallerView` configures its retained `AttentiveCall`; presentation does
not reset that object's preferences. Set
`CallerUIConfiguration.showsAudioInputSettings` to `false` if your app supplies
its own controls.

When starting with a non-default preference, the SDK waits up to ten seconds for
confirmation before enabling the microphone. This gives the worker a chance to
enable speech detection before receiving background noise. Unsupported or failed
settings leave the call usable and remain visible in the settings panel. The
default profile does not add this startup wait.

## Headless SDK

```swift
import AttentiveVoice

@MainActor
func makeCall() -> AttentiveCall {
    AttentiveCall(
        apiKey: "YOUR_API_KEY",
        agentID: "YOUR_AGENT_ID",
        audioInput: .init(
            noiseControl: .noisyEnvironment,
            responseSensitivity: .strict
        )
    )
}

@MainActor
func updateListening(_ call: AttentiveCall) async throws {
    try await call.setAudioInputConfiguration(.init(
        noiseControl: .noisyEnvironment,
        responseSensitivity: .balanced
    ))
}
```

Observe `audioInputConfiguration` for the requested selection and
`audioInputState` for acknowledgement. Its status is `idle`, `pending`,
`applied`, `unsupported` or `failed`; `applied` contains the last acknowledged
configuration for this connection. A selection is not proof it is active.
The same state is available through `CallEvent.audioInputChanged`.
Only a response matching the requested configuration marks an update applied.
If a response is lost, the worker may have changed settings even though the SDK
cannot confirm them; retrying the same request is safe.

An older worker reports unsupported without disconnecting the call. Existing
echo cancellation, gain control and standard client noise suppression remain
enabled. Reconnection requires settings to be acknowledged again. Call end
clears the effective connection state while retaining the requested preference
for the next call.

## What the modes do

Standard + Balanced preserves the existing RVC audio/turn path. Opt-in profiles
use WebRTC VAD on continuous 20 ms PCM frames to qualify speech; VAD does not cut
audio out of the stream. A 200 ms history preserves leading consonants for batch
recognition. Quiet speech is not rejected by raising the existing RMS threshold.

Noisy environment additionally applies SpeexDSP noise reduction in the worker
**before both streaming and batch speech recognition**. This leaves the iOS
capture engine and its echo cancellation intact and avoids a second microphone
engine. Speex's AGC and VAD are disabled; it is used only for denoising. Its
state is isolated per call and cleaned up when that call ends.

Responsive requires less speech evidence; Strict uses more conservative speech
classification and a longer confirmation before interrupting agent playback.
Short answers while the agent is listening remain valid. These are listening
preferences, not pitch controls, caller identification, or authentication.

Noise reduction can help with steady background sounds. Nearby voices, music,
and overlapping speech remain difficult; no primary-speaker isolation or
noise-immunity guarantee is made. A physical-device trial in the intended
environment is necessary before making quality claims.

## Worker deployment

Use the updated `latency_lab.livekit_worker` with dispatch name
`rvc-livekit-comparison`. The Dockerfile includes `libspeexdsp1`; the Python
requirements pin `webrtcvad-wheels==2.0.14`. For a local macOS worker:

```sh
brew install speexdsp
python -m pip install -r latency_lab/requirements.txt -c latency_lab/constraints-livekit.txt
export RVC_LAB_SPEEXDSP_LIBRARY="$(brew --prefix speexdsp)/lib/libspeexdsp.dylib"
```

The library path is optional when the system library loader finds SpeexDSP.
Missing opt-in dependencies reject the requested settings rather than claiming
they were applied. Standard + Balanced does not load these processors.

The worker advertises `attentive.listening.version=1` after initialization and
registers the reliable LiveKit RPC `attentive.listening.configure`. Only the
participant bound to that call can configure it. The request and successful
response contain exactly this versioned shape:

```json
{"version":1,"noiseControl":"noisyEnvironment","responseSensitivity":"strict"}
```

Settings are bounded enums and isolated per session. The worker validates
unknown/duplicate fields and protocol versions, serializes changes with audio
input, and acknowledges only after applying them. Errors include invalid
configuration (400), another participant (403), active caller speech (409), and
unavailable processing or initialization (503).

## Reproducible verification

From `sdks/ios`, run `swift test` and `swift build`. From the repository root,
run the Python regression tests and the local transport check:

```sh
.venv/bin/python -m pytest latency_lab/tests
.venv/bin/python sdks/ios/script/verify_listening_controls.py
```

The transport check requires `livekit-server` on PATH and the native denoiser.
It creates an isolated loopback-only LiveKit instance and local credential
fixture, exercises the public Swift SDK against the production RVC RPC handler,
and rejects an unrelated participant. It does not contact model endpoints or
record the microphone. It is a protocol/integration check, not a full spoken
conversation or marketplace-quality benchmark.

The sample UI test `testListeningSettingsCanBeConfiguredBeforeCalling` verifies
the built-in controls and saves a screenshot. Build a local-source sample with
`node sdks/ios/script/create_binary_test_app.mjs "$PWD/sdks/ios"`; the checked-in
sample otherwise consumes its pinned published binary.
