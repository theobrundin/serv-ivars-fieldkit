#!/usr/bin/env python3
"""Tests for the config-driven pipeline features added for field deployment.

Covers the things most likely to be wrong on install day:
  * ROI non-zero path (never exercised before — plates simply stop being
    detected if the coordinate order is wrong, with no error)
  * dwell zone in/out, anchors, and the vehicle-area depth proxy
  * per-hash cooldown, which is the fix for the f02961/f02966 duplicate
  * vehicle-only events for plateless cars (Problem 2)
  * config validation refusing bad spatial values
  * a 4-character junk read never confirming
"""
import pathlib
import sys
import time
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from plate_reader.config import (  # noqa: E402
    Alpr, AlprThresholds, Capture, Classifiers, Dedup, DetectionZone,
    Diagnostics, DwellZone, EngineAdvanced, NormBox, Thresholds, VehicleOnly,
)
from plate_reader.detector import (  # noqa: E402
    Detector, ConfirmedIdentification, VehicleOnlyIdentification,
)
from plate_reader.engines import RawPlateRead, UltimateAlprEngine  # noqa: E402

_passed = _failed = 0


def check(label, got, want):
    global _passed, _failed
    if got == want:
        _passed += 1
        print(f"PASS  {label}  (got={got!r})")
    else:
        _failed += 1
        print(f"FAIL  {label}  (got={got!r} want={want!r})")


@dataclass
class C:
    thresholds: object
    detection_zone: object
    alpr: object
    dwell_zone: object
    dedup: object
    diagnostics: object
    capture: object
    vehicle_only: object


def make_cfg(dwell_zone=None, dedup=None, roi=None, vehicle=False,
             app_ocr=0.5, app_det=0.5, vehicle_only=None):
    return C(
        thresholds=Thresholds(
            min_detection_confidence=app_det,
            min_ocr_confidence=app_ocr,
            min_group_confidence=0.5,
            vote_window_seconds=5.0,
            min_normalized_length=4,
            max_normalized_length=12,
        ),
        detection_zone=DetectionZone(False, 0.0, 1.0, 0.0, 1.0),
        alpr=Alpr(
            engine="ultimate_alpr",
            roi=roi or NormBox(),
            thresholds=AlprThresholds(),
            classifiers=Classifiers(vehicle_detect_enabled=vehicle),
            engine_advanced=EngineAdvanced(),
        ),
        dwell_zone=dwell_zone or DwellZone(),
        dedup=dedup or Dedup(cooldown_seconds=0.0),
        diagnostics=Diagnostics(),
        capture=Capture(width=1000, height=1000),
        # Production defaults unless a test says otherwise, so the tests
        # exercise what actually ships rather than a permissive variant.
        vehicle_only=vehicle_only or VehicleOnly(),
    )


class StubEngine:
    """Feeds the detector a scripted list of RawPlateRead per frame."""
    name = "stub"

    def __init__(self, frames):
        self._frames = list(frames)
        self._i = 0

    def open(self):
        pass

    def close(self):
        pass

    def infer(self, frame):
        if self._i >= len(self._frames):
            return []
        out = self._frames[self._i]
        self._i += 1
        return out


class FakeFrame:
    shape = (1000, 1000, 3)


def read(text="ABC123", bbox=(400, 400, 500, 450), vbox=None, det=0.9,
         ocr=0.9, encounter=None, competing=False, observation=None):
    return RawPlateRead(text=text, detection_confidence=det,
                        ocr_confidence=ocr, bbox=bbox,
                        vehicle_bbox=vbox, vehicle_confidence=0.99 if vbox else None,
                        encounter_id=encounter,
                        had_competing_variants=competing,
                        source_observation_id=observation)


def run(cfg, frames, on_frame=None):
    d = Detector(cfg)
    d._engine = StubEngine(frames)
    confirms, outcomes = [], []
    for i in range(len(frames)):
        _, o = d.process_frame(FakeFrame())
        outcomes.extend(o)
        if on_frame:
            on_frame(d, i)
    confirms = [x for x in outcomes
                if isinstance(x, ConfirmedIdentification) and not x.suppressed]
    suppressed = [x for x in outcomes
                  if isinstance(x, ConfirmedIdentification) and x.suppressed]
    return d, confirms, suppressed


print("\n=== pending miss freshness is encounter-specific ===")


class BusyLaneFreshness:
    def __init__(self):
        self.calls = []

    def has_fresh_vehicle(self, encounter_id=None):
        self.calls.append(encounter_id)
        # Some other car is active, but the pending miss's car is gone.
        return encounter_id is None


class OtherEncounterVote:
    def encounter_id(self):
        return "72" * 16


class AlreadyCountedEngine:
    @staticmethod
    def has_fresh_vehicle(encounter_id=None):
        return False

    @staticmethod
    def terminal_visit_emitted(encounter_id):
        return True


_old_encounter = "71" * 16
_busy_detector = Detector(make_cfg())
_busy_freshness = BusyLaneFreshness()
_busy_detector._engine = _busy_freshness
_other_vote = OtherEncounterVote()
_busy_detector._buckets[("72" * 16, "OTHER123")] = _other_vote
_busy_detector._rescue_buckets[("72" * 16, "OTHER123")] = _other_vote
_busy_detector._note_pending_miss(
    "TEST123",
    now=0.0,
    detected_at_iso="2026-08-30T16:00:00Z",
    det=0.65,
    ocr=0.70,
    inference_ms=10,
    outcome="below_threshold",
    encounter_id=_old_encounter,
)
_busy_flush = _busy_detector._flush_quiet_misses(11.0)
check("another car's vote/freshness cannot freeze an older encounter's miss",
      (len(_busy_flush), _busy_freshness.calls),
      (1, [_old_encounter]))

