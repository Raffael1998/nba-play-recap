from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
import urllib.request
from pathlib import Path


VIDEO_HEADERS = {
    "Accept": "*/*",
    "Accept-Encoding": "identity",
    "Referer": "https://www.nba.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download a few NBA clip URLs, fingerprint sampled frames, and compare them.",
    )
    parser.add_argument(
        "--clip",
        action="append",
        nargs=2,
        metavar=("LABEL", "URL"),
        required=True,
        help="Clip label and URL. Pass multiple times.",
    )
    parser.add_argument(
        "--ffmpeg-binary",
        default="ffmpeg",
        help="ffmpeg executable name or full path.",
    )
    parser.add_argument(
        "--ffprobe-binary",
        default="ffprobe",
        help="ffprobe executable name or full path.",
    )
    parser.add_argument(
        "--save-frame-dir",
        type=Path,
        help="Optional directory where sampled frames are exported for manual inspection.",
    )
    return parser.parse_args()


def download_clip(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers=VIDEO_HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        destination.write_bytes(response.read())


def probe_duration_seconds(ffprobe_binary: str, clip_path: Path) -> float:
    command = [
        ffprobe_binary,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(clip_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    return float(result.stdout.strip())


def extract_frame_hash(ffmpeg_binary: str, clip_path: Path, timestamp_seconds: float) -> str:
    command = [
        ffmpeg_binary,
        "-v",
        "error",
        "-ss",
        f"{timestamp_seconds:.3f}",
        "-i",
        str(clip_path),
        "-frames:v",
        "1",
        "-vf",
        "scale=16:16,format=gray",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-",
    ]
    result = subprocess.run(command, capture_output=True, check=True)
    return hashlib.sha256(result.stdout).hexdigest()


def export_frame_png(
    ffmpeg_binary: str,
    clip_path: Path,
    timestamp_seconds: float,
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg_binary,
        "-v",
        "error",
        "-ss",
        f"{timestamp_seconds:.3f}",
        "-i",
        str(clip_path),
        "-frames:v",
        "1",
        str(output_path),
    ]
    subprocess.run(command, capture_output=True, check=True)


def fingerprint_clip(ffmpeg_binary: str, ffprobe_binary: str, clip_path: Path) -> dict[str, object]:
    duration_seconds = probe_duration_seconds(ffprobe_binary, clip_path)
    sample_positions = [0.2, 0.5, 0.8]
    frame_hashes = []
    for fraction in sample_positions:
        timestamp_seconds = max(0.0, min(duration_seconds - 0.05, duration_seconds * fraction))
        frame_hashes.append(
            {
                "fraction": fraction,
                "timestamp_seconds": round(timestamp_seconds, 3),
                "sha256": extract_frame_hash(ffmpeg_binary, clip_path, timestamp_seconds),
            }
        )
    signature = hashlib.sha256(
        "|".join(frame["sha256"] for frame in frame_hashes).encode("ascii")
    ).hexdigest()
    return {
        "duration_seconds": round(duration_seconds, 3),
        "frame_hashes": frame_hashes,
        "signature": signature,
    }


def main() -> int:
    args = parse_args()
    clips = args.clip

    with tempfile.TemporaryDirectory(prefix="nba-clip-fingerprint-") as temp_dir:
        temp_path = Path(temp_dir)
        results: list[dict[str, object]] = []
        for index, (label, url) in enumerate(clips, start=1):
            clip_path = temp_path / f"{index:02d}_{label}.mp4"
            download_clip(url, clip_path)
            fingerprint = fingerprint_clip(args.ffmpeg_binary, args.ffprobe_binary, clip_path)
            results.append(
                {
                    "label": label,
                    "url": url,
                    "local_path": str(clip_path),
                    **fingerprint,
                }
            )

        pairwise: list[dict[str, object]] = []
        for left_index in range(len(results)):
            for right_index in range(left_index + 1, len(results)):
                left = results[left_index]
                right = results[right_index]
                identical_fractions = []
                different_fractions = []
                for left_frame, right_frame in zip(left["frame_hashes"], right["frame_hashes"]):
                    fraction = left_frame["fraction"]
                    if left_frame["sha256"] == right_frame["sha256"]:
                        identical_fractions.append(fraction)
                    else:
                        different_fractions.append(fraction)
                pairwise.append(
                    {
                        "left": left["label"],
                        "right": right["label"],
                        "same_signature": left["signature"] == right["signature"],
                        "same_duration": left["duration_seconds"] == right["duration_seconds"],
                        "identical_fractions": identical_fractions,
                        "different_fractions": different_fractions,
                    }
                )

        if args.save_frame_dir:
            root = args.save_frame_dir
            identical_dir = root / "identical"
            different_dir = root / "different"
            exported = set()
            result_by_label = {result["label"]: result for result in results}
            for comparison in pairwise:
                for bucket_name, fractions in (
                    ("identical", comparison["identical_fractions"]),
                    ("different", comparison["different_fractions"]),
                ):
                    for fraction in fractions:
                        for label in (comparison["left"], comparison["right"]):
                            key = (bucket_name, label, fraction)
                            if key in exported:
                                continue
                            clip = result_by_label[label]
                            clip_path = Path(clip["local_path"])
                            timestamp_seconds = next(
                                frame["timestamp_seconds"]
                                for frame in clip["frame_hashes"]
                                if frame["fraction"] == fraction
                            )
                            target_dir = identical_dir if bucket_name == "identical" else different_dir
                            export_frame_png(
                                args.ffmpeg_binary,
                                clip_path,
                                timestamp_seconds,
                                target_dir / f"{label}_f{fraction:.1f}.png",
                            )
                            exported.add(key)

        print(json.dumps({"clips": results, "pairwise": pairwise}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
