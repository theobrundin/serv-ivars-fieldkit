#!/usr/bin/env python3
"""Self-tests for the vehicle-record architecture. No SDK, no video, no network.

Run anywhere:  python3 vehicle_records/selftest.py

These cover the properties that are expensive to get wrong:
  * the plate invariant (a hash exists only with CONFIRMED status)
  * the tri-state plate_status, including that it never silently collapses
  * highest-confidence-wins for description, and that it does NOT apply to
    the plate
  * freeze-on-send: nothing changes after transmission
  * each send trigger commits at the right moment
"""
from __future__ import annotations

import pathlib
import sys

_REPO = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from vehicle_records.config import SendTrigger, VehicleRecordConfig  # noqa: E402
from vehicle_records.record import (  # noqa: E402
    PlateStatus, RecordFrozenError, VehicleRecord,
)
from vehicle_records.tracker import VehicleTracker, iou  # noqa: E402

_p = _f = 0
H = "a" * 64          # a syntactically valid SHA-256 hex digest
H2 = "b" * 64


def check(label, got, want):
    global _p, _f
    if got == want:
        _p += 1
        print(f"PASS  {label}  (got={got!r})")
    else:
        _f += 1
        print(f"FAIL  {label}  (got={got!r} want={want!r})")


def raises(label, fn, exc):
    global _p, _f
    try:
        fn()
    except exc:
        _p += 1
        print(f"PASS  {label}  (raised {exc.__name__})")
        return
    except Exception as e:
        _f += 1
        print(f"FAIL  {label}  (raised {type(e).__name__}, wanted {exc.__name__})")
        return
    _f += 1
    print(f"FAIL  {label}  (did not raise {exc.__name__})")


# ------------------------------------------------ the plate invariant ------
print("\n=== plate invariant: a hash exists ONLY with CONFIRMED status ===")

r = VehicleRecord(record_id="v1", first_seen=0.0)
check("a new record starts with no plate", r.plate_hash, None)
check("...and status no_plate_visible",
      r.plate_status, PlateStatus.NO_PLATE_VISIBLE)
r.validate()

r.confirm_plate(H)
check("confirm_plate sets the hash", r.plate_hash, H)
check("...and the status together", r.plate_status, PlateStatus.CONFIRMED)
r.validate()

# The old design made this unrepresentable by having no field to hash into.
# Here it is unrepresentable because the only setter sets both.
bad = VehicleRecord(record_id="v2", first_seen=0.0)
bad.plate_hash = H                       # forced, bypassing confirm_plate()
raises("a hash without CONFIRMED status fails validate()",
       bad.validate, ValueError)

bad2 = VehicleRecord(record_id="v3", first_seen=0.0)
bad2.plate_status = PlateStatus.CONFIRMED
raises("CONFIRMED status without a hash fails validate()",
       bad2.validate, ValueError)

raises("a malformed hash is refused outright",
       lambda: VehicleRecord(record_id="v4", first_seen=0.0).confirm_plate("nope"),
       ValueError)

# -------------------------------------------------- tri-state status -------
print("\n=== plate_status keeps three outcomes, not two ===")

r = VehicleRecord(record_id="v5", first_seen=0.0)
check("never saw a plate -> no_plate_visible",
      r.plate_status, PlateStatus.NO_PLATE_VISIBLE)
r.note_plate_seen_but_unread()
check("saw a plate, couldn't read it -> low_confidence",
      r.plate_status, PlateStatus.LOW_CONFIDENCE)

# These two describe different failures — a mounting/geometry problem versus
# a model/threshold problem — and must not collapse into each other.
check("the two failure modes are distinct values",
      PlateStatus.NO_PLATE_VISIBLE == PlateStatus.LOW_CONFIDENCE, False)

r2 = VehicleRecord(record_id="v6", first_seen=0.0)
r2.confirm_plate(H)
r2.note_plate_seen_but_unread()
check("a later weak read never demotes a CONFIRMED record",
      r2.plate_status, PlateStatus.CONFIRMED)

# ------------------------------------------- highest confidence wins -------
print("\n=== descriptive fields: highest confidence wins ===")

r = VehicleRecord(record_id="v7", first_seen=0.0)
r.offer_color("silver", 0.60)
check("first reading is taken", r.color, "silver")
r.offer_color("gray", 0.40)
check("a LOWER-confidence reading does not replace it", r.color, "silver")
r.offer_color("gray", 0.90)
check("a HIGHER-confidence reading does replace it", r.color, "gray")
r.offer_color(None, 0.99)
check("a null reading is ignored even at high confidence", r.color, "gray")
check("observations are counted", r._color.observations, 3)

