#!/usr/bin/env python3
"""Dump EVERY raw plate read, so a missed car can be blamed on a named gate.

The problem this solves: the pipeline reports "13 confirms" and the raw engine
reports "19 readable plates", and nothing in between says which gate ate the
other six. Confirms are the end of a six-stage funnel:

    engine detect        -> a plate box exists at all
    engine recognise     -> characters, scored with recogn_score_type=min
    app detect floor     -> thresholds.min_detection_confidence
    app OCR floor        -> thresholds.min_ocr_confidence
    normalize()          -> NORMALIZATION_SPEC rules 1-4 (length 4-12)
    detection_zone       -> legacy percentage box
    vote                 -> vote_min_frames frames, IDENTICAL normalized
                            string, inside vote_window_seconds, with
                            min(det) and min(ocr) >= min_group_confidence

A car can die at any one of those and the summary line looks the same. This
records the raw engine output BEFORE any gate, plus which gate each read would
have hit, so the funnel can be measured instead of guessed at.

Deliberately does NOT re-implement any gate: it imports the real thresholds
and the real normalize(), so the verdict here is the verdict the pipeline
would reach. It only bypasses them for RECORDING.

Usage (on the Pi, engine env set):
    python3 tools/diag_read_funnel.py --video clip.mp4 \
        --config /home/serv/plate_reader/config.json --out reads.csv
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import sys
import time

_REPO = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--video", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", default="reads.csv")
    ap.add_argument("--fps", type=float, default=5.0,
                    help="video fps, for converting frame index to a timestamp")
    ap.add_argument("--max-frames", type=int, default=0)
    args = ap.parse_args()

    import cv2
    from plate_reader.config import load as load_cfg
    from plate_reader.engines import build_engine
    from plate_reader.normalizer import normalize
    from plate_reader.camera import bbox_inside_zone

    cfg = load_cfg(pathlib.Path(args.config))
    th = cfg.thresholds

    # Echo the EFFECTIVE gates. alpr.thresholds.app_* silently overwrite the
    # legacy thresholds block at load time, so the numbers that actually run
    # are not necessarily the ones written in the legacy block.
    print("effective gates actually applied:")
    print(f"  min_detection_confidence : {th.min_detection_confidence}")
    print(f"  min_ocr_confidence       : {th.min_ocr_confidence}")
    print(f"  min_group_confidence     : {th.min_group_confidence}")
    print(f"  vote_min_frames          : {th.vote_min_frames}")
    print(f"  vote_window_seconds      : {th.vote_window_seconds}")
    print(f"  normalized length        : {th.min_normalized_length}"
          f"-{th.max_normalized_length}")
    print(f"  detection_zone enabled   : {cfg.detection_zone.enabled}")
    print(f"  engine detect_minscore   : {cfg.alpr.thresholds.detect_minscore}")
    print(f"  engine recogn_minscore   : {cfg.alpr.thresholds.recogn_minscore}")
    print()

    engine = build_engine(cfg)
    engine.open()

    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        print(f"ERROR: cannot open {args.video}", file=sys.stderr)
        return 2

    rows = []
    idx = 0
    t0 = time.time()
    try:
        while True:
            ok, bgr = cap.read()
            if not ok:
                break
            if args.max_frames and idx >= args.max_frames:
                break
            frame = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            if idx == 0 and hasattr(engine, "set_frame_size"):
                h, w = frame.shape[:2]
                engine.set_frame_size(w, h)

            for r in engine.infer(frame) or []:
                norm = normalize(
                    r.text,
                    min_length=th.min_normalized_length,
                    max_length=th.max_normalized_length,
                )
                x1, y1, x2, y2 = r.bbox if r.bbox else (0, 0, 0, 0)
                in_zone = bbox_inside_zone(
                    r.bbox, frame.shape, cfg.detection_zone)

                # First gate that would reject this read, in pipeline order.
                if not r.text:
                    gate = "no_text_localized_only"
                elif r.detection_confidence < th.min_detection_confidence:
                    gate = "app_detect_min"
                elif r.ocr_confidence < th.min_ocr_confidence:
                    gate = "app_ocr_min"
                elif norm is None:
                    gate = "normalize"
                elif not in_zone:
                    gate = "detection_zone"
                else:
                    gate = "PASSES_to_vote"

                rows.append({
                    "frame": idx,
                    "t_sec": round(idx / args.fps, 2),
                    "raw_text": r.text,
                    "normalized": norm or "",
                    "det_conf": round(r.detection_confidence, 4),
                    "ocr_conf": round(r.ocr_confidence, 4),
                    "plate_w_px": x2 - x1,
                    "plate_h_px": y2 - y1,
                    "in_zone": int(in_zone),
                    "has_vehicle_box": int(r.vehicle_bbox is not None),
                    "first_gate_hit": gate,
                })
            idx += 1
            if idx % 250 == 0:
                el = time.time() - t0
                print(f"  frame {idx}  ({idx/el:.1f} fps)  reads={len(rows)}",
                      flush=True)
    finally:
        cap.release()
        engine.close()

    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else
                           ["frame"])
        w.writeheader()
        w.writerows(rows)

    print(f"\nframes={idx}  raw reads={len(rows)}  -> {args.out}")
    tally: dict[str, int] = {}
    for r in rows:
        tally[r["first_gate_hit"]] = tally.get(r["first_gate_hit"], 0) + 1
    print("\nreads by first gate hit:")
    for k, v in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {k:26s} {v:6d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
