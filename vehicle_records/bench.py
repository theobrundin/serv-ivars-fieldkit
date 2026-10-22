#!/usr/bin/env python3
"""CM5 / Pi performance benchmark for the vehicle-record pipeline.

This is the script to hand to the hardware contractor. It answers the three
questions that decide whether a board is viable, in priority order:

  1. WHAT IS THE PER-FRAME COST, and where does it go? Split into engine
     inference vs. record/tracking overhead. Inference dominates by roughly
     two orders of magnitude, so if the total is over budget the answer is
     almost always the model config, not this layer.

  2. DOES IT SUSTAIN THE TARGET RATE? A burst of 100 frames proves nothing.
     Drive-thrus run for hours, and a board that throttles turns every timing
     number optimistic. Reports p50/p95/max per frame plus achieved fps over
     the whole run, and re-reports them for the last third so decay is
     visible.

  3. DOES IT THERMALLY THROTTLE? On a Pi/CM5 this is read from vcgencmd and
     the sticky throttle bits. A clean average with a throttled tail is a
     failing result, not a passing one.

Budget context: production captures at 5 fps, so the per-frame budget is
200ms. The measured profile on a Pi 5 with plates + vehicle box + colour was
~106ms/frame; adding LPCI+VMMR+VBSR took it to ~268ms, which does NOT fit.

Usage (on the target board, SDK env set):
    export LD_LIBRARY_PATH=/path/to/ultimateALPR-SDK/binaries/linux/aarch64
    export PYTHONPATH=/path/to/ultimateALPR-SDK/python:$LD_LIBRARY_PATH
    python3 vehicle_records/bench.py \
        --video clip_5fps.mp4 \
        --config /home/serv/plate_reader/config.json \
        --duration 600 --target-fps 5
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import statistics
import subprocess
import sys
import time

_REPO = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

SOFT_LIMIT_C = 80.0
HARD_LIMIT_C = 85.0


def _sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except Exception:
        return ""


def read_temp():
    out = _sh(["vcgencmd", "measure_temp"])
    m = re.search(r"([\d.]+)", out)
    if m:
        return float(m.group(1))
    try:
        return int(pathlib.Path(
            "/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000.0
    except Exception:
        return None


def read_throttled():
    out = _sh(["vcgencmd", "get_throttled"])
    m = re.search(r"0x([0-9a-fA-F]+)", out)
    return int(m.group(1), 16) if m else None


def read_arm_mhz():
    """ARM clock in MHz. Tracked separately from the throttle bits because
    clock can dip under thermal/voltage pressure before those bits latch —
    bits alone can miss an early warning sign."""
    out = _sh(["vcgencmd", "measure_clock", "arm"])  # frequency(0)=1500000000
    m = re.search(r"=(\d+)", out)
    return round(int(m.group(1)) / 1e6) if m else None


def decode_throttled(bits: int) -> list[str]:
    flags = [
        (0x1, "under-voltage NOW"), (0x2, "ARM freq capped NOW"),
        (0x4, "throttled NOW"), (0x8, "soft temp limit NOW"),
        (0x10000, "under-voltage has occurred"),
        (0x20000, "freq capping has occurred"),
        (0x40000, "throttling has occurred"),
        (0x80000, "soft temp limit has occurred"),
    ]
    return [label for mask, label in flags if bits & mask]


def pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video", required=True,
                    help="footage to loop as representative load")
    ap.add_argument("--config", required=True)
    ap.add_argument("--vr-config", default=None)
    ap.add_argument("--duration", type=int, default=600,
                    help="seconds of sustained load (default 600; thermals "
                         "need minutes, not seconds)")
    ap.add_argument("--target-fps", type=float, default=5.0,
                    help="production capture rate; sets the per-frame budget")
    ap.add_argument("--pace", action="store_true",
                    help="sleep the remainder of each frame budget, matching "
                         "field pacing. Off = run flat out to find the "
                         "ceiling.")
    ap.add_argument("--out", default="vr_bench.json")
    args = ap.parse_args()

    import cv2
    from vehicle_records.config import VehicleRecordConfig
    from vehicle_records.engine import RichUltimateAlprEngine
    from vehicle_records.pipeline import VehicleRecordPipeline
    from vehicle_records.run import _load_prod_cfg as load_prod

    prod_cfg = load_prod(args.config)
    vr_cfg = (VehicleRecordConfig.load(args.vr_config) if args.vr_config
              else VehicleRecordConfig())

    budget_ms = 1000.0 / args.target_fps
    print(f"target {args.target_fps} fps -> {budget_ms:.0f}ms per-frame budget")
    cls = prod_cfg.alpr.classifiers
    print(f"classifiers: vehicle_detect={cls.vehicle_detect_enabled} "
          f"color={cls.color_enabled} lpci={cls.lpci_enabled} "
          f"vmmr={cls.vmmr_enabled} vbsr={cls.vbsr_enabled}")
    print(f"pacing: {'ON (field-realistic)' if args.pace else 'OFF (flat out)'}")
    print("thermals need sustained load — do not stop this early.\n")

    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        print(f"ERROR: cannot open {args.video}", file=sys.stderr)
        return 2
    fw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    fh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    engine = RichUltimateAlprEngine(prod_cfg)
    engine.set_frame_size(fw, fh)
    pipe = VehicleRecordPipeline(prod_cfg, vr_cfg, engine=engine)
    pipe.open()

    frame_ms: list[float] = []
    samples: list[dict] = []
    temp0 = read_temp()
    thr0 = read_throttled()
    print(f"start: temp={temp0}C throttled=0x{(thr0 or 0):x}")

    t_end = time.time() + args.duration
    t_start = time.time()
    n_frames = 0
    n_records = 0
    last_sample = 0.0

    try:
        while time.time() < t_end:
            ok, bgr = cap.read()
            if not ok:                       # loop the clip
                cap.release()
                cap = cv2.VideoCapture(args.video)
                continue
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

            f0 = time.perf_counter()
            for _ in pipe.process_frame(rgb, now=n_frames / args.target_fps):
                n_records += 1
            dt_ms = (time.perf_counter() - f0) * 1000.0
            frame_ms.append(dt_ms)
            n_frames += 1

            el = time.time() - t_start
            if el - last_sample >= 2.0:
                last_sample = el
                samples.append({
                    "t_s": round(el, 1),
                    "temp_c": read_temp(),
                    "throttled": read_throttled(),
                    "arm_mhz": read_arm_mhz(),
                    "frame_ms": round(dt_ms, 1),
                })

            if args.pace:
                # Elapsed-aware: sleep only the REMAINDER of the budget, never
                # the full budget on top of processing. The additive form
                # silently caps throughput below target.
                sleep_s = max(0.0, (budget_ms - dt_ms) / 1000.0)
                if sleep_s > 0:
                    time.sleep(sleep_s)
    except KeyboardInterrupt:
        print("\ninterrupted — reporting on what was collected")
    finally:
        cap.release()
        pipe.close()

    elapsed = time.time() - t_start
    temps = [s["temp_c"] for s in samples if isinstance(s["temp_c"], float)]
    arm_mhz_vals = [s["arm_mhz"] for s in samples if isinstance(s["arm_mhz"], int)]
    thr_final = read_throttled()
    arm_final = read_arm_mhz()
    third = max(1, len(frame_ms) // 3)

    report = {
        "board": _sh("cat /proc/device-tree/model 2>/dev/null | tr -d '\\0'")
                 or "unknown",
        "frames": n_frames,
        "elapsed_s": round(elapsed, 1),
        "achieved_fps": round(n_frames / elapsed, 2) if elapsed else 0,
        "target_fps": args.target_fps,
        "budget_ms": round(budget_ms, 1),
        "frame_ms": {
            "mean": round(statistics.mean(frame_ms), 1) if frame_ms else 0,
            "p50": round(pct(frame_ms, 50), 1),
            "p95": round(pct(frame_ms, 95), 1),
            "max": round(max(frame_ms), 1) if frame_ms else 0,
            "last_third_mean": (round(statistics.mean(frame_ms[-third:]), 1)
                                if frame_ms else 0),
        },
        "stage_timing": pipe.summary()["timing"],
        "records_emitted": n_records,
        "thermal": {
            "start_c": temp0,
            "min_c": min(temps) if temps else None,
            "mean_c": round(statistics.mean(temps), 1) if temps else None,
            "max_c": max(temps) if temps else None,
            "throttled_final_hex": f"0x{(thr_final or 0):x}",
            "throttle_flags": decode_throttled(thr_final or 0),
            "arm_mhz_start": arm_mhz_vals[0] if arm_mhz_vals else None,
            "arm_mhz_min": min(arm_mhz_vals) if arm_mhz_vals else None,
            "arm_mhz_final": arm_final,
        },
        "samples": samples,
    }

    pathlib.Path(args.out).write_text(json.dumps(report, indent=2))

    print("\n" + "=" * 68)
    print(f"CM5/Pi BENCHMARK — {report['board']}")
    print("=" * 68)
    fm = report["frame_ms"]
    print(f"\n1. PER-FRAME COST (budget {budget_ms:.0f}ms at "
          f"{args.target_fps} fps)")
    print(f"   mean {fm['mean']}ms  p50 {fm['p50']}ms  p95 {fm['p95']}ms  "
          f"max {fm['max']}ms")
    st = report["stage_timing"]
    print(f"   engine {st['engine_ms_per_frame']}ms/frame  |  "
          f"record+track {st['track_ms_per_frame']}ms/frame")
    if fm["p95"] > budget_ms:
        print(f"   *** p95 EXCEEDS the {budget_ms:.0f}ms budget — this board "
              f"cannot hold {args.target_fps} fps on this config ***")
    else:
        print(f"   fits the budget at p95 "
              f"({budget_ms - fm['p95']:.0f}ms headroom)")
    print(f"   max sustainable: {st['max_sustainable_fps']} fps")

    print(f"\n2. SUSTAINED THROUGHPUT")
    print(f"   achieved {report['achieved_fps']} fps over "
          f"{report['elapsed_s']}s ({n_frames} frames)")
    print(f"   frame cost first->last third: {fm['mean']}ms -> "
          f"{fm['last_third_mean']}ms")
    if fm["last_third_mean"] > fm["mean"] * 1.15:
        print("   *** cost grew >15% over the run — correlate with thermals ***")

    th = report["thermal"]
    print(f"\n3. THERMALS")
    if th["max_c"]:
        print(f"   temp min {th['min_c']}C  mean {th['mean_c']}C  "
              f"max {th['max_c']}C")
        if th["max_c"] >= HARD_LIMIT_C:
            print(f"   *** HARD LIMIT {HARD_LIMIT_C}C reached — clocks cut ***")
        elif th["max_c"] >= SOFT_LIMIT_C:
            print(f"   *** SOFT LIMIT {SOFT_LIMIT_C}C reached ***")
        else:
            print(f"   stayed below the {SOFT_LIMIT_C}C soft limit")
    else:
        print("   (no thermal sensor readable on this host)")
    print(f"   get_throttled {th['throttled_final_hex']}")
    for f in th["throttle_flags"] or ["clean: no throttling since boot"]:
        print(f"     - {f}")
    if th.get("arm_mhz_min") is not None:
        print(f"   ARM clock  start {th['arm_mhz_start']} MHz  "
              f"min {th['arm_mhz_min']} MHz  final {th['arm_mhz_final']} MHz")
        if th["arm_mhz_min"] < 0.9 * (th["arm_mhz_start"] or th["arm_mhz_min"]):
            print("     *** clock dropped >10% below its starting value — "
                  "possible early throttle not yet reflected in the sticky "
                  "bits ***")

    print(f"\nfull report -> {args.out}")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
