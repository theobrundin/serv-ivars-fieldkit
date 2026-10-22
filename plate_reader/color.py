"""Vehicle color signature — a *corroborating* signal for presence tracking.

Purpose and scope
-----------------
This is deliberately NOT a color-identity system. Color is fragile as a primary
key (same-color cars merge; lighting shifts hue). We use it only to:

  1. Detect when a single presence track has silently merged TWO vehicles
     (nose-to-tail cars whose plate boxes overlapped enough to associate).
     A sharp color change within one track is strong evidence of a car change.
  2. Annotate drive-off events with a coarse dominant color, purely for
     later hand-labeling / sanity-checking. It is never stored as identity
     and never leaves as anything but a coarse label.

Design choices (each matters):
  * Sample the VEHICLE BODY region (a box above/around the plate), NOT the
    plate itself — plates are white/yellow and carry no vehicle color.
  * Work in HSV and build a coarse HUE histogram. Robust-ish to brightness.
  * Pixels below a saturation floor are "neutral" (white/gray/black/silver) and
    counted in a single neutral bucket rather than assigned a noisy hue. Most
    real cars are neutral-colored, so this bucket does real work.
  * Signatures are small fixed-length vectors; distance is L1 over the
    normalized histogram, in [0, 1]. A caller thresholds it.

Everything here is best-effort and must never raise into the detection loop.
"""
from __future__ import annotations

from typing import Optional

from .log import get

log = get(__name__)

# Number of hue bins across the 0-179 OpenCV hue range.
_HUE_BINS = 12
# Saturation/Value floors below which a pixel is "neutral" (no meaningful hue).
_SAT_FLOOR = 40   # 0-255
_VAL_FLOOR = 25   # 0-255 — near-black pixels are unreliable, drop them
# Signature length: hue bins + one neutral bucket.
SIGNATURE_LEN = _HUE_BINS + 1


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def vehicle_body_roi(
    frame,
    plate_bbox: Optional[tuple[int, int, int, int]],
    vehicle_bbox: Optional[tuple[int, int, int, int]] = None,
):
    """Return a numpy sub-image of the likely vehicle body given a plate box.

    Heuristic: the body is mostly ABOVE and slightly wider than the plate.
    We take a region whose width is ~3x the plate width (centered on the plate)
    and whose height rises from just above the plate up by ~3 plate-heights,
    clamped to the frame. Returns None if inputs are unusable.
    """
    try:
        import numpy as np  # noqa: F401

        if frame is None:
            return None
        h, w = frame.shape[:2]
        if vehicle_bbox is not None:
            vx1, vy1, vx2, vy2 = vehicle_bbox
            vw = max(1, vx2 - vx1)
            vh = max(1, vy2 - vy1)
            # Stay inside the SDK's car box and trim the edges/road. The
            # central body is less contaminated by background than expanding
            # an already-large vehicle box as if it were a plate.
            bx1 = _clamp(vx1 + int(0.10 * vw), 0, w - 1)
            bx2 = _clamp(vx2 - int(0.10 * vw), 0, w)
            by1 = _clamp(vy1 + int(0.15 * vh), 0, h - 1)
            by2 = _clamp(vy2 - int(0.15 * vh), 0, h)
            if bx2 - bx1 < 4 or by2 - by1 < 4:
                return None
            return frame[by1:by2, bx1:bx2]

        if plate_bbox is None:
            return None
        x1, y1, x2, y2 = plate_bbox
        pw = max(1, x2 - x1)
        ph = max(1, y2 - y1)
        cx = (x1 + x2) // 2

        bx1 = _clamp(cx - int(1.5 * pw), 0, w - 1)
        bx2 = _clamp(cx + int(1.5 * pw), 0, w)
        # Body region: from ~3 plate-heights above the plate top, down to the
        # plate top (exclude the plate itself and below-plate bumper shadow).
        by2 = _clamp(y1, 0, h)                       # bottom = plate top
        by1 = _clamp(y1 - 3 * ph, 0, h - 1)          # top
        if bx2 - bx1 < 4 or by2 - by1 < 4:
            return None
        return frame[by1:by2, bx1:bx2]
    except Exception as e:
        log.debug("vehicle_body_roi failed: %s", e)
        return None


