`short-answers.wav` is synthetic English speech saying “Yes. No. Stop.”, generated
locally with macOS `say -v Samantha` and converted with `afconvert` to mono 16-bit
16 kHz WAV. It contains no customer recording. It exercises actual speech through
the WebRTC VAD; deterministic synthetic PCM separately tests frame retention and
noise attenuation. It is a regression fixture, not evidence of marketplace WER.
