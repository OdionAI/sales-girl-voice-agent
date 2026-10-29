#!/usr/bin/env python3
"""Prepare short private ICL references from the local SalesGirl voice pack.

The source recordings and generated references are intentionally kept out of
Git. The source pack must include its existing consent metadata; this script
copies it without changing its status.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import wave
from pathlib import Path


VOICE_CUTS = (
    {
        "voice_id": "wema-en-us-female",
        "asset": "american-female-slt",
        "duration_seconds": 6.5,
        "transcript": (
            "Author of the Danger Trail, Philip Steels, etc. Not at this "
            "particular case. Tom apologized."
        ),
    },
    {
        "voice_id": "wema-en-us-male",
        "asset": "american-male-bdl",
        "duration_seconds": 6.5,
        "transcript": (
            "Author of the Danger Trail, Philip Steels, etc. Not at this "
            "particular case, Tom apologized, Whittemore."
        ),
    },
    {
        "voice_id": "wema-en-ng-female",
        "asset": "nigerian-female-ngozi",
        "spoken_duration_seconds": 5.6,
        "duration_seconds": 6.0,
        "transcript": (
            "Hello. Thanks for reaching out. My name is Yinka, and I'm here "
            "to help you today."
        ),
    },
    {
        "voice_id": "wema-en-ng-male",
        "asset": "nigerian-male-femi",
        "duration_seconds": 7.0,
        "transcript": (
            "Hello, thanks for reaching out. My name is Femi, and I'm here "
            "to help you today."
        ),
    },
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_short_reference(
    source: Path,
    target: Path,
    spoken_duration_seconds: float,
    duration_seconds: float,
) -> None:
    with wave.open(str(source), "rb") as reader:
        settings = reader.getparams()
        if (settings.nchannels, settings.sampwidth, settings.framerate, settings.comptype) != (
            1,
            2,
            16000,
            "NONE",
        ):
            raise ValueError(f"Expected 16 kHz mono PCM16 reference: {source}")
        max_frames = int(spoken_duration_seconds * settings.framerate)
        if reader.getnframes() < max_frames:
            raise ValueError(f"Source is shorter than the requested cut: {source}")
        audio = reader.readframes(max_frames)
    target_frames = int(duration_seconds * settings.framerate)
    if target_frames < max_frames:
        raise ValueError("Target duration cannot be shorter than the spoken reference cut")
    audio += bytes((target_frames - max_frames) * settings.nchannels * settings.sampwidth)

    target.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(target), "wb") as writer:
        writer.setparams(settings)
        writer.writeframes(audio)


def prepare(source_dir: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for item in VOICE_CUTS:
        source_voice_dir = source_dir / item["asset"]
        source_audio = source_voice_dir / "ref.wav"
        source_consent = source_voice_dir / "consent.json"
        if not source_audio.is_file() or not source_consent.is_file():
            raise FileNotFoundError(f"Missing reference audio or consent metadata for {item['asset']}")

        voice_dir = output_dir / item["voice_id"]
        reference = voice_dir / "ref.wav"
        write_short_reference(
            source_audio,
            reference,
            item.get("spoken_duration_seconds", item["duration_seconds"]),
            item["duration_seconds"],
        )
        (voice_dir / "ref.txt").write_text(item["transcript"] + "\n", encoding="utf-8")
        (voice_dir / "consent.json").write_bytes(source_consent.read_bytes())
        source_metadata_path = source_voice_dir / "meta.json"
        source_metadata = json.loads(source_metadata_path.read_text(encoding="utf-8"))
        metadata = {
            "voice_id": item["voice_id"],
            "source_asset": item["asset"],
            "clone_mode": "ICL ref_audio + ref_text",
            "tts_model_family": "Qwen3-TTS-12Hz-1.7B-Base",
            "reference": {
                "file": "ref.wav",
                "sample_rate": 16000,
                "channels": 1,
                "duration_sec": item["duration_seconds"],
                "spoken_duration_sec": item.get(
                    "spoken_duration_seconds", item["duration_seconds"]
                ),
                "sha256": sha256(reference),
            },
            "ref_text": item["transcript"],
            "source_reference_sha256": source_metadata["reference"]["sha256"],
            "consent_status": json.loads(source_consent.read_text(encoding="utf-8")).get(
                "status", "unknown"
            ),
        }
        (voice_dir / "meta.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        manifest.append({"voice_id": item["voice_id"], **metadata["reference"]})

    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Prepared {len(manifest)} private ICL references in {output_dir}")
    print("Consent metadata was copied unchanged; review it before production use.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", type=Path, help="Folder containing the four voice asset folders")
    parser.add_argument("output_dir", type=Path, help="Private output folder for the short references")
    args = parser.parse_args()
    prepare(args.source_dir.expanduser(), args.output_dir.expanduser())


if __name__ == "__main__":
    main()
