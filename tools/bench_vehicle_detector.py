#!/usr/bin/env python3
"""On-device benchmark: can THIS Pi afford a vehicle detector? (Problem 2)

Run this ON THE PI (not the Mac — Mac numbers prove nothing about the Pi)
before committing to a vehicle-detection model. It measures, with real
frames, what the per-frame budget looks like:

  1. BASELINE — fast-alpr alone (what production pays today).
  2. +DETECTOR — fast-alpr plus a candidate ONNX vehicle detector, at
     every-frame and every-Nth-frame schedules.

The decision rule, stated up front so the numbers can't be argued with
after the fact: at 5fps the total budget is 200ms/frame. If
baseline + detector/N (amortized) fits under ~180ms p95, the detector is
affordable at that schedule; the motion model interpolates between detector
frames, so every-2nd or every-3rd frame is fine for tracking. If nothing
fits, we stay on motion boxes and revisit with a lighter model.

Usage (on the Pi):
    # Baseline only:
    python tools/bench_vehicle_detector.py --video dtfootage.mp4

    # With a candidate ONNX detector (e.g. a yolo-nano class model at 320px):
    python tools/bench_vehicle_detector.py --video dtfootage.mp4 \
        --model vehicle_nano_320.onnx --input-size 320

No model weights ship with this script on purpose: the model choice should
follow the baseline numbers, not precede them.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))]


def _report_block(name, ms):
    if not ms:
        print(f"  {name}: no samples")
        return
    print(f"  {name}:")
    print(f"    frames   : {len(ms)}")
    print(f"    mean     : {statistics.mean(ms):7.1f} ms")
    print(f"    p50      : {_pct(ms, 50):7.1f} ms")
    print(f"    p95      : {_pct(ms, 95):7.1f} ms")
    print(f"    max      : {max(ms):7.1f} ms")


def main() -> int:
    ap = argparse.ArgumentParser(description="Vehicle detector Pi benchmark")
    ap.add_argument("--video", required=True, help="footage to feed (mp4)")
    ap.add_argument("--frames", type=int, default=150,
                    help="frames to benchmark (default 150 = 30s at 5fps)")
    ap.add_argument("--model", default=None,
                    help="candidate ONNX vehicle detector (optional)")
    ap.add_argument("--input-size", type=int, default=320,
                    help="detector input size (square, default 320)")
    ap.add_argument("--skip-alpr", action="store_true",
                    help="benchmark detector only (no fast-alpr)")
    args = ap.parse_args()

    try:
        import cv2
        import numpy as np
    except ImportError:
        print("ERROR: opencv required", file=sys.stderr)
        return 2

    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        print(f"ERROR: cannot open {args.video}", file=sys.stderr)
        return 2

    # --- fast-alpr baseline ---
    # NOT ported to the PlateEngine protocol (plate_reader/engines.py), on
    # purpose. This tool measures the raw per-frame cost of fast-alpr as the
    # production baseline for a vehicle-detector budget (Problem 2). Putting
    # the adapter in the middle would time the adapter too, and the whole
    # point is an unmediated number. It also deliberately uses a bare ALPR()
    # with no provider pinning because it only ever runs on the Pi.
    # If this tool ever needs to bench a NON-fast-alpr engine, port it then.
    alpr = None
    if not args.skip_alpr:
        try:
            from fast_alpr import ALPR
            print("loading fast-alpr...")
            alpr = ALPR()
        except Exception as e:
            print(f"WARNING: fast-alpr unavailable ({e}); baseline skipped")

    # --- candidate ONNX detector ---
    sess = None
    input_name = None
    if args.model:
        try:
            import onnxruntime as ort
            print(f"loading ONNX model {args.model} (CPU provider)...")
            sess = ort.InferenceSession(
                args.model, providers=["CPUExecutionProvider"]
            )
            input_name = sess.get_inputs()[0].name
        except Exception as e:
            print(f"ERROR: could not load ONNX model: {e}", file=sys.stderr)
            return 2

    alpr_ms: list[float] = []
    det_ms: list[float] = []
    n = 0
    print(f"benchmarking up to {args.frames} frames...")
    while n < args.frames:
        ret, frame = cap.read()
        if not ret:
            break
        n += 1
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        if alpr is not None:
            t0 = time.monotonic()
            try:
                alpr.predict(rgb)
            except Exception:
                pass
            alpr_ms.append((time.monotonic() - t0) * 1000.0)

        if sess is not None:
            s = args.input_size
            t0 = time.monotonic()
            blob = cv2.resize(rgb, (s, s)).astype("float32") / 255.0
            blob = blob.transpose(2, 0, 1)[None, ...]
            try:
                sess.run(None, {input_name: blob})
            except Exception as e:
                print(f"  detector inference failed: {e}", file=sys.stderr)
                sess = None
                continue
            det_ms.append((time.monotonic() - t0) * 1000.0)

        if n % 25 == 0:
            print(f"  ...{n} frames")

    cap.release()
    budget = 200.0  # ms/frame at 5fps
    print()
    print("=" * 60)
    print("RESULTS (run on the Pi or the numbers are meaningless)")
    print("=" * 60)
    _report_block("fast-alpr baseline", alpr_ms)
    _report_block("vehicle detector", det_ms)

    if alpr_ms:
        base_p95 = _pct(alpr_ms, 95)
        print()
        print(f"  5fps budget: {budget:.0f} ms/frame; "
              f"baseline p95 uses {base_p95:.0f} ms "
              f"({100 * base_p95 / budget:.0f}%)")
        if det_ms:
            d_p95 = _pct(det_ms, 95)
            print()
            print("  amortized cost per frame if detector runs every Nth frame")
            print("  (motion model interpolates between detector frames):")
            for nth in (1, 2, 3, 5):
                total = base_p95 + d_p95 / nth
                verdict = "FITS" if total <= budget * 0.9 else "DOES NOT FIT"
                print(f"    every {nth}: {base_p95:.0f} + {d_p95:.0f}/{nth} "
                      f"= {total:6.0f} ms  -> {verdict}")
            print()
            print("  Decision rule: pick the smallest N that FITS with margin.")
            print("  If none fit, stay on motion boxes; try a smaller model")
            print("  or input size before giving up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