_encounter_a = "73" * 16
_encounter_b = "74" * 16
_, _cross_car_confirms, _ = run(
    make_cfg(),
    [[
        read(text="SAME123", encounter=_encounter_a, observation="obs-a",
             ocr=0.80),
        read(text="SAME123", encounter=_encounter_b, observation="obs-b",
             ocr=0.80),
    ]],
)
check("same OCR from two physical encounters cannot form one vote",
      len(_cross_car_confirms), 0)

_split_misses = Detector(make_cfg())
for _miss_key, _miss_encounter, _miss_now in (
    ("FIRST1", _encounter_a, 0.0),
    ("OTHER9", _encounter_b, 1.0),
):
    _split_misses._note_pending_miss(
        _miss_key,
        now=_miss_now,
        detected_at_iso="2026-08-30T16:00:00Z",
        det=0.60,
        ocr=0.70,
        inference_ms=10,
        outcome="below_threshold",
        encounter_id=_miss_encounter,
    )
check("time-adjacent misses from different encounters remain separate",
      {miss.encounter_id for miss in _split_misses._pending_miss.values()},
      {_encounter_a, _encounter_b})
_split_misses._drop_pending_miss_family(
    "FIRST1", now=2.0, encounter_id=_encounter_a
)
check("one encounter confirmation cannot erase another car's miss",
      [miss.encounter_id for miss in _split_misses._pending_miss.values()],
      [_encounter_b])

_late_miss_detector = Detector(make_cfg())
_late_miss_detector._engine = AlreadyCountedEngine()
_late_miss_detector._note_pending_miss(
    "LATE12",
    now=0.0,
    detected_at_iso="2026-08-30T16:00:00Z",
    det=0.60,
    ocr=0.70,
    inference_ms=10,
    outcome="below_threshold",
    encounter_id=_encounter_a,
)
_late_miss_outcomes = _late_miss_detector._flush_quiet_misses(11.0)
check("late weak plate evidence still flushes after the traffic Visit",
      (len(_late_miss_outcomes),
       _late_miss_outcomes[0].encounter_id,
       len(_late_miss_detector._pending_miss)),
      (1, _encounter_a, 0))


# ---------------------------------------------------------------- ROI ------
print("\n=== ROI (engine detect_roi) ===")

# Disabled ROI must produce the exact legacy value.
eng = UltimateAlprEngine(make_cfg())
check("ROI disabled -> full frame [0,0,0,0]", eng._detect_roi_pixels(), [0, 0, 0, 0])

# Enabled ROI converts normalized -> pixels in the SDK's [left,right,top,bottom].
roi = NormBox(enabled=True, x=0.25, y=0.10, w=0.50, h=0.40)
eng = UltimateAlprEngine(make_cfg(roi=roi))
eng.set_frame_size(1000, 1000)
check("ROI enabled -> [left,right,top,bottom] px",
      eng._detect_roi_pixels(), [250, 750, 100, 500])

# Resolution independence: the SAME normalized box at a different capture
# resolution must scale, not silently mean a different region.
eng2 = UltimateAlprEngine(make_cfg(roi=roi))
eng2.set_frame_size(1456, 1088)
check("same ROI at 1456x1088 scales",
      eng2._detect_roi_pixels(), [364, 1092, 109, 544])

# A plate detected OUTSIDE the ROI must not survive. The engine enforces this
# itself, so at adapter level we assert the ROI is actually passed through
# rather than dropped (the failure mode is a silently full-frame search).
cfg_roi = make_cfg(roi=NormBox(enabled=True, x=0.0, y=0.0, w=0.2, h=0.2))
eng3 = UltimateAlprEngine(cfg_roi)
eng3.set_frame_size(1000, 1000)
jc = eng3._build_json_config()
check("detect_roi reaches engine json", jc["detect_roi"], [0, 200, 0, 200])
check("ROI is not full-frame when enabled", jc["detect_roi"] == [0, 0, 0, 0], False)

# ---------------------------------------------------------- dwell zone -----
print("\n=== dwell zone ===")

# Right half of frame only.
dz = DwellZone(enabled=True, x=0.5, y=0.0, w=0.5, h=1.0)

inside = [[read(bbox=(700, 400, 800, 450))] for _ in range(3)]
_, c_in, _ = run(make_cfg(dwell_zone=dz), inside)
check("plate INSIDE dwell zone confirms", len(c_in), 1)

outside = [[read(bbox=(100, 400, 200, 450))] for _ in range(3)]
_, c_out, _ = run(make_cfg(dwell_zone=dz), outside)
check("plate OUTSIDE dwell zone never confirms", len(c_out), 0)

# Disabled zone must not impose any positional requirement.
_, c_dis, _ = run(make_cfg(dwell_zone=DwellZone()), outside)
check("dwell zone DISABLED -> position ignored", len(c_dis), 1)

# vehicle_bottom anchor: plate box is inside the zone but the car's ground
# contact is not, so the read must be rejected.
dz_bottom = DwellZone(enabled=True, x=0.0, y=0.8, w=1.0, h=0.2,
                      anchor="vehicle_bottom")
frames = [[read(bbox=(400, 400, 500, 450), vbox=(300, 100, 600, 500))]
          for _ in range(3)]
_, c_anch, _ = run(make_cfg(dwell_zone=dz_bottom, vehicle=True), frames)
check("vehicle_bottom anchor outside zone -> reject", len(c_anch), 0)

frames_low = [[read(bbox=(400, 400, 500, 450), vbox=(300, 500, 600, 900))]
              for _ in range(3)]
