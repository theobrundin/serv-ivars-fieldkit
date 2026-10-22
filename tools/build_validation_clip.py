#!/usr/bin/env python3
"""Build a compact, plate-readable replay clip from labeled event timestamps.

The output keeps the source resolution, removes audio, samples at the target
pipeline FPS, and concatenates only short windows around the public event
timestamps. Raw plate labels are neither read nor written by this tool.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path


def _events(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or "timestamp_seconds" not in rows[0]:
        raise ValueError("ground-truth CSV needs timestamp_seconds")
    return rows


def _windows(rows: list[dict[str, str]], radius: float) -> list[dict]:
    spans = []
    for row in rows:
        center = float(row["timestamp_seconds"])
        spans.append({
            "start": max(0.0, center - radius),
            "end": center + radius,
            "events": [row],
        })
    spans.sort(key=lambda span: span["start"])

    merged: list[dict] = []
    for span in spans:
        if merged and span["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], span["end"])
            merged[-1]["events"].extend(span["events"])
        else:
            merged.append(span)
    return merged


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _encode_segment(
    video: Path, output: Path, start: float, end: float, fps: float, crf: int
) -> None:
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}", "-i", str(video),
        "-t", f"{end - start:.3f}", "-map", "0:v:0",
        "-vf", f"fps={fps:g}", "-an", "-c:v", "libx264",
        "-preset", "veryfast", "-crf", str(crf), "-threads", "2",
        "-pix_fmt", "yuv420p", "-video_track_timescale", "10240",
        str(output),
    ], check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--radius", type=float, default=4.0)
    parser.add_argument("--fps", type=float, default=5.0)
    parser.add_argument("--crf", type=int, default=18)
    args = parser.parse_args()

    rows = _events(args.events)
    spans = _windows(rows, args.radius)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="serv-validation-clip-") as tmp_name:
        tmp = Path(tmp_name)
        segment_paths = [
            tmp / f"segment-{index:03d}.mp4"
            for index in range(len(spans))
        ]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(
                    _encode_segment,
                    args.video,
                    segment_paths[index],
                    span["start"],
                    span["end"],
                    args.fps,
                    args.crf,
                )
                for index, span in enumerate(spans)
            ]
            for future in futures:
                future.result()

        concat_file = tmp / "segments.ffconcat"
        concat_file.write_text(
            "ffconcat version 1.0\n" + "".join(
                f"file '{path.as_posix()}'\n" for path in segment_paths
            )
        )
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_file),
            "-c", "copy", "-movflags", "+faststart", str(args.output),
        ], check=True)

    clip_cursor = 0.0
    manifest_rows = []
    for segment_number, span in enumerate(spans, start=1):
        for row in span["events"]:
            source_time = float(row["timestamp_seconds"])
            manifest_rows.append({
                "car_number": int(row["car_number"]),
                "plate_category": row["plate_category"],
                "source_timestamp_seconds": source_time,
                "clip_timestamp_seconds": round(
                    clip_cursor + source_time - span["start"], 3
                ),
                "segment_number": segment_number,
                "segment_source_start": span["start"],
                "segment_source_end": span["end"],
            })
        clip_cursor += span["end"] - span["start"]

    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    with args.manifest.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(manifest_rows[0]))
        writer.writeheader()
        writer.writerows(sorted(manifest_rows, key=lambda row: row["car_number"]))

    probe = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration,size:stream=width,height,r_frame_rate,nb_frames",
        "-of", "json", str(args.output),
    ], check=True, capture_output=True, text=True)
    result = {
        "output": str(args.output),
        "manifest": str(args.manifest),
        "events": len(rows),
        "segments": len(spans),
        "source_window_seconds": clip_cursor,
        "sha256": _sha256(args.output),
        "probe": json.loads(probe.stdout),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
