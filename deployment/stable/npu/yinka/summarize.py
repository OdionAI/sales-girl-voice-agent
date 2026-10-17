"""Summarize saved probe runs without confusing codec frames with text tokens."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import statistics


def summarize(directory):
    groups = defaultdict(list)
    for path in sorted(Path(directory).glob("*.json")):
        record = json.loads(path.read_text())
        if "text" in record:
            groups[path.stem.rsplit("-", 1)[0]].append(record)
    result = {}
    for name, records in groups.items():
        complete = [r for r in records if r["completed"]]
        metrics = {"requests": len(records), "completed": len(complete),
                   "audio_seconds": sorted({r["audio_s"] for r in records}),
                   "pcm_hashes": sorted({r["pcm_sha256"] for r in records}),
                   "languages": sorted({r.get("language", "Auto") for r in records})}
        for field in ("ttfp_ms", "elapsed_s", "rtf"):
            values = [r[field] for r in complete if r[field] is not None]
            if values:
                metrics[field] = {"median": statistics.median(values),
                                  "p95": statistics.quantiles(values, n=100, method="inclusive")[94]
                                  if len(values) > 1 else values[0]}
        gaps = [b["s"] - a["s"] for r in records
                for a, b in zip(r["chunks"], r["chunks"][1:])]
        if gaps:
            metrics["max_interchunk_gap_s"] = max(gaps)
        if complete:
            metrics["estimated_codec_frames_per_second"] = statistics.median(
                r["audio_s"] * 12.5 / r["elapsed_s"] for r in complete
            )
        result[name] = metrics
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", nargs="+")
    args = parser.parse_args()
    print(json.dumps({d: summarize(d) for d in args.directories}, indent=2))