_, c_anch2, _ = run(make_cfg(dwell_zone=dz_bottom, vehicle=True), frames_low)
check("vehicle_bottom anchor inside zone -> accept", len(c_anch2), 1)

# Area proxy for depth: a small distant car is rejected, a large near one passes.
dz_area = DwellZone(enabled=True, x=0.0, y=0.0, w=1.0, h=1.0,
                    anchor="vehicle_center", min_vehicle_box_area=0.10)
small = [[read(bbox=(400, 400, 420, 410), vbox=(400, 400, 500, 500))]
         for _ in range(3)]           # 100x100 / 1e6 = 0.01
_, c_small, _ = run(make_cfg(dwell_zone=dz_area, vehicle=True), small)
check("small (distant) vehicle rejected by area", len(c_small), 0)

big = [[read(bbox=(400, 400, 500, 450), vbox=(200, 200, 800, 800))]
       for _ in range(3)]             # 600x600 / 1e6 = 0.36
_, c_big, _ = run(make_cfg(dwell_zone=dz_area, vehicle=True), big)
check("large (near) vehicle passes area gate", len(c_big), 1)

# ------------------------------------------------------------- dedup -------
print("\n=== dedup cooldown (the f02961/f02966 fix) ===")

# Faithful reproduction of the f02961/f02966 duplicate:
#   1. plate confirms
#   2. the read FLICKERS OUT for >= release_requires_absent_frames, which
#      latches left_frame_since_confirm ("this car left")
#   3. another vehicle's track closes, satisfying
#      dwell_release_after_other_vehicles
#   4. the plate returns and re-confirms -> a second countable visit for one
#      physical car
# Step 3 is what the harness does via note_vehicle_departed(); without it the
# dwell gate alone would have held, which is why this needs the hook.
flicker = (
    [[read()], [read()]]          # confirm #1
    + [[] for _ in range(4)]      # flicker out, latches the absence gate
    + [[read()], [read()]]        # returns -> confirms again
)


def _other_vehicle_passes(d, i):
    if i == 4:                    # while the plate is absent
        d.note_vehicle_departed(None)


_, c_nocool, _ = run(make_cfg(dedup=Dedup(cooldown_seconds=0.0)), flicker,
                     on_frame=_other_vehicle_passes)
check("cooldown OFF reproduces the duplicate", len(c_nocool), 2)

_, c_cool, s_cool = run(make_cfg(dedup=Dedup(cooldown_seconds=120.0)), flicker,
                        on_frame=_other_vehicle_passes)
check("cooldown ON -> exactly 1 countable confirm", len(c_cool), 1)
check("the re-confirm is emitted as suppressed", len(s_cool), 1)
check("suppress reason names the cooldown",
      s_cool[0].suppress_reason.startswith("cooldown"), True)
check("suppressed record keeps repeat_index", s_cool[0].repeat_index >= 1, True)

# -------------------------------------------------- near-match dedup ------
print("\n=== near-match dedup (the CHR7067 / CHR706 fix) ===")

check("edit distance: truncation",
      Detector._edit_distance_within("CHR7067", "CHR706", 1), True)
check("edit distance: single substitution",
      Detector._edit_distance_within("CEV1527", "CEN1527", 1), True)
check("edit distance: two edits is NOT a near match",
      Detector._edit_distance_within("CHR7067", "CHR70", 1), False)
check("edit distance: unrelated plates",
      Detector._edit_distance_within("ABC1234", "XYZ9999", 1), False)

# Real scenario: a car confirms, then a TRUNCATED read of the same plate
# confirms 1s later. Different string -> different hash -> the hash cooldown
# cannot see it. Without near-match dedup that is two visits for one car.
misread = [[read(text="CHR7067")], [read(text="CHR7067")],
           [read(text="CHR706")], [read(text="CHR706")]]

_, c_off, _ = run(make_cfg(dedup=Dedup(cooldown_seconds=120.0,
                                       near_match_max_distance=0)), misread)
check("near-match OFF -> spurious 2nd identity", len(c_off), 2)

_, c_on, s_on = run(make_cfg(dedup=Dedup(cooldown_seconds=120.0,
                                         near_match_max_distance=1)), misread)
check("near-match ON  -> exactly 1 identity", len(c_on), 1)
check("the misread is emitted as suppressed", len(s_on), 1)
check("suppress reason names near_match",
      s_on[0].suppress_reason.startswith("near_match"), True)

# Guard against over-merging: two genuinely different plates must still both
# count, even though they arrive close together.
two_cars = [[read(text="ABC1234")], [read(text="ABC1234")],
            [read(text="XYZ9999")], [read(text="XYZ9999")]]
_, c_two, _ = run(make_cfg(dedup=Dedup(cooldown_seconds=120.0,
                                       near_match_max_distance=1)), two_cars)
check("two DIFFERENT plates both still confirm", len(c_two), 2)

# Physical encounter ownership outranks edit distance. Two real cars can have
# legitimate plates one character apart; the global temporal fallback must
# never suppress the second known vehicle.
near_two_cars = [
    [read(text="ABC1234", encounter=_encounter_a)],
    [read(text="ABC1234", encounter=_encounter_a)],
    [read(text="ABC1235", encounter=_encounter_b)],
    [read(text="ABC1235", encounter=_encounter_b)],
]
_, c_near_two, s_near_two = run(
    make_cfg(dedup=Dedup(
        cooldown_seconds=120.0,
        near_match_max_distance=1,
    )),
    near_two_cars,
)
check("one-edit plates from two known encounters both confirm",
      (len(c_near_two), len(s_near_two)), (2, 0))

