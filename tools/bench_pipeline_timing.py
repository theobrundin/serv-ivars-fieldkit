"""Production-shaped per-frame timing on the Pi.

Measures the components production actually runs — Detector.process_frame
(engine + vote + dwell) and PresenceTracker.observe — over a DENSE segment of
the footage. Deliberately excludes the harness's assoc diagnostics, per-track
recorder and console printing, none of which ship.
"""
import statistics
import sys
import time
from pathlib import Path

import cv2

from plate_reader.config import load
from plate_reader.detector import Detector
from plate_reader.presence import PresenceTracker

VIDEO = sys.argv[1] if len(sys.argv) > 1 else "/home/serv/test_footage.mp4"
START = int(sys.argv[2]) if len(sys.argv) > 2 else 2800
N = int(sys.argv[3]) if len(sys.argv) > 3 else 500

cfg = load(Path("config.json"))
d = Detector(cfg)
d.open()
tracker = PresenceTracker(cfg)
cap = cv2.VideoCapture(VIDEO)

for _ in range(START):
    cap.read()

decode_ms, det_ms, trk_ms, tot_ms = [], [], [], []
for i in range(N):
    t0 = time.monotonic()
    ok, bgr = cap.read()
    if not ok:
        break
    frame = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    t1 = time.monotonic()
    cands, _ = d.process_frame(frame)
    t2 = time.monotonic()
    tracker.observe(frame, cands, now=(START + i) / 5.0)
    t3 = time.monotonic()
    decode_ms.append((t1 - t0) * 1000)
    det_ms.append((t2 - t1) * 1000)
    trk_ms.append((t3 - t2) * 1000)
    tot_ms.append((t3 - t0) * 1000)

cap.release()
d.close()


def show(name, xs):
    xs = sorted(xs)
    print("  {:<30} mean={:7.1f}  p50={:7.1f}  p95={:7.1f}  max={:7.1f}".format(
        name, statistics.mean(xs), xs[len(xs) // 2],
        xs[int(len(xs) * 0.95)], xs[-1]))


print("")
print("PRODUCTION-SHAPED path, frames {}-{} (dense queue), ms:".format(
    START, START + len(tot_ms)))
show("decode + cvtColor", decode_ms)
show("detector (engine+vote+dwell)", det_ms)
show("presence tracker", trk_ms)
show("TOTAL per frame", tot_ms)

t = sorted(tot_ms)
p50, p95 = t[len(t) // 2], t[int(len(t) * 0.95)]
mean = statistics.mean(tot_ms)
print("")
print("  budget @5fps = 200 ms/frame")
print("    p50 {:.1f} ms -> {}".format(p50, "FITS" if p50 < 200 else "EXCEEDS"))
print("    p95 {:.1f} ms -> {}".format(p95, "FITS" if p95 < 200 else "EXCEEDS"))
print("    sustained {:.2f} fps".format(1000.0 / mean))
