#!/usr/bin/env python3
"""Compare Plate Recognizer's Snapshot Cloud API (fast mode) against ground
truth, as an external baseline for our own recall/accuracy numbers.

Not part of the production pipeline — a one-off benchmark, run locally
(NOT on the Pi — this hits an external paid API from whatever machine you
run it on, and there is no reason to route it through the field unit).
Extracts every Nth frame from a video, sends each to Plate Recognizer, and
writes a CSV of every plate read with its timestamp, score, and region.

Auth: reads the token from the PLATERECOGNIZER_TOKEN env var only. Never
accept it as a CLI arg (leaks into `ps`/shell history) and never write it to
a file.

Usage:
    export PLATERECOGNIZER_TOKEN=...   # not on the command line
    python3 tools/compare_platerecognizer.py \
        --video dtfootagefinal_5fps_local.mp4 --stride 5 --fps 5 \
        --out pr_reads.csv
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import sys
import time

import requests

API_URL = "https://api.platerecognizer.com/v1/plate-reader/"
FIELDS = ["frame", "t_sec", "plate", "score", "dscore", "region_code",
          "region_score", "box_xmin", "box_ymin", "box_xmax", "box_ymax"]


class Aborted(Exception):
    """Raised on 401/403 — an auth problem, not a transient failure."""


def call_with_retry(token: str, jpeg_bytes: bytes, mode: str,
                    max_retries: int = 4, timeout: float = 15.0):
    """One frame, with retry+backoff on 429 (rate limit).

    Plate Recognizer's own 429 body says "Expected available in 1 second" —
    honoring that literally (with a little headroom) is what fixed the
    all-3xx-are-drops bug in the first version of this script, which made
    the comparison unfairly bad for Plate Recognizer rather than reflecting
    what it can actually read.

    Returns list[dict] of result rows (possibly empty). Raises Aborted on
    401/403. Any other persistent failure returns [] and prints a warning —
    a benchmark script should not silently under- or over-count on error.
    """
    backoff = 1.2
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(
                API_URL,
                headers={"Authorization": f"Token {token}"},
                files={"upload": ("frame.jpg", jpeg_bytes, "image/jpeg")},
                data={"config": json.dumps({"mode": mode})},
                timeout=timeout,
            )
        except requests.RequestException as e:
            last_err = str(e)
            time.sleep(backoff)
            backoff *= 1.5
            continue

        if resp.status_code in (401, 403):
            raise Aborted(f"HTTP {resp.status_code}: {resp.text[:200]}")

        if resp.status_code == 429:
            last_err = "429 throttled"
            time.sleep(backoff)
            backoff *= 1.5
            continue

        if resp.status_code != 201:
            last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            break  # not a retryable condition (e.g. 400 bad image)

        try:
            body = resp.json()
        except ValueError:
            last_err = "non-JSON 201 response"
            break

        rows = []
        for r in body.get("results", []):
            box = r.get("box", {})
            rows.append({
                "plate": (r.get("plate") or "").upper(),
                "score": r.get("score"),
                "dscore": r.get("dscore"),
                "region_code": (r.get("region") or {}).get("code"),
                "region_score": (r.get("region") or {}).get("score"),
                "box_xmin": box.get("xmin"), "box_ymin": box.get("ymin"),
                "box_xmax": box.get("xmax"), "box_ymax": box.get("ymax"),
            })
        return rows

    print(f"  giving up after {attempt + 1} attempts: {last_err}",
          file=sys.stderr)
    return []


def parse_gt_csv(path: str) -> list[tuple[int, float, str]]:
    """Ground-truth CSV: Car #,Time,Missing plate?. Time is M.SS (e.g. 12.4
    means 12:40), matching the sheet's own format — NOT decimal minutes."""
    import csv as _csv
    rows = []
    with open(path) as f:
        for r in _csv.DictReader(f):
            t = r["Time"].strip()
            if not t:
                continue
            if "." in t:
                mm, ss = t.split(".", 1)
                ss = (ss + "0")[:2]
            else:
                mm, ss = t, "00"
            secs = int(mm) * 60 + int(ss)
            rows.append((int(r["Car #"]), float(secs),
                        (r.get("Missing plate?") or "").strip()))
    rows.sort(key=lambda r: r[1])
    return rows


def wanted_frame_indices_from_gt(gt_rows, fps: float, window_s: float,
                                 window_stride: int, only_plated: bool):
    """Frame indices within +/- window_s of each labeled car, so the API is
    only called where a plate could plausibly be, not across dead lane time.
    """
    wanted = set()
    for _, secs, missing in gt_rows:
        if only_plated and missing:
            continue
        center = int(round(secs * fps))
        half = int(round(window_s * fps))
        for f in range(center - half, center + half + 1, window_stride):
            if f >= 0:
                wanted.add(f)
    return sorted(wanted)