# ------------------------------------------------------ vehicle-only -------
print("\n=== vehicle-only events: the missed-vehicle KPI ===")

# A car with no readable plate that DRIVES THROUGH: the box travels across
# the frame. This is the KPI case — the FIFO queue cannot match it to an
# order, so it must be counted.
def drive_through(n=6, step=60, vbox0=(200, 300, 500, 700)):
    x1, y1, x2, y2 = vbox0
    return [[read(text="", bbox=(0, 0, 0, 0), det=0.0, ocr=0.0,
                  vbox=(x1 + i * step, y1, x2 + i * step, y2))]
            for i in range(n)]


VZ = DwellZone(enabled=True, x=0.0, y=0.0, w=1.0, h=1.0)

# The KPI must work with NO dwell zone. It used to be suppressed entirely,
# which meant an uncalibrated unit silently reported zero missed vehicles —
# the worst failure mode for a metric that exists to count what we miss.
d_nozone, _, _ = run(make_cfg(vehicle=True, dwell_zone=DwellZone()),
                     drive_through())
check("NO dwell zone -> plateless car still counted",
      len(d_nozone.collect_vehicle_only(flush=True)), 1)

# ...unless a deployment explicitly opts back in to requiring one.
d_req, _, _ = run(
    make_cfg(vehicle=True, dwell_zone=DwellZone(),
             vehicle_only=VehicleOnly(require_dwell_zone=True)),
    drive_through())
check("require_dwell_zone=True + no zone -> suppressed",
      len(d_req.collect_vehicle_only(flush=True)), 0)

d_v, _, _ = run(make_cfg(vehicle=True, dwell_zone=VZ), drive_through())

# A vehicle still in frame must NOT be reported yet — it may still identify.
check("still-present vehicle is NOT emitted early",
      len(d_v.collect_vehicle_only(timeout_seconds=3.0)), 0)

# Once it's been gone past the timeout (or at end of stream), it is emitted.
vo = d_v.collect_vehicle_only(flush=True)
check("plateless vehicle emits a vehicle-only event", len(vo), 1)
check("vehicle-only event is its own type",
      isinstance(vo[0], VehicleOnlyIdentification), True)
check("vehicle-only carries NO plate/hash field",
      hasattr(vo[0], "normalized"), False)
check("movement evidence travels with the event",
      vo[0].travel_fraction > 0.0, True)
check("degenerate (0,0,0,0) box is NOT counted as a plate candidate",
      vo[0].had_plate_candidate, False)
check("draining is idempotent (no duplicate event)",
      len(d_v.collect_vehicle_only(flush=True)), 0)

with_plate = [[read(vbox=(300 + i * 60, 300, 700 + i * 60, 700))]
              for i in range(3)]
d_p, _, _ = run(make_cfg(vehicle=True, dwell_zone=VZ), with_plate)
check("plated vehicle emits NO vehicle-only event",
      len(d_p.collect_vehicle_only(flush=True)), 0)

# A one-frame detector blip must not become an event.
blip = [[read(text="", bbox=(0, 0, 0, 0), vbox=(300, 300, 700, 700),
              det=0.0, ocr=0.0)]]
d_b, _, _ = run(make_cfg(vehicle=True, dwell_zone=VZ), blip)
check("single-frame blip is filtered by min_frames",
      len(d_b.collect_vehicle_only(flush=True, min_frames=2)), 0)

# One car moving across the frame must produce ONE event, not one per
# position — this is why association is by box overlap, not a fixed grid.
d_m, _, _ = run(make_cfg(vehicle=True, dwell_zone=VZ), drive_through())
check("one MOVING vehicle -> exactly one event",
      len(d_m.collect_vehicle_only(flush=True)), 1)

# Two vehicles far apart are two events.
two_veh = [[read(text="", bbox=(0, 0, 0, 0), det=0.0, ocr=0.0,
                 vbox=(20 + i * 40, 50, 180 + i * 40, 200)),
            read(text="", bbox=(0, 0, 0, 0), det=0.0, ocr=0.0,
                 vbox=(600 + i * 40, 700, 780 + i * 40, 950))]
           for i in range(5)]
d_2, _, _ = run(make_cfg(vehicle=True, dwell_zone=VZ), two_veh)
check("two distinct vehicles -> two events",
      len(d_2.collect_vehicle_only(flush=True)), 2)

# --- the movement gate: what replaces the dwell zone -----------------------
print("\n=== movement gate (parked cars must not count as misses) ===")

# A parked car: same box, frame after frame, for a long time. Measured on
# real footage this was one of 20 false events with the zone off, tracked for
# 207.68s. It must not be reported as a customer we failed to identify.
parked = [[read(text="", bbox=(0, 0, 0, 0), vbox=(300, 300, 700, 700),
                det=0.0, ocr=0.0)] for _ in range(40)]
d_park, _, _ = run(make_cfg(vehicle=True, dwell_zone=DwellZone()), parked)
check("STATIC (parked) vehicle is NOT counted",
      len(d_park.collect_vehicle_only(flush=True)), 0)
check("...but it IS recorded as raw, for hand-count comparison",
      d_park.vehicle_only_seen_raw, 1)
check("...and attributed to the static gate",
      d_park.vehicle_only_static_dropped, 1)

# A car approaching head-on barely moves laterally but grows a lot. Travel
# alone would miss it, which is why either signal suffices.
approaching = [[read(text="", bbox=(0, 0, 0, 0), det=0.0, ocr=0.0,
                     vbox=(500 - i * 18, 500 - i * 18,
                           560 + i * 18, 560 + i * 18))]
               for i in range(8)]
