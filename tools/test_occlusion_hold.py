"""Occlusion-hold tests (Problem 3, FIFO queue awareness).

The rule under test: a track that goes quiet mid-lane while the board slot is
occupied by a STATIONARY car must NOT be reaped as a drive-off — the lane is
FIFO, so nobody behind a parked lead car can have left. It goes HELD (timeout
paused). It re-associates on reappearance (plate string first), and only
closes (end_reason='lost_occluded') after the queue has advanced
occlusion_max_advance_positions cars past it without it reappearing.
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
        track_timeout_seconds=2.0,
        min_track_frames=3,
        motion_area_fraction=0.12,
        color_enabled=False,
        color_split_distance=0.55,
        motion_boxes_enabled=False,
        board_zone=(0.7, 0.0, 1.0, 1.0),
        stop_speed_frac_per_s=0.02,
        stop_confirm_frames=3,
        trigger_max_wait_seconds=4.0,
        occlusion_hold_enabled=True,
        occlusion_max_advance_positions=2,
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


def box_at(cx, w=60.0, cy=300.0, h=24.0):
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def park_a_at_board(t, now, plate="AAA111"):
    """Drive car A into the zone and settle it; returns (triggers, now)."""
    trigs = []
    for cx in [420, 500, 560, 600, 620, 622, 623, 624]:
        ev = t.observe(FRAME, [Cand(bbox=box_at(cx), normalized=plate)],
                       now=now)
        trigs.extend(ev)
        now += 0.2
    return trigs, now


# =========================================================================
# Case 1: B vanishes behind parked A -> HELD, not reaped; reappears -> same id
# =========================================================================
t = PresenceTracker(make_cfg())
now = 1000.0
trigs, now = park_a_at_board(t, now)
check("A holds the slot", len(trigs), 1)

# B appears behind (left of zone), visible 5 frames, then vanishes (occluded).
b_id = None
for i in range(5):
    a = Cand(bbox=box_at(624), normalized="AAA111")
    b = Cand(bbox=box_at(300, cy=380), normalized="BBB222")
    t.observe(FRAME, [a, b], now=now)
    now += 0.2
for tr in t._tracks.values():
    if "BBB222" in tr.seen_normalized:
        b_id = tr.track_id
check("B has a track", b_id is not None, True)

# B gone; A keeps sitting. Advance well past track_timeout (2s).
driveoffs = []
for i in range(20):   # 4 seconds
    t.observe(FRAME, [Cand(bbox=box_at(624), normalized="AAA111")], now=now)
    driveoffs.extend(t.reap(now=now))
    now += 0.2
b_track = t._tracks.get(b_id)
check("B not reaped while A parked (no drive-offs)", len(driveoffs), 0)
check("B is HELD", b_track.held if b_track else None, True)

# B reappears (queue nudges forward): same identity, unheld.
for i in range(3):
    a = Cand(bbox=box_at(624), normalized="AAA111")
    b = Cand(bbox=box_at(340, cy=380), normalized="BBB222")
    t.observe(FRAME, [a, b], now=now)
    now += 0.2
b_track = t._tracks.get(b_id)
check("B re-associated to SAME track id", b_track is not None, True)
check("B unheld after reappearing", b_track.held if b_track else None, False)
check("no phantom second track for B",
      sum(1 for tr in t._tracks.values() if "BBB222" in tr.seen_normalized), 1)

# =========================================================================
# Case 2: B never reappears; queue advances 2 cars -> B closes lost_occluded
# =========================================================================
t = PresenceTracker(make_cfg())
now = 2000.0
trigs, now = park_a_at_board(t, now)

# B appears behind, then vanishes.
for i in range(5):
    a = Cand(bbox=box_at(624), normalized="AAA111")
    b = Cand(bbox=box_at(300, cy=380), normalized="BBB222")
    t.observe(FRAME, [a, b], now=now)
    now += 0.2
# A parked, B gone -> B becomes held.
for i in range(15):
    t.observe(FRAME, [Cand(bbox=box_at(624), normalized="AAA111")], now=now)
    list(t.reap(now=now))
    now += 0.2
held_now = [tr for tr in t._tracks.values() if tr.held]
check("B held before queue advances", len(held_now), 1)

# A departs (stops appearing; its track times out -> slot release, pass #1).
driveoffs = []
for i in range(15):
    t.observe(FRAME, [], now=now)
    driveoffs.extend(t.reap(now=now))
    now += 0.2
check("pass count after A", t.board_pass_count(), 1)
check("B still held after ONE advance",
      sum(1 for tr in t._tracks.values() if tr.held), 1)

# Car C arrives, triggers, departs -> pass #2.
trigs2, now = park_a_at_board(t, now, plate="CCC333")
check("C triggered", len(trigs2), 1)
for i in range(15):
    t.observe(FRAME, [], now=now)
    driveoffs.extend(t.reap(now=now))
    now += 0.2
check("pass count after C", t.board_pass_count(), 2)

lost = [d for d in driveoffs if d.end_reason == "lost_occluded"]
check("B closed as lost_occluded after 2 advances", len(lost), 1)
check("lost drive-off had plate candidate",
      lost[0].had_plate_candidate if lost else None, True)

# =========================================================================
# Case 3: hold disabled -> old behavior (B reaped on plain timeout)
# =========================================================================
t = PresenceTracker(make_cfg(occlusion_hold_enabled=False))
now = 3000.0
trigs, now = park_a_at_board(t, now)
for i in range(5):
    a = Cand(bbox=box_at(624), normalized="AAA111")
    b = Cand(bbox=box_at(300, cy=380), normalized="BBB222")
    t.observe(FRAME, [a, b], now=now)
    now += 0.2
driveoffs = []
for i in range(20):
    t.observe(FRAME, [Cand(bbox=box_at(624), normalized="AAA111")], now=now)
    driveoffs.extend(t.reap(now=now))
    now += 0.2
check("hold disabled -> B reaped on timeout", len(driveoffs), 1)
check("plain timeout reason",
      driveoffs[0].end_reason if driveoffs else None, "timeout")

print("-" * 60)
fails = [n for n, ok in results if not ok]
print(f"{len(results) - len(fails)}/{len(results)} passed")
sys.exit(1 if fails else 0)
