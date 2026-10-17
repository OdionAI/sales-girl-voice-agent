import asyncio
from types import SimpleNamespace
import unittest

from warmup import warmup_yinka, WARMUP_TEXTS


class Service:
    model_name = "yinka-v7-test"

    def __init__(self, mode="ok"):
        self.mode = mode
        self.requests = []
        self.closed = 0

    async def _iter_pcm_audio_bytes(self, request):
        self.requests.append(request)
        try:
            if self.mode == "hang":
                await asyncio.sleep(60)
            if self.mode == "error":
                raise RuntimeError("stage failed")
            if self.mode == "odd":
                yield b"\1" * 24001
            elif self.mode != "empty":
                yield (b"\0\0" if self.mode == "silent" else b"\1\0") * 12000
        finally:
            self.closed += 1


class WarmupTests(unittest.IsolatedAsyncioTestCase):
    async def test_drains_streams_using_production_settings(self):
        service = Service()
        await warmup_yinka(service, SimpleNamespace)
        expected_texts = list(WARMUP_TEXTS) + [WARMUP_TEXTS[1]] * 6
        self.assertEqual(service.closed, len(expected_texts))
        self.assertEqual([r.input for r in service.requests], expected_texts)
        for request in service.requests:
            self.assertTrue(request.stream)
            self.assertFalse(request.non_streaming_mode)
            self.assertEqual(request.initial_codec_chunk_frames, 6)
            self.assertEqual(request.max_new_tokens, 4096)
            self.assertEqual(request.seed, 1)
            self.assertEqual(request.language, "English")

    async def test_failed_audio_prevents_readiness(self):
        for mode in ("empty", "silent", "error", "odd"):
            with self.subTest(mode=mode):
                service = Service(mode)
                with self.assertRaises(RuntimeError):
                    await warmup_yinka(service, SimpleNamespace)
                self.assertEqual(service.closed, 1)

    async def test_timeout_cancels_and_closes_stream(self):
        service = Service("hang")
        with self.assertRaises(TimeoutError):
            await warmup_yinka(service, SimpleNamespace, timeout=0.01)
        self.assertEqual(service.closed, 1)

    async def test_batch_failure_cancels_other_warmup_streams(self):
        class BatchFailure(Service):
            async def _iter_pcm_audio_bytes(self, request):
                self.requests.append(request)
                try:
                    if len(self.requests) == len(WARMUP_TEXTS) + 1:
                        await asyncio.sleep(60)
                    elif len(self.requests) > len(WARMUP_TEXTS):
                        raise RuntimeError("batch stage failed")
                    yield b"\1\0" * 12000
                finally:
                    self.closed += 1

        service = BatchFailure()
        with self.assertRaises(ExceptionGroup):
            await warmup_yinka(service, SimpleNamespace, timeout=1)
        self.assertEqual(service.closed, len(WARMUP_TEXTS) + 2)


if __name__ == "__main__":
    unittest.main()
