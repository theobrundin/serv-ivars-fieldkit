#!/usr/bin/env python3
"""Tests for episode-level detection-event telemetry.

The motivating failure is the 16:45 LJS event, which reported
`top_detection_confidence=0.8086 / top_ocr_confidence=0.8041` as if one frame
had scored both. It hadn't: 0.8086 came from an `EQDAL` read (det 0.809, ocr
0.701) and 0.8041 from an `EQUAL` read 23 seconds later (det 0.410, ocr
0.804). No frame ever cleared both gates, so the pair described a frame that
never existed and made the episode look one nudge away from confirming.

These tests pin the fix: any pair of numbers the cloud can read as "the best
frame" must come from ONE real frame, and the counts must say plainly how
many frames cleared which gate.

Run: python3 tools/test_episode_telemetry.py
"""
import pathlib
import sys
from dataclasses import dataclass, fields

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from plate_reader.config import (  # noqa: E402
    Alpr, AlprThresholds, Capture, Classifiers, Dedup, DetectionZone,
    Diagnostics, DwellZone, EngineAdvanced, NormBox, Thresholds, VehicleOnly,
)
from plate_reader.detector import (  # noqa: E402
    Detector, FailedIdentification,
)
from plate_reader.engines import RawPlateRead  # noqa: E402

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


# Live LJS gates: per-frame det >= 0.50 / ocr >= 0.75, group floors equal to
# them, vote_min_frames 2 inside a 3.0s window.
def make_cfg(vote_window=3.0, high_ocr=0.85, votes=2):
    th = Thresholds(
        min_detection_confidence=0.50,
        min_ocr_confidence=0.75,
        min_group_confidence=0.50,
        vote_window_seconds=vote_window,
        min_normalized_length=4,
        max_normalized_length=12,
        vote_min_frames=votes,
        high_ocr_single_frame=high_ocr,
        min_group_detection_confidence=0.50,
        min_group_ocr_confidence=0.75,
    )
    return C(
        thresholds=th,
        detection_zone=DetectionZone(False, 0.0, 1.0, 0.0, 1.0),
        alpr=Alpr(engine="ultimate_alpr", roi=NormBox(),
                  thresholds=AlprThresholds(),
                  classifiers=Classifiers(vehicle_detect_enabled=False),
                  engine_advanced=EngineAdvanced()),
        dwell_zone=DwellZone(),
        dedup=Dedup(cooldown_seconds=0.0),
        diagnostics=Diagnostics(),
        capture=Capture(width=1000, height=1000),
        vehicle_only=VehicleOnly(),
    )


class StubEngine:
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


def read(text, det, ocr):
    return RawPlateRead(text=text, detection_confidence=det,
                        ocr_confidence=ocr, bbox=(400, 400, 500, 450),
                        vehicle_bbox=None, vehicle_confidence=None)


T0 = 1_770_000_000.0          # fixed epoch: ISO output is then deterministic


