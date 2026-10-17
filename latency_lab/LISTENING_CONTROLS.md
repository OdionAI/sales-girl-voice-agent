# RVC listening controls

The `rvc-livekit-comparison` worker accepts per-call iOS listening configuration.
The SDK's default `standard` + `balanced` preserves the previous raw PCM/RMS path;
no optional audio dependencies are loaded until a different profile is requested.
Client echo cancellation, noise suppression, and automatic gain control stay on.

## Runtime dependencies

Install the pinned lab dependencies and the SpeexDSP shared library. The repository
Dockerfile includes Debian's `libspeexdsp1`; native workers need it separately:

```sh
# Debian/Ubuntu worker
apt-get install libspeexdsp1
python -m pip install -r latency_lab/requirements.txt -c latency_lab/constraints-livekit.txt

# Local macOS worker
brew install speexdsp
python -m pip install -r latency_lab/requirements.txt -c latency_lab/constraints-livekit.txt
export RVC_LAB_SPEEXDSP_LIBRARY="$(brew --prefix speexdsp)/lib/libspeexdsp.dylib"
```

SpeexDSP 1.2.1 and `webrtcvad-wheels==2.0.14` are the validated versions. Linux
normally resolves the library through its dynamic loader; the explicit library
variable is available for hosts whose loader does not search its install location.
A missing dependency rejects the requested configuration and keeps the previous
settings. It never claims noise reduction was enabled when it was not.

## Protocol and authorization

Once initialized, the agent publishes participant attribute
`attentive.listening.version=1` and handles LiveKit RPC method
`attentive.listening.configure`. Only the signed dispatch's caller identity can
change that pipeline's settings. There is no room-wide/global configuration.

Request and success response have this exact shape:

```json
{"version":1,"noiseControl":"noisyEnvironment","responseSensitivity":"strict"}
```

All three fields are required. Unknown fields, duplicate JSON keys, non-integer
versions, unsupported versions, and unknown enum strings are rejected with RPC
error 400. Wrong participant: 403. Caller currently speaking or a speech candidate
in progress: 409 (`Pause speaking and try again.`). Initialization or unavailable
audio dependency: 503. An idempotent request for the current configuration succeeds
even while speaking. SDK/UI applied state should only advance after a success
response; a rejected configuration is not queued.

The worker serializes changes with the same lock used for PCM/text input. A changed
configuration applies between utterances; settings cannot reset a stateful filter
halfway through a word. This method is registered by `register_listening_rpc`, also
used by the isolated local transport verification harness.

## Processing and sensitivity

`noisyEnvironment` runs a local SpeexDSP preprocessor on 20 ms, mono PCM16 at 16 kHz,
with a conservative −12 dB noise suppression floor. Speex AGC, echo cancellation,
reverb processing, and its deprecated VAD are not used. Noise reduction is before
both realtime ASR append and the authoritative batch-ASR utterance buffer. The
banking voice-auth capture retains its original raw input.

Opt-in profiles use WebRTC VAD to qualify speech instead of increasing a loudness
threshold. With noise reduction, independent raw and processed classifiers must
agree: this prevents the denoiser's adaptation artifacts from creating speech
candidates. VAD does not remove, gate, or zero audio. The stream retains PCM timing,
including silence needed by end-of-turn detection. There is a fixed 20 ms native
filter overlap delay, plus at most one 20 ms input-framing interval for irregular
packet sizes. The normal LiveKit 100 ms packets divide exactly into five frames.

The batch utterance retains 200 ms of processed pre-roll and every candidate frame,
so delayed VAD onset does not remove initial consonants. A brief unvoiced gap below
60 ms does not reset a candidate. Existing end-silence configuration is unchanged.

| Configuration | Waiting for an answer | Interrupting agent speech |
|---|---:|---:|
| Standard + balanced (default) | Existing configured minimum (240 ms default), RMS | Same existing minimum |
| Responsive (opt-in) | 80 ms qualified speech | 120 ms qualified speech |
| Noisy environment + balanced | 120 ms qualified speech | Existing configured minimum (240 ms default) |
| Strict (opt-in) | 120 ms qualified speech | At least 360 ms qualified speech |

Strict makes interruptions more deliberate while retaining short answers when the
agent is listening. Responsive trades false-trigger resistance for quicker onset.
These are non-speech noise reduction and speech detection, **not primary-speaker
isolation**. Nearby voices, loud/nonstationary noise, or very low SNR can still fool
classic VAD. Marketplace reliability needs device recordings and ASR/false-turn
measurements; this change does not claim complete noise immunity.

## Verification

```sh
python -m unittest latency_lab.tests.test_listening latency_lab.tests.test_lab latency_lab.tests.test_livekit_transport
```

The checked-in synthetic speech fixture contains “Yes. No. Stop.” Tests exercise
all three words at normal and 12% amplitude, quiet speech mixed with stationary
noise at 5/10 dB SNR, initial/pre-confirmation audio retention, end silence, native
noise attenuation, frame accounting, strict clap/noise interruption rejection,
configuration failure, authorization, and input-lock ordering. They do not call
external speech services or use customer audio.

On the development Mac, seeded stationary noise was attenuated by 11.69 dB after
adaptation; the clean speech fixture's overall RMS changed by −0.083 dB. Native
processing p95 was about 0.020 ms per 20 ms frame. Quiet mixed speech retained over
90% of raw-VAD speech frames and all three short answers. These are bounded fixture
results, not production capacity, ASR accuracy, or competing-speaker benchmarks.

References: [SpeexDSP preprocessor API](https://www.speex.org/docs/api/speex-api-reference/group__SpeexPreprocessState.html),
[SpeexDSP 1.2.1 implementation](https://github.com/xiph/speexdsp/blob/SpeexDSP-1.2.1/libspeexdsp/preprocess.c),
[WebRTC VAD Python wheels](https://github.com/daanzu/py-webrtcvad-wheels).