def extract_frames(video_path: str, stride: int = 0, max_frames: int = 0,
                   wanted_indices: list[int] | None = None):
    """Decode once, return [(frame_idx, jpeg_bytes), ...].

    Either every Nth frame (stride mode, for a full sweep) or an explicit
    set of indices (windowed mode, for a ground-truth-scoped run). Upfront
    extraction — not interleaved with network calls — is what made the first
    version's control flow hard to reason about; decode speed and network
    speed fighting over the same loop is exactly what produced an unkillable
    hang. Decoding first is slower wall-clock but the network stage
    afterward is then a simple, ordinary thread-pool fan-out.
    """
    import cv2
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {video_path}")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    want = set(wanted_indices) if wanted_indices is not None else None
    out = []
    idx = 0
    while True:
        ok, bgr = cap.read()
        if not ok:
            break
        take = (idx in want) if want is not None else (idx % stride == 0)
        if take:
            if max_frames and len(out) >= max_frames:
                break
            success, buf = cv2.imencode(
                ".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            if success:
                out.append((idx, buf.tobytes()))
        idx += 1
    cap.release()
    return out, total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--video", required=True)
    ap.add_argument("--stride", type=int, default=5,
                    help="full-sweep mode: send every Nth frame")
    ap.add_argument("--gt-csv", default=None,
                    help="windowed mode: ground-truth CSV (Car #,Time,"
                         "Missing plate?) — only call the API near a "
                         "labeled car, not across the whole clip")
    ap.add_argument("--window-seconds", type=float, default=1.5,
                    help="windowed mode: +/- seconds around each label")
    ap.add_argument("--window-stride", type=int, default=3,
                    help="windowed mode: every Nth frame within a window")
    ap.add_argument("--only-plated", action="store_true", default=True,
                    help="windowed mode: skip rows flagged missing-plate "
                         "(default True — no point paying to ask an OCR "
                         "API to read a plate that isn't there)")
    ap.add_argument("--fps", type=float, default=5.0,
                    help="video fps, for frame-index -> timestamp")
    ap.add_argument("--mode", default="fast", choices=["fast", "accurate"])
    ap.add_argument("--out", default="pr_reads.csv")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="parallel requests (billed, quota-capped API — "
                         "keep modest; 2 was already enough to get "
                         "rate-limited on this account)")
    ap.add_argument("--max-frames", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print the call count, make NO "
                         "requests, spend NOTHING")
    args = ap.parse_args()

    token = os.environ.get("PLATERECOGNIZER_TOKEN")
    if not token and not args.dry_run:
        print("ERROR: set PLATERECOGNIZER_TOKEN in the environment "
              "(not as a CLI arg).", file=sys.stderr)
        return 2

    wanted = None
    if args.gt_csv:
        gt_rows = parse_gt_csv(args.gt_csv)
        wanted = wanted_frame_indices_from_gt(
            gt_rows, args.fps, args.window_seconds, args.window_stride,
            args.only_plated)
        n_cars = sum(1 for _, _, m in gt_rows if not (args.only_plated and m))
        print(f"windowed mode: {len(gt_rows)} labeled cars, {n_cars} in "
              f"scope, +/-{args.window_seconds}s window, every "
              f"{args.window_stride} frame(s) -> {len(wanted)} frames wanted")

    print(f"decoding {args.video}...")
    frames, total = extract_frames(args.video, args.stride, args.max_frames,
                                   wanted_indices=wanted)
    print(f"video has {total} frames; THIS WILL MAKE {len(frames)} BILLED "
          f"API CALLS in '{args.mode}' mode, concurrency={args.concurrency}")

    if args.dry_run:
        print("\n--dry-run: stopping here, zero requests made.")
        return 0

    print("aborting immediately on 401/403.\n")

    out_f = open(args.out, "w", newline="")
    writer = csv.DictWriter(out_f, fieldnames=FIELDS)
    writer.writeheader()
    out_f.flush()

    n_reads = 0
    n_done = 0
    aborted_msg = None
    t0 = time.time()

    def task(frame_idx, jpeg_bytes):
        rows = call_with_retry(token, jpeg_bytes, args.mode)
        return frame_idx, rows

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = [pool.submit(task, idx, jb) for idx, jb in frames]
        try:
            for fut in concurrent.futures.as_completed(futs, timeout=None):
                try:
                    frame_idx, rows = fut.result()
                except Aborted as e:
                    aborted_msg = str(e)
                    for f2 in futs:
                        f2.cancel()
                    break
                n_done += 1
                for r in rows:
                    r["frame"] = frame_idx
                    r["t_sec"] = round(frame_idx / args.fps, 2)
                    writer.writerow(r)
                    n_reads += 1
                if rows:
                    out_f.flush()
                if n_done % 25 == 0 or n_done == len(frames):
                    el = time.time() - t0
                    print(f"  {n_done}/{len(frames)}  ({n_done/el:.1f}/s)  "
                          f"reads={n_reads}", flush=True)
        except KeyboardInterrupt:
            print("\ninterrupted — cancelling remaining requests",
                  file=sys.stderr)
            for f2 in futs:
                f2.cancel()
            raise

    out_f.close()
    print(f"\ndone={n_done}/{len(frames)}  reads={n_reads}  -> {args.out}")
    if aborted_msg:
        print(f"ABORTED: {aborted_msg}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