def iso(epoch: float) -> str:
    """Same ISO shape the detector stamps on a capture."""
    import datetime as _dt
    return (
        _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def drive(cfg, script):
    """Run (offset_seconds, [reads]) frames; return FailedIdentification list.

    Supplies an explicit `now` per frame so vote windows, encounter gaps and
    flush silence are exercised deterministically rather than by sleeping.
    """
    d = Detector(cfg, enable_vehicle_only_tracking=False)
    d._engine = StubEngine([reads for _, reads in script])
    failed = []
    for offset, _ in script:
        _, outs = d.process_frame(FakeFrame(), now=T0 + offset)
        failed += [o for o in outs if isinstance(o, FailedIdentification)]
    return d, failed


# `_miss_flush_silence()` is max(vote_window * 2, 3) = 6s at the live window,
# so an episode needs 6+ quiet seconds and no open bucket before it posts.
QUIET = [(t, []) for t in (30.0, 40.0)]


print("\n=== the 16:45 event: two frames peaking on different metrics ===")

# EQDAL clears det, fails ocr. EQUAL (edit distance 1 -> same episode) fails
# det, clears ocr. Independent maxima would report 0.809/0.804.
ljs = [
    (0.0, [read("EQDAL", det=0.8086, ocr=0.7010)]),
    (23.0, [read("EQUAL", det=0.4102, ocr=0.8041)]),
] + QUIET
_, ev = drive(make_cfg(), ljs)
check("the episode posts exactly one detection-event", len(ev), 1)
e = ev[0]

check("legacy top_* still report the misleading independent maxima",
      (e.top_detection_confidence, e.top_ocr_confidence), (0.8086, 0.8041))
# The whole point: this pair must be a frame that actually happened.
check("best_joint_* describe ONE real frame (the det-clearing EQDAL read)",
      (e.best_joint_det, e.best_joint_ocr), (0.8086, 0.701))
check("...and that pair is never the phantom 0.8086/0.8041 blend",
      (e.best_joint_det, e.best_joint_ocr) ==
      (e.top_detection_confidence, e.top_ocr_confidence), False)
check("best frame is flagged as having cleared the det gate",
      e.best_joint_cleared_det, True)
check("one frame cleared det", e.frames_clearing_det, 1)
check("one frame cleared ocr", e.frames_clearing_ocr, 1)
check("NO frame cleared both — which top_* hid", e.frames_clearing_both, 0)
check("reject counts name the gate each frame lost at",
      e.reject_reason_counts,
      {"failed_det_only": 1, "failed_ocr_only": 1, "failed_both": 0})
check("no frame was ever admitted to a vote", e.frames_admitted, 0)
check("no vote bucket ever formed", e.max_bucket_size, 0)
check("reads_in_episode counts both reads", e.reads_in_episode, 2)
check("frame_count stays an alias of reads_in_episode",
      e.frame_count, e.reads_in_episode)

print("\n=== below_threshold keeps its own outcome string ===")

# It answers a different question than low_confidence ('the image was too
# weak' vs 'the vote timed out'), so it must never be folded into it.
check("outcome is exactly 'below_threshold'", e.outcome, "below_threshold")

print("\n=== when nothing clears det, say so instead of implying it ===")

no_det = [
    (0.0, [read("EQUAL", det=0.4102, ocr=0.8041)]),
    (1.0, [read("EQUAL", det=0.3000, ocr=0.6000)]),
] + QUIET
_, ev2 = drive(make_cfg(), no_det)
check("one event", len(ev2), 1)
e2 = ev2[0]
check("falls back to the highest-OCR frame overall",
      (e2.best_joint_det, e2.best_joint_ocr), (0.4102, 0.8041))
check("...explicitly flagged as never having cleared det",
      e2.best_joint_cleared_det, False)
check("frames clearing det is zero", e2.frames_clearing_det, 0)
check("both-gate failures are counted separately from single-gate ones",
      e2.reject_reason_counts,
      {"failed_det_only": 1, "failed_ocr_only": 0, "failed_both": 1})

print("\n=== admitted frames: expired vote != weak image ===")

# Two agreeing reads that clear BOTH per-frame gates but arrive too far apart
# to share a 1.0s vote window: each opens a bucket that expires alone.
admitted = [
    (0.0, [read("BXV8862", det=0.6700, ocr=0.7800)]),
    (5.0, [read("BXV8862", det=0.6600, ocr=0.7900)]),
] + QUIET
_, ev3 = drive(make_cfg(vote_window=1.0, high_ocr=0.0), admitted)
check("one event", len(ev3), 1)
e3 = ev3[0]
check("outcome is the vote-timing signal, not the image-quality one",
      e3.outcome, "low_confidence")
check("both frames are reported as admitted", e3.frames_admitted, 2)
check("largest single vote held one frame", e3.max_bucket_size, 1)
check("both frames cleared both gates", e3.frames_clearing_both, 2)
check("no frame is blamed on a gate it passed",
      e3.reject_reason_counts,
      {"failed_det_only": 0, "failed_ocr_only": 0, "failed_both": 0})
check("best_joint is a real admitted frame's own pair",
      (e3.best_joint_det, e3.best_joint_ocr), (0.66, 0.79))
check("frames_admitted is distinguishable from reads_in_episode",
      (e3.frames_admitted, e3.reads_in_episode), (2, 2))

print("\n=== max_bucket_size reports real agreement ===")

# Three agreeing frames inside one window, held below the group floor so the
# bucket expires instead of confirming.
agreed = [
    (0.0, [read("BXV8862", det=0.6700, ocr=0.7800)]),
    (0.4, [read("BXV8862", det=0.6800, ocr=0.7700)]),
    (0.8, [read("BXV8862", det=0.6600, ocr=0.7600)]),
] + QUIET
cfg_floor = make_cfg(vote_window=1.5, high_ocr=0.0)
cfg_floor.thresholds.min_group_ocr_confidence = 0.90   # unreachable on purpose
_, ev4 = drive(cfg_floor, agreed)
check("one event", len(ev4), 1)
e4 = ev4[0]
check("outcome names the group floor, not the reads",
      e4.outcome, "group_confidence_floor")
check("max_bucket_size reports the 3 agreeing frames", e4.max_bucket_size, 3)
check("all 3 were admitted", e4.frames_admitted, 3)

print("\n=== episode identity ===")

check("episode_id is present and uuid-shaped", len(e.episode_id), 32)
check("episode_start mirrors detected_at_iso (first sighting, unchanged)",
      e.episode_start, e.detected_at_iso)
check("detectedAt is still the FIRST sighting, not the flush time (t=0, "
      "though the episode flushed at t=30)",
      e.detected_at_iso, iso(T0))
check("episode_end is the LAST sighting, 23s later",
      e.episode_end, iso(T0 + 23.0))
check("episode_end is not the same instant as episode_start",
      e.episode_end == e.episode_start, False)
check("two separate episodes get distinct ids",
      e.episode_id == e3.episode_id, False)

print("\n=== additive and null-safe ===")

# The cloud must degrade gracefully: an event built the old way (positional
# legacy fields only) still constructs, with every new field defaulted.
legacy = FailedIdentification(
    outcome="low_confidence",
    detected_at_iso="2026-08-10T14:07:07.626Z",
    frame_count=1,
    top_detection_confidence=0.9961,
    top_ocr_confidence=0.8924,
    inference_duration_ms=59,
)
check("legacy construction still works", legacy.outcome, "low_confidence")
check("new numeric fields default to 0, never None",
      [getattr(legacy, f.name) for f in fields(legacy)
       if f.name.startswith(("frames_", "reads_", "max_", "best_joint_d",
                             "best_joint_o"))],
      [0.0, 0.0, 0, 0, 0, 0, 0, 0])
check("reject_reason_counts defaults to an empty dict, not None",
      legacy.reject_reason_counts, {})
check("episode fields default to empty strings, not None",
      (legacy.episode_id, legacy.episode_start, legacy.episode_end),
      ("", "", ""))
check("each FailedIdentification gets its own reject_reason_counts dict",
      legacy.reject_reason_counts is FailedIdentification(
          outcome="x", detected_at_iso="", frame_count=0,
          top_detection_confidence=0.0, top_ocr_confidence=0.0,
          inference_duration_ms=0).reject_reason_counts,
      False)

print("\n=== confirm behaviour is untouched ===")

# The same two agreeing frames INSIDE one window must still confirm, with no
# detection-event emitted. If this breaks, the telemetry change leaked into
# the identify path.
from plate_reader.detector import ConfirmedIdentification  # noqa: E402

d5 = Detector(make_cfg(high_ocr=0.0), enable_vehicle_only_tracking=False)
d5._engine = StubEngine([[read("BXV8862", det=0.67, ocr=0.78)],
                         [read("BXV8862", det=0.67, ocr=0.78)]])
confirms = []
for i in range(2):
    _, outs = d5.process_frame(FakeFrame(), now=T0 + i * 0.4)
    confirms += [o for o in outs if isinstance(o, ConfirmedIdentification)]
check("two agreeing in-window frames still confirm", len(confirms), 1)
check("...and the confirm still reports frames in the vote",
      confirms[0].frame_count, 2)

# The 0.85 fast path still confirms on ONE frame (the 14:07 shape).
d6 = Detector(make_cfg(), enable_vehicle_only_tracking=False)
d6._engine = StubEngine([[read("BXV8862", det=0.9961, ocr=0.8924)]])
_, outs6 = d6.process_frame(FakeFrame(), now=T0)
check("a single ocr 0.8924 read confirms via high_ocr_single_frame",
      len([o for o in outs6 if isinstance(o, ConfirmedIdentification)]), 1)

print("")
print("-" * 60)
print(f"{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
