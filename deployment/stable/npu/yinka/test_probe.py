import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

import requests

from probe import probe


class Response:
    status_code = 200
    headers = {}

    def __init__(self, fail=False):
        self.fail = fail
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield b"\1\0" * 24000
        if self.fail:
            raise requests.ReadTimeout("no more chunks")
        yield b"\2\0" * 12000


class ProbeTests(unittest.TestCase):
    def test_complete_and_stalled_audio_preserved(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / "stream"
                response = Response(fail)
                with patch("probe.requests.post", return_value=response):
                    result = probe("http://localhost/", "hello", destination, 5)
                self.assertEqual(result["completed"], not fail)
                self.assertTrue(response.closed)
                self.assertEqual(result["audio_s"], 1 if fail else 1.5)
                self.assertEqual(destination.with_suffix(".pcm").stat().st_size, result["bytes"])
                self.assertEqual(json.loads(destination.with_suffix(".json").read_text()), result)
                with wave.open(str(destination.with_suffix(".wav"))) as audio:
                    self.assertEqual(audio.getnframes(), result["bytes"] // 2)
                if fail:
                    self.assertIn("ReadTimeout", result["error"])


if __name__ == "__main__":
    unittest.main()
