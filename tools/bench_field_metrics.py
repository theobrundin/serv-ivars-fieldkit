#!/usr/bin/env python3
"""Sustained-load field diagnostics for ultimateALPR on a Pi 5.

Answers three questions, in priority order. Everything else in the reference
guide is diagnostic detail you only need once one of these comes back bad.

  1. DOES IT THROTTLE?  Our 85.9 ms p95 was a 500-frame burst. A drive-thru
     runs for hours. The Pi 5 soft-limits near 80 C and hard-throttles near
     85 C, and frequency collapse is the most common real-world killer. If the
     unit throttles, every timing number we have is optimistic.

  2. DOES FPS DECAY?  Sampled over time and correlated with temperature, so a
     drop can be attributed to thermal limiting rather than guessed at.

  3. COMPUTE-BOUND OR MEMORY-BOUND?  Decides where effort goes next. Low IPC
     with a high L3 refill rate means faster cores won't help and the answer is
     a higher-bandwidth SoC or an NPU. High IPC with low miss rates means the
     opposite. (Needs --perf and a kernel exposing the A76 PMU.)

Usage
-----
  # 10-minute sustained run against footage, with PMU counters
  python3 tools/bench_field_metrics.py --video ~/test_footage.mp4 \
      --duration 600 --perf

  # just watch the system while something else drives the load
  python3 tools/bench_field_metrics.py --duration 300 --no-pipeline

Writes a per-sample CSV and prints a verdict. Run under representative load;
idle numbers prove nothing.
"""
import argparse
import csv
import json
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# Pi 5 thermal limits (degrees C).
SOFT_LIMIT = 80.0
HARD_LIMIT = 85.0
NOMINAL_ARM_HZ = 2_400_000_000

# get_throttled bitfield. Bits 0-3 are live, 16-19 are sticky since boot.
THROTTLE_BITS = [
    (0,  0x1,     "under-voltage NOW"),
    (1,  0x2,     "ARM frequency capped NOW"),
    (2,  0x4,     "throttled NOW"),
    (3,  0x8,     "soft temperature limit NOW"),
    (16, 0x10000, "under-voltage has occurred"),
    (17, 0x20000, "frequency capping has occurred"),
    (18, 0x40000, "throttling has occurred"),
    (19, 0x80000, "soft temp limit has occurred"),
]


def sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except Exception:
        return ""


