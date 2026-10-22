"""Verify the track instrumentation records what actually happened.

Drives PresenceTracker with synthetic box sequences where the correct answer
is known, then checks the recorder's numbers match reality.
"""
import sys
import pathlib
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from plate_reader.presence import PresenceTracker  # noqa: E402
from plate_reader.track_report import TrackRecorder, TrackSummary  # noqa: E402
from plate_reader.config import Presence  # noqa: E402


def P(**over):
    p = Presence(
        enabled=True,
        iou_match_threshold=0.3,
        track_timeout_seconds=3.0,
        min_track_frames=3,
        motion_area_fraction=0.12,
        color_enabled=False,
        color_split_distance=0.55,
        motion_boxes_enabled=False,
    )
    for k, v in over.items():
        setattr(p, k, v)
    return p


@dataclass
class Zone:
    enabled: bool = False
    x_min_pct: float = 0.0
    x_max_pct: float = 1.0
    y_min_pct: float = 0.0
    y_max_pct: float = 1.0


@dataclass
class C:
    presence: object
    detection_zone: Zone = None

    def __post_init__(self):
        if self.detection_zone is None:
            self.detection_zone = Zone()


FRAME = np.zeros((480, 1200, 3), dtype="uint8")


@dataclass
class Cand:
    bbox: tuple
    detection_confidence: float = 0.8
    ocr_confidence: float = 0.9
    normalized: str = None
    in_zone: bool = True


results = []


def check(name, got, want):
    ok = got == want
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}  (got={got} want={want})")


# ---------- Scenario A: one car, boxes overlap heavily -> ONE track ----------
print("=== A: single car, high overlap frame-to-frame ===")
t = PresenceTracker(C(presence=P()))
rec = TrackRecorder()
t.attach_recorder(rec)

x = 500
for i in range(6):
    rec.set_frame(i)
    t.observe(FRAME, [Cand(bbox=(x, 300, x + 100, 350))], now=1000.0 + i * 0.2)
    x += 5   # small movement -> high IoU

matched = sum(1 for a in rec.attempts if a.chosen_track is not None)
new = sum(1 for a in rec.attempts if a.chosen_track is None)
print(f"  attempts={len(rec.attempts)} matched={matched} new_tracks={new}")
check("A: exactly 1 new track created", new, 1)
check("A: remaining boxes all matched", matched, 5)

# ---------- Scenario B: teleporting boxes -> fragmentation ----------
print()
print("=== B: boxes jump far each frame (simulates fast motion / low fps) ===")
t2 = PresenceTracker(C(presence=P()))
rec2 = TrackRecorder()
t2.attach_recorder(rec2)

x = 100
for i in range(6):
    rec2.set_frame(i)
    t2.observe(FRAME, [Cand(bbox=(x, 300, x + 100, 350))], now=2000.0 + i * 0.2)
    x += 300   # huge jump -> zero IoU

matched2 = sum(1 for a in rec2.attempts if a.chosen_track is not None)
new2 = sum(1 for a in rec2.attempts if a.chosen_track is None)
print(f"  attempts={len(rec2.attempts)} matched={matched2} new_tracks={new2}")
check("B: every box started its own track (fragmentation)", new2, 6)
check("B: nothing matched", matched2, 0)

# ---------- Scenario C: near-miss detection (motion-model cost gate) ----------
print()
print("=== C: boxes fail the cost gate but come CLOSE (near-miss) ===")
# NOTE: the old IoU near-miss case (40px shift, IoU 0.429 vs 0.5) now
# correctly MATCHES under the motion model — that improvement is exactly
# Problem 3's fix. A near-miss for the cost gate is a bigger jump: 250px on a
# 100px box with no velocity history -> d_norm 2.5 (inside the 3.0 hard gate),
# cost = 0.5*2/2 + 0.3*1 = 0.8, within 1.3x of the 0.7 threshold.
t3 = PresenceTracker(C(presence=P()))
rec3 = TrackRecorder()
t3.attach_recorder(rec3)

x = 100
for i in range(4):
    rec3.set_frame(i)
    t3.observe(FRAME, [Cand(bbox=(x, 300, x + 100, 350))], now=3000.0 + i * 0.2)
    x += 250   # too far to match, close enough to be a near-miss

nm = rec3.near_misses()
print(f"  attempts={len(rec3.attempts)} near_misses={len(nm)}")
for a in rec3.attempts:
    print(f"    f{a.frame} chosen={a.chosen_track} cost={a.cost} "
          f"cost_thresh={a.cost_threshold} reason={a.reason}")
check("C: near-misses were detected", len(nm) > 0, True)

# ---------- Scenario D: report renders without crashing ----------
print()
print("=== D: report generation ===")
summaries = [
    TrackSummary(track_id=1, born_frame=0, last_frame=5, frames=6,
                 outcome="drive_off", had_plate=True, top_det=0.8, top_ocr=0.9,
                 duration_s=1.2, color="cyan", split_from_color=False,
                 min_plate_px=40, max_plate_px=100, assoc_attempts=5,
                 assoc_near_misses=1),
]
out = rec2.report(summaries)
check("D: report is non-empty", len(out) > 100, True)
check("D: report flags fragmentation", "fragmenting" in out, True)
print()
print(out)

print()
if all(results):
    print(f"All {len(results)} instrumentation tests passed.")
else:
    print(f"{results.count(False)} FAILED")
    sys.exit(1)
