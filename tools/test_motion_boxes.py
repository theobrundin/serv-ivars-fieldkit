"""Motion-contour box tests (Problem 3, weak vehicle signal pre-detector).

What must hold:
  1. A moving plateless car (white block on black, no plate boxes) produces a
     motion-source track with a real position — not just a boolean timer.
  2. A plate box appearing INSIDE that motion track upgrades it in place:
     same track id, same queue position, source becomes 'plate'.
  3. A moving PLATED car does not double-track (its motion blob is suppressed
     because it explains an already-matched plate box).
"""
import sys
import pathlib
from dataclasses import dataclass, field

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

try:  # OpenCV is installed on the Pi; the portable audit runtime may omit it.
    import cv2  # noqa: F401, E402
except ModuleNotFoundError:
    print("SKIP  test_motion_boxes.py requires OpenCV (installed on field Pi)")
    raise SystemExit(0)

from plate_reader.config import Presence  # noqa: E402
from plate_reader.presence import PresenceTracker  # noqa: E402


@dataclass
class Z:
    enabled: bool = False
    x_min_pct: float = 0.0
    x_max_pct: float = 1.0
    y_min_pct: float = 0.0
    y_max_pct: float = 1.0


@dataclass
class FakeCfg:
    presence: Presence = None
    detection_zone: Z = field(default_factory=Z)


def make_cfg(**over):
    p = Presence(
        enabled=True,
        iou_match_threshold=0.3,
        track_timeout_seconds=3.0,
        min_track_frames=3,
        motion_area_fraction=0.12,
        color_enabled=False,
        color_split_distance=0.55,
        motion_boxes_enabled=True,
        motion_box_min_area_frac=0.005,
    )
    for k, v in over.items():
        setattr(p, k, v)
    return FakeCfg(presence=p)


@dataclass
class Cand:
    bbox: tuple
    detection_confidence: float = 0.6
    ocr_confidence: float = 0.6
    normalized: str | None = None
    in_zone: bool = True


def car_frame(cx, cy=280, w=180, h=120):
    """A white 'car body' block on black at center (cx, cy)."""
    f = np.zeros((480, 720, 3), dtype="uint8")
    x1, x2 = int(cx - w / 2), int(cx + w / 2)
    y1, y2 = int(cy - h / 2), int(cy + h / 2)
    f[max(0, y1):y2, max(0, x1):x2] = 220
    return f


results = []


def check(name, got, want):
    ok = got == want
    results.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  (got={got} want={want})")


# --- 1. Plateless moving car -> exactly one motion-source track ---
t = PresenceTracker(make_cfg())
now = 1000.0
for i in range(8):
    t.observe(car_frame(150 + i * 40), [], now=now)   # no plate boxes at all
    now += 0.2
motion_tracks = [tr for tr in t._tracks.values() if tr.source == "motion"]
check("plateless car gets a motion track", len(motion_tracks) >= 1, True)
check("motion track has a real bbox",
      motion_tracks[0].bbox is not None if motion_tracks else False, True)
check("no plate-source tracks",
      sum(1 for tr in t._tracks.values() if tr.source == "plate"), 0)
main_id = max(t._tracks.values(), key=lambda tr: tr.frames).track_id

# --- 2. Plate becomes readable inside the motion track -> upgrade in place ---
cx = 150 + 8 * 40
plate = Cand(bbox=(cx - 40, 300, cx + 40, 330), normalized="NEWREAD")
t.observe(car_frame(cx), [plate], now=now)
now += 0.2
tr = t._tracks.get(main_id)
check("upgrade kept the SAME track id", tr is not None, True)
check("upgraded to plate source", tr.source if tr else None, "plate")
check("upgrade recorded the plate string",
      "NEWREAD" in tr.seen_normalized if tr else False, True)
check("upgrade did not spawn a new track for the plate",
      sum(1 for x in t._tracks.values()
          if "NEWREAD" in x.seen_normalized), 1)

# --- 3. Moving PLATED car does not double-track ---
t = PresenceTracker(make_cfg())
now = 2000.0
for i in range(8):
    cx = 150 + i * 40
    plate = Cand(bbox=(cx - 40, 300, cx + 40, 330), normalized="PLT1234")
    t.observe(car_frame(cx), [plate], now=now)
    now += 0.2
check("plated moving car -> 1 track total", t.active_track_count(), 1)
check("and it is plate-source",
      next(iter(t._tracks.values())).source, "plate")

print("-" * 60)
fails = [n for n, ok in results if not ok]
print(f"{len(results) - len(fails)}/{len(results)} passed")
sys.exit(1 if fails else 0)
