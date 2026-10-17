"""Record complete or partial streaming PCM, timings, and failures for Yinka."""
import argparse
import hashlib
import json
from pathlib import Path
import signal
import time
import wave

import requests

TEXTS = {
    "short": "Hello. How can I help you today?",
    "medium": "I can help you check your account balance. Please tell me which account you would like to check first, and I will look it up for you.",
    "long": "To help you transfer money, I'll need the following details: 1. The bank name where the recipient's account is held 2. The recipient's account number (10 digits) 3. The amount you want to transfer (in naira) 4. A brief description or narration for the transfer (optional) Would you like to start by telling me the recipient's bank name?",
    "money": "Your account balance is 125,650 naira and 50 kobo. The recipient's name is Adeola. Would you like to transfer 10,000 naira to this recipient? Please confirm before I continue.",
    "long-prose": "To help you transfer money, I'll need the bank name where the recipient's account is held and the recipient's account number, which should have ten digits. Please also tell me the amount you want to transfer in naira. You can include a brief description or narration for the transfer, but this is optional. Would you like to start by telling me the recipient's bank name?",
    "long-punctuated": "To help you transfer money, I'll need the following details: 1. The bank name where the recipient's account is held. 2. The recipient's account number (10 digits). 3. The amount you want to transfer (in naira). 4. A brief description or narration for the transfer (optional). Would you like to start by telling me the recipient's bank name?",
}


def payload(text):
    return {
        "model": "yinka-v7-test", "input": text, "voice": "yinka",
        "task_type": "CustomVoice", "language": "Auto", "seed": 1,
        "response_format": "pcm", "stream": True, "stream_format": "audio",
        "non_streaming_mode": False, "initial_codec_chunk_frames": 6,
        "max_new_tokens": 4096,
    }


def timeout_handler(signum, frame):
    raise TimeoutError("Stream exceeded its wall-clock deadline")


def probe(url, text, destination, deadline, *, full_text_conditioning=False, language="Auto"):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    events, pcm = [], bytearray()
    result = {"completed": False, "text": text, "sample_rate": 24000,
              "full_text_conditioning": full_text_conditioning, "language": language}
    previous_handler = signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(deadline)
    try:
        body = payload(text)
        body["non_streaming_mode"] = full_text_conditioning
        body["language"] = language
        with requests.post(url, json=body, stream=True, timeout=(10, 30)) as response:
            result.update(status=response.status_code,
                          header_ms=1000 * (time.perf_counter() - started),
                          request_id=response.headers.get("x-request-id"))
            response.raise_for_status()
            result["sample_rate"] = int(response.headers.get("x-sample-rate", 24000))
            with destination.with_suffix(".pcm").open("wb") as audio:
                for block in response.iter_content(chunk_size=None):
                    if not block:
                        continue
                    events.append({"s": time.perf_counter() - started, "bytes": len(block)})
                    audio.write(block)
                    audio.flush()
                    pcm.extend(block)
            if not pcm or len(pcm) % 2:
                raise ValueError("Missing or incomplete PCM16 samples")
            result["completed"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)
        result.update(elapsed_s=time.perf_counter() - started,
                      bytes=len(pcm), chunks=events,
                      pcm_sha256=hashlib.sha256(pcm).hexdigest())
        result["audio_s"] = len(pcm) / (2 * result["sample_rate"])
        result["ttfp_ms"] = 1000 * events[0]["s"] if events else None
        result["last_pcm_s"] = events[-1]["s"] if events else None
        result["rtf"] = result["elapsed_s"] / result["audio_s"] if pcm else None
        destination.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n")
        with wave.open(str(destination.with_suffix(".wav")), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(result["sample_rate"])
            audio.writeframes(pcm[:len(pcm) // 2 * 2])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--sizes", nargs="+", choices=list(TEXTS), default=["short", "medium", "long"])
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--deadline", type=int, default=90)
    parser.add_argument("--full-text-conditioning", action="store_true",
                        help="Diagnostic: prefill the complete text; audio still streams")
    parser.add_argument("--language", default="Auto")
    args = parser.parse_args()
    failures = 0
    for repeat in range(args.repeats):
        for size in args.sizes:
            result = probe(args.url, TEXTS[size], Path(args.output) / f"{size}-{repeat + 1}",
                           args.deadline, full_text_conditioning=args.full_text_conditioning,
                           language=args.language)
            print(json.dumps({k: v for k, v in result.items() if k != "chunks"}), flush=True)
            failures += not result["completed"]
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
