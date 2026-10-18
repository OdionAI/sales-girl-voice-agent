# Noise control and response sensitivity

Audio input settings require a compatible SDK and the matching
`rvc-livekit-comparison` worker. An SDK update alone does not update the worker.

Configure audio input in code. The supplied caller does not present listening
settings, noise sliders, or sensitivity menus. Set the `audioInput` parameter on
`AttentiveAgentView` before presenting the call:

- `noiseControl`: `.standard` or `.noisyEnvironment`.
- `responseSensitivity`: `.responsive`, `.balanced`, or `.strict`.

## Supplied UI

```swift
import AttentiveVoiceUI

AttentiveAgentView(
    apiKey: "YOUR_API_KEY",
    agentID: "YOUR_AGENT_ID",
    audioInput: .init(
        noiseControl: .noisyEnvironment,
        responseSensitivity: .balanced,
        noiseSuppressionDB: -12,
        minimumSpeechLevelDBFS: -50,
        speechConfirmationMilliseconds: 120,
        interruptionConfirmationMilliseconds: 240
    )
)
```

The numeric settings are optional; omitting them preserves the chosen preset.
The values above are a starting point for device testing. Adjust them directly in
`AttentiveAgentView` and rebuild your app.

| Setting | Values | Effect |
| --- | --- | --- |
| `noiseSuppressionDB` | Integers from `-30` through `0`; Noisy environment only | More negative values request stronger background-noise reduction. |
| `minimumSpeechLevelDBFS` | Finite values from `-80` through `-10` | Minimum input level for qualifying speech. A less negative value rejects quieter sounds, including quiet speech. |
| `speechConfirmationMilliseconds` | `40` through `500`, in steps of `20` | How much speech evidence is needed before accepting a turn. |
| `interruptionConfirmationMilliseconds` | `80` through `1000`, in steps of `20` | How much speech evidence is needed before interrupting agent playback. |

Speech level measures input loudness, not voice pitch. These controls do not
shift pitch or identify a particular speaker. Higher thresholds and longer
confirmation can reduce unwanted responses but can also miss quiet or brief
speech. Invalid values are rejected; they are not silently clamped.

Numeric overrides require an updated comparison worker with numeric protocol
support. Preset-only configurations remain compatible with protocol version 1.

The default is Standard noise control with Balanced sensitivity. A host using
`AttentiveCallerView` configures its retained `AttentiveCall`; presentation does
not reset that object's preferences. `showsAudioInputSettings` is deprecated and
has no effect; audio input configuration never adds controls to the caller UI.

When starting with a non-default preference, the SDK waits up to ten seconds for
confirmation before enabling the microphone. This gives the worker a chance to
enable speech detection before receiving background noise. Unsupported or failed
settings leave the call usable. A host retaining an `AttentiveCall` can observe
`audioInputState` to inspect confirmation. The default profile does not add this
startup wait.

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

For code-driven changes during a call, pause input before updating: an active
speech candidate or utterance rejects changes to avoid resetting a filter halfway
through a word. A rejected change retains the previous configuration and can be
retried through the same API. Settings do not change microphone permission or mute.

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

Standard + Balanced without numeric overrides preserves the existing RVC audio/turn path. Opt-in profiles
use WebRTC VAD on continuous 20 ms PCM frames to qualify speech; VAD does not cut
audio out of the stream. A 200 ms history preserves leading consonants for batch
recognition. Presets do not raise the existing RMS threshold. Setting
`minimumSpeechLevelDBFS` explicitly adds the chosen input-level threshold.

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
they were applied. Standard + Balanced without numeric overrides does not load
these processors.
Start the RVC worker with `python -m latency_lab.livekit_worker start` using its
existing environment and dispatch name. The repository Docker image's default
`main.py start` command starts the legacy worker; an RVC deployment must override it.

The worker advertises `attentive.listening.version=1` and
`attentive.listening.numericVersion=2` after initialization, and registers the
reliable LiveKit RPC `attentive.listening.configure`. Only the participant bound
to that call can configure it. Preset-only requests use version 1:

```json
{"version":1,"noiseControl":"noisyEnvironment","responseSensitivity":"strict"}
```

Numeric overrides use version 2 with a `tuning` object. Omitted numeric fields
retain the preset values:

```json
{"version":2,"noiseControl":"noisyEnvironment","responseSensitivity":"balanced","tuning":{"noiseSuppressionDB":-12,"minimumSpeechLevelDBFS":-50,"speechConfirmationMilliseconds":120,"interruptionConfirmationMilliseconds":240}}
```

The SDK requires the numeric capability before sending overrides; an older worker
reports unsupported instead of silently using a preset. Successful responses must
match every requested value before the SDK considers the settings applied.

Settings are bounded and isolated per session. The worker validates
unknown/duplicate fields and protocol versions, serializes changes with audio
input, and acknowledges only after applying them. Errors include invalid
configuration (400), another participant (403), active caller speech (409), and
unavailable processing or initialization (503).

## Reproducible verification

From `sdks/ios`, run `swift test` and `swift build`. From the repository root,
run the Python regression tests and the local transport check:

```sh
.venv/bin/python -m unittest discover -s latency_lab/tests -p 'test_*.py'
.venv/bin/python sdks/ios/script/verify_listening_controls.py
```

The transport check requires `livekit-server` on PATH and the native denoiser.
It creates an isolated loopback-only LiveKit instance and local credential
fixture, exercises the public Swift SDK against the production RVC RPC handler,
and rejects an unrelated participant. It does not contact model endpoints or
record the microphone. It is a protocol/integration check, not a full spoken
conversation or marketplace-quality benchmark.

The sample UI test `testGenericCallerDoesNotExposeListeningOrOptionsMenus` verifies
that audio settings are not exposed in the caller UI and saves a screenshot.
Build a local-source sample with
`node sdks/ios/script/create_binary_test_app.mjs "$PWD/sdks/ios"`; the checked-in
sample otherwise consumes its pinned published binary.