d_appr, _, _ = run(make_cfg(vehicle=True, dwell_zone=DwellZone()), approaching)
check("APPROACHING vehicle (grows, little travel) IS counted",
      len(d_appr.collect_vehicle_only(flush=True)), 1)

# Turning both gates off restores the old count-everything behaviour, for
# deliberate diagnosis of the raw detector layer.
d_raw, _, _ = run(
    make_cfg(vehicle=True, dwell_zone=DwellZone(),
             vehicle_only=VehicleOnly(min_travel_fraction=0.0,
                                      min_area_growth_ratio=0.0)),
    parked)
check("gates OFF -> static vehicle counted again",
      len(d_raw.collect_vehicle_only(flush=True)), 1)

# The feature can be switched off wholesale.
d_off, _, _ = run(
    make_cfg(vehicle=True, dwell_zone=VZ,
             vehicle_only=VehicleOnly(enabled=False)),
    drive_through())
check("vehicle_only.enabled=False -> nothing tracked",
      len(d_off.collect_vehicle_only(flush=True)), 0)

# --------------------------------------------------- 4-char junk read ------
# ------------------------------------------- group confidence floors -------
print("\n=== group floor must never out-rank the per-frame floor ===")

# The bug this locks down: min_group_confidence 0.80 sat BEHIND per-frame
# floors of det 0.50 / ocr 0.75, so a read admitted at ocr 0.76 was re-judged
# at 0.80 and dropped. On hand-labelled footage that cost 5 of 20 cars.
th_split = Thresholds(
    min_detection_confidence=0.50, min_ocr_confidence=0.75,
    min_group_confidence=0.80,
    vote_window_seconds=5.0, min_normalized_length=4, max_normalized_length=12,
    min_group_detection_confidence=0.80, min_group_ocr_confidence=0.80,
)
# A stricter group floor is ALLOWED — "a confirmed identity must clear a
# higher bar than a candidate" is real policy. Clamping it away would make
# this gate incapable of ever rejecting anything, since bucket members have
# already passed the per-frame gate. What must not happen is it being SILENT.
check("a stricter group floor is honoured, not clamped away",
      (th_split.group_detection_floor, th_split.group_ocr_floor), (0.80, 0.80))
check("...and is flagged as stricter so it gets logged",
      th_split.group_floor_is_stricter, True)

th_none = Thresholds(
    min_detection_confidence=0.50, min_ocr_confidence=0.75,
    min_group_confidence=0.80,
    vote_window_seconds=5.0, min_normalized_length=4, max_normalized_length=12,
)
check("unset group floors fall back to the per-frame floors",
      (th_none.group_detection_floor, th_none.group_ocr_floor), (0.50, 0.75))

th_lower = Thresholds(
    min_detection_confidence=0.50, min_ocr_confidence=0.75,
    min_group_confidence=0.80,
    vote_window_seconds=5.0, min_normalized_length=4, max_normalized_length=12,
    min_group_detection_confidence=0.20, min_group_ocr_confidence=0.30,
)
check("a group floor LOWER than per-frame is honoured, not clamped up",
      (th_lower.group_detection_floor, th_lower.group_ocr_floor), (0.20, 0.30))
check("a floor at/below per-frame is NOT flagged as stricter",
      th_lower.group_floor_is_stricter, False)

# End-to-end: the exact shape that was being lost. Two frames agreeing on the
# same string, each clearing the per-frame floors, with ocr between the old
# group floor (0.80) and the per-frame floor (0.75).
def cfg_with(th, votes=2):
    c = make_cfg()
    th.vote_min_frames = votes
    c.thresholds = th
    return c


borderline = [[read(text="BXV8862", det=0.67, ocr=0.78)] for _ in range(3)]

def mk_th(gdet, gocr):
    return Thresholds(
        min_detection_confidence=0.50, min_ocr_confidence=0.75,
        min_group_confidence=0.80, vote_window_seconds=5.0,
        min_normalized_length=4, max_normalized_length=12,
        min_group_detection_confidence=gdet, min_group_ocr_confidence=gocr,
    )


# The exact shape that was being lost: agreeing frames, each clearing the
# per-frame floors, with ocr between the old group floor and the per-frame
# floor. Under the OLD strict group floor it must still be rejected...
_, c_strict, _ = run(cfg_with(mk_th(0.80, 0.80)), borderline)
check("strict group floor still rejects a det .67 / ocr .78 vote",
      len(c_strict), 0)

# ...and under floors ALIGNED with the per-frame gate it confirms. This is
# the 5-cars-of-20 recovery, reduced to one assertion.
_, c_aligned, _ = run(cfg_with(mk_th(0.50, 0.75)), borderline)
check("aligned group floor CONFIRMS the same det .67 / ocr .78 vote",
      len(c_aligned), 1)

# A read genuinely below the per-frame floor never reaches a vote at all.
too_weak = [[read(text="BXV8862", det=0.67, ocr=0.60)] for _ in range(3)]
_, c_weak, _ = run(cfg_with(mk_th(0.50, 0.75)), too_weak)
check("read below the per-frame ocr floor still rejected", len(c_weak), 0)

# Three-frame voting: the precision half of the change.
_, c_v3, _ = run(cfg_with(mk_th(0.50, 0.75), votes=3),
                 [[read(text="BXV8862", det=0.67, ocr=0.78)] for _ in range(3)])
check("vote_min_frames=3 confirms when 3 frames agree", len(c_v3), 1)
_, c_v3short, _ = run(cfg_with(mk_th(0.50, 0.75), votes=3),
                      [[read(text="BXV8862", det=0.67, ocr=0.78)]
                       for _ in range(2)])
check("vote_min_frames=3 does NOT confirm on only 2 frames", len(c_v3short), 0)

