"""Motion-model association tests (Problem 3, identity mechanism).

The headline case reproduces the REAL measured fragmentation from footage:
one car approaching over 15 frames (3s at 5fps), box moving ~78px/frame with
width growing 79px -> 151px. Under IoU-only association this fragmented into
~14 one-frame tracks. Under the motion model it must form ONE track.

Also proves: two nose-to-tail cars stay two tracks with no identity swap, and
a genuinely distant new box does NOT get absorbed into an existing track.
"""
import sys
import pathlib
from dataclasses import dataclass, field

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

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
        motion_boxes_enabled=False,   # pure plate-box tests
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


FRAME = np.zeros((480, 720, 3), dtype="uint8")

results = []


def check(name, got, want):
    ok = got == want
    results.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  (got={got} want={want})")


# --- 1. The real fragmentation case: ONE approaching car -> ONE track ---
t = PresenceTracker(make_cfg())
now = 1000.0
x, y, w = 100.0, 300.0, 79.0
for i in range(15):
    bbox = (x, y, x + w, y + w * 0.35)
    t.observe(FRAME, [Cand(bbox=bbox, normalized="CAR1")], now=now)
    now += 0.2
    x += 78.0          # measured: ~1177px over 15 frames
    y -= 16.0          # measured: ~241px up over 15 frames
    w += 4.8           # measured: 79 -> 151px width
check("approaching car forms exactly 1 track", t.active_track_count(), 1)

# --- 2. IoU-only sanity contrast: consecutive boxes barely overlap ---
from plate_reader.presence import _iou  # noqa: E402
b1 = (100, 300, 179, 328)
b2 = (178, 284, 262, 313)
check("consecutive boxes are IoU-hostile (<0.3)", _iou(b1, b2) < 0.3, True)

# --- 3. Two nose-to-tail cars: 2 tracks, no swap ---
t = PresenceTracker(make_cfg())
now = 2000.0
ax, bx = 100.0, 340.0    # ~240px apart, both 80 wide
ids_a, ids_b = set(), set()
for i in range(10):
    ca = Cand(bbox=(ax, 300, ax + 80, 328), normalized="AAA111")
    cb = Cand(bbox=(bx, 300, bx + 80, 328), normalized="BBB222")
    t.observe(FRAME, [ca, cb], now=now)
    # Which track saw which plate? (identity audit)
    for tr in t._tracks.values():
        if "AAA111" in tr.seen_normalized:
            ids_a.add(tr.track_id)
        if "BBB222" in tr.seen_normalized:
            ids_b.add(tr.track_id)
    now += 0.2
    ax += 20.0
    bx += 20.0
check("two nose-to-tail cars -> 2 tracks", t.active_track_count(), 2)
check("car A stayed one identity", len(ids_a), 1)
check("car B stayed one identity", len(ids_b), 1)
check("no identity swap (disjoint)", len(ids_a & ids_b), 0)

# --- 4. A distant new box opens a NEW track, is not absorbed ---
t = PresenceTracker(make_cfg())
now = 3000.0
for i in range(3):
    t.observe(FRAME, [Cand(bbox=(100, 300, 180, 328))], now=now)
    now += 0.2
t.observe(FRAME, [Cand(bbox=(600, 100, 680, 128))], now=now)
check("far-away box starts a new track", t.active_track_count(), 2)

# --- 5. Stationary jitter (legacy scenario shape) still matches ---
t = PresenceTracker(make_cfg())
now = 4000.0
for i in range(6):
    j = (i % 2) * 2
    t.observe(FRAME, [Cand(bbox=(170 + j, 150, 230 + j, 180))], now=now)
    now += 0.2
check("stationary jittering box stays 1 track", t.active_track_count(), 1)

print("-" * 60)
fails = [n for n, ok in results if not ok]
print(f"{len(results) - len(fails)}/{len(results)} passed")
sys.exit(1 if fails else 0)
