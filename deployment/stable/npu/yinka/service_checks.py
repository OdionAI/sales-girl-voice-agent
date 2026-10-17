"""Isolated seeded concurrency, cancellation and recovery checks (never live)."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time
import wave

import aiohttp

from probe import TEXTS, payload


async def synthesize(session, url, text, *, cancel=False, audio_path=None):
    body = payload(text)
    body["language"] = "English"
    started = time.perf_counter()
    pcm = bytearray()
    first = None
    async with session.post(url, json=body) as response:
        response.raise_for_status()
        async for block in response.content.iter_any():
            if not block:
                continue
            if first is None:
                first = time.perf_counter() - started
            pcm.extend(block)
            if cancel:
                response.close()
                break
    if not pcm or len(pcm) % 2:
        raise RuntimeError("Missing or malformed PCM16")
    elapsed = time.perf_counter() - started
    if audio_path is not None:
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(audio_path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24000)
            audio.writeframes(pcm)
    return {"ttfp_ms": first * 1000, "elapsed_s": elapsed,
            "bytes": len(pcm), "audio_s": len(pcm) / 48000,
            "pcm_sha256": hashlib.sha256(pcm).hexdigest(), "cancelled": cancel}


async def run(url, output, repeats, concurrencies=(1, 2, 4)):
    results = {}
    timeout = aiohttp.ClientTimeout(total=120, sock_connect=10, sock_read=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            for concurrency in concurrencies:
                batches = []
                results[str(concurrency)] = batches
                for repeat in range(repeats):
                    batches.append(await asyncio.gather(*(
                        synthesize(session, url, TEXTS["medium"], audio_path=(
                            output.with_suffix("") / f"batch-{concurrency}-{repeat}-{index}.wav"
                        )) for index in range(concurrency)
                    )))
            results["before_cancel"] = await synthesize(session, url, TEXTS["short"])
            results["cancelled"] = await synthesize(session, url, TEXTS["long"], cancel=True)
            results["after_cancel"] = await synthesize(session, url, TEXTS["short"])
            results["recovered_identically"] = (
                results["before_cancel"]["pcm_sha256"] == results["after_cancel"]["pcm_sha256"]
            )
            if not results["recovered_identically"]:
                raise RuntimeError("Recovery PCM differs from the pre-cancellation request")
        except Exception as exc:
            results["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--concurrency", type=int, choices=(1, 2, 3, 4, 8), nargs="+", default=[1, 2, 4])
    args = parser.parse_args()
    asyncio.run(run(args.url, args.output, args.repeats, args.concurrency))
