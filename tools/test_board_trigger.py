"""Board-trigger tests (Problem 3, event mechanism).

The trigger is arrival-AND-settle, single-occupant slot:
  fire once when: center in board zone AND smoothed speed ~0 for
  stop_confirm_frames (OR continuously in zone >= trigger_max_wait_seconds,
  the slow-creep fallback) AND the slot is vacant.
  Slot releases only when the occupant genuinely leaves (past hysteresis).

Frame: 720x480. Board zone: x >= 0.7 (right 30%), full height.
Video-time driven (dt=0.2s = 5fps), fully deterministic.
"""
import sys
import pathlib
from dataclasses import dataclass, field

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from plate_reader.config import Presence  # noqa: E402
from plate_reader.presence import PresenceTracker  # noqa: E402
from plate_reader.stream_engine import StreamVehicle  # noqa: E402


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
        motion_boxes_enabled=False,
        board_zone=(0.7, 0.0, 1.0, 1.0),
        stop_speed_frac_per_s=0.02,     # ~17 px/s on this frame
        stop_confirm_frames=3,
        trigger_max_wait_seconds=4.0,
        depart_hysteresis_pct=0.05,
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


def drive(t, positions, now, plate="CAR1", dt=0.2):
    """Feed a sequence of center-x positions; return (triggers, end_now)."""
    trig = []
    for cx in positions:
        ev = t.observe(FRAME, [Cand(bbox=box_at(cx), normalized=plate)], now=now)
        trig.extend(ev)
        now += dt
    return trig, now


# Zone starts at x = 0.7 * 720 = 504.
APPROACH = [100, 180, 260, 340, 420, 500]          # fast, outside zone
ARRIVE_AND_STOP = [540, 545, 548, 550, 551, 552]   # decelerate to stop in zone

# --- 1. Approach + settle -> exactly one trigger, reason 'settled' ---
t = PresenceTracker(make_cfg())
trig, now = drive(t, APPROACH + ARRIVE_AND_STOP, 1000.0)
check("settle fires exactly 1 trigger", len(trig), 1)
check("reason is settled", trig[0].reason if trig else None, "settled")
check("trigger carries the plate", trig[0].plate_normalized if trig else None,
      "CAR1")

# --- 2. Continued sitting does NOT re-fire ---
more, now = drive(t, [552] * 10, now)
check("sitting car does not re-fire", len(more), 0)

# --- 3. Wobble around the zone edge does not re-fire (hysteresis) ---
# hysteresis margin = 0.05 * diag(865) ~ 43px; wobble to 495 stays in band.
wob, now = drive(t, [500, 495, 510, 520, 552, 552], now)
check("edge wobble does not re-fire", len(wob), 0)

# --- 4. Slow-creep (never 'stopped') fires via dwell fallback ---
t = PresenceTracker(make_cfg())
# 24px/frame = 120px/s >> stop threshold; in zone from 510 onward.
creep = [510 + 4 * i for i in range(25)]           # 5s in zone, always moving
# speeds: 4px/0.2s = 20px/s > 17 -> never "stopped"
trig, _ = drive(t, [420, 470] + creep, 2000.0)
check("slow creep fires via dwell fallback", len(trig), 1)
check("fallback reason", trig[0].reason if trig else None, "dwell_fallback")

# --- 5. Fast pass-through never triggers ---
t = PresenceTracker(make_cfg())
through = [100, 220, 340, 460, 580, 700]            # 600px/s, ~2 frames in zone
trig, _ = drive(t, through, 3000.0)
check("fast pass-through does not trigger", len(trig), 0)

# --- 6. Single slot: second car cannot trigger while first holds it ---
t = PresenceTracker(make_cfg())
now = 4000.0
trigs = []
# Car A arrives and settles deep in the zone.
for cx in [420, 500, 560, 600, 620, 622, 623, 624]:
    ev = t.observe(FRAME, [Cand(bbox=box_at(cx), normalized="AAA111")], now=now)
    trigs.extend(ev)
    now += 0.2
check("car A triggered", len(trigs), 1)
# Car B pulls into the zone behind A and stops. Both fed each frame.
b_cx = 480.0
for i in range(12):
    a = Cand(bbox=box_at(624), normalized="AAA111")
    b_cx = min(b_cx + (6 if i < 5 else 0.5), 530)
    b = Cand(bbox=box_at(b_cx, cy=380), normalized="BBB222")
    ev = t.observe(FRAME, [a, b], now=now)
    trigs.extend(ev)
    now += 0.2
check("car B blocked while A holds slot", len(trigs), 1)
# A departs: moves right out of frame edge (past zone max = frame edge)...
# zone extends to x=720 (frame edge), so departing means A's box leaves view.
# Simulate by A no longer appearing; its track times out -> slot released on
# reap. Keep feeding B meanwhile.
for i in range(20):
    ev = t.observe(FRAME, [Cand(bbox=box_at(530, cy=380),
                                normalized="BBB222")], now=now)
    trigs.extend(ev)
    list(t.reap(now=now))
    now += 0.2
check("car B triggers after A's slot releases", len(trigs), 2)
check("second trigger is B",
      trigs[1].plate_normalized if len(trigs) > 1 else None, "BBB222")

# --- 7. Stream's live vehicle observations can drive the same trigger ---
t = PresenceTracker(make_cfg())
stream_trigs = []
for i in range(4):
    stream_trigs.extend(t.observe(FRAME, [StreamVehicle(
        vehicle_bbox=tuple(int(v) for v in box_at(600)),
        vehicle_confidence=0.92,
        encounter_id="e" * 32,
        saw_plate=True,
        plate_detection_confidence=0.88,
        plate_ocr_confidence=0.94,
    )], now=5000.0 + i * 0.2))
check("Stream vehicle observations reach the board trigger",
      len(stream_trigs), 1)
check("Stream observation preserves had-plate evidence",
      stream_trigs[0].had_plate if stream_trigs else None, True)
check("Stream observation preserves the vehicle encounter for future delivery",
      stream_trigs[0].encounter_id if stream_trigs else None, "e" * 32)

print("-" * 60)
fails = [n for n, ok in results if not ok]
print(f"{len(results) - len(fails)}/{len(results)} passed")
sys.exit(1 if fails else 0)
