"""End-to-end dwell suppression test against the REAL failure from footage.

Reproduces the observed case: plate 7073c4b0d503 confirmed 11 times in ~5.5s
while one car sat in the queue. Drives the actual Detector.process_frame vote
path (with inference stubbed) and asserts we now get 1 countable visit instead
of 11.
"""
import sys
import pathlib
import time
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from plate_reader.detector import Detector, ConfirmedIdentification, DetectionResult  # noqa: E402


# Build the config from the REAL dataclasses rather than hand-rolled stubs.
#
# This test used to define its own `T` / `Z` / `C` stand-ins, which silently
# drifted from plate_reader.config: when `vote_min_frames` was added to the
# real Thresholds, the stub didn't grow it and process_frame() started dying
# with AttributeError on the BASELINE commit (see docs/KNOWN_ISSUES.md #1).
# Using the real types means any future field arrives here with its default
# and this whole class of breakage cannot recur.
from plate_reader.config import (  # noqa: E402
    Thresholds, DetectionZone, Alpr, DwellZone, Dedup, Diagnostics, Capture,
)


def T(**over):
    """Real Thresholds, with this test's looser gates."""
    base = dict(
        min_detection_confidence=0.5,
        min_ocr_confidence=0.5,
        min_group_confidence=0.6,
        vote_window_seconds=2.0,
        min_normalized_length=2,
        max_normalized_length=10,
        dwell_suppress_seconds=60.0,
        dwell_release_after_other_vehicles=1,
        dwell_release_requires_absent_frames=3,
    )
    base.update(over)
    return Thresholds(**base)


def Z(**over):
    base = dict(enabled=False, x_min_pct=0.0, x_max_pct=1.0,
                y_min_pct=0.0, y_max_pct=1.0)
    base.update(over)
    return DetectionZone(**base)


@dataclass
class C:
    """Minimal Config-shaped object carrying every block Detector touches."""
    thresholds: object
    detection_zone: object
    alpr: object = None
    dwell_zone: object = None
    dedup: object = None
    diagnostics: object = None
    capture: object = None

    def __post_init__(self):
        # Defaults mirror a fresh config: every new feature disabled, so this
        # test exercises the same behaviour it always did.
        if self.alpr is None:
            self.alpr = Alpr()
        if self.dwell_zone is None:
            self.dwell_zone = DwellZone()
        if self.dedup is None:
            # cooldown 0 => the legacy dwell path is what's under test here.
            self.dedup = Dedup(cooldown_seconds=0.0)
        if self.diagnostics is None:
            self.diagnostics = Diagnostics()
        if self.capture is None:
            self.capture = Capture()


def make_det(plate, epoch, bbox=(100, 100, 200, 140)):
    return DetectionResult(
        raw_text=plate,
        normalized=plate,
        detection_confidence=0.85,
        ocr_confidence=0.98,
        bbox=bbox,
        inference_ms=20,
        captured_at_epoch=epoch,
        captured_at_iso="2026-07-24T14:00:00.000Z",
        in_zone=True,
        below_threshold=False,
    )


class StubDetector(Detector):
    """Detector with inference stubbed — we inject detections directly."""
    def __init__(self, cfg):
        super().__init__(cfg)
        self._injected = []

    def set_frame(self, dets):
        self._injected = dets

    def _run_one(self, frame):
        return self._injected


cfg = C(thresholds=T(), detection_zone=Z())
d = StubDetector(cfg)

print("=== SCENARIO: one car dwelling in queue, plate read every frame ===")
print("(reproduces observed 7073c4b0d503 confirming 11x in real footage)\n")

countable = 0
suppressed = 0
base = time.time()

# 22 frames of the SAME plate continuously visible — mirrors a car sitting
# at the order point being read over and over.
for i in range(22):
    d.set_frame([make_det("PLATE1", base + i * 0.2)])
    _, outcomes = d.process_frame(frame=None)
    for o in outcomes:
        if isinstance(o, ConfirmedIdentification):
            if o.suppressed:
                suppressed += 1
            else:
                countable += 1

print(f"countable confirms : {countable}")
print(f"suppressed re-reads: {suppressed}")
print()

ok1 = countable == 1
print(f"{'PASS' if ok1 else 'FAIL'}  exactly 1 countable visit (got {countable})")
ok2 = suppressed > 0
print(f"{'PASS' if ok2 else 'FAIL'}  re-reads were flagged, not dropped (got {suppressed})")

# --- Now the car LEAVES and a genuinely different car comes through ---
print("\n=== SCENARIO: car leaves, another car passes, first car returns ===")
for i in range(5):  # plate absent
    d.set_frame([])
    d.process_frame(frame=None)

d.note_vehicle_departed("PLATE2")  # a different vehicle finished passing

countable2 = 0
for i in range(4):
    d.set_frame([make_det("PLATE1", base + 100 + i * 0.2)])
    _, outcomes = d.process_frame(frame=None)
    for o in outcomes:
        if isinstance(o, ConfirmedIdentification) and not o.suppressed:
            countable2 += 1

ok3 = countable2 >= 1
print(f"{'PASS' if ok3 else 'FAIL'}  returning car counts as a NEW visit (got {countable2})")

print()
if ok1 and ok2 and ok3:
    print("All end-to-end dwell tests passed.")
else:
    print("FAILURES PRESENT")
    sys.exit(1)
