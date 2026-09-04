import io
import os
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np


def _tone(freq: float, seconds: float = 2.0, sample_rate: int = 16000, phase: float = 0.0) -> np.ndarray:
    t = np.linspace(0.0, seconds, int(sample_rate * seconds), endpoint=False)
    return (0.35 * np.sin(2.0 * np.pi * freq * t + phase)).astype(np.float32)


def _speechlike(seed: int, f0: float, seconds: float = 2.2, sample_rate: int = 16000) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.linspace(0.0, seconds, int(sample_rate * seconds), endpoint=False)
    vibrato = f0 * (1.0 + 0.02 * np.sin(2.0 * np.pi * 3.2 * t))
    phase = np.cumsum(2.0 * np.pi * vibrato / sample_rate)
    harm = sum((0.55 / k) * np.sin(k * phase) for k in range(1, 7))
    formant = 1.0 + 0.4 * np.sin(2.0 * np.pi * (f0 * 2.8) * t)
    env = np.clip(0.4 + 0.6 * np.sin(2.0 * np.pi * 3.5 * t + seed), 0.1, 1.0)
    return (0.28 * harm * formant * env + 0.015 * rng.standard_normal(harm.size)).astype(np.float32)


def _wav_bytes(samples: np.ndarray, sample_rate: int = 16000) -> bytes:
    pcm = np.clip(samples, -1.0, 1.0)
    frames = (pcm * 32767.0).astype(np.int16).tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(frames)
    return buffer.getvalue()


class VoiceAuthStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        os.environ["VOICE_AUTH_DB_PATH"] = str(Path(self._tmpdir.name) / "voice-auth.sqlite")
        os.environ["VOICE_AUTH_EMBEDDER"] = "numpy"
        os.environ["VOICE_AUTH_COSINE_THRESHOLD"] = "0.75"
        from agent import voice_auth

        voice_auth._encoder = None

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_same_voice_matches_and_other_audio_fails(self) -> None:
        from agent.voice_auth import compare_pcm, enroll_pcm, enroll_wav_bytes, is_enrolled

        enrolled = _speechlike(1, 110)
        later = _speechlike(1, 112)
        other = _speechlike(9, 195)
        enroll_pcm("Caller@Example.com", enrolled)

        self.assertTrue(is_enrolled("caller@example.com"))
        matched = compare_pcm("caller@example.com", later)
        self.assertTrue(matched["matched"])
        self.assertGreaterEqual(matched["score"], 0.75)

        missed = compare_pcm("caller@example.com", other)
        self.assertFalse(missed["matched"])
        self.assertLess(missed["score"], 0.75)

        replaced = enroll_wav_bytes("caller@example.com", _wav_bytes(other))
        self.assertTrue(replaced["enrolled"])
        self.assertTrue(compare_pcm("caller@example.com", other)["matched"])
        self.assertFalse(compare_pcm("nobody@example.com", later)["enrolled"])

    def test_compare_clips_scores_same_and_different_voices(self) -> None:
        from agent.voice_auth import compare_clips

        same = compare_clips(_speechlike(1, 110), 16000, _speechlike(1, 112), 16000, threshold=0.85)
        other = compare_clips(_speechlike(1, 110), 16000, _speechlike(9, 195), 16000, threshold=0.85)
        self.assertTrue(same["matched"])
        self.assertGreaterEqual(same["score"], 0.85)
        self.assertFalse(other["matched"])
        self.assertLess(other["score"], 0.85)

    def test_short_or_unvoiced_audio_does_not_match(self) -> None:
        from agent.voice_auth import compare_pcm, enroll_pcm

        enroll_pcm("a@example.com", _tone(180, seconds=2.2))
        short = compare_pcm("a@example.com", _tone(180, seconds=0.2))
        self.assertFalse(short["matched"])
        self.assertIn(short["reason"], {"audio_too_short", "not_speech"})
        noise = compare_pcm(
            "a@example.com",
            (0.01 * np.random.default_rng(3).standard_normal(16000 * 3)).astype(np.float32),
        )
        self.assertFalse(noise["matched"])
        self.assertNotEqual(noise["reason"], "")


if __name__ == "__main__":
    unittest.main()