r.offer_make_model("bmw", "4series", 0.5, 0.5)
check("make/model pair is accepted together", (r.make, r.model),
      ("bmw", "4series"))
r.offer_make_model("honda", "civic", 0.99, 0.40)
check("a pair with a weaker model does not manufacture a mixed result",
      (r.make, r.model), ("bmw", "4series"))
r.offer_make_model("honda", "civic", 0.90, 0.90)
check("a stronger pair replaces both fields atomically",
      (r.make, r.model), ("honda", "civic"))

# The plate deliberately does NOT work this way: a single high-confidence
# misread must never become the canonical identity. Confirmation comes only
# from multi-frame exact agreement in the production Detector.
check("VehicleRecord exposes no confidence-based plate setter",
      any(n for n in dir(r) if "offer" in n and "plate" in n), False)

# --------------------------------------------------------- freezing --------
print("\n=== freeze on send: the commit point is final ===")

r = VehicleRecord(record_id="v8", first_seen=0.0)
r.offer_color("black", 0.8)
r.touch(1.0)
r.freeze(now=9.0, trigger="zone_exit")
check("sent_at is stamped", r.sent_at, 9.0)
check("the trigger is recorded", r.send_trigger, "zone_exit")
check("frozen flag set", r.frozen, True)

raises("colour cannot change after send",
       lambda: r.offer_color("white", 0.99), RecordFrozenError)
raises("a plate cannot be attached after send",
       lambda: r.confirm_plate(H), RecordFrozenError)
raises("the record cannot be touched after send",
       lambda: r.touch(10.0), RecordFrozenError)
raises("freezing twice is an error",
       lambda: r.freeze(now=10.0), RecordFrozenError)

# ------------------------------------------------------ serialization ------
print("\n=== wire shape ===")

r = VehicleRecord(record_id="v9", first_seen=0.0)
r.touch(1.0)
r.offer_color("red", 0.9)
r.confirm_plate(H)
r.freeze(now=5.0, trigger="zone_exit")
d = r.to_dict()
for k in ("record_id", "vehicle_detected", "plate_hash", "plate_status",
          "color", "make", "model", "first_seen", "sent_at"):
    check(f"wire shape has {k}", k in d, True)
check("plate_status serializes as its string value",
      d["plate_status"], "confirmed")
check("unset descriptive fields are null, not missing",
      (d["make"], d["model"]), (None, None))
check("vehicle_detected is always true", d["vehicle_detected"], True)
check("diagnostics are nested, not mixed into the contract",
      isinstance(d.get("diagnostics"), dict), True)
import json as _json
_json.dumps(d)      # must be JSON-serializable
check("record is JSON-serializable", True, True)

# ------------------------------------------------------------ tracker ------
print("\n=== tracker: association and send triggers ===")

check("iou of identical boxes is 1", round(iou((0, 0, 10, 10), (0, 0, 10, 10)), 3), 1.0)
check("iou of disjoint boxes is 0", iou((0, 0, 10, 10), (50, 50, 60, 60)), 0.0)

cfg = VehicleRecordConfig(send_trigger=SendTrigger.ZONE_EXIT,
                          min_track_frames=2, track_timeout_seconds=3.0)
tr = VehicleTracker(cfg)
box = (100, 100, 300, 300)
for i in range(4):
    tr.observe_vehicle(box, 0.99, now=float(i), in_zone=True)
check("one vehicle -> one track", tr.active_tracks, 1)
tr.tick(now=4.0)
check("still in zone -> not committed yet", len(list(tr.drain())), 0)

# Leaves the zone, then goes quiet past the timeout.
tr.observe_vehicle(box, 0.99, now=5.0, in_zone=False)
tr.tick(now=9.0)
recs = list(tr.drain())
check("zone exit + timeout -> exactly one record", len(recs), 1)
check("...committed by the zone_exit trigger",
      recs[0].send_trigger in ("zone_exit", "frame_exit"), True)
check("...as a plateless record", recs[0].plate_status,
      PlateStatus.NO_PLATE_VISIBLE)

# min_track_frames suppresses a one-frame blip.
tr2 = VehicleTracker(VehicleRecordConfig(min_track_frames=3))
tr2.observe_vehicle((0, 0, 50, 50), 0.9, now=0.0, in_zone=True)
tr2.flush(now=100.0)
check("a 1-frame blip produces no record", len(list(tr2.drain())), 0)
check("...and is counted as dropped", tr2.stats["dropped_min_frames"], 1)

