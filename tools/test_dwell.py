"""Tests for dwell suppression (Problem 1, option "c").

Exercises _DwellRecord.can_release directly with a fake thresholds object,
so the semantics are verified without needing camera/alpr/cv2.

The rule under test:
  A re-confirm is allowed only if the plate has been ABSENT for N consecutive
  frames AND (enough time elapsed OR other vehicles passed).
  Absence is a hard gate — it is required for BOTH release paths.
"""
import sys
import pathlib
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from plate_reader.detector import _DwellRecord  # noqa: E402


@dataclass
class FakeThresholds:
    dwell_suppress_seconds: float = 60.0
    dwell_release_after_other_vehicles: int = 1
    dwell_release_requires_absent_frames: int = 3


T = FakeThresholds()
results = []


def check(name, got, want):
    ok = got == want
    results.append((name, ok, got, want))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  (got={got} want={want})")


# --- 1. Car still in frame, no time passed → suppressed ---
r = _DwellRecord("ABC123", first_confirm_epoch=1000.0, last_confirm_epoch=1000.0)
r.absent_frames = 0
ok, _ = r.can_release(1002.0, T)
check("still in frame, 2s later -> suppressed", ok, False)

# --- 2. Car STILL in frame after 90s (dwell elapsed) -> STILL suppressed ---
# This is the core of option (c): time alone must NOT release a car that
# never left. A car idling 90s at the order point is not two visits.
r = _DwellRecord("ABC123", first_confirm_epoch=1000.0, last_confirm_epoch=1000.0)
r.absent_frames = 0
ok, _ = r.can_release(1090.0, T)
check("in frame 90s (>60s dwell) -> STILL suppressed", ok, False)

# --- 3. Car still in frame, 5 other vehicles passed -> STILL suppressed ---
# Also core to (c): other traffic must not release a car that never left.
r = _DwellRecord("ABC123", first_confirm_epoch=1000.0, last_confirm_epoch=1000.0)
r.absent_frames = 0
r.other_vehicles_since = 5
ok, _ = r.can_release(1010.0, T)
check("in frame + 5 others passed -> STILL suppressed", ok, False)

# --- 4. Car LEFT (absent 3 frames) and 60s elapsed -> released ---
r = _DwellRecord("ABC123", first_confirm_epoch=1000.0, last_confirm_epoch=1000.0)
r.absent_frames = 3
r.left_frame_since_confirm = True
ok, _ = r.can_release(1061.0, T)
check("absent 3 frames + 61s elapsed -> RELEASED", ok, True)

# --- 5. Car LEFT and 1 other vehicle passed, only 10s elapsed -> released ---
# The slow-queue case: don't make a genuine revisit wait out a fixed timer
# when the lane has obviously cycled.
r = _DwellRecord("ABC123", first_confirm_epoch=1000.0, last_confirm_epoch=1000.0)
r.absent_frames = 3
r.left_frame_since_confirm = True
r.other_vehicles_since = 1
ok, _ = r.can_release(1010.0, T)
check("absent + 1 other passed, 10s -> RELEASED early", ok, True)

# --- 6. Car left only 1 frame (< 3 needed), 90s elapsed -> suppressed ---
# Guards against a single blurry/missed frame being read as "the car left".
r = _DwellRecord("ABC123", first_confirm_epoch=1000.0, last_confirm_epoch=1000.0)
r.absent_frames = 1
ok, _ = r.can_release(1090.0, T)
check("absent only 1 frame + 90s -> suppressed (flicker guard)", ok, False)

# --- 7. mark_seen resets absence ---
r = _DwellRecord("ABC123", first_confirm_epoch=1000.0, last_confirm_epoch=1000.0)
r.absent_frames = 2
r.mark_seen()
check("mark_seen resets absent_frames", r.absent_frames, 0)

# --- 8. mark_absent accumulates ---
r = _DwellRecord("ABC123", first_confirm_epoch=1000.0, last_confirm_epoch=1000.0)
r.mark_absent(); r.mark_absent(); r.mark_absent()
check("mark_absent accumulates to 3", r.absent_frames, 3)

# --- 9. early-release disabled (0 others required) still honors time path ---
T0 = FakeThresholds(dwell_release_after_other_vehicles=0)
r = _DwellRecord("ABC123", first_confirm_epoch=1000.0, last_confirm_epoch=1000.0)
r.absent_frames = 3
r.left_frame_since_confirm = True
r.other_vehicles_since = 99
ok, _ = r.can_release(1010.0, T0)
check("others-release disabled -> 10s not enough", ok, False)
ok, _ = r.can_release(1061.0, T0)
check("others-release disabled -> 61s releases", ok, True)

# --- 10. The emitted string is the exact bucket string ---
#         Bucketing is exact-match, so the hashed identity is unambiguous.
from plate_reader.detector import (  # noqa: E402
    _VoteBucket, DetectionResult, _to_scalar_confidence,
)


def _mk(norm, det, ocr):
    return DetectionResult(
        raw_text=norm, normalized=norm, detection_confidence=det,
        ocr_confidence=ocr, bbox=(0, 0, 10, 10), inference_ms=1,
        captured_at_epoch=0.0, captured_at_iso="x", in_zone=True,
        below_threshold=False,
    )


b = _VoteBucket(normalized="TEST1234", first_seen_epoch=0.0, first_seen_iso="x")
b.add(_mk("TEST1234", 0.62, 0.97))
b.add(_mk("TEST1234", 0.68, 0.98))
check("emits the exact bucket string", b.best_normalized(), "TEST1234")

# --- 11. OCR confidence is the MIN per character, never the mean ---
# This is what stops a single misread character from riding a high average
# past the gate. Synthetic case: "TESTI234" scores mean 0.971 but min 0.794;
# the expected fixture is "TEST1234".
check("per-char confidence uses MIN not mean",
      round(_to_scalar_confidence([1.0, 1.0, 1.0, 1.0, 0.79, 1.0, 0.98, 0.94]), 3),
      0.79)
check("scalar confidence passes through",
      _to_scalar_confidence(0.93), 0.93)
check("empty confidence list is untrusted",
      _to_scalar_confidence([]), 0.0)

print()
failed = [r for r in results if not r[1]]
if failed:
    print(f"{len(failed)} TEST(S) FAILED")
    sys.exit(1)
print(f"All {len(results)} dwell suppression tests passed.")
