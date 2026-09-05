"""Measure PCM delivery gaps without changing the voice or playback settings."""

import argparse
import asyncio
import json
import time

import aiohttp


async def probe(url: str) -> dict:
    payload = {
        "input": "Hello, this is SAW. How can I help you today?",
        "model": "Qwen3-TTS",
        "task_type": "Base",
        "voice": "helen-mavino-0030",
        "language": "English",
        "x_vector_only_mode": False,
        "response_format": "pcm",
        "stream": True,
        "stream_format": "audio",
        "initial_codec_chunk_frames": 2,
    }
    arrivals = []
    size = 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as http:
        start = time.monotonic()
        async with http.post(url, json=payload) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if "pcm" not in content_type and "octet-stream" not in content_type:
                raise ValueError(f"Expected raw PCM, got {content_type}")
            rate = int(response.headers.get("X-Sample-Rate", 24000))
            channels = int(response.headers.get("X-Channels", 1))
            async for chunk in response.content.iter_any():
                arrivals.append((time.monotonic() - start, size, len(chunk)))
                size += len(chunk)
    if not arrivals or size % (2 * channels):
        raise ValueError("Missing or incomplete PCM samples")
    bytes_per_second = rate * channels * 2
    # Treat reads arriving within 20 ms as one delivery burst, not codec chunks.
    bursts = []
    previous = None
    for elapsed, before, length in arrivals:
        gap = elapsed - previous if previous is not None else 0
        if previous is None or gap > 0.02:
            bursts.append({"at_s": round(elapsed, 3), "gap_s": round(gap, 3),
                           "audio_s": 0})
        bursts[-1]["audio_s"] += length / bytes_per_second
        previous = elapsed
    for burst in bursts:
        burst["audio_s"] = round(burst["audio_s"], 3)
    return {
        "first_audio_s": round(arrivals[0][0], 3),
        "total_s": round(arrivals[-1][0], 3),
        "audio_s": round(size / bytes_per_second, 3),
        "startup_buffer_needed_s": round(max(0, max(
            elapsed - arrivals[0][0] - before / bytes_per_second
            for elapsed, before, _ in arrivals
        )), 3),
        "bursts": bursts,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://102.88.137.124:8080/tts/v1/audio/speech")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(probe(args.url)), indent=2))
