#!/usr/bin/env python3
"""On-site calibration: turn "what numbers go in config?" into copy-paste.

Run this ON THE UNIT, at the final mount position, with a car at the speaker.
It reports every plate and vehicle box in BOTH pixel and normalized
coordinates, suggests an `alpr.roi` block that bounds what it actually saw,
and writes an annotated JPEG so you can eyeball aim, ROI and dwell zone
together.

Config stores normalized 0.0-1.0 coordinates, never pixels, because capture
resolution varies per unit and can change; this tool prints both so you can
sanity-check the conversion.

Usage
-----
  # live frame from the camera
  python3 tools/calibrate.py

  # or against a saved still / video frame
  python3 tools/calibrate.py --image /home/serv/frame.jpg
  python3 tools/calibrate.py --video /home/serv/test_footage.mp4 --frame 2900

  --out       annotated JPEG path (default calibration.jpg)
  --config    config path (default ./config.json)
  --margin    fraction added around observed plates in the ROI suggestion
"""
import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def _load_frame(args):
    """Return an RGB numpy frame from --image, --video, or the live camera."""
    import numpy as np  # noqa: F401

    if args.image:
        import cv2
        bgr = cv2.imread(args.image)
        if bgr is None:
            raise SystemExit(f"ERROR: could not read image {args.image}")
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), f"image {args.image}"

    if args.video:
        import cv2
        cap = cv2.VideoCapture(args.video)
        if not cap.isOpened():
            raise SystemExit(f"ERROR: could not open video {args.video}")
        if args.frame:
            cap.set(cv2.CAP_PROP_POS_FRAMES, args.frame)
        ok, bgr = cap.read()
        cap.release()
        if not ok:
            raise SystemExit(f"ERROR: could not read frame {args.frame}")
        return (cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB),
                f"{args.video} frame {args.frame}")

    # Live camera.
    from plate_reader.camera import Camera
    from plate_reader.config import load as load_cfg
    cfg = load_cfg(pathlib.Path(args.config))
    cam = Camera(cfg.camera)
    cam.open()
    try:
        return cam.capture(), "live camera"
    finally:
        cam.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--image")
    ap.add_argument("--video")
    ap.add_argument("--frame", type=int, default=0)
    ap.add_argument("--out", default="calibration.jpg")
    ap.add_argument("--margin", type=float, default=0.05,
                    help="fraction of frame added around observed plates")
    args = ap.parse_args()

    from plate_reader.config import load as load_cfg
    from plate_reader.detector import Detector

    cfg_path = pathlib.Path(args.config)
    if not cfg_path.is_file():
        raise SystemExit(f"ERROR: no config at {cfg_path}")
    cfg = load_cfg(cfg_path)

    frame, source = _load_frame(args)
    h, w = frame.shape[:2]

    print("=" * 72)
    print(f"CALIBRATION  |  source: {source}")
    print(f"frame: {w} x {h} px")
    print("=" * 72)

    det = Detector(cfg)
    # Give the engine the true frame size so a normalized ROI maps correctly.
    eng = getattr(det, "_engine", None)
    det.open()
    if hasattr(det._engine, "set_frame_size"):
        det._engine.set_frame_size(w, h)
    try:
        reads = det._engine.infer(frame)
    finally:
        det.close()

    if not reads:
        print("\nNo plates or vehicles detected in this frame.")
        print("  - is a car actually in view?")
        print("  - if alpr.roi is enabled, the plate may be outside it")
        print(f"  - current roi: enabled={cfg.alpr.roi.enabled} "
              f"x={cfg.alpr.roi.x} y={cfg.alpr.roi.y} "
              f"w={cfg.alpr.roi.w} h={cfg.alpr.roi.h}")

    plate_boxes = []
    print(f"\n{len(reads)} detection(s):\n")
    for i, r in enumerate(reads, 1):
        x1, y1, x2, y2 = r.bbox
        has_plate = bool(r.text)
        if has_plate and (x2 - x1) > 0:
            plate_boxes.append(r.bbox)
        print(f"  [{i}] {'PLATE' if has_plate else 'VEHICLE-ONLY'}")
        if has_plate:
            print(f"      text            : {r.text!r}")
            print(f"      det / ocr conf  : {r.detection_confidence:.3f} / "
                  f"{r.ocr_confidence:.3f}")
            print(f"      plate box px    : ({x1}, {y1}) -> ({x2}, {y2})")
            print(f"      plate box norm  : x={x1/w:.4f} y={y1/h:.4f} "
                  f"w={(x2-x1)/w:.4f} h={(y2-y1)/h:.4f}")
            print(f"      plate centre    : x={((x1+x2)/2)/w:.4f} "
                  f"y={((y1+y2)/2)/h:.4f}")
        if r.vehicle_bbox:
            vx1, vy1, vx2, vy2 = r.vehicle_bbox
            area = ((vx2 - vx1) * (vy2 - vy1)) / float(w * h)
            print(f"      vehicle box px  : ({vx1}, {vy1}) -> ({vx2}, {vy2})")
            print(f"      vehicle box norm: x={vx1/w:.4f} y={vy1/h:.4f} "
                  f"w={(vx2-vx1)/w:.4f} h={(vy2-vy1)/h:.4f}")
            print(f"      vehicle area    : {area:.4f} of frame "
                  f"  <-- min_vehicle_box_area candidate")
            print(f"      vehicle bottom  : x={((vx1+vx2)/2)/w:.4f} "
                  f"y={vy2/h:.4f}   <-- 'vehicle_bottom' anchor")
        print("")

    # ---- suggested ROI ------------------------------------------------
    if plate_boxes:
        m = args.margin
        xs1 = min(b[0] for b in plate_boxes) / w
        ys1 = min(b[1] for b in plate_boxes) / h
        xs2 = max(b[2] for b in plate_boxes) / w
        ys2 = max(b[3] for b in plate_boxes) / h
        rx = max(0.0, xs1 - m)
        ry = max(0.0, ys1 - m)
        rw = min(1.0 - rx, (xs2 - xs1) + 2 * m)
        rh = min(1.0 - ry, (ys2 - ys1) + 2 * m)
        print("-" * 72)
        print(f"SUGGESTED alpr.roi  (bounds {len(plate_boxes)} observed plate(s) "
              f"+ {m:.0%} margin)")
        print("-" * 72)
        print(json.dumps({"roi": {"enabled": True,
                                  "x": round(rx, 4), "y": round(ry, 4),
                                  "w": round(rw, 4), "h": round(rh, 4)}},
                         indent=2))
        print("\n  Take several samples (different cars, different times of")
        print("  day) before committing — one frame is one car's position.")
    else:
        print("-" * 72)
        print("No plate boxes seen, so no ROI suggestion. Re-run with a car "
              "at the speaker.")

    # ---- annotated image ----------------------------------------------
    try:
        import cv2
        bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        for r in reads:
            if r.vehicle_bbox:
                vx1, vy1, vx2, vy2 = r.vehicle_bbox
                cv2.rectangle(bgr, (vx1, vy1), (vx2, vy2), (255, 160, 0), 2)
            if r.text:
                x1, y1, x2, y2 = r.bbox
                cv2.rectangle(bgr, (x1, y1), (x2, y2), (0, 220, 0), 2)
                cv2.putText(bgr, r.text, (x1, max(0, y1 - 8)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 220, 0), 2)
        if cfg.alpr.roi.enabled:
            x1, y1, x2, y2 = cfg.alpr.roi.to_pixels(w, h)
            cv2.rectangle(bgr, (x1, y1), (x2, y2), (0, 0, 255), 3)
            cv2.putText(bgr, "ROI", (x1 + 6, y1 + 28),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        if cfg.dwell_zone.enabled:
            x1, y1, x2, y2 = cfg.dwell_zone.to_pixels(w, h)
            cv2.rectangle(bgr, (x1, y1), (x2, y2), (255, 0, 255), 3)
            cv2.putText(bgr, f"DWELL ({cfg.dwell_zone.anchor})",
                        (x1 + 6, y1 + 56), cv2.FONT_HERSHEY_SIMPLEX,
                        0.8, (255, 0, 255), 2)
        out = pathlib.Path(args.out)
        cv2.imwrite(str(out), bgr)
        print(f"\nannotated image written: {out.resolve()}")
        print("  green = plate   orange = vehicle   red = ROI   magenta = dwell zone")
    except Exception as e:
        print(f"\n(could not write annotated image: {e})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
