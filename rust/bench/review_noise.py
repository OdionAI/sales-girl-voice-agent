"""Offline native/Python filter comparison; optional existing loopback ASR.
No worker or coordinator bridge. The Python side is the unchanged reference.
"""

import argparse, asyncio, hashlib, io, json, math, os, random, subprocess, sys, time, wave
from array import array
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit


def wav_bytes(pcm):
    result = io.BytesIO()
    with wave.open(result, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(pcm)
    return result.getvalue()


def prepare(args):
    sys.path.insert(0, str(args.reference))
    from latency_lab.input_noise import InputNoiseFilter

    class Trace:
        def __init__(self):
            self.events = []

        def record(self, event, **data):
            self.events.append({"event": event, "data": data})

    with wave.open(str(args.input), "rb") as source:
        assert (
            source.getnchannels(),
            source.getsampwidth(),
            source.getframerate(),
        ) == (1, 2, 16000)
        raw = source.readframes(source.getnframes())
    samples = array("h", raw)
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    rng = random.Random(20261031)
    sigma = rms / (10 ** (8 / 20))
    noisy = array(
        "h", (max(-32768, min(32767, round(s + rng.gauss(0, sigma)))) for s in samples)
    ).tobytes()
    outputs = []
    for label, pcm in [("clean", raw), ("white-noise-8db", noisy)]:
        path = args.output / (label + "-unfiltered.wav")
        path.write_bytes(wav_bytes(pcm))
        outputs.append(
            {
                "input": label,
                "runtime": "unfiltered",
                "path": path.name,
                "samples": len(pcm) // 2,
            }
        )
        for implementation in ["webrtc", "rnnoise"]:
            os.environ.update(
                RVC_INPUT_NOISE_SUPPRESSION="1",
                RVC_INPUT_NOISE_AGENT_IDS="filter-review",
                RVC_INPUT_NOISE_IMPLEMENTATION=implementation,
            )
            trace = Trace()
            filter_ = InputNoiseFilter(SimpleNamespace(agent_id="filter-review"), trace)
            assert filter_.enabled
            started = time.perf_counter()
            filtered = (
                b"".join(
                    filter_.process(pcm[i : i + 3200]) for i in range(0, len(pcm), 3200)
                )
                + filter_.finish()
            )
            duration = (time.perf_counter() - started) * 1000
            assert not any(
                e["event"] == "input_noise_filter_error" for e in trace.events
            )
            path = args.output / f"{label}-python-{implementation}.wav"
            path.write_bytes(wav_bytes(filtered))
            outputs.append(
                {
                    "input": label,
                    "runtime": "python",
                    "implementation": implementation,
                    "path": path.name,
                    "samples": len(filtered) // 2,
                    "wall_ms": duration,
                }
            )
            inp = args.output / (label + ".s16le")
            inp.write_bytes(pcm)
            out = args.output / f"{label}-{implementation}.s16le"
            command = [str(args.binary), str(inp), str(out), implementation]
            run = subprocess.run(command, check=True, capture_output=True, text=True)
            metadata = json.loads(run.stdout)
            assert not any(
                e["event"] == "input_noise_filter_error" for e in metadata["trace"]
            )
            path = args.output / f"{label}-rust-{implementation}.wav"
            path.write_bytes(wav_bytes(out.read_bytes()))
            outputs.append(
                {
                    "input": label,
                    "runtime": "rust",
                    "implementation": implementation,
                    "path": path.name,
                    "samples": metadata["output_samples"],
                    "wall_ms": metadata["wall_ms"],
                }
            )
    return {
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "reference_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=args.reference, text=True
        ).strip(),
        "outputs": outputs,
        "limits": [
            "Small synthetic speech/noise check; not a human-labelled WER corpus.",
            "ASR agreement is against the unfiltered service response, not human ground truth.",
            "Different RNNoise implementations/resamplers need not produce identical waveforms.",
        ],
    }


async def transcribe(args, report):
    if not args.private:
        return
    import aiohttp

    cfg = json.loads(args.private.read_text())
    url = cfg["stt_batch_url"]
    parsed = urlsplit(url)
    assert (
        parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1"}
        and not parsed.username
    )
    baselines = {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as http:
        for row in report["outputs"]:
            form = aiohttp.FormData()
            form.add_field(
                "file",
                (args.output / row["path"]).read_bytes(),
                filename="review.wav",
                content_type="audio/wav",
            )
            form.add_field("model", cfg["stt_model"])
            form.add_field("language", "en")
            async with http.post(url, data=form, allow_redirects=False) as response:
                response.raise_for_status()
                body = await response.json()
            text = str(body.get("text") or "")
            row["transcript"] = text
            normalize = lambda s: " ".join(
                "".join(c.casefold() if c.isalnum() else " " for c in s).split()
            )
            if row["runtime"] == "unfiltered":
                baselines[row["input"]] = normalize(text)
            row["agrees_with_unfiltered_asr"] = (
                normalize(text) == baselines[row["input"]]
            )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("reference", type=Path)
    p.add_argument("binary", type=Path)
    p.add_argument("input", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--private", type=Path)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = prepare(args)
    asyncio.run(transcribe(args, report))
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "outputs": len(report["outputs"]),
                "asr_disagreements": sum(
                    not r.get("agrees_with_unfiltered_asr", True)
                    for r in report["outputs"]
                ),
            }
        )
    )


if __name__ == "__main__":
    main()
