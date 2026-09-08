#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import wave
from pathlib import Path

import aiohttp


HYPOTHESIS_PREFIX = re.compile(r"^\s*language\b", re.IGNORECASE)


def clean_transcript(value: object) -> str:
    text = str(value or "")
    marker = "<asr_text>"
    marker_index = text.lower().rfind(marker)
    if marker_index >= 0:
        text = text[marker_index + len(marker) :]
    return " ".join(text.strip().split())


async def run_probe(args: argparse.Namespace) -> None:
    with wave.open(str(args.audio), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != 16000:
            raise SystemExit("Audio must be mono PCM16 WAV at 16 kHz")
        pcm = wav.readframes(wav.getnframes())

    chunk_bytes = 16000 * 2 * args.chunk_ms // 1000
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(args.endpoint) as websocket:
            created = await websocket.receive_json()
            if created.get("type") != "session.created":
                raise RuntimeError(f"Unexpected first event: {created}")

            await websocket.send_json(
                {"type": "session.update", "model": args.model, "language": "English"}
            )
            await websocket.send_json(
                {"type": "input_audio_buffer.commit", "final": False}
            )

            input_ended = False

            async def send_audio() -> None:
                nonlocal input_ended
                for offset in range(0, len(pcm), chunk_bytes):
                    chunk = pcm[offset : offset + chunk_bytes]
                    await websocket.send_json(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(chunk).decode("ascii"),
                        }
                    )
                    await asyncio.sleep(args.chunk_ms / 1000)
                input_ended = True
                await websocket.send_json(
                    {"type": "input_audio_buffer.commit", "final": True}
                )

            sender = asyncio.create_task(send_audio())
            raw_transcript = ""
            final_transcript = ""
            try:
                async for message in websocket:
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    event = json.loads(message.data)
                    event_type = event.get("type")
                    if event_type == "transcription.delta":
                        delta = str(event.get("delta", ""))
                        if raw_transcript and HYPOTHESIS_PREFIX.match(delta):
                            raw_transcript = delta
                        else:
                            raw_transcript += delta
                    elif event_type == "transcription.done":
                        final_transcript = clean_transcript(event.get("text")) or clean_transcript(
                            raw_transcript
                        )
                        if final_transcript:
                            print(f"HYPOTHESIS: {final_transcript}")
                        raw_transcript = ""
                        if input_ended:
                            break
                        await websocket.send_json(
                            {"type": "input_audio_buffer.commit", "final": False}
                        )
                    elif event_type == "error":
                        raise RuntimeError(event)
            finally:
                await sender

            if not final_transcript:
                raise RuntimeError("Realtime ASR returned no transcript")
            print(f"FINAL: {final_transcript}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe the stable realtime ASR WebSocket")
    parser.add_argument("audio", type=Path, help="mono PCM16 16 kHz WAV file")
    parser.add_argument(
        "--endpoint",
        default="ws://102.88.137.124:8080/asr-rt/v1/realtime",
    )
    parser.add_argument("--model", default="Qwen3-ASR")
    parser.add_argument("--chunk-ms", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=120)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run_probe(parse_args()))