def signature(
    frame,
    plate_bbox: Optional[tuple[int, int, int, int]] = None,
    vehicle_bbox: Optional[tuple[int, int, int, int]] = None,
):
    """Compute a coarse HSV color signature for the vehicle body near a plate.

    Returns a list[float] of length SIGNATURE_LEN (normalized, sums to ~1.0),
    or None if a signature can't be computed. Never raises.
    """
    try:
        import cv2
        import numpy as np

        roi = vehicle_body_roi(frame, plate_bbox, vehicle_bbox)
        if roi is None or roi.size == 0:
            return None
        # Downscale for speed and noise reduction.
        roi = cv2.resize(roi, (48, 48), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(roi, cv2.COLOR_RGB2HSV)
        hue = hsv[:, :, 0].astype("int32")
        sat = hsv[:, :, 1].astype("int32")
        val = hsv[:, :, 2].astype("int32")

        hist = [0.0] * SIGNATURE_LEN
        # Drop near-black pixels entirely; they're unreliable.
        valid = val >= _VAL_FLOOR
        neutral = valid & (sat < _SAT_FLOOR)
        colored = valid & (sat >= _SAT_FLOOR)

        # Neutral bucket (last index).
        hist[_HUE_BINS] = float(np.count_nonzero(neutral))

        # Colored pixels → hue bins.
        if np.any(colored):
            hues = hue[colored]
            bin_idx = (hues * _HUE_BINS // 180).clip(0, _HUE_BINS - 1)
            counts = np.bincount(bin_idx, minlength=_HUE_BINS)
            for i in range(_HUE_BINS):
                hist[i] = float(counts[i])

        total = sum(hist)
        if total <= 0:
            return None
        return [h / total for h in hist]
    except Exception as e:
        log.debug("color signature failed: %s", e)
        return None


def _circular_emd(h1: list, h2: list) -> float:
    """1-D Earth Mover's Distance between two normalized histograms on a
    CIRCLE (hue wraps: bin 0 is adjacent to the last bin). Returns a value
    in [0, n/2] roughly; we normalize by the max possible so it lands in [0,1].

    Circularity matters: red sits at both ends of the OpenCV hue range, so a
    plain per-bin L1 would call two nearly-identical reds 'maximally different'
    when one lands in bin 0 and the other in the last bin. EMD on a circle
    treats those bins as neighbors, which is the correct behavior.
    """
    n = len(h1)
    diff = [h1[i] - h2[i] for i in range(n)]
    # Optimal circular EMD = min over rotations of prefix-sum L1. We find the
    # offset that minimizes total flow (Werman's method for circular EMD).
    prefix = []
    acc = 0.0
    for d in diff:
        acc += d
        prefix.append(acc)
    prefix_sorted = sorted(prefix)
    median = prefix_sorted[n // 2]
    emd = sum(abs(p - median) for p in prefix)
    # Max possible EMD on a circle of n bins for prob distributions is n/2.
    return emd / (n / 2.0) if n > 0 else 0.0


def distance(a: Optional[list], b: Optional[list]) -> Optional[float]:
    """Perceptual distance between two color signatures, in [0, 1].

    Handles hue circularity (red wraparound) and the neutral bucket separately:
      * If both signatures are dominated by the neutral bucket, they are
        'both neutral' → distance ~0 (we can't distinguish white/gray/silver,
        so we DON'T fabricate a difference).
      * Otherwise we compare the hue distribution with circular EMD, plus the
        difference in how much mass is neutral vs colored.

    Returns None if either signature is missing.
    """
    if not a or not b or len(a) != len(b):
        return None

    neutral_a, neutral_b = a[-1], b[-1]
    hue_a, hue_b = a[:-1], b[:-1]
    colored_a = sum(hue_a)
    colored_b = sum(hue_b)

    # Component 1: difference in neutral-vs-colored balance (in [0,1]).
    balance_diff = abs(neutral_a - neutral_b)

    # Component 2: hue-shape distance, only meaningful where both have color.
    if colored_a > 1e-6 and colored_b > 1e-6:
        # Renormalize hue parts to compare SHAPE independent of neutral mass.
        na = [x / colored_a for x in hue_a]
        nb = [x / colored_b for x in hue_b]
        hue_dist = _circular_emd(na, nb)
    else:
        # One or both are essentially all-neutral → no hue shape to compare.
        hue_dist = 0.0

    # Weight hue distance by how much colored mass both actually have, so two
    # near-neutral cars don't get a big hue distance from tiny noisy color.
    colored_weight = min(colored_a, colored_b)
    combined = balance_diff * (1 - colored_weight) + hue_dist * colored_weight
    return max(0.0, min(1.0, combined))
