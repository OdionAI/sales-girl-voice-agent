"""Wema/ALAT voice-authentication model pipeline.

Reproduces the bank's documented flow exactly:

    raw 16 kHz mono float32 waveform
        -> demucs_16k_dynamic.onnx   (denoise, pad-to-valid-length then trim)
        -> speaker_embedding.onnx    (512-dim, UNNORMALISED voiceprint)
        -> cosine similarity vs enrolled voiceprints

Both ONNX sessions are created once per process and reused. Tensor names are
used exactly as the bank's integration summary specifies (``input``/``output``
for the denoiser, ``wave``/``embedding`` for the speaker encoder).

The two model files are pinned by SHA-256 so a later model swap can never be
silently compared against enrolments made by older weights: every stored
voiceprint is tagged with ``model_tag()`` and only same-tag prints are ever
compared.
"""

from __future__ import annotations

import hashlib
import math
import os
import threading
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
EMBEDDING_DIM = 512
DENOISER_FILE = "demucs_16k_dynamic.onnx"
ENCODER_FILE = "speaker_embedding.onnx"

# The bank's summary rejects clips shorter than 1 second.
MIN_SECONDS = 1.0

# demucs_16k_dynamic architecture constants (from the integration summary).
_DEPTH = 5
_KERNEL = 8
_STRIDE = 4
_RESAMPLE = 4

_lock = threading.Lock()
_denoiser = None
_encoder = None
_model_tag: str | None = None


def model_dir() -> Path:
    raw = str(os.getenv("VOICE_AUTH_MODEL_DIR") or "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parent.parent


def _model_paths() -> tuple[Path, Path]:
    root = model_dir()
    return root / DENOISER_FILE, root / ENCODER_FILE


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def model_tag() -> str:
    """Stable short id for the exact pair of model files in use."""
    global _model_tag
    if _model_tag is None:
        with _lock:
            if _model_tag is None:
                denoiser, encoder = _model_paths()
                joined = f"{_sha256(denoiser)}:{_sha256(encoder)}"
                _model_tag = hashlib.sha256(joined.encode()).hexdigest()[:16]
    return _model_tag


def _sessions():
    """Return the two ONNX Runtime sessions, creating them once per process."""
    global _denoiser, _encoder
    if _denoiser is not None and _encoder is not None:
        return _denoiser, _encoder
    with _lock:
        if _denoiser is None or _encoder is None:
            import onnxruntime as ort

            denoiser_path, encoder_path = _model_paths()
            for path in (denoiser_path, encoder_path):
                if not path.exists():
                    raise FileNotFoundError(f"Voice model missing: {path}")
            options = ort.SessionOptions()
            options.intra_op_num_threads = int(os.getenv("VOICE_AUTH_ONNX_THREADS", "0") or 0)
            _denoiser = ort.InferenceSession(
                str(denoiser_path), sess_options=options, providers=["CPUExecutionProvider"]
            )
            _encoder = ort.InferenceSession(
                str(encoder_path), sess_options=options, providers=["CPUExecutionProvider"]
            )
    return _denoiser, _encoder


def warmup() -> bool:
    """Load both sessions (slow) so the first live verify is not delayed."""
    try:
        _sessions()
        model_tag()
        return True
    except Exception:
        return False


def valid_length(length: int, depth: int = _DEPTH, kernel: int = _KERNEL,
                 stride: int = _STRIDE, resample: int = _RESAMPLE) -> int:
    """The padded input length demucs needs so the output is not shortened."""
    value = math.ceil(length * resample)
    for _ in range(depth):
        value = max(math.ceil((value - kernel) / stride) + 1, 1)
    for _ in range(depth):
        value = (value - 1) * stride + kernel
    return math.ceil(value / resample)


def _as_mono_float(audio: np.ndarray) -> np.ndarray:
    return np.asarray(audio, dtype=np.float32).reshape(-1)


def denoise(audio: np.ndarray) -> np.ndarray:
    """Pad -> demucs -> trim back to the original length."""
    wave = _as_mono_float(audio)
    if wave.size == 0:
        return wave
    denoiser, _ = _sessions()
    target = max(valid_length(int(wave.size)), int(wave.size))
    padded = np.zeros(target, dtype=np.float32)
    padded[: wave.size] = wave
    output = denoiser.run(None, {"input": padded.reshape(1, 1, -1)})[0]
    cleaned = np.asarray(output, dtype=np.float32).reshape(-1)[: wave.size]
    return cleaned.astype(np.float32)


def embed(audio: np.ndarray) -> np.ndarray:
    """Denoise then encode; returns the bank's raw (unnormalised) 512-d vector."""
    cleaned = denoise(audio)
    _, encoder = _sessions()
    output = encoder.run(None, {"wave": cleaned.reshape(1, 1, -1)})[0]
    vector = np.asarray(output, dtype=np.float32).reshape(-1)
    if vector.size != EMBEDDING_DIM:
        raise RuntimeError(f"Speaker model returned {vector.size} values, expected {EMBEDDING_DIM}.")
    return vector


def cosine_similarity(left: np.ndarray, right: np.ndarray) -> float:
    a = _as_mono_float(left)
    b = _as_mono_float(right)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b) or 1.0)
    return float(np.dot(a, b) / denom)
