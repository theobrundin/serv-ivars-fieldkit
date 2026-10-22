#!/usr/bin/env python3
"""Capture rich engine output from real footage into a replay dataset.

Run this ONCE on aarch64 hardware with the licensed SDK. It writes a JSONL
file of every read — including the colour / make / model / vehicle-box fields
the production adapter currently discards — which ReplayEngine can then feed
through the full pipeline on any machine, including a Mac with no SDK.

    python3 vehicle_records/capture.py \
        --video clip_5fps.mp4 \
        --config /home/serv/plate_reader/config.json \
        --out replay_clip.jsonl

Environment (the SDK needs both, systemd sets them in production):
    export LD_LIBRARY_PATH=/home/serv/ultimateALPR-SDK/binaries/linux/aarch64
    export PYTHONPATH=/home/serv/ultimateALPR-SDK/python:$LD_LIBRARY_PATH
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

_REPO = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from plate_reader.config import load as load_cfg  # noqa: E402
from vehicle_records.engine import RichUltimateAlprEngine  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--video", required=True)
    ap.add_argument("--config", required=True,
                    help="a production config.json — supplies the licence "
                         "token path, thresholds and classifier switches")
    ap.add_argument("--out", required=True, help="replay JSONL to write")
    ap.add_argument("--max-frames", type=int, default=0)
    args = ap.parse_args()

    import cv2

    cfg = load_cfg(pathlib.Path(args.config))
    cls = cfg.alpr.classifiers
    print(f"classifiers: vehicle_detect={cls.vehicle_detect_enabled} "
          f"color={cls.color_enabled} vmmr={cls.vmmr_enabled} "
          f"lpci={cls.lpci_enabled}")
    if not cls.vehicle_detect_enabled:
        print("WARNING: vehicle_detect_enabled is OFF. Without the SDK car "
              "box there is no vehicle to build a record around, and every "
              "plateless car is invisible. This capture will be of limited "
              "use.", file=sys.stderr)

    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        print(f"ERROR: cannot open {args.video}", file=sys.stderr)
        return 2
    fw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    fh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    engine = RichUltimateAlprEngine(cfg)
    if hasattr(engine, "set_frame_size"):
        engine.set_frame_size(fw, fh)
    engine.open()

    n_frames = n_reads = n_color = n_mmy = 0
    t0 = time.time()
    out_path = pathlib.Path(args.out)
    fd = os.open(
        out_path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    with os.fdopen(fd, "w") as out:
        out.write(json.dumps({"_meta": True, "frame_w": fw, "frame_h": fh,
                              "source": pathlib.Path(args.video).name}) + "\n")
        try:
            while True:
                ok, bgr = cap.read()
                if not ok:
                    break
                if args.max_frames and n_frames >= args.max_frames:
                    break
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                reads = engine.infer(rgb) or []
                if reads:
                    out.write(json.dumps({
                        "frame": n_frames,
                        "reads": [r.to_json() for r in reads],
                    }) + "\n")
                    n_reads += len(reads)
                    n_color += sum(1 for r in reads if r.color)
                    n_mmy += sum(1 for r in reads if r.make)
                n_frames += 1
                if n_frames % 250 == 0:
                    el = time.time() - t0
                    print(f"  frame {n_frames} ({n_frames/el:.1f} fps) "
                          f"reads={n_reads} color={n_color}", flush=True)
        finally:
            cap.release()
            engine.close()

    print(f"\nframes={n_frames} reads={n_reads} "
          f"with_color={n_color} with_make_model={n_mmy} -> {args.out}")
    if n_reads and not n_color:
        print("NOTE: zero colour readings. klass_vcr_enabled is probably off "
              "in this config, so the colour field will be null everywhere.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