# The failure REASON must name the group floor, not blame the reads. Buckets
# are keyed by exact string, so every candidate in one agrees by construction
# and "vote_disagreement" was impossible — reporting it is what disguised this
# threshold bug as an OCR problem. Needs the vote window to actually elapse,
# so the bucket is reaped: short window, then one more frame past it.
th_reason = mk_th(0.80, 0.80)
th_reason.vote_window_seconds = 0.05
d_r = Detector(cfg_with(th_reason))
d_r._engine = StubEngine(borderline + [[]])
reasons = []
for i in range(len(borderline) + 1):
    _, outs = d_r.process_frame(FakeFrame())
    reasons += [o.outcome for o in outs if hasattr(o, "outcome")]
    time.sleep(0.03)          # push the bucket past its window
check("an agreed-but-floored vote reports group_confidence_floor",
      "group_confidence_floor" in reasons, True)
check("...and never the impossible 'vote_disagreement'",
      "vote_disagreement" in reasons, False)

print("\n=== encounter-bound sub-threshold consensus rescue ===")


def rescue_cfg():
    c = make_cfg(app_det=0.70, app_ocr=0.85)
    c.alpr.thresholds.app_detect_min = 0.70
    c.alpr.thresholds.app_ocr_min = 0.85
    c.alpr.thresholds.consensus_rescue_min_frames = 3
    c.alpr.thresholds.consensus_rescue_detect_min = 0.65
    c.alpr.thresholds.consensus_rescue_ocr_min = 0.80
    return c


enc = "a" * 32
weak_ocr = [[read(text="BXV8862", det=0.75, ocr=0.83,
                  encounter=enc)] for _ in range(3)]
_, rescued, _ = run(rescue_cfg(), weak_ocr)
check("3 exact OCR-only misses from one encounter confirm",
      (len(rescued), rescued[0].confirmation_method if rescued else None),
      (1, "consensus_rescue"))
check("rescue preserves actual weak scores and frame count",
      ((rescued[0].detection_confidence, rescued[0].ocr_confidence,
        rescued[0].frame_count) if rescued else None),
      (0.75, 0.83, 3))

_, only_two, _ = run(rescue_cfg(), weak_ocr[:2])
check("2 exact weak reads remain a miss", len(only_two), 0)

no_encounter = [[read(text="BXV8862", det=0.75, ocr=0.83)]
                for _ in range(3)]
_, no_encounter_confirms, _ = run(rescue_cfg(), no_encounter)
check("weak reads without a physical encounter cannot rescue",
      len(no_encounter_confirms), 0)

split_encounters = [
    [read(text="BXV8862", det=0.75, ocr=0.83, encounter=str(i) * 32)]
    for i in range(3)
]
_, split_confirms, _ = run(rescue_cfg(), split_encounters)
check("the same weak text from different cars cannot combine",
      len(split_confirms), 0)

mixed_strings = [
    [read(text=text, det=0.75, ocr=0.83, encounter=enc)]
    for text in ("BXV8862", "BXV886Z", "BXV8862")
]
_, mixed_confirms, _ = run(rescue_cfg(), mixed_strings)
check("three weak reads with string disagreement cannot rescue",
      len(mixed_confirms), 0)

both_gates_fail = [[read(text="BXV8862", det=0.68, ocr=0.82,
                         encounter=enc)] for _ in range(3)]
_, both_fail_confirms, _ = run(rescue_cfg(), both_gates_fail)
check("repetition cannot rescue a read that fails both normal gates",
      len(both_fail_confirms), 0)

below_rescue_floor = [[read(text="BXV8862", det=0.75, ocr=0.79,
                            encounter=enc)] for _ in range(3)]
_, below_floor_confirms, _ = run(rescue_cfg(), below_rescue_floor)
check("repetition below a rescue floor remains a miss",
      len(below_floor_confirms), 0)

weak_det = [[read(text="BXV8862", det=0.67, ocr=0.90,
                  encounter=enc)] for _ in range(3)]
_, det_rescued, _ = run(rescue_cfg(), weak_det)
check("3 exact detection-only misses can use the symmetric path",
      (len(det_rescued),
       det_rescued[0].confirmation_method if det_rescued else None),
      (1, "consensus_rescue"))

one_encounter_two_strings = weak_ocr + [
    [read(text="XYZ789", det=0.75, ocr=0.83, encounter=enc)]
    for _ in range(3)
]
_, one_terminal, _ = run(rescue_cfg(), one_encounter_two_strings)
check("a confirmed encounter cannot later produce a second identity",
      len(one_terminal), 1)

print("\n=== provider-selected temporal single (PR Stream only) ===")


def provider_single_cfg():
    c = make_cfg(app_det=0.70, app_ocr=0.85)
    c.alpr.engine = "pr_stream"
    c.alpr.thresholds.app_detect_min = 0.70
    c.alpr.thresholds.app_ocr_min = 0.85
    c.alpr.thresholds.provider_selected_single_enabled = True
    c.alpr.thresholds.provider_selected_single_min_length = 5
    c.alpr.thresholds.provider_selected_single_min_relative_x = 0.45
    c.thresholds.high_ocr_single_frame = 0.99
    return c


provider_enc = "p" * 32
selected = [[read(
    text="BXV8862", det=0.90, ocr=0.95, encounter=provider_enc,
    bbox=(650, 500, 750, 550), vbox=(200, 200, 900, 850),
)]]
_, selected_confirm, _ = run(provider_single_cfg(), selected)
check("one provider-selected normal-gate result confirms",
      (len(selected_confirm),
       selected_confirm[0].confirmation_method if selected_confirm else None),
      (1, "provider_selected_single"))

