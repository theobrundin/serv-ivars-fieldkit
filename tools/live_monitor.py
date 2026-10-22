#!/usr/bin/env python3
"""Live detection monitor — one compact line per event.

On site the question is never "did it detect something", it's "why didn't that
confirm?". Every line therefore ends with the GATE that rejected the read, so
the answer is visible without attaching a debugger.

    HH:MM:SS  f00123  PLATE  ABC1234  det=0.94 ocr=0.88  veh=(120,80)-(640,700)
              zone=IN   -> OK
    HH:MM:SS  f00124  PLATE  P311     det=0.41 ocr=0.38  veh=-
              zone=IN   -> rejected: app_ocr_min

Usage
-----
  python3 tools/live_monitor.py                       # live camera
  python3 tools/live_monitor.py --video clip.mp4      # replay a file
  python3 tools/live_monitor.py --video clip.mp4 --only-rejected
"""
import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--video", help="replay a file instead of the camera")
    ap.add_argument("--fps", type=float, default=5.0)
    ap.add_argument("--only-rejected", action="store_true",
                    help="show only reads that failed a gate")
    ap.add_argument("--only-plates", action="store_true",
                    help="hide vehicle-only detections")
    args = ap.parse_args()

    from plate_reader.config import load as load_cfg
    from plate_reader.detector import Detector, ConfirmedIdentification

    cfg = load_cfg(pathlib.Path(args.config))
    det = Detector(cfg)
    det.open()

    src, cam, cap = None, None, None
    if args.video:
        import cv2
        cap = cv2.VideoCapture(args.video)
        if not cap.isOpened():
            raise SystemExit(f"ERROR: cannot open {args.video}")
        src = args.video
    else:
        from plate_reader.camera import Camera
        cam = Camera(cfg.camera)
        cam.open()
        src = "live camera"

    print("=" * 78)
    print(f"LIVE MONITOR  |  {src}")
    print(f"engine={cfg.alpr.engine}  roi={'ON' if cfg.alpr.roi.enabled else 'off'}"
          f"  dwell_zone={'ON' if cfg.dwell_zone.enabled else 'off'}"
          f"  cooldown={cfg.dedup.cooldown_seconds:g}s")
    print(f"gates: app_detect_min={cfg.thresholds.min_detection_confidence} "
          f"app_ocr_min={cfg.thresholds.min_ocr_confidence}")
    print("=" * 78)

    frame_idx = 0
    interval = 1.0 / args.fps if args.fps > 0 else 0.0
    try:
        while True:
            t0 = time.monotonic()
            if cap is not None:
                import cv2
                ok, bgr = cap.read()
                if not ok:
                    break
                frame = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            else:
                frame = cam.capture()
            frame_idx += 1

            if frame_idx == 1 and hasattr(det._engine, "set_frame_size"):
                h, w = frame.shape[:2]
                det._engine.set_frame_size(w, h)

            candidates, outcomes = det.process_frame(frame)
            ts = time.strftime("%H:%M:%S")

            for c in candidates:
                if args.only_plates and not c.raw_text:
                    continue
                if args.only_rejected and c.rejected_by is None:
                    continue
                kind = "PLATE  " if c.raw_text else "VEHICLE"
                veh = "-"
                if c.vehicle_bbox:
                    vx1, vy1, vx2, vy2 = c.vehicle_bbox
                    veh = f"({vx1},{vy1})-({vx2},{vy2})"
                zone = "IN " if c.in_dwell_zone else "OUT"
                verdict = ("OK" if c.rejected_by is None
                           else f"rejected: {c.rejected_by}")
                print(f"{ts}  f{frame_idx:05d}  {kind} "
                      f"{(c.raw_text or '-'):<10} "
                      f"det={c.detection_confidence:.2f} "
                      f"ocr={c.ocr_confidence:.2f}  "
                      f"veh={veh}  zone={zone}  -> {verdict}")

            for o in outcomes:
                if isinstance(o, ConfirmedIdentification):
                    if o.suppressed:
                        print(f"{ts}  f{frame_idx:05d}  ~SUPPRESSED  "
                              f"repeat #{o.repeat_index}  ({o.suppress_reason})")
                    else:
                        # Plate text is PII-adjacent; show it only when the
                        # operator has explicitly asked for rejected-read
                        # detail (i.e. they are calibrating).
                        shown = (o.normalized if cfg.diagnostics.log_rejected_reads
                                 else "<hidden>")
                        print(f"{ts}  f{frame_idx:05d}  *** CONFIRMED {shown} "
                              f"det={o.detection_confidence:.2f} "
                              f"ocr={o.ocr_confidence:.2f} "
                              f"frames={o.frame_count} ***")

            for v in det.collect_vehicle_only():
                print(f"{ts}  f{frame_idx:05d}  --- VEHICLE-ONLY (no plate) "
                      f"frames={v.frame_count} in_zone={v.seconds_in_zone}s ---")

            if interval:
                dt = interval - (time.monotonic() - t0)
                if dt > 0:
                    time.sleep(dt)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        det.close()
        if cap is not None:
            cap.release()
        if cam is not None:
            cam.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
