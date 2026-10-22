"""Association diagnostics for the presence tracker.

Answers ONE question with real numbers, from real footage:
  "As a car drives off and its plate shrinks, does IoU association break —
   and would center-distance association have held the track together?"

It does this by watching the sequence of plate boxes the detector emits and,
for each consecutive pair that plausibly belong to the same car (close in
time), computing:

  - plate size (px, the smaller dimension is what OCR cares about)
  - size-change ratio frame-to-frame (shrinking as car recedes)
  - center displacement (px)
  - IoU of the two boxes  ......... what the tracker uses today
  - normalized center distance ... what a scale-robust matcher would use

Then it reports how often IoU would have DROPPED the association (below the
configured iou_match_threshold) while center-distance would have KEPT it.
That fraction is the size of your fast-motion / shrinking-plate problem.

This is measurement only. It changes no tracking behavior.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter == 0:
        return 0.0
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _center(b):
    return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)


def _diag(b):
    w = max(1.0, b[2] - b[0])
    h = max(1.0, b[3] - b[1])
    return (w * w + h * h) ** 0.5


def center_distance_norm(a, b) -> float:
    """Center distance normalized by average box diagonal. Scale-robust:
    a shrinking plate that stays roughly centered stays 'close' even as IoU
    collapses. Lower = more likely the same object. ~0 identical; >1 far."""
    ca, cb = _center(a), _center(b)
    d = ((ca[0] - cb[0]) ** 2 + (ca[1] - cb[1]) ** 2) ** 0.5
    scale = 0.5 * (_diag(a) + _diag(b))
    return d / scale if scale > 0 else 999.0


@dataclass
class AssocStats:
    iou_threshold: float
    # center-distance threshold to count as "would associate". 0.5 means the
    # centers are within half a box-diagonal — a reasonable same-object bar.
    center_threshold: float = 0.5
    pairs: int = 0
    iou_would_drop: int = 0                 # IoU below threshold
    iou_drop_but_center_ok: int = 0         # IoU dropped, center would hold
    both_drop: int = 0                      # neither would associate
    shrinking_pairs: int = 0                # size ratio < 0.9 (receding)
    iou_drop_while_shrinking: int = 0       # the core failure mode
    min_plate_px: float = 1e9
    max_plate_px: float = 0.0
    samples: list = field(default_factory=list)  # (size, ratio, disp, iou, cd)

    def observe_pair(self, prev_box, box) -> None:
        self.pairs += 1
        i = iou(prev_box, box)
        cd = center_distance_norm(prev_box, box)

        prev_small = min(prev_box[2] - prev_box[0], prev_box[3] - prev_box[1])
        cur_small = min(box[2] - box[0], box[3] - box[1])
        cur_small = max(1.0, cur_small)
        ratio = cur_small / max(1.0, prev_small)   # <1 shrinking, >1 growing
        disp = (
            (_center(prev_box)[0] - _center(box)[0]) ** 2
            + (_center(prev_box)[1] - _center(box)[1]) ** 2
        ) ** 0.5

        self.min_plate_px = min(self.min_plate_px, cur_small)
        self.max_plate_px = max(self.max_plate_px, cur_small)

        iou_drop = i < self.iou_threshold
        center_ok = cd <= self.center_threshold
        if iou_drop:
            self.iou_would_drop += 1
            if center_ok:
                self.iou_drop_but_center_ok += 1
            else:
                self.both_drop += 1
        if ratio < 0.9:
            self.shrinking_pairs += 1
            if iou_drop:
                self.iou_drop_while_shrinking += 1

        # keep a light sample for the printed table (cap memory)
        if len(self.samples) < 2000:
            self.samples.append((round(cur_small, 1), round(ratio, 2),
                                 round(disp, 1), round(i, 3), round(cd, 3)))

    def report(self) -> str:
        if self.pairs == 0:
            return "No consecutive box pairs observed — no plates tracked."
        pct = lambda n: f"{100.0 * n / self.pairs:5.1f}%"
        lines = []
        lines.append("ASSOCIATION DIAGNOSTIC (IoU vs shrinking plate)")
        lines.append(f"  consecutive box pairs analyzed : {self.pairs}")
        lines.append(f"  plate size range (px, short side): "
                     f"{self.min_plate_px:.0f} .. {self.max_plate_px:.0f}")
        lines.append(f"  pairs IoU would DROP (< {self.iou_threshold}) : "
                     f"{self.iou_would_drop} ({pct(self.iou_would_drop)})")
        lines.append(f"    ...of those, center-distance would HOLD : "
                     f"{self.iou_drop_but_center_ok} "
                     f"({pct(self.iou_drop_but_center_ok)} of all pairs)")
        lines.append(f"    ...neither would associate (real gap)   : "
                     f"{self.both_drop} ({pct(self.both_drop)})")
        if self.shrinking_pairs:
            sp = self.shrinking_pairs
            frac = 100.0 * self.iou_drop_while_shrinking / sp
            lines.append(f"  shrinking pairs (car receding)          : {sp}")
            lines.append(f"    ...where IoU dropped                  : "
                         f"{self.iou_drop_while_shrinking} ({frac:4.1f}% of shrinking)")
        lines.append("")
        # Verdict
        fixable = self.iou_drop_but_center_ok
        if self.iou_would_drop == 0:
            lines.append("  VERDICT: IoU held on every pair. No tracking-association")
            lines.append("           problem in this footage. Ship IoU as-is.")
        elif fixable >= max(1, int(0.5 * self.iou_would_drop)):
            lines.append("  VERDICT: IoU is breaking, and center-distance matching")
            lines.append("           would fix MOST of it. Switch association to")
            lines.append("           center-distance (scale-robust). High-value change.")
        else:
            lines.append("  VERDICT: IoU is breaking and center-distance alone would")
            lines.append("           NOT rescue most cases — the boxes are genuinely")
            lines.append("           far apart frame-to-frame. Raise capture FPS first")
            lines.append("           (less motion/shrink per frame), then re-measure.")
        return "\n".join(lines)
