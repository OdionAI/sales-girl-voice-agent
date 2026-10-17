"""Per-call listening controls for the RVC PCM16/16 kHz input path.

Standard + balanced deliberately retains the original path. Opt-in processing is
continuous: speech classification never removes samples. Speex reduces stationary
noise; neither it nor WebRTC VAD identifies the primary speaker.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import json
from dataclasses import dataclass
from typing import Any

RPC_METHOD = "attentive.listening.configure"
CAPABILITY_ATTRIBUTE = "attentive.listening.version"
FRAME_BYTES = 640  # 20 ms, mono PCM16, 16 kHz


class ListeningBusy(RuntimeError):
    pass


class ListeningUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ListeningConfiguration:
    noise_control: str = "standard"
    response_sensitivity: str = "balanced"

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
        if not isinstance(value, dict) or set(value) != {
            "version", "noiseControl", "responseSensitivity"
        }:
            raise ValueError("Expected version, noiseControl, and responseSensitivity only")
        if type(value["version"]) is not int or value["version"] != 1:
            raise ValueError("Unsupported listening configuration version")
        noise, sensitivity = value["noiseControl"], value["responseSensitivity"]
        if not isinstance(noise, str) or noise not in {"standard", "noisyEnvironment"}:
            raise ValueError("Unknown noiseControl")
        if not isinstance(sensitivity, str) or sensitivity not in {"responsive", "balanced", "strict"}:
            raise ValueError("Unknown responseSensitivity")
        return cls(noise, sensitivity)

    def payload(self) -> dict[str, Any]:
        return {"version": 1, "noiseControl": self.noise_control,
                "responseSensitivity": self.response_sensitivity}

    @property
    def is_baseline(self) -> bool:
        return self == ListeningConfiguration()

    def confirmation_ms(self, baseline_ms: float, *, interrupting: bool) -> float:
        if self.is_baseline:
            return baseline_ms
        if interrupting:
            return {"responsive": 120.0, "balanced": baseline_ms,
                    "strict": max(360.0, baseline_ms)}[self.response_sensitivity]
        # A short answer is valid speech, irrespective of interruption preference.
        return 80.0 if self.response_sensitivity == "responsive" else 120.0


class SpeexNoiseSuppressor:
    """One native state per call; owner serializes all calls on its input lock."""

    def __init__(self) -> None:
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
            for request, setting in ((0, 1), (2, 0), (8, 0), (18, -12)):
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
            if configuration.noise_control == "noisyEnvironment" and denoiser is None:
                denoiser = SpeexNoiseSuppressor()
        except Exception:
            if denoiser is not None and denoiser is not self._denoiser:
                denoiser.close()
            raise
        if configuration.noise_control == "standard":
            if denoiser is not None:
                denoiser.close()
            denoiser = None
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
            frames.append(ListeningFrame(processed, speech))
        return frames

    def close(self) -> None:
        if self._denoiser is not None:
            self._denoiser.close()
            self._denoiser = None
        self._pending.clear()
