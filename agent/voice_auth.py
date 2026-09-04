import io
import logging
import os
import sqlite3
import struct
import wave
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_RATE = 16000
DEFAULT_THRESHOLD = 0.85
ECAPA_THRESHOLD = 0.4
MIN_SECONDS = 1.2
EMBED_BANDS = 40
PITCH_BINS = 24
_encoder = None


def voice_auth_root() -> Path:
    raw = str(os.getenv("VOICE_AUTH_DIR") or "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parent.parent / ".local"


def voice_auth_db_path() -> Path:
    raw = str(os.getenv("VOICE_AUTH_DB_PATH") or "").strip()
    if raw:
        return Path(raw)
    path = voice_auth_root() / "voice-auth.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def voice_auth_threshold() -> float:
    raw = str(os.getenv("VOICE_AUTH_COSINE_THRESHOLD") or "").strip()
    default = ECAPA_THRESHOLD if voice_auth_embedder() == "ecapa" else DEFAULT_THRESHOLD
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = default
    return min(0.99, max(0.15, value))


def voice_auth_embedder() -> str:
    raw = str(os.getenv("VOICE_AUTH_EMBEDDER") or "").strip().lower()
    if raw in {"ecapa", "numpy"}:
        return raw
    try:
        import speechbrain  # noqa: F401
        import torch  # noqa: F401
    except Exception:
        return "numpy"
    return "ecapa"


def normalize_owner_email(value: str) -> str:
    return str(value or "").strip().lower()


def _connect() -> sqlite3.Connection:
    path = voice_auth_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_embeddings (
            owner_email TEXT PRIMARY KEY,
            embedding BLOB NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    return conn


def pcm_to_wav_bytes(samples: np.ndarray, sample_rate: int = DEFAULT_SAMPLE_RATE) -> bytes:
    audio = resample_mono(samples, sample_rate, DEFAULT_SAMPLE_RATE)
    pcm = np.clip(audio, -1.0, 1.0)
    frames = (pcm * 32767.0).astype(np.int16).tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(DEFAULT_SAMPLE_RATE)
        wav.writeframes(frames)
    return buffer.getvalue()


def sidecar_url() -> str:
    return str(os.getenv("VOICE_AUTH_SIDECAR_URL") or "").strip().rstrip("/")


def _local_ecapa_ready() -> bool:
    try:
        import speechbrain  # noqa: F401
        import torch  # noqa: F401
    except Exception:
        return False
    return True


def _compare_pcm_remote(
    owner_email: str,
    samples: np.ndarray,
    sample_rate: int,
    threshold: float,
) -> dict[str, Any]:
    import httpx

    files = {
        "email": (None, owner_email),
        "threshold": (None, str(threshold)),
        "audio": ("live.wav", pcm_to_wav_bytes(samples, sample_rate), "audio/wav"),
    }
    response = httpx.post(f"{sidecar_url()}/v1/voice-auth/verify", files=files, timeout=30.0)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Voice sidecar returned an invalid compare payload.")
    return payload


def pcm_from_wav_bytes(payload: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(payload), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    if sample_width != 2:
        raise ValueError("Enrollment audio must be 16-bit PCM WAV.")
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, int(sample_rate)


def resample_mono(samples: np.ndarray, sample_rate: int, target_rate: int = DEFAULT_SAMPLE_RATE) -> np.ndarray:
    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    if audio.size == 0:
        return audio
    if int(sample_rate) == int(target_rate):
        return audio
    duration = audio.size / float(sample_rate)
    target_count = max(1, int(round(duration * target_rate)))
    source_x = np.linspace(0.0, 1.0, audio.size, endpoint=False)
    target_x = np.linspace(0.0, 1.0, target_count, endpoint=False)
    return np.interp(target_x, source_x, audio).astype(np.float32)


def _hz_to_mel(hz: np.ndarray | float) -> np.ndarray | float:
    return 2595.0 * np.log10(1.0 + np.asarray(hz) / 700.0)


def _mel_to_hz(mel: np.ndarray | float) -> np.ndarray | float:
    return 700.0 * (10 ** (np.asarray(mel) / 2595.0) - 1.0)


def _normalize_vector(vector: np.ndarray) -> np.ndarray:
    values = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(values) or 1.0)
    return values / norm


def _pitch_counts(audio: np.ndarray, sample_rate: int = DEFAULT_SAMPLE_RATE) -> np.ndarray:
    window = 1024
    hop = 256
    hist = np.zeros(PITCH_BINS, dtype=np.float32)
    if audio.size < window:
        return hist
    min_lag = int(sample_rate / 400)
    max_lag = int(sample_rate / 70)
    edges = np.linspace(70.0, 400.0, PITCH_BINS + 1)
    frames = np.lib.stride_tricks.sliding_window_view(audio, window)[::hop]
    for frame in frames:
        centered = frame - float(frame.mean())
        if float(np.max(np.abs(centered))) < 0.02:
            continue
        corr = np.correlate(centered, centered, mode="full")
        corr = corr[corr.size // 2 :]
        if corr.size <= max_lag or float(corr[0]) <= 0:
            continue
        peak = int(np.argmax(corr[min_lag:max_lag])) + min_lag
        if float(corr[peak]) < 0.3 * float(corr[0]):
            continue
        f0 = sample_rate / float(peak)
        index = int(np.searchsorted(edges, f0) - 1)
        if 0 <= index < PITCH_BINS:
            hist[index] += 1.0
    return hist


def _pitch_histogram(audio: np.ndarray, sample_rate: int = DEFAULT_SAMPLE_RATE) -> np.ndarray:
    hist = _pitch_counts(audio, sample_rate)
    return hist / float(hist.sum() or 1.0)


def last_speech_segment(
    samples: np.ndarray,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> np.ndarray:
    audio = resample_mono(samples, sample_rate, DEFAULT_SAMPLE_RATE)
    frame = 480
    if audio.size < frame * 4:
        return audio
    usable = audio[: audio.size // frame * frame]
    frames = usable.reshape(-1, frame)
    rms = np.sqrt(np.mean(np.square(frames), axis=1))
    threshold = max(0.015, float(np.percentile(rms, 40)) * 1.8)
    speech = rms >= threshold
    if not np.any(speech):
        return np.zeros(0, dtype=np.float32)
    closed = speech.copy()
    gap = 6
    for index in range(1, len(closed)):
        if (not closed[index]) and closed[index - 1] and np.any(closed[index : index + gap + 1]):
            closed[index] = True
    indices = np.where(closed)[0]
    end = int(indices[-1])
    start = end
    while start > 0 and closed[start - 1]:
        start -= 1
    pad = 2
    start = max(0, start - pad)
    end = min(len(closed) - 1, end + pad)
    segment = usable[start * frame : (end + 1) * frame]
    if segment.size < int(MIN_SECONDS * DEFAULT_SAMPLE_RATE):
        return audio
    return segment


def speech_quality(
    samples: np.ndarray,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> dict[str, float]:
    segment = last_speech_segment(samples, sample_rate)
    duration = float(segment.size) / float(DEFAULT_SAMPLE_RATE)
    counts = _pitch_counts(segment, DEFAULT_SAMPLE_RATE)
    voiced_frames = float(counts.sum())
    dominant = float(np.argmax(counts)) if voiced_frames else -1.0
    return {
        "duration": duration,
        "voiced_frames": voiced_frames,
        "dominant_bin": dominant,
        "usable": 1.0 if duration >= MIN_SECONDS and voiced_frames >= 12 else 0.0,
    }


def _mfcc_stats(audio: np.ndarray, sample_rate: int = DEFAULT_SAMPLE_RATE) -> np.ndarray:
    window = 400
    hop = 160
    n_fft = 512
    n_coeff = 13
    if audio.size < window:
        return np.zeros((n_coeff - 1) * 2, dtype=np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(audio, window)[::hop]
    windowed = frames * np.hanning(window)
    spectrum = np.abs(np.fft.rfft(windowed, n=n_fft)) ** 2
    mel_points = np.linspace(_hz_to_mel(0.0), _hz_to_mel(sample_rate / 2.0), EMBED_BANDS + 2)
    hz_points = _mel_to_hz(mel_points)
    bins = np.clip(
        np.floor(hz_points / (sample_rate / n_fft)).astype(int),
        0,
        spectrum.shape[1] - 1,
    )
    filters = np.zeros((EMBED_BANDS, spectrum.shape[1]), dtype=np.float32)
    for index in range(EMBED_BANDS):
        left, center, right = bins[index : index + 3]
        if center > left:
            filters[index, left:center] = np.linspace(0.0, 1.0, center - left, endpoint=False)
        if right > center:
            filters[index, center:right] = np.linspace(1.0, 0.0, right - center, endpoint=False)
    log_mel = np.log(np.maximum(spectrum @ filters.T, 1e-10))
    k = np.arange(n_coeff)[:, None]
    n = np.arange(EMBED_BANDS)[None, :]
    dct = np.cos(np.pi * k * (2 * n + 1) / (2 * EMBED_BANDS))
    mfcc = log_mel @ dct.T
    body = mfcc[:, 1:]
    return np.concatenate([body.mean(axis=0), body.std(axis=0)]).astype(np.float32)


def _embed_numpy(audio: np.ndarray) -> np.ndarray:
    pitch = _pitch_histogram(audio)
    mfcc = _mfcc_stats(audio)
    return _normalize_vector(np.concatenate([pitch * 3.0, mfcc * 0.15]))


def _ecapa_encoder():
    global _encoder
    if _encoder is not None:
        return _encoder
    from speechbrain.inference.speaker import EncoderClassifier

    cache = voice_auth_root() / "speechbrain-ecapa"
    cache.mkdir(parents=True, exist_ok=True)
    _encoder = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir=str(cache),
        run_opts={"device": "cpu"},
    )
    return _encoder


def _embed_ecapa(audio: np.ndarray) -> np.ndarray:
    import torch

    wav = torch.from_numpy(np.asarray(audio, dtype=np.float32)).unsqueeze(0)
    with torch.no_grad():
        embedded = _ecapa_encoder().encode_batch(wav)
    return _normalize_vector(embedded.squeeze().detach().cpu().numpy())


def embed_pcm(samples: np.ndarray, sample_rate: int = DEFAULT_SAMPLE_RATE) -> np.ndarray:
    audio = resample_mono(samples, sample_rate, DEFAULT_SAMPLE_RATE)
    if audio.size < int(MIN_SECONDS * DEFAULT_SAMPLE_RATE):
        raise ValueError("Audio is too short to enroll or compare.")
    peak = float(np.max(np.abs(audio)) or 1.0)
    audio = audio / peak
    if voice_auth_embedder() == "numpy":
        return _embed_numpy(audio)
    return _embed_ecapa(audio)


def cosine_similarity(left: np.ndarray, right: np.ndarray) -> float:
    a = np.asarray(left, dtype=np.float32).reshape(-1)
    b = np.asarray(right, dtype=np.float32).reshape(-1)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b) or 1.0)
    return float(np.dot(a, b) / denom)


def _dump_embedding(vector: np.ndarray) -> bytes:
    values = np.asarray(vector, dtype=np.float32).reshape(-1)
    return struct.pack(f"<{values.size}f", *values.tolist())


def _load_embedding(payload: bytes) -> np.ndarray:
    count = len(payload) // 4
    return np.asarray(struct.unpack(f"<{count}f", payload), dtype=np.float32)


def enroll_pcm(owner_email: str, samples: np.ndarray, sample_rate: int = DEFAULT_SAMPLE_RATE) -> dict[str, Any]:
    email = normalize_owner_email(owner_email)
    if not email or "@" not in email:
        raise ValueError("A valid email is required to enroll a voice.")
    vector = embed_pcm(samples, sample_rate)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO voice_embeddings (owner_email, embedding, created_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(owner_email) DO UPDATE SET
                embedding = excluded.embedding,
                created_at = CURRENT_TIMESTAMP
            """,
            (email, _dump_embedding(vector)),
        )
        conn.commit()
    logger.info("[VOICE-AUTH] enrolled owner=%s dim=%s embedder=%s", email, vector.size, voice_auth_embedder())
    return {
        "enrolled": True,
        "owner_email": email,
        "dim": int(vector.size),
        "embedder": voice_auth_embedder(),
    }


def enroll_wav_bytes(owner_email: str, payload: bytes) -> dict[str, Any]:
    samples, sample_rate = pcm_from_wav_bytes(payload)
    return enroll_pcm(owner_email, samples, sample_rate)


def load_embedding(owner_email: str) -> np.ndarray | None:
    email = normalize_owner_email(owner_email)
    if not email:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT embedding FROM voice_embeddings WHERE owner_email = ?",
            (email,),
        ).fetchone()
    if not row:
        return None
    return _load_embedding(row[0])


def is_enrolled(owner_email: str) -> bool:
    return load_embedding(owner_email) is not None


def compare_pcm(
    owner_email: str,
    samples: np.ndarray,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    threshold: float | None = None,
) -> dict[str, Any]:
    email = normalize_owner_email(owner_email)
    cutoff = voice_auth_threshold() if threshold is None else float(threshold)
    if sidecar_url() and voice_auth_embedder() == "ecapa" and not _local_ecapa_ready():
        return _compare_pcm_remote(email, samples, sample_rate, cutoff)
    stored = load_embedding(email)
    if stored is None:
        return {
            "matched": False,
            "enrolled": False,
            "score": 0.0,
            "threshold": cutoff,
            "owner_email": email,
            "reason": "not_enrolled",
        }
    quality = speech_quality(samples, sample_rate)
    if quality["usable"] < 1.0:
        return {
            "matched": False,
            "enrolled": True,
            "score": 0.0,
            "threshold": cutoff,
            "owner_email": email,
            "reason": "not_speech" if quality["duration"] >= MIN_SECONDS else "audio_too_short",
        }
    segment = last_speech_segment(samples, sample_rate)
    try:
        live = embed_pcm(segment, DEFAULT_SAMPLE_RATE)
    except ValueError:
        return {
            "matched": False,
            "enrolled": True,
            "score": 0.0,
            "threshold": cutoff,
            "owner_email": email,
            "reason": "audio_too_short",
        }
    if stored.size != live.size:
        return {
            "matched": False,
            "enrolled": True,
            "score": 0.0,
            "threshold": cutoff,
            "owner_email": email,
            "reason": "embedding_mismatch",
            "embedder": voice_auth_embedder(),
        }
    score = cosine_similarity(stored, live)
    if voice_auth_embedder() != "ecapa":
        stored_pitch = np.abs(np.asarray(stored[:PITCH_BINS], dtype=np.float32))
        stored_pitch = stored_pitch / float(stored_pitch.sum() or 1.0)
        live_pitch = _pitch_histogram(segment, DEFAULT_SAMPLE_RATE)
        pitch_score = cosine_similarity(stored_pitch, live_pitch)
        if pitch_score < 0.5:
            return {
                "matched": False,
                "enrolled": True,
                "score": score,
                "threshold": cutoff,
                "owner_email": email,
                "reason": "pitch_mismatch",
            }
    matched = score >= cutoff
    return {
        "matched": matched,
        "enrolled": True,
        "score": score,
        "threshold": cutoff,
        "owner_email": email,
        "reason": "" if matched else "below_threshold",
        "embedder": voice_auth_embedder(),
    }


def compare_clips(
    left_samples: np.ndarray,
    left_rate: int,
    right_samples: np.ndarray,
    right_rate: int,
    threshold: float | None = None,
) -> dict[str, Any]:
    cutoff = voice_auth_threshold() if threshold is None else float(threshold)
    left_segment = last_speech_segment(left_samples, left_rate)
    right_segment = last_speech_segment(right_samples, right_rate)
    left_quality = speech_quality(left_samples, left_rate)
    right_quality = speech_quality(right_samples, right_rate)
    try:
        left = embed_pcm(left_segment if left_segment.size else left_samples, DEFAULT_SAMPLE_RATE)
        right = embed_pcm(right_segment if right_segment.size else right_samples, DEFAULT_SAMPLE_RATE)
    except ValueError:
        return {
            "matched": False,
            "score": 0.0,
            "pitch_score": 0.0,
            "threshold": cutoff,
            "reason": "audio_too_short",
            "left": left_quality,
            "right": right_quality,
        }
    score = cosine_similarity(left, right)
    pitch_score = cosine_similarity(
        _pitch_histogram(left_segment if left_segment.size else left_samples),
        _pitch_histogram(right_segment if right_segment.size else right_samples),
    )
    if score < cutoff:
        reason = "below_threshold"
    elif left_quality["usable"] < 1.0 or right_quality["usable"] < 1.0:
        reason = "weak_speech"
    elif voice_auth_embedder() != "ecapa" and pitch_score < 0.5:
        reason = "pitch_mismatch"
    else:
        reason = ""
    return {
        "matched": score >= cutoff,
        "score": score,
        "pitch_score": pitch_score,
        "threshold": cutoff,
        "reason": reason,
        "embedder": voice_auth_embedder(),
        "left": left_quality,
        "right": right_quality,
    }