no_vehicle = [[read(
    text="BXV8862", det=0.90, ocr=0.95, encounter=provider_enc,
)]]
_, no_vehicle_confirm, _ = run(provider_single_cfg(), no_vehicle)
check("provider-selected result requires containing vehicle geometry",
      len(no_vehicle_confirm), 0)

short_fragment = [[read(
    text="P311", det=0.90, ocr=0.95, encounter=provider_enc,
    bbox=(650, 500, 750, 550), vbox=(200, 200, 900, 850),
)]]
_, fragment_confirm, _ = run(provider_single_cfg(), short_fragment)
check("provider-selected path rejects short OCR fragments",
      len(fragment_confirm), 0)

# LJS 25 Sep: explicit site override, not a global default change.
def four_character_cfg():
    c = provider_single_cfg()
    c.alpr.thresholds.provider_selected_single_min_length = 4
    c.alpr.thresholds.app_ocr_min = 0.80
    c.thresholds.min_ocr_confidence = 0.80
    return c

for label, text, det, ocr, encounter, vbox, want in [
    ("four-character field read", "5487", .853, .801, provider_enc, (200,200,900,850), 1),
    ("three-character fragment", "548", .853, .801, provider_enc, (200,200,900,850), 0),
    ("four characters below OCR gate", "5487", .853, .799, provider_enc, (200,200,900,850), 0),
    ("four characters below detection gate", "5487", .699, .801, provider_enc, (200,200,900,850), 0),
    ("four characters without encounter", "5487", .853, .801, None, (200,200,900,850), 0),
    ("four characters without vehicle box", "5487", .853, .801, provider_enc, None, 0),
]:
    _, confirmed_four, _ = run(four_character_cfg(), [[read(
        text=text, det=det, ocr=ocr, encounter=encounter,
        bbox=(650,500,750,550), vbox=vbox,
    )]])
    check(label, len(confirmed_four), want)

left_side_text = [[read(
    text="STICKER", det=0.90, ocr=0.95, encounter=provider_enc,
    bbox=(250, 500, 330, 550), vbox=(200, 200, 900, 850),
)]]
_, sticker_confirm, _ = run(provider_single_cfg(), left_side_text)
check("provider-selected path rejects measured non-plate geometry",
      len(sticker_confirm), 0)

below_normal_gate = [[read(
    text="BXV8862", det=0.69, ocr=0.95, encounter=provider_enc,
    bbox=(650, 500, 750, 550), vbox=(200, 200, 900, 850),
)]]
_, below_gate_confirm, _ = run(provider_single_cfg(), below_normal_gate)
check("provider-selected path does not lower normal confidence gates",
      len(below_gate_confirm), 0)

print("\n=== provider-selected high-OCR / low-detection rescue ===")


def provider_high_ocr_rescue_cfg():
    c = provider_single_cfg()
    c.alpr.thresholds.provider_high_ocr_rescue_enabled = True
    c.alpr.thresholds.provider_high_ocr_rescue_detect_min = 0.35
    c.alpr.thresholds.provider_high_ocr_rescue_ocr_min = 0.99
    return c


clear_low_det = [[read(
    text="N3H799", det=0.39, ocr=1.00, encounter="h" * 32,
    bbox=(650, 500, 750, 550), vbox=(200, 200, 900, 850),
)]]
_, high_ocr_rescued, _ = run(
    provider_high_ocr_rescue_cfg(), clear_low_det
)
check("field-observed high-OCR / low-detection plate is rescued",
      (len(high_ocr_rescued),
       high_ocr_rescued[0].confirmation_method
       if high_ocr_rescued else None),
      (1, "provider_high_ocr_rescue"))

below_rescue_det = [[read(
    text="N3H799", det=0.34, ocr=1.00, encounter="h" * 32,
    bbox=(650, 500, 750, 550), vbox=(200, 200, 900, 850),
)]]
_, below_rescue_det_confirms, _ = run(
    provider_high_ocr_rescue_cfg(), below_rescue_det
)
check("high OCR cannot rescue below its calibrated detection floor",
      len(below_rescue_det_confirms), 0)

below_rescue_ocr = [[read(
    text="N3H799", det=0.39, ocr=0.98, encounter="h" * 32,
    bbox=(650, 500, 750, 550), vbox=(200, 200, 900, 850),
)]]
_, below_rescue_ocr_confirms, _ = run(
    provider_high_ocr_rescue_cfg(), below_rescue_ocr
)
check("low detection requires near-certain OCR",
      len(below_rescue_ocr_confirms), 0)

both_weak_single = [[read(
    text="908WGS", det=0.64, ocr=0.82, encounter="h" * 32,
    bbox=(650, 500, 750, 550), vbox=(200, 200, 900, 850),
)]]
_, both_weak_single_confirms, _ = run(
    provider_high_ocr_rescue_cfg(), both_weak_single
)
check("one result failing both normal gates remains a miss",
      len(both_weak_single_confirms), 0)

competing_variant = [[read(
    text="N3H799", det=0.39, ocr=1.00, encounter="h" * 32,
    bbox=(650, 500, 750, 550), vbox=(200, 200, 900, 850),
    competing=True,
)]]
_, competing_variant_confirms, _ = run(
    provider_high_ocr_rescue_cfg(), competing_variant
)
check("a competing encounter variant blocks one-shot rescue",
      len(competing_variant_confirms), 0)

bad_rescue_geometry = [[read(
    text="N3H799", det=0.39, ocr=1.00, encounter="h" * 32,
    bbox=(250, 500, 330, 550), vbox=(200, 200, 900, 850),
)]]
_, bad_rescue_geometry_confirms, _ = run(
    provider_high_ocr_rescue_cfg(), bad_rescue_geometry
)
check("high-OCR rescue retains the measured rear-plate geometry gate",
      len(bad_rescue_geometry_confirms), 0)

