"""Opt-in Ivar's window trajectory policy; no OCR or appearance inputs.

Calibration candidate for the 2026-10-28 camera pose. A native track must
prove an approach followed by sustained near-window occupancy. These zones
refer to box bottom-centres, not plate locations or vehicle centroids.
"""
from __future__ import annotations

import math

POLICY = "ivars_window_v1"
ENTRY_ZONE = (0.40, 0.55, 0.95, 0.74)
WINDOW_ZONE = (0.05, 0.65, 0.39, 1.0)
FRAME_SIZE = (2304, 1296)


def unique_continuation(previous, candidates):
    """Resolve duplicate-time boxes by geometry alone, never zone membership.

    Small nested detections must not outrank the whole vehicle: use union IoU
    rather than overlap / smaller area. Require a strong match and a large
    margin; equally plausible vehicles remain ambiguous.
    """
    def iou(box):
        area = (box[2] - box[0]) * (box[3] - box[1])
        prev_area = (previous[2] - previous[0]) * (previous[3] - previous[1])
        intersection = (max(0, min(box[2], previous[2]) - max(box[0], previous[0]))
                        * max(0, min(box[3], previous[3]) - max(box[1], previous[1])))
        return intersection / (area + prev_area - intersection)
    ranked = sorted(((iou(box), index) for index, box in candidates), reverse=True)
    if ranked[0][0] >= 0.50 and ranked[0][0] - ranked[1][0] >= 0.30:
        return ranked[0][1]
    return None


def bottom_center(box, width, height):
    if not box or len(box) != 4 or width <= 0 or height <= 0:
        return None
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in box):
        return None
    x1, y1, x2, y2 = box
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        return None
    return ((x1 + x2) / (2 * width), y2 / height)


def invalid_rows(rows):
    """A suspect native trajectory must not reconcile customer identities."""
    if any(not isinstance(t, (int, float)) or not math.isfinite(t)
           or bottom_center(b, 1, 1) is None for _, t, b in rows):
        return "trajectory_invalid_geometry_or_timing"
    for a, b in zip(rows, rows[1:]):
        if b[1] <= a[1]:
            return "trajectory_nonmonotonic_timing"
        x, y = a[2], b[2]
        if (b[1] - a[1] > 3.0
                or min(x[2], y[2]) <= max(x[0], y[0])
                or min(x[3], y[3]) <= max(x[1], y[1])):
            return "trajectory_discontinuous"
    return None


def classify(rows):
    """Return (status, reason, original window-start index).

    Rows are (original index, finite epoch, normalized box). The adapter must
    resolve coincident samples conservatively before calling. No evidence is
    joined across native tracks, missing timestamps, gaps, or disjoint boxes.
    This proves a physical route, not a purchase or a completed transaction.
    """
    if len(rows) < 8:
        return "rejected", "trajectory_insufficient_points", None
    invalid = invalid_rows(rows)
    if invalid:
        return "rejected", invalid, None

    def inside(p, z):
        return z[0] <= p[0] <= z[2] and z[1] <= p[1] <= z[3]

    entry_run = []
    entry = None
    window = []
    saw_entry = False
    for index, epoch, box in rows:
        point = bottom_center(box, 1, 1)
        size = box[2] - box[0]
        if inside(point, ENTRY_ZONE) and size >= 0.18:
            entry_run.append((index, epoch, point))
            if len(entry_run) >= 2 and entry is None:
                entry = entry_run[-2]
                saw_entry = True
        else:
            entry_run = []
        # Tall vehicles beside the window have a higher visible bottom edge
        # than sedans. Require foreground height as well as width; lowering
        # the bottom threshold alone would admit distant broadside trucks.
        if (entry is not None and inside(point, WINDOW_ZONE) and size >= 0.30
                and box[3] - box[1] >= 0.48):
            window.append((index, epoch, point))
            if (len(window) >= 3
                    and epoch - window[0][1] >= 2.0
                    and window[0][2][1] - entry[2][1] >= 0.10
                    and entry[2][0] - window[0][2][0] >= 0.08):
                return "qualified", "approach_then_window_dwell", window[0][0]
        else:
            window = []
    return ("rejected", "window_dwell_not_proven" if saw_entry
            else "window_approach_not_proven", None)