# INSTANT commits on the first tick.
tr3 = VehicleTracker(VehicleRecordConfig(send_trigger=SendTrigger.INSTANT,
                                         min_track_frames=1))
tr3.observe_vehicle((0, 0, 50, 50), 0.9, now=0.0, in_zone=False)
tr3.tick(now=0.0)
check("INSTANT trigger commits immediately", len(list(tr3.drain())), 1)

# A confirmed plate attaches to the vehicle that saw that exact string.
tr4 = VehicleTracker(VehicleRecordConfig(min_track_frames=1))
a = (0, 0, 100, 100)
b = (500, 500, 600, 600)
tr4.observe_vehicle(a, 0.9, now=0.0, in_zone=True)
tr4.observe_vehicle(b, 0.9, now=0.0, in_zone=True)
tr4.note_plate_box(b, normalized="ABC1234", readable=True)
check("plate attaches to the right vehicle",
      tr4.attach_confirmed_plate("ABC1234", H, now=1.0), True)
check("...by string match, not a guess",
      tr4.stats["plate_attach_by_string"], 1)
tr4.flush(now=100.0)
recs = sorted(tr4.drain(), key=lambda r: r.record_id)
statuses = {r.plate_status for r in recs}
check("two vehicles -> two records", len(recs), 2)
# Vehicle b saw a plate box and confirmed it. Vehicle a never had a plate box
# observed at all, so it is NO_PLATE_VISIBLE rather than LOW_CONFIDENCE —
# exactly the distinction the tri-state exists to preserve. Getting
# LOW_CONFIDENCE here would mean we were blaming the model for a car that
# never showed us a plate.
check("confirmed vehicle + never-saw-a-plate vehicle",
      statuses, {PlateStatus.CONFIRMED, PlateStatus.NO_PLATE_VISIBLE})

# Two observations in one capture may not claim the same prior track.
tr_one = VehicleTracker(VehicleRecordConfig(min_track_frames=1,
                                            iou_match_threshold=0.1))
tr_one.observe_vehicle((0, 0, 100, 100), 0.9, now=0.0, in_zone=True)
tracks = tr_one.observe_vehicles([
    ((0, 0, 90, 100), 0.9, True),
    ((10, 0, 100, 100), 0.9, True),
], now=1.0)
check("one capture assigns at most one observation to each old track",
      len({t.track_id for t in tracks}), 2)

# When two live cars offer no exact or spatial attribution evidence, refusing
# the plate is safer than guessing the most recently seen one.
tr_amb = VehicleTracker(VehicleRecordConfig(min_track_frames=1))
tr_amb.observe_vehicles([
    ((0, 0, 100, 100), 0.9, True),
    ((500, 500, 600, 600), 0.9, True),
], now=0.0)
check("ambiguous plate attribution is refused",
      tr_amb.attach_confirmed_plate("UNKNOWN1", H2, now=1.0), False)
check("ambiguous refusal is observable",
      tr_amb.stats["plate_attach_ambiguous"], 1)

# emit_plateless=False drops unidentified vehicles entirely.
tr5 = VehicleTracker(VehicleRecordConfig(min_track_frames=1,
                                         emit_plateless=False))
tr5.observe_vehicle((0, 0, 50, 50), 0.9, now=0.0, in_zone=True)
tr5.flush(now=100.0)
check("emit_plateless=False suppresses plateless records",
      len(list(tr5.drain())), 0)

# ------------------------------------------------------------- config ------
print("\n=== config ===")
check("default send trigger is zone_exit",
      VehicleRecordConfig().send_trigger, SendTrigger.ZONE_EXIT)
check("make/model default OFF (VMMR is over the frame budget)",
      VehicleRecordConfig().make_model_enabled, False)
check("plateless records are emitted by default",
      VehicleRecordConfig().emit_plateless, True)
raises("an unknown send_trigger is rejected loudly",
       lambda: VehicleRecordConfig.from_dict(
           {"vehicle_records": {"send_trigger": "whenever"}}), ValueError)
raises("quoted false is rejected instead of becoming true",
       lambda: VehicleRecordConfig.from_dict(
           {"vehicle_records": {"emit_plateless": "false"}}), ValueError)
raises("unknown vehicle-record config keys are rejected",
       lambda: VehicleRecordConfig.from_dict(
           {"vehicle_records": {"typo_min_frames": 3}}), ValueError)

print("\n" + "-" * 60)
print(f"{_p} passed, {_f} failed")
sys.exit(1 if _f else 0)