def read_temp() -> float | None:
    out = sh(["vcgencmd", "measure_temp"])           # temp=54.0'C
    m = re.search(r"([\d.]+)", out)
    if m:
        return float(m.group(1))
    # Fallback for boards without vcgencmd on PATH.
    try:
        return int(pathlib.Path(
            "/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000.0
    except Exception:
        return None


def read_throttled() -> int | None:
    out = sh(["vcgencmd", "get_throttled"])           # throttled=0x0
    m = re.search(r"0x([0-9a-fA-F]+)", out)
    return int(m.group(1), 16) if m else None


def read_arm_hz() -> int | None:
    out = sh(["vcgencmd", "measure_clock", "arm"])  # frequency(0)=2400000000
    m = re.search(r"=(\d+)", out)
    return int(m.group(1)) if m else None


def read_mem() -> tuple[float, float]:
    """(used_mb, swap_used_mb). Any sustained swap is catastrophic for fps."""
    used = swap = 0.0
    try:
        info = {}
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            info[k] = float(v.strip().split()[0]) / 1024.0
        used = info.get("MemTotal", 0) - info.get("MemAvailable", 0)
        swap = info.get("SwapTotal", 0) - info.get("SwapFree", 0)
    except Exception:
        pass
    return used, swap


_prev_cpu = {}


def read_cpu_pct() -> float | None:
    """Whole-machine busy %, from successive /proc/stat reads."""
    global _prev_cpu
    try:
        parts = pathlib.Path("/proc/stat").read_text().split("\n")[0].split()[1:]
        vals = [int(x) for x in parts]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        if _prev_cpu:
            dt, di = total - _prev_cpu["t"], idle - _prev_cpu["i"]
            _prev_cpu = {"t": total, "i": idle}
            return round(100.0 * (dt - di) / dt, 1) if dt > 0 else None
        _prev_cpu = {"t": total, "i": idle}
    except Exception:
        pass
    return None


def decode_throttled(bits: int) -> list[str]:
    return [label for _, mask, label in THROTTLE_BITS if bits & mask]


# ---------------------------------------------------------------- sampler ---
class Sampler(threading.Thread):
    def __init__(self, path, interval=2.0):
        super().__init__(daemon=True)
        self.path, self.interval = path, interval
        self.rows: list[dict] = []
        self._halt = threading.Event()
        self.frames_done = 0          # updated by the pipeline thread

    def run(self):
        read_cpu_pct()                # prime the delta
        t0 = time.monotonic()
        last_frames, last_t = 0, t0
        while not self._halt.is_set():
            now = time.monotonic()
            temp, thr, hz = read_temp(), read_throttled(), read_arm_hz()
            used, swap = read_mem()
            frames = self.frames_done
            dt = now - last_t
            fps = (frames - last_frames) / dt if dt > 0 else 0.0
            last_frames, last_t = frames, now
            self.rows.append({
                "t_s": round(now - t0, 1),
                "temp_c": temp,
                "throttled_hex": f"0x{thr:x}" if thr is not None else "",
                "throttled_now": int(bool(thr & 0xF)) if thr is not None else "",
                "arm_mhz": round(hz / 1e6) if hz else "",
                "cpu_pct": read_cpu_pct(),
                "mem_used_mb": round(used),
                "swap_mb": round(swap),
                "fps": round(fps, 2),
            })
            self._halt.wait(self.interval)

    def stop(self):
        # NB: the attribute is _halt, not _stop — threading.Thread
        # already defines a _stop() method and shadowing it breaks join().
        self._halt.set()

    def write_csv(self):
        if not self.rows:
            return
        with open(self.path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(self.rows[0].keys()))
            w.writeheader()
            w.writerows(self.rows)


# --------------------------------------------------------------- pipeline ---
def run_pipeline(video, duration, sampler, cfg_path):
    """Drive the REAL detector + presence tracker over footage on a loop."""
    import cv2
    from plate_reader.config import load as load_cfg
    from plate_reader.detector import Detector
    from plate_reader.presence import PresenceTracker

    cfg = load_cfg(pathlib.Path(cfg_path))
    det = Detector(cfg)
    det.open()
    tracker = PresenceTracker(cfg)

    # Mirror main.py's real production pacing exactly, elapsed-aware: sleep
    # only the remainder of the target interval, not the full interval on top
    # of however long capture+processing already took (that additive-sleep
    # bug is what main.py had until today - see plate_reader/main.py). Without
    # this the benchmark measures an uncapped loop regardless of what
    # capture_interval_ms says, which is a different, more aggressive load
    # than what actually runs in the field.
    cap_interval_s = cfg.behavior.capture_interval_ms / 1000.0
    print(f"pacing: capture_interval_ms={cfg.behavior.capture_interval_ms} "
          f"({'uncapped' if cap_interval_s <= 0 else f'target {1.0/cap_interval_s:.2f} fps'})")

    t_end = time.monotonic() + duration
    idx = 0
    try:
        while time.monotonic() < t_end:
            cap = cv2.VideoCapture(video)
            if not cap.isOpened():
                print(f"ERROR: cannot open {video}", file=sys.stderr)
                return
            while time.monotonic() < t_end:
                loop_start_t = time.monotonic()
                ok, bgr = cap.read()
                if not ok:
                    break                       # loop the clip
                frame = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                if idx == 0 and hasattr(det._engine, "set_frame_size"):
                    h, w = frame.shape[:2]
                    det._engine.set_frame_size(w, h)
                cands, _ = det.process_frame(frame)
                tracker.observe(frame, cands, now=idx / 5.0)
                idx += 1
                sampler.frames_done = idx

                elapsed_s = time.monotonic() - loop_start_t
                sleep_s = max(0.0, cap_interval_s - elapsed_s)
                if sleep_s > 0:
                    time.sleep(sleep_s)
            cap.release()
    finally:
        det.close()


# ------------------------------------------------------------------ perf ----
PERF_EVENTS = (
    "cycles,instructions,"
    "l2d_cache,l2d_cache_refill,"
    "l3d_cache,l3d_cache_refill,"
    "stall_backend"
)


def start_perf(duration):
    """A focused counter set. The A76 PMU has ~6-7 programmable counters, so
    asking for more than that multiplexes and degrades accuracy."""
    if shutil.which("perf") is None:
        print("  perf not installed — skipping PMU counters "
              "(sudo apt install linux-perf)")
        return None
    cmd = [
        "perf", "stat", "-a", "-e", PERF_EVENTS, "-x,", "-o",
        "/tmp/perf_alpr.csv", "--", "sleep", str(int(duration)),
    ]
    return subprocess.Popen(cmd,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def parse_perf():
    p = pathlib.Path("/tmp/perf_alpr.csv")
    if not p.is_file():
        return {}
    vals = {}
    for line in p.read_text().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split(",")
        if len(parts) >= 3:
            try:
                vals[parts[2].strip()] = float(parts[0])
            except ValueError:
                pass
    return vals


# ---------------------------------------------------------------- verdict ---
def report(sampler, perf_vals, args):
    rows = sampler.rows
    if not rows:
        print("no samples collected")
        return 1

    temps = [r["temp_c"] for r in rows if isinstance(r["temp_c"], float)]
    mhz = [r["arm_mhz"] for r in rows if isinstance(r["arm_mhz"], int)]
    fps = [r["fps"] for r in rows if isinstance(r["fps"], float) and r["fps"] > 0]
    swap = [r["swap_mb"] for r in rows if isinstance(r["swap_mb"], int)]

    print("\n" + "=" * 72)
    print(f"FIELD METRICS — {len(rows)} samples over {rows[-1]['t_s']:.0f}s")
    print("=" * 72)

    # --- 1. thermals, the headline ---------------------------------------
    print("\n1. THERMALS & THROTTLING")
    if temps:
        print(f"   temperature   min {min(temps):.1f}C  "
              f"mean {sum(temps)/len(temps):.1f}C  max {max(temps):.1f}C")
        if max(temps) >= HARD_LIMIT:
            print(f"   *** HARD LIMIT reached ({HARD_LIMIT}C) — clocks cut ***")
        elif max(temps) >= SOFT_LIMIT:
            print(f"   *** SOFT LIMIT reached ({SOFT_LIMIT}C) — throttling ***")
        else:
            print(f"   stayed below the {SOFT_LIMIT}C soft limit")
    final = next((r["throttled_hex"] for r in reversed(rows)
                  if r["throttled_hex"]), "")
    if final:
        bits = int(final, 16)
        flags = decode_throttled(bits)
        print(f"   get_throttled {final}")
        if flags:
            for f in flags:
                print(f"     - {f}")
        else:
            print("     - clean: no throttling or under-voltage since boot")
    if mhz:
        print(f"   ARM clock     min {min(mhz)} MHz  max {max(mhz)} MHz "
              f"(nominal {NOMINAL_ARM_HZ // 10**6})")
        if min(mhz) < 0.9 * NOMINAL_ARM_HZ / 1e6:
            print("     *** clock dropped >10% below nominal ***")

    # --- 2. fps decay -----------------------------------------------------
    print("\n2. SUSTAINED THROUGHPUT")
    if fps:
        third = max(1, len(fps) // 3)
        first_t, last_t = fps[:third], fps[-third:]
        a, b = sum(first_t) / len(first_t), sum(last_t) / len(last_t)
        print(f"   fps           mean {sum(fps)/len(fps):.2f}  "
              f"min {min(fps):.2f}  max {max(fps):.2f}")
        print(f"   first third {a:.2f}  ->  last third {b:.2f}"
              f"   ({(b - a) / a * 100:+.1f}%)")
        if a > 0 and b < 0.85 * a:
            print("   *** throughput decayed >15% — correlate with temp above ***")
        if b < 5.0:
            print("   *** below the 5 fps capture target ***")
    else:
        print("   (no pipeline run — use --video to measure fps)")
    if swap and max(swap) > 1:
        print(f"   *** SWAP IN USE ({max(swap)} MB) — catastrophic for fps ***")

    # --- 3. compute vs memory bound --------------------------------------
    print("\n3. COMPUTE-BOUND OR MEMORY-BOUND?")
    if not perf_vals:
        print("   (no PMU data — re-run with --perf)")
    else:
        cyc = perf_vals.get("cycles")
        ins = perf_vals.get("instructions")
        l2, l2r = perf_vals.get("l2d_cache"), perf_vals.get("l2d_cache_refill")
        l3, l3r = perf_vals.get("l3d_cache"), perf_vals.get("l3d_cache_refill")
        ipc = ins / cyc if cyc and ins else None
        if ipc:
            print(f"   IPC           {ipc:.2f}")
        if l2 and l2r:
            print(f"   L2 miss rate  {100 * l2r / l2:.1f}%")
        if l3 and l3r:
            print(f"   L3 miss rate  {100 * l3r / l3:.1f}%")
            secs = rows[-1]["t_s"] or 1
            print(f"   est. DRAM     {l3r * 64 / secs / 1e9:.2f} GB/s "
                  f"(STREAM on a Pi 5 is ~11-12 GB/s)")
        if ipc is not None:
            if ipc < 1.3:
                print("\n   -> MEMORY-BOUND. Faster cores give diminishing")
                print("      returns; prefer more memory bandwidth, a larger")
                print("      last-level cache, or an NPU that keeps weights")
                print("      on-chip.")
            elif ipc > 1.8:
                print("\n   -> COMPUTE-BOUND. Higher clocks and better")
                print("      single-thread performance help most.")
            else:
                print("\n   -> MIXED, which is normal for ALPR: image buffers")
                print("      plus model weights overflow the 2 MB L3.")

    print(f"\nper-sample CSV: {sampler.path}")
    print("=" * 72)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video", help="footage to loop as representative load")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--duration", type=int, default=600,
                    help="seconds of sustained load (default 600)")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--perf", action="store_true", help="collect PMU counters")
    ap.add_argument("--no-pipeline", action="store_true",
                    help="only sample the system; drive load elsewhere")
    ap.add_argument("--out", default="field_metrics.csv")
    args = ap.parse_args()

    if not args.no_pipeline and not args.video:
        ap.error("need --video, or pass --no-pipeline to only sample")

    print(f"sampling every {args.interval}s for {args.duration}s"
          f"{' with PMU counters' if args.perf else ''}")
    if not args.no_pipeline:
        print(f"driving load from {args.video} (looped)")
    print("thermals need sustained load — do not stop this early.\n")

    sampler = Sampler(args.out, args.interval)
    sampler.start()
    perf_proc = start_perf(args.duration) if args.perf else None

    try:
        if args.no_pipeline:
            time.sleep(args.duration)
        else:
            run_pipeline(args.video, args.duration, sampler, args.config)
    except KeyboardInterrupt:
        print("\ninterrupted — reporting on what was collected")
    finally:
        sampler.stop()
        sampler.join(timeout=5)
        sampler.write_csv()
        if perf_proc:
            try:
                perf_proc.wait(timeout=15)
            except Exception:
                perf_proc.kill()

    return report(sampler, parse_perf() if args.perf else {}, args)


if __name__ == "__main__":
    raise SystemExit(main())
