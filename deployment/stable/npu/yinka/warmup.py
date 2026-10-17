"""Fail-closed startup speech readiness for the pinned Yinka serving image."""
import asyncio
from contextlib import aclosing
import logging
import time

logger = logging.getLogger(__name__)

WARMUP_TEXTS = (
    "Hello. How can I help you today?",
    "I can help you check your account balance. Please tell me which account you would like to check first, and I will look it up for you.",
    "To help you transfer money, I'll need the following details: 1. The bank name where the recipient's account is held 2. The recipient's account number (10 digits) 3. The amount you want to transfer (in naira) 4. A brief description or narration for the transfer (optional) Would you like to start by telling me the recipient's bank name?",
)


async def warmup_yinka(service, request_type, *, timeout=180):
    """Drain real streaming requests before the API accepts customer traffic.

    The medium utterance crosses the 72-frame decoder context boundary and
    the long one crosses 256 talker frames. Short-only warmup leaves cold
    mid-sentence kernels in the long-response path.
    Failure deliberately propagates to startup instead of declaring readiness.
    """
    async def synthesize(text):
        request = request_type(
            model=service.model_name, input=text, voice="yinka",
            task_type="CustomVoice", language="English", seed=1,
            response_format="pcm", stream=True, stream_format="audio",
            non_streaming_mode=False, initial_codec_chunk_frames=6,
            max_new_tokens=4096,
        )
        total = 0
        has_signal = False
        async with aclosing(service._iter_pcm_audio_bytes(request)) as chunks:
            async for chunk in chunks:
                total += len(chunk)
                has_signal = has_signal or any(chunk)
        if total < 24000 or total % 2 or not has_signal:
            raise RuntimeError("Yinka startup warmup produced empty, silent, or malformed PCM")

    started = time.monotonic()
    async with asyncio.timeout(timeout):
        for text in WARMUP_TEXTS:
            await synthesize(text)
        # Batching has additional lazy kernels even after single-request warmup.
        # TaskGroup cancels sibling streams if any warmup fails.
        for concurrency in (2, 4):
            async with asyncio.TaskGroup() as tasks:
                for _ in range(concurrency):
                    tasks.create_task(synthesize(WARMUP_TEXTS[1]))
    logger.info("Yinka streaming speech readiness complete in %.3fs", time.monotonic() - started)
