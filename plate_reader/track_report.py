"""Track instrumentation — makes tracking decisions auditable.

The problem this solves: when a run reports 21 drive-offs for 8 real cars,
the console output tells you WHAT happened but not WHY. There's no way to ask
"which track was this, why did it not associate with the previous box, how
close did it come to matching?" — so tuning is guesswork.

This module records every association decision (win AND loss, with costs) and
every track state change, then reports them as a per-track table plus an
optional CSV for offline analysis.

Recording is OFF unless a recorder is attached, so production Pi runs pay
nothing. The harness attaches one; main.py does not.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class AssocAttempt:
    """One association decision for one detection box in one frame."""
    frame: int
    bbox: tuple[int, int, int, int]
    chosen_track: Optional[int]        # None => started a new track
    chosen_iou: float
    runner_up_track: Optional[int]     # next best candidate, if any
    runner_up_iou: float
    threshold: float
    reason: str                        # "matched" | "below_threshold" | "no_tracks"
    plate_w: float
    plate_h: float
    # v2 motion-model fields. For method="motion_model", `cost` is the blended
    # association cost (lower = better) and `cost_threshold` its gate; the
    # iou fields remain populated as reference signal.
    cost: float = 0.0
    runner_up_cost: float = 0.0
    cost_threshold: float = 0.0
    method: str = "iou"

    @property
    def margin(self) -> float:
        """How much better the winner was than the runner-up. A small margin
        means the association was nearly ambiguous — those are the cases most
        likely to be wrong when two cars are nose-to-tail."""
        return round(self.chosen_iou - self.runner_up_iou, 4)


@dataclass
class TrackEvent:
    frame: int
    track_id: int
    event: str          # "born" | "reinforced" | "identified" | "split" | "reaped"
    detail: str = ""
    bbox: Optional[tuple[int, int, int, int]] = None


@dataclass
class TrackSummary:
    track_id: int
    born_frame: int
    last_frame: int
    frames: int
    outcome: str                 # "identified" | "drive_off" | "open"
    had_plate: bool
    top_det: float
    top_ocr: float
    duration_s: float
    color: Optional[str]
    split_from_color: bool
    min_plate_px: float
    max_plate_px: float
    assoc_attempts: int
    assoc_near_misses: int       # attempts that failed but were close to the bar


class TrackRecorder:
    """Collects instrumentation. Attach to a PresenceTracker to enable."""

    def __init__(self, near_miss_band: float = 0.15) -> None:
        # An association that failed but came within this much of the threshold
        # is a "near miss" — the single most useful signal for whether the
        # threshold is set wrong vs. the tracking approach being wrong.
        self.near_miss_band = near_miss_band
        self.attempts: list[AssocAttempt] = []
        self.events: list[TrackEvent] = []
        self.frame = 0

    def set_frame(self, n: int) -> None:
        self.frame = n

    def record_assoc(self, **kw) -> None:
        self.attempts.append(AssocAttempt(frame=self.frame, **kw))

    def record_event(self, track_id: int, event: str, detail: str = "",
                     bbox=None) -> None:
        self.events.append(
            TrackEvent(frame=self.frame, track_id=track_id, event=event,
                       detail=detail, bbox=bbox)
        )

    # ---- analysis ----

    def near_misses(self) -> list[AssocAttempt]:
        """Failed associations that were close to the threshold. If these are
        numerous, the threshold is likely too strict for real motion — a
        tuning fix. If they're rare but tracks still fragment, the problem is
        the association METHOD (IoU), not its threshold."""
        out = []
        for a in self.attempts:
            if a.chosen_track is not None:
                continue
            if a.method == "motion_model":
                # Near-miss for a cost gate: failed, but cost within 30% above
                # the threshold (there was a real candidate, just not close
                # enough).
                if a.cost > 0 and a.cost_threshold > 0 and \
                        a.cost <= a.cost_threshold * 1.3:
                    out.append(a)
            else:
                if a.runner_up_iou > 0 and \
                        (a.threshold - a.runner_up_iou) <= self.near_miss_band:
                    out.append(a)
        return out

    def ambiguous(self, margin_thresh: float = 0.1) -> list[AssocAttempt]:
        """Associations that WON but only barely beat the runner-up. These are
        the identity-swap risks: two cars close together, either could have
        claimed the box."""
        out = []
        for a in self.attempts:
            if a.chosen_track is None or a.runner_up_track is None:
                continue
            if a.method == "motion_model":
                # Won, but the runner-up's cost was nearly as good.
                if (a.runner_up_cost - a.cost) <= (a.cost_threshold * 0.15):
                    out.append(a)
            elif a.margin <= margin_thresh:
                out.append(a)
        return out

    def write_csv(self, path: str) -> tuple[str, str]:
        """Writes attempts and events to CSVs for offline analysis."""
        apath = path.replace(".csv", "") + "_assoc.csv"
        epath = path.replace(".csv", "") + "_events.csv"

        with open(apath, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "frame", "bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2",
                "chosen_track", "chosen_iou", "runner_up_track",
                "runner_up_iou", "margin", "threshold", "reason",
                "plate_w", "plate_h",
                "cost", "runner_up_cost", "cost_threshold", "method",
            ])
            for a in self.attempts:
                w.writerow([
                    a.frame, *a.bbox, a.chosen_track, a.chosen_iou,
                    a.runner_up_track, a.runner_up_iou, a.margin,
                    a.threshold, a.reason, a.plate_w, a.plate_h,
                    a.cost, a.runner_up_cost, a.cost_threshold, a.method,
                ])

        with open(epath, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["frame", "track_id", "event", "detail", "bbox"])
            for e in self.events:
                w.writerow([e.frame, e.track_id, e.event, e.detail, e.bbox])

        return apath, epath

    def report(self, summaries: list[TrackSummary]) -> str:
        lines = []
        lines.append("")
        lines.append("=" * 78)
        lines.append("PER-TRACK REPORT")
        lines.append("=" * 78)

        if not summaries:
            lines.append("  (no tracks recorded)")
        else:
            hdr = (f"{'id':>4} {'born':>6} {'last':>6} {'frms':>5} "
                   f"{'outcome':<11} {'plate':<6} {'det':>5} {'ocr':>5} "
                   f"{'dur_s':>6} {'px_min':>7} {'px_max':>7} {'color':<8} {'assoc':>6}")
            lines.append(hdr)
            lines.append("-" * len(hdr))
            for s in summaries:
                lines.append(
                    f"{s.track_id:>4} {s.born_frame:>6} {s.last_frame:>6} "
                    f"{s.frames:>5} {s.outcome:<11} "
                    f"{'yes' if s.had_plate else 'no':<6} "
                    f"{s.top_det:>5.2f} {s.top_ocr:>5.2f} {s.duration_s:>6.1f} "
                    f"{s.min_plate_px:>7.0f} {s.max_plate_px:>7.0f} "
                    f"{(s.color or '-'):<8} {s.assoc_attempts:>6}"
                )

        # --- association health ---
        lines.append("")
        lines.append("ASSOCIATION HEALTH")
        total = len(self.attempts)
        matched = sum(1 for a in self.attempts if a.chosen_track is not None)
        new_tracks = total - matched
        nm = self.near_misses()
        amb = self.ambiguous()

        lines.append(f"  association attempts        : {total}")
        if total:
            lines.append(
                f"  matched an existing track   : {matched} "
                f"({100.0*matched/total:.1f}%)"
            )
            lines.append(
                f"  started a NEW track         : {new_tracks} "
                f"({100.0*new_tracks/total:.1f}%)"
            )
        lines.append(
            f"  near-miss failures          : {len(nm)}  "
            f"(failed, but within {self.near_miss_band} of threshold)"
        )
        lines.append(
            f"  ambiguous wins              : {len(amb)}  "
            f"(beat runner-up by <=0.1 IoU — identity-swap risk)"
        )

        # --- interpretation, stated plainly ---
        lines.append("")
        lines.append("READING THIS:")
        if total == 0:
            lines.append("  No association attempts — no plate boxes were tracked.")
        elif new_tracks > matched:
            lines.append("  More boxes STARTED new tracks than joined existing ones.")
            lines.append("  That means tracks are fragmenting: one real car is being")
            lines.append("  counted as several. Check near-misses below.")
        if nm and total and len(nm) >= 0.2 * new_tracks:
            lines.append(f"  {len(nm)} failures were CLOSE to the gate.")
            if any(a.method == "motion_model" for a in self.attempts):
                lines.append("  => The cost gate may be slightly strict. Try raising")
                lines.append("     presence.assoc_cost_threshold a little (e.g. +0.1)")
                lines.append("     before touching weights.")
            else:
                lines.append("  => The threshold is likely too strict. Try lowering")
                lines.append("     presence.iou_match_threshold before changing methods.")
        elif new_tracks > matched:
            if any(a.method == "motion_model" for a in self.attempts):
                lines.append("  Failures were NOT close to the cost gate even with the")
                lines.append("  motion model. Boxes are appearing far from any track's")
                lines.append("  predicted position — check for camera shake, missed")
                lines.append("  frames, or genuinely new cars entering.")
            else:
                lines.append("  Failures were NOT close to the threshold — boxes are")
                lines.append("  landing far apart frame-to-frame. Lowering the threshold")
                lines.append("  will not help; this needs a motion-predicting matcher.")
        if amb:
            lines.append(f"  {len(amb)} associations were nearly ambiguous — with two")
            lines.append("  cars close together these are where identity swaps happen.")

        return "\n".join(lines)