short_rescue_fragment = [[read(
    text="P311", det=0.39, ocr=1.00, encounter="h" * 32,
    bbox=(650, 500, 750, 550), vbox=(200, 200, 900, 850),
)]]
_, short_rescue_confirms, _ = run(
    provider_high_ocr_rescue_cfg(), short_rescue_fragment
)
check("high-OCR rescue rejects short fragments",
      len(short_rescue_confirms), 0)

print("\n=== 4-char junk read must not become an identity ===")

# 'P311' is a real observed junk read: exactly 4 chars, so it clears the
# NORMALIZATION_SPEC length floor. The confidence gate is what must stop it.
junk = [[read(text="P311", ocr=0.40, det=0.40)] for _ in range(3)]
_, c_junk, _ = run(make_cfg(app_ocr=0.75, app_det=0.50), junk)
check("4-char LOW-confidence read never confirms", len(c_junk), 0)

# Same string at high confidence is allowed through — the length rule is not
# what we rely on, and we are not changing NORMALIZATION_SPEC.
junk_hi = [[read(text="P311", ocr=0.95, det=0.95)] for _ in range(3)]
_, c_junk_hi, _ = run(make_cfg(app_ocr=0.75, app_det=0.50), junk_hi)
check("4-char HIGH-confidence read is allowed", len(c_junk_hi), 1)

# ------------------------------------------------- no sample defaults ------
print("\n=== adapter must not inherit SDK sample defaults ===")

jc = UltimateAlprEngine(make_cfg())._build_json_config()
check("pyramidal_search OFF (sample ships it ON)",
      jc["pyramidal_search_enabled"], False)
check("rectify OFF (sample ships it ON)", jc["recogn_rectify_enabled"], False)
check("recogn_score_type is 'min' (SDK default is median)",
      jc["recogn_score_type"], "min")
check("LPCI off", jc["klass_lpci_enabled"], False)
check("VMMR off", jc["klass_vmmr_enabled"], False)
check("VBSR off", jc["klass_vbsr_enabled"], False)

# ------------------------------------------------- config validation ------
print("\n=== config validation must FAIL LOUD ===")

import json          # noqa: E402
import tempfile      # noqa: E402

from plate_reader.config import ConfigError, load  # noqa: E402

_base = json.loads((pathlib.Path(__file__).resolve().parents[1]
                    / "config.example.json").read_text())
_base.update(device_id="serv-unit-t", api_key="a" * 64, location_id="loc-t")
_base["alpr"].pop("license_token_file", None)   # no token on a dev box
_tmp = pathlib.Path(tempfile.mkdtemp())


def rejects(label, mutate):
    raw = json.loads(json.dumps(_base))
    mutate(raw)
    p = _tmp / "c.json"
    p.write_text(json.dumps(raw))
    try:
        load(p)
        check(label, "accepted", "ConfigError")
    except ConfigError:
        check(label, "ConfigError", "ConfigError")


rejects("roi x outside [0,1] rejected",
        lambda r: r["alpr"].update(roi={"enabled": True, "x": 1.5, "y": 0,
                                        "w": 0.2, "h": 0.2}))
rejects("roi w <= 0 rejected",
        lambda r: r["alpr"].update(roi={"enabled": True, "x": 0, "y": 0,
                                        "w": 0, "h": 0.2}))
rejects("roi overflowing the frame rejected",
        lambda r: r["alpr"].update(roi={"enabled": True, "x": 0.9, "y": 0,
                                        "w": 0.5, "h": 0.2}))
rejects("threshold outside [0,1] rejected",
        lambda r: r["alpr"]["thresholds"].update(app_ocr_min=1.7))
# The token guard is scoped to the engines that actually load the ultimateALPR
# SDK. pr_stream (the shipping default) recognises inside the Stream container
# and is licensed separately, so it must NOT be stranded by a stale token path
# — but the guard must still fire for ultimateALPR, where falling through to
# trial mode emits '*'-redacted plates that hash to confident wrong identities.


def set_ultimate_with_bad_token(raw):
    raw["alpr"].update(
        engine="ultimatealpr", license_token_file="/nope/missing.token"
    )
    # Thresholds are one unit with the engine. Disable PR's explicitly tuned
    # rescue before testing the separate ultimateALPR token guard.
    raw["alpr"]["thresholds"]["consensus_rescue_min_frames"] = 0


rejects("unreadable license_token_file rejected",
        set_ultimate_with_bad_token)
rejects("bad dwell anchor rejected",
        lambda r: r["presence"]["dwell_zone"].update(anchor="sideways"))
rejects("negative cooldown rejected",
        lambda r: r["presence"]["dedup"].update(cooldown_seconds=-1))
rejects("target_fps <= 0 rejected",
        lambda r: r["capture"].update(target_fps=0))

# And the default example config must still LOAD.
p = _tmp / "ok.json"
p.write_text(json.dumps(_base))
try:
    cfg_ok = load(p)
    # Shipping engine is Plate Recognizer Stream as of the cutover; tag
    # v2.1.0-ultimatealpr is the ultimateALPR rollback point.
    check("config.example.json loads", cfg_ok.alpr.engine, "pr_stream")
    check("defaults keep ROI disabled", cfg_ok.alpr.roi.enabled, False)
    check("defaults keep dwell zone disabled", cfg_ok.dwell_zone.enabled, False)
except ConfigError as e:
    check("config.example.json loads", f"ConfigError: {e}", "ok")

print("")
print("-" * 60)
print(f"{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
