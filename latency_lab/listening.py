"""Per-call listening controls for the RVC PCM16/16 kHz input path.

Standard + balanced deliberately retains the original path. Opt-in processing is
continuous: speech classification never removes samples. Speex reduces stationary
noise; neither it nor WebRTC VAD identifies the primary speaker.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import json
import math
import struct
from dataclasses import dataclass
from typing import Any

RPC_METHOD = "attentive.listening.configure"
CAPABILITY_ATTRIBUTE = "attentive.listening.version"
NUMERIC_CAPABILITY_ATTRIBUTE = "attentive.listening.numericVersion"
FRAME_BYTES = 640  # 20 ms, mono PCM16, 16 kHz
_TUNING_FIELDS = {
    "noiseSuppressionDB": "noise_suppression_db",
    "minimumSpeechLevelDBFS": "minimum_speech_level_dbfs",
    "speechConfirmationMilliseconds": "speech_confirmation_milliseconds",
    "interruptionConfirmationMilliseconds": "interruption_confirmation_milliseconds",
}


class ListeningBusy(RuntimeError):
    pass


class ListeningUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ListeningConfiguration:
    noise_control: str = "standard"
    response_sensitivity: str = "balanced"
    noise_suppression_db: int | None = None
    minimum_speech_level_dbfs: float | None = None
    speech_confirmation_milliseconds: int | None = None
    interruption_confirmation_milliseconds: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.noise_control, str) or self.noise_control not in {"standard", "noisyEnvironment"}:
            raise ValueError("Unknown noiseControl")
        if not isinstance(self.response_sensitivity, str) or self.response_sensitivity not in {"responsive", "balanced", "strict"}:
            raise ValueError("Unknown responseSensitivity")
        if self.noise_suppression_db is not None:
            if type(self.noise_suppression_db) is not int or not -30 <= self.noise_suppression_db <= 0:
                raise ValueError("noiseSuppressionDB must be an integer from -30 through 0")
            if self.noise_control != "noisyEnvironment":
                raise ValueError("noiseSuppressionDB requires noisyEnvironment")
        if self.minimum_speech_level_dbfs is not None:
            level = self.minimum_speech_level_dbfs
            if type(level) not in (int, float) or not -80 <= level <= -10 or not math.isfinite(level):
                raise ValueError("minimumSpeechLevelDBFS must be a finite number from -80 through -10")
        for name, value, minimum, maximum in (
            ("speechConfirmationMilliseconds", self.speech_confirmation_milliseconds, 40, 500),
            ("interruptionConfirmationMilliseconds", self.interruption_confirmation_milliseconds, 80, 1000),
        ):
            if value is not None and (type(value) is not int or not minimum <= value <= maximum or value % 20):
                raise ValueError(f"{name} must be an integer from {minimum} through {maximum} in 20 ms steps")

    @classmethod
    def parse(cls, payload: str) -> "ListeningConfiguration":
        if len(payload.encode("utf-8")) > 1024:
            raise ValueError("Listening configuration is too large")
        try:
            def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate configuration field")
                    result[key] = value
                return result
            value = json.loads(payload, object_pairs_hook=unique_keys)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("Listening configuration must be a JSON object") from exc
        if not isinstance(value, dict):
            raise ValueError("Listening configuration must be a JSON object")
        version = value.get("version")
        if type(version) is not int or version not in (1, 2):
            raise ValueError("Unsupported listening configuration version")
        expected = {"version", "noiseControl", "responseSensitivity"}
        if version == 2:
            expected.add("tuning")
        if set(value) != expected:
            raise ValueError("Unexpected or missing listening configuration fields")
        tuning = value.get("tuning", {})
        if version == 2 and (
            not isinstance(tuning, dict) or not tuning or set(tuning) - _TUNING_FIELDS.keys()
            or any(setting is None for setting in tuning.values())
        ):
            raise ValueError("tuning must contain supported numeric fields only")
        return cls(value["noiseControl"], value["responseSensitivity"],
                   **{_TUNING_FIELDS[name]: setting for name, setting in tuning.items()})

    def payload(self) -> dict[str, Any]:
        tuning = {name: getattr(self, attribute) for name, attribute in _TUNING_FIELDS.items()
                  if getattr(self, attribute) is not None}
        value = {"version": 2 if tuning else 1, "noiseControl": self.noise_control,
                 "responseSensitivity": self.response_sensitivity}
        if tuning:
            value["tuning"] = tuning
        return value

    @property
    def is_baseline(self) -> bool:
        return self == ListeningConfiguration()

    def confirmation_ms(self, baseline_ms: float, *, interrupting: bool) -> float:
        override = (self.interruption_confirmation_milliseconds if interrupting
                    else self.speech_confirmation_milliseconds)
        if override is not None:
            return float(override)
        if self.noise_control == "standard" and self.response_sensitivity == "balanced":
            return baseline_ms
        if interrupting:
            return {"responsive": 120.0, "balanced": baseline_ms,
                    "strict": max(360.0, baseline_ms)}[self.response_sensitivity]
        # A short answer is valid speech, irrespective of interruption preference.
        return 80.0 if self.response_sensitivity == "responsive" else 120.0


class SpeexNoiseSuppressor:
    """One native state per call; owner serializes all calls on its input lock."""

    def __init__(self, suppression_db: int = -12) -> None:
        import os
        path = os.getenv("RVC_LAB_SPEEXDSP_LIBRARY") or ctypes.util.find_library("speexdsp")
        if not path:
            raise ListeningUnavailable("Noisy environment requires libspeexdsp on the worker")
        try:
            self.lib = ctypes.CDLL(path)
            self.lib.speex_preprocess_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
            self.lib.speex_preprocess_state_init.restype = ctypes.c_void_p
            self.lib.speex_preprocess_state_destroy.argtypes = [ctypes.c_void_p]
            self.lib.speex_preprocess_state_destroy.restype = None
            self.lib.speex_preprocess_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            self.lib.speex_preprocess_ctl.restype = ctypes.c_int
            self.lib.speex_preprocess_run.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16)]
            self.lib.speex_preprocess_run.restype = ctypes.c_int
        except (OSError, AttributeError) as exc:
            raise ListeningUnavailable("The worker could not load libspeexdsp") from exc
        self.state = self.lib.speex_preprocess_state_init(320, 16000)
        if not self.state:
            raise ListeningUnavailable("The worker could not initialize noise reduction")
        try:
            # speex_preprocess.h: denoise=0, AGC=2, dereverb=8, suppression=18.
            # Speex VAD defaults off; leave it off (its deprecated setter warns).
            # Keep AEC/AGC owned by the client and speech detection in WebRTC VAD.
            for request, setting in ((0, 1), (2, 0), (8, 0), (18, suppression_db)):
                value = ctypes.c_int(setting)
                if self.lib.speex_preprocess_ctl(self.state, request, ctypes.byref(value)) != 0:
                    raise ListeningUnavailable("The worker rejected noise reduction settings")
        except Exception:
            self.close()
            raise

    def process(self, pcm: bytes) -> bytes:
        if len(pcm) != FRAME_BYTES:
            raise ValueError("Speex expects exactly 20 ms of PCM16/16 kHz mono")
        # The deployed macOS/Linux workers are little-endian, as is LiveKit PCM16.
        samples = (ctypes.c_int16 * 320).from_buffer_copy(pcm)
        self.lib.speex_preprocess_run(self.state, samples)
        return bytes(samples)

    def close(self) -> None:
        if self.state:
            self.lib.speex_preprocess_state_destroy(self.state)
            self.state = None


@dataclass(frozen=True)
class ListeningFrame:
    pcm: bytes
    speech: bool | None  # None selects the unchanged baseline RMS detector.


class ListeningInput:
    def __init__(self) -> None:
        self.configuration = ListeningConfiguration()
        self._denoiser: SpeexNoiseSuppressor | None = None
        self._vad: Any = None
        self._raw_vad: Any = None
        self._pending = bytearray()

    def configure(self, configuration: ListeningConfiguration) -> None:
        if configuration == self.configuration:
            return
        denoiser = self._denoiser
        vad = None
        raw_vad = None
        # Prepare all dependencies before publishing a changed configuration.
        try:
            if not configuration.is_baseline:
                try:
                    import webrtcvad
                    mode = {"responsive": 0, "balanced": 1, "strict": 2}[configuration.response_sensitivity]
                    vad = webrtcvad.Vad(mode)
                    if configuration.noise_control == "noisyEnvironment":
                        raw_vad = webrtcvad.Vad(mode)
                except (ImportError, OSError) as exc:
                    raise ListeningUnavailable("Listening sensitivity requires webrtcvad-wheels on the worker") from exc
            if configuration.noise_control == "noisyEnvironment":
                suppression = configuration.noise_suppression_db
                previous_suppression = self.configuration.noise_suppression_db
                if denoiser is None or suppression != previous_suppression:
                    denoiser = SpeexNoiseSuppressor(-12 if suppression is None else suppression)
        except Exception:
            if denoiser is not None and denoiser is not self._denoiser:
                denoiser.close()
            raise
        if configuration.noise_control == "standard":
            denoiser = None
        if self._denoiser is not None and denoiser is not self._denoiser:
            self._denoiser.close()
        self._denoiser, self._vad = denoiser, vad
        self._raw_vad, self.configuration = raw_vad, configuration

    def process(self, pcm: bytes) -> list[ListeningFrame]:
        if len(pcm) % 2:
            raise ValueError("PCM16 must contain complete samples")
        if self.configuration.is_baseline:
            # A switch can leave <20 ms in the framer. Retain those samples.
            if self._pending:
                pcm = bytes(self._pending) + pcm
                self._pending.clear()
            return [ListeningFrame(pcm, None)] if pcm else []
        self._pending.extend(pcm)
        frames = []
        while len(self._pending) >= FRAME_BYTES:
            frame = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
            processed = self._denoiser.process(frame) if self._denoiser else frame
            speech = self._vad.is_speech(processed, 16000)
            if self._raw_vad is not None:
                # Noise adaptation can create speech-like spectral artifacts. Both
                # independent detectors must advance on every frame and agree.
                raw_speech = self._raw_vad.is_speech(frame, 16000)
                speech = speech and raw_speech
            if self.configuration.minimum_speech_level_dbfs is not None:
                # Measure the original level so denoising does not make a quiet
                # voice fail the floor twice. Advance both VADs even below it.
                samples = struct.unpack("<320h", frame)
                mean_square = sum(sample * sample for sample in samples) / len(samples)
                minimum_rms = 32768 * 10 ** (self.configuration.minimum_speech_level_dbfs / 20)
                speech = speech and mean_square >= minimum_rms * minimum_rms
            frames.append(ListeningFrame(processed, speech))
        return frames

    def close(self) -> None:
        if self._denoiser is not None:
            self._denoiser.close()
            self._denoiser = None
        self._pending.clear()
