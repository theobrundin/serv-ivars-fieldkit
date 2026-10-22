"""Vehicle-presence tracking, independent of plate identification.

v2 — motion-model association + board-trigger + occlusion-aware queue.

What changed vs v1 (IoU-only)
-----------------------------
Real footage proved raw IoU association structurally fails on an approaching
car: between consecutive frames at 5fps the plate box moves far enough and
grows fast enough that boxes become genuinely non-overlapping (measured:
runner-up IoU decays smoothly 0.21 -> 0.0, one real car fragmented into ~14
one-frame tracks, several timing out into phantom drive-offs).

v2 mechanisms, each with one job:

  IDENTITY   Motion model per track (position + size + EMA velocity). The
             association cost blends predicted-position distance, scale-ratio
             consistency, and IoU as ONE term — not IoU as the sole gate.
             Association is generic over "a box + confidence + source", so a
             future vehicle-detector box plugs in without a rewrite.

  EVENTS     A configurable board ZONE (not a bare line) with a single
             occupant "trigger slot". The trigger is an arrival-and-settle
             event: in zone + smoothed speed ~0 for N frames (or a dwell
             fallback for slow-creep queues) + slot vacant. Fires exactly
             once per car. Line-only triggering was rejected as fragile
             (car stopping on the line / drifting would double-fire).

  QUEUE      The lane is strictly FIFO — cars cannot pass each other — so we
             never need every car in view at once. Queue order is a virtual
             ordered list by first-seen. A track that vanishes mid-lane while
             the board slot is occupied by a stationary car goes HELD
             (timeout paused): the queue physically cannot be moving, so
             nobody can have left. Held tracks re-associate on reappearance
             (plate string, then color, then position); they only close after
             the queue has demonstrably advanced past them.

  WEAK CARS  Until a real vehicle detector exists, plateless cars get boxes
             from motion-contour extraction on the same frame-diff we already
             compute (source="motion"). Much weaker than a detector — a
             stopped car produces no diff — but upgrades "invisible" to
             "tracked with a weak signal" for near-zero cost. A plate box
             appearing inside a motion track upgrades that track in place
             (same id, same queue position).

Timing: all methods accept an explicit `now` timestamp so the replay harness
can drive them on video time deterministically; production passes nothing and
gets wall clock.

Everything here is best-effort and must never crash the detection loop.
"""
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Iterator, Optional

from . import color as colormod
from .log import get

log = get(__name__)

# Coarse hue-bin → human label, purely for diagnostic annotation on events.
_HUE_LABELS = [
    "red", "orange", "yellow", "yellow-green", "green", "green-cyan",
    "cyan", "blue", "blue", "purple", "magenta", "red",
]


def _color_label(sig: Optional[list]) -> Optional[str]:
    """Coarse dominant-color label from a signature. Diagnostic only."""
    if not sig:
        return None
    try:
        idx = max(range(len(sig)), key=lambda i: sig[i])
        if idx == len(sig) - 1:
            return "neutral"
        return _HUE_LABELS[idx] if idx < len(_HUE_LABELS) else "unknown"
    except Exception:
        return None


def _iou(a: tuple, b: tuple) -> float:
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


def _center(b: tuple) -> tuple[float, float]:
    return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)


@dataclass
class DriveOff:
    """Emitted when a presence track ends without a confirmed plate."""
    outcome: str                       # always 'drive_off'
    detected_at_iso: str
    frame_count: int
    duration_seconds: float
    top_detection_confidence: float
    top_ocr_confidence: float
    had_plate_candidate: bool
    inference_duration_ms: int
    dominant_color: Optional[str] = None
    split_from_color: bool = False
    # --- v2 additions (additive; cloud schema tolerant of extras) ---
    passed_board: bool = False         # True if this track ever held the slot
    end_reason: str = "timeout"        # timeout | lost_occluded | color_split
    # --- episode identity (additive) ---
    # Same contract as FailedIdentification: detected_at_iso stays the first
    # sighting, episode_start mirrors it, episode_end is the last sighting.
    episode_id: str = ""
    episode_start: str = ""
    episode_end: str = ""


@dataclass
class BoardTrigger:
    """Emitted exactly once when a car arrives AND settles at the menu board.

    This is the event the ConverseNow board signal will eventually ride.
    For now it is logged/returned only — no network send (wiring decided
    later, deliberately).
    """
    track_id: int
    fired_at_epoch: float
    fired_at_iso: str
    reason: str                        # "settled" | "dwell_fallback"
    had_plate: bool
    plate_normalized: Optional[str]    # best known plate string, if any
    wait_frames: int                   # frames in zone before firing
    queue_position: int                # 0 = head of virtual queue
    queue_depth: int                   # active tracks at fire time
    encounter_id: Optional[str] = None # vehicle-first key; never hash input


@dataclass
class _Track:
    track_id: int
    first_seen_epoch: float
    first_seen_iso: str
    last_seen_epoch: float
    bbox: Optional[tuple]              # last associated box (px)
    source: str = "plate"              # "plate" | "motion" (future: "vehicle")
    frames: int = 1
    had_plate_candidate: bool = False
    top_det: float = 0.0
    top_ocr: float = 0.0
    identified: bool = False
    seen_normalized: set = field(default_factory=set)
    encounter_ids: set = field(default_factory=set)
    color_sig: Optional[list] = None
    split_from_color: bool = False
    force_close: bool = False          # color split etc. — bypasses hold
    # --- motion model ---
    vx: float = 0.0                    # px/s, EMA (for prediction)
    vy: float = 0.0
    growth: float = 0.0                # relative width change /s, EMA
    inst_speed: float = 0.0            # px/s, LAST frame pair (for stop detect)
    #   EMA velocity smooths prediction (good) but lags a hard stop by ~1s at
    #   5fps (bad for trigger latency). Stop detection therefore uses the raw
    #   per-frame displacement; stop_confirm_frames absorbs its jitter.
    # --- board / queue state ---
    in_board_zone: bool = False
    zone_enter_epoch: Optional[float] = None
    stopped_frames: int = 0
    triggered: bool = False            # board trigger already fired
    passed_board: bool = False
    frames_outside_zone: int = 0       # consecutive, for slot release
    # --- occlusion hold ---
    held: bool = False
    held_since: Optional[float] = None
    passes_at_hold: int = 0
    # Stable identity for this encounter, so the cloud can dedupe/count
    # drive-offs the same way it does detector miss episodes. Minted once at
    # birth; track_id is per-process and restarts at 0, so it cannot serve.
    episode_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    # -- geometry helpers --

    def width(self) -> float:
        return max(1.0, self.bbox[2] - self.bbox[0]) if self.bbox else 1.0

    def diag(self) -> float:
        if not self.bbox:
            return 1.0
        return math.hypot(self.bbox[2] - self.bbox[0],
                          self.bbox[3] - self.bbox[1])

    def predict(self, dt: float) -> Optional[tuple]:
        """Predicted box at last_seen + dt using EMA velocity + growth."""
        if self.bbox is None:
            return None
        cx, cy = _center(self.bbox)
        cx += self.vx * dt
        cy += self.vy * dt
        w = self.width() * max(0.2, 1.0 + self.growth * dt)
        h = max(1.0, self.bbox[3] - self.bbox[1]) * max(
            0.2, 1.0 + self.growth * dt
        )
        return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)

    def update_motion(self, new_bbox: tuple, dt: float, alpha: float = 0.5):
        """EMA-update velocity + growth from the newly associated box."""
        dt = max(dt, 1e-3)
        ocx, ocy = _center(self.bbox) if self.bbox else _center(new_bbox)
        ncx, ncy = _center(new_bbox)
        ivx = (ncx - ocx) / dt
        ivy = (ncy - ocy) / dt
        self.inst_speed = math.hypot(ivx, ivy)
        ow = self.width()
        nw = max(1.0, new_bbox[2] - new_bbox[0])
        ig = (nw / ow - 1.0) / dt
        if self.frames <= 1:
            # First real measurement — take it whole; no stale EMA to blend.
            self.vx, self.vy, self.growth = ivx, ivy, ig
        else:
            self.vx = alpha * ivx + (1 - alpha) * self.vx
            self.vy = alpha * ivy + (1 - alpha) * self.vy
            self.growth = alpha * ig + (1 - alpha) * self.growth

    def speed(self) -> float:
        return math.hypot(self.vx, self.vy)

    def reinforce(self, now, bbox, det, ocr, normalized,
                  has_plate_candidate: bool = False):
        dt = now - self.last_seen_epoch
        if bbox is not None:
            if self.bbox is not None:
                self.update_motion(bbox, dt)
            self.bbox = bbox
        self.last_seen_epoch = now
        self.frames += 1
        if has_plate_candidate:
            self.had_plate_candidate = True
        self.top_det = max(self.top_det, det)
        self.top_ocr = max(self.top_ocr, ocr)
        if normalized:
            self.seen_normalized.add(normalized)


class PresenceTracker:
    """Associates per-frame presence signals into tracks; maintains the
    virtual FIFO queue; owns the board-trigger slot; emits DriveOff on
    unidentified track close and BoardTrigger on arrival-and-settle.

    Not thread-safe: driven entirely from the single detection-loop thread.
    """

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        p = cfg.presence
        self._enabled = p.enabled
        self._iou_thresh = p.iou_match_threshold      # legacy, kept for report
        self._timeout = p.track_timeout_seconds
        self._min_frames = p.min_track_frames
        self._motion_frac = p.motion_area_fraction
        self._color_enabled = p.color_enabled
        self._color_split_distance = p.color_split_distance
        # v2 config
        self._cost_thresh = p.assoc_cost_threshold
        self._w_dist = p.assoc_dist_weight
        self._w_scale = p.assoc_scale_weight
        self._w_iou = p.assoc_iou_weight
        self._stop_speed_frac = p.stop_speed_frac_per_s
        self._stop_frames = p.stop_confirm_frames
        self._max_wait_s = p.trigger_max_wait_seconds
        self._hyst_pct = p.depart_hysteresis_pct
        self._hold_enabled = p.occlusion_hold_enabled
        self._hold_max_adv = p.occlusion_max_advance_positions
        self._motion_boxes = p.motion_boxes_enabled
        self._motion_min_area = p.motion_box_min_area_frac
        self._driveoff_requires_plate = p.driveoff_requires_plate

        self._tracks: dict[int, _Track] = {}
        self._next_id = 1
        self._prev_gray = None
        self._frame_wh: Optional[tuple[int, int]] = None
        # Board slot
        self._slot_track_id: Optional[int] = None
        self._board_pass_count = 0
        # Closed-track info for the harness's per-track report (bounded)
        self.closed_track_info: dict[int, dict] = {}

    # ---------- time / geometry helpers ----------

    def _now_iso(self, epoch: float) -> str:
        import datetime as _dt
        return (
            _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    def _board_zone_px(self) -> Optional[tuple]:
        """Board zone in pixels for the current frame size. None if unknown."""
        if self._frame_wh is None:
            return None
        w, h = self._frame_wh
        bz = self.cfg.presence.resolved_board_zone()
        return (w * bz[0], h * bz[1], w * bz[2], h * bz[3])

    def _in_board_zone(self, bbox: tuple, margin_px: float = 0.0) -> bool:
        z = self._board_zone_px()
        if z is None or bbox is None:
            return False
        cx, cy = _center(bbox)
        return (z[0] - margin_px <= cx <= z[2] + margin_px
                and z[1] - margin_px <= cy <= z[3] + margin_px)

    def _frame_diag(self) -> float:
        if self._frame_wh is None:
            return 1.0
        return math.hypot(*self._frame_wh)

    # ---------- motion signal ----------

    def _motion_analysis(self, frame):
        """One frame-diff pass. Returns (any_motion: bool, boxes: list[bbox]).

        Boxes come from contours on the thresholded diff, scaled to full
        resolution, filtered to the detection zone and a minimum area. This
        is the cheap stand-in for a vehicle detector: real positions for
        plateless cars, but blind to stopped cars (no diff) — the occlusion
        hold covers exactly that gap.
        """
        try:
            import cv2
            import numpy as np

            h, w = frame.shape[:2]
            z = self.cfg.detection_zone
            if z.enabled:
                x1, x2 = int(w * z.x_min_pct), int(w * z.x_max_pct)
                y1, y2 = int(h * z.y_min_pct), int(h * z.y_max_pct)
            else:
                x1, y1, x2, y2 = 0, 0, w, h
            roi = frame[y1:y2, x1:x2]
            if roi.size == 0:
                return False, []
            gray = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)
            gray = cv2.resize(gray, (160, 120), interpolation=cv2.INTER_AREA)
            gray = cv2.GaussianBlur(gray, (5, 5), 0)
            prev = self._prev_gray
            self._prev_gray = gray
            if prev is None or prev.shape != gray.shape:
                return False, []
            delta = cv2.absdiff(prev, gray)
            _, thresh = cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)
            changed = float(np.count_nonzero(thresh)) / thresh.size
            any_motion = changed >= self._motion_frac

            boxes: list[tuple] = []
            if self._motion_boxes:
                # Dilate to merge fragments of one car body before contouring.
                thresh = cv2.dilate(thresh, np.ones((5, 5), np.uint8))
                contours, _ = cv2.findContours(
                    thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                )
                sx = (x2 - x1) / 160.0
                sy = (y2 - y1) / 120.0
                min_area = self._motion_min_area * (w * h)
                for c in contours:
                    bx, by, bw, bh = cv2.boundingRect(c)
                    fb = (x1 + bx * sx, y1 + by * sy,
                          x1 + (bx + bw) * sx, y1 + (by + bh) * sy)
                    if (fb[2] - fb[0]) * (fb[3] - fb[1]) >= min_area:
                        boxes.append(fb)
            return any_motion, boxes
        except Exception as e:
            log.debug("motion analysis failed: %s", e)
            return False, []

    # ---------- association (motion-model cost) ----------

    def _pair_cost(self, tr: _Track, bbox: tuple, now: float):
        """Blended association cost. Lower = better. Returns (cost, iou) or
        (None, iou) if hard-gated out.

        Terms:
          dist  — distance from the track's PREDICTED position (velocity-
                  extrapolated) to the box center, normalized by box scale.
                  This is what survives fast approach motion that breaks IoU.
          scale — |log(width ratio)| vs predicted width. Two adjacent cars at
                  different depths have very different plate widths; this is
                  the anti-identity-swap term.
          iou   — 1 - IoU(predicted box, box). Still useful signal when
                  motion is slow; just no longer the only gate.
        """
        dt = max(now - tr.last_seen_epoch, 1e-3)
        pred = tr.predict(dt)
        if pred is None:
            return None, 0.0
        pcx, pcy = _center(pred)
        bcx, bcy = _center(bbox)
        scale_ref = max(
            (pred[2] - pred[0] + bbox[2] - bbox[0]) / 2.0, 8.0
        )
        d_norm = math.hypot(bcx - pcx, bcy - pcy) / scale_ref
        # Hard gate: wildly far from prediction — never the same object.
        if d_norm > 3.0:
            return None, 0.0
        pw = max(1.0, pred[2] - pred[0])
        bw = max(1.0, bbox[2] - bbox[0])
        scale = min(abs(math.log(bw / pw)), 1.5)
        iou = _iou(pred, bbox)
        cost = (
            self._w_dist * min(d_norm, 2.0) / 2.0
            + self._w_scale * scale / 1.5
            + self._w_iou * (1.0 - iou)
        )
        return cost, iou

    def _assign(self, boxes: list[tuple], now: float, source: str,
                normalized_by_box: dict[int, Optional[str]]):
        """Globally-greedy assignment of boxes to live (non-held) tracks of a
        compatible source. Returns {box_index: track or None}."""
        def compatible(track_source: str) -> bool:
            if source == "motion":
                return track_source == "motion"
            return track_source in ("plate", "vehicle")

        live = [
            t for t in self._tracks.values()
            if not t.held and t.bbox is not None
            and compatible(t.source)
        ]
        # (cost, box_idx, track, iou)
        pairs = []
        per_box: dict[int, list] = {i: [] for i in range(len(boxes))}
        for i, b in enumerate(boxes):
            for t in live:
                cost, iou = self._pair_cost(t, b, now)
                if cost is not None:
                    pairs.append((cost, i, t, iou))
                    per_box[i].append((cost, t, iou))
        pairs.sort(key=lambda x: x[0])
        assigned_boxes: set[int] = set()
        assigned_tracks: set[int] = set()
        result: dict[int, Optional[_Track]] = {i: None for i in range(len(boxes))}
        chosen_cost: dict[int, float] = {}
        chosen_iou: dict[int, float] = {}
        for cost, i, t, iou in pairs:
            if i in assigned_boxes or t.track_id in assigned_tracks:
                continue
            if cost <= self._cost_thresh:
                result[i] = t
                chosen_cost[i] = cost
                chosen_iou[i] = iou
                assigned_boxes.add(i)
                assigned_tracks.add(t.track_id)

        # Instrumentation (harness only)
        rec = getattr(self, "_recorder", None)
        if rec is not None:
            for i, b in enumerate(boxes):
                cands = sorted(per_box[i], key=lambda x: x[0])
                t = result[i]
                if t is not None:
                    runner = next(
                        (c for c in cands if c[1].track_id != t.track_id), None
                    )
                    rec.record_assoc(
                        bbox=tuple(int(v) for v in b),
                        chosen_track=t.track_id,
                        chosen_iou=round(chosen_iou.get(i, 0.0), 4),
                        runner_up_track=runner[1].track_id if runner else None,
                        runner_up_iou=round(runner[2], 4) if runner else 0.0,
                        threshold=self._iou_thresh,
                        reason="matched",
                        plate_w=round(b[2] - b[0], 1),
                        plate_h=round(b[3] - b[1], 1),
                        cost=round(chosen_cost.get(i, 0.0), 4),
                        runner_up_cost=round(runner[0], 4) if runner else 0.0,
                        cost_threshold=self._cost_thresh,
                        method="motion_model",
                    )
                else:
                    best = cands[0] if cands else None
                    rec.record_assoc(
                        bbox=tuple(int(v) for v in b),
                        chosen_track=None,
                        chosen_iou=0.0,
                        runner_up_track=best[1].track_id if best else None,
                        runner_up_iou=round(best[2], 4) if best else 0.0,
                        threshold=self._iou_thresh,
                        reason=("below_threshold" if best else "no_tracks"),
                        plate_w=round(b[2] - b[0], 1),
                        plate_h=round(b[3] - b[1], 1),
                        cost=round(best[0], 4) if best else 0.0,
                        runner_up_cost=0.0,
                        cost_threshold=self._cost_thresh,
                        method="motion_model",
                    )
        return result

    def _try_match_held(self, bbox: tuple, normalized: Optional[str],
                        color_sig: Optional[list]) -> Optional[_Track]:
        """Relaxed re-association for HELD (occluded) tracks, strongest
        evidence first: plate string, then color, then position proximity.
        Held tracks have stale motion state, so prediction is useless —
        position matching uses a generous radius around last-seen."""
        held = [t for t in self._tracks.values() if t.held]
        if not held:
            return None
        # 1. Plate string — near-certain identity, in BOTH directions.
        if normalized:
            for t in held:
                if normalized in t.seen_normalized:
                    return t
            # A box carrying a plate string a held track has NEVER seen is a
            # DIFFERENT car (both were readable; strings would agree). Without
            # this veto, a new car driving past the occlusion spot steals the
            # held identity via position matching — caught by test, not review.
            held = [t for t in held if not t.seen_normalized]
            if not held:
                return None
        # 2. Color signature — good identity if established.
        if color_sig is not None:
            best, best_d = None, None
            for t in held:
                d = colormod.distance(t.color_sig, color_sig)
                if d is not None and d < (self._color_split_distance * 0.6):
                    if best_d is None or d < best_d:
                        best, best_d = t, d
            if best is not None:
                return best
        # 3. Position — a held car reappears near where it vanished, or
        #    slightly advanced toward the board. Generous radius.
        bcx, bcy = _center(bbox)
        radius = self._frame_diag() * 0.25
        best, best_d = None, None
        for t in held:
            if t.bbox is None:
                continue
            tcx, tcy = _center(t.bbox)
            d = math.hypot(bcx - tcx, bcy - tcy)
            if d < radius and (best_d is None or d < best_d):
                best, best_d = t, d
        return best

    # ---------- track lifecycle ----------

    def attach_recorder(self, recorder) -> None:
        """Enable instrumentation. Harness-only; production never calls this,
        so the Pi pays no recording cost."""
        self._recorder = recorder

    def _new_track(self, now, bbox, det, ocr, color_sig, source="plate",
                   had_plate_candidate: bool = False):
        tr = _Track(
            track_id=self._next_id,
            first_seen_epoch=now,
            first_seen_iso=self._now_iso(now),
            last_seen_epoch=now,
            bbox=bbox,
            source=source,
            had_plate_candidate=had_plate_candidate,
            top_det=det,
            top_ocr=ocr,
            color_sig=color_sig,
        )
        self._tracks[self._next_id] = tr
        rec = getattr(self, "_recorder", None)
        if rec is not None:
            rec.record_event(
                tr.track_id, "born",
                detail=(f"det={det:.2f} ocr={ocr:.2f} "
                        f"plate={had_plate_candidate} "
                        f"source={source}"),
                bbox=tuple(int(v) for v in bbox) if bbox else None,
            )
        self._next_id += 1
        return tr

    # ---------- main per-frame entry ----------

    def observe(self, frame, plate_candidates, now: Optional[float] = None
                ) -> list[BoardTrigger]:
        """Update presence tracks from one frame. Returns board-trigger
        events fired this frame (usually empty).

        `plate_candidates`: DetectionResult-shaped objects (.bbox,
        .detection_confidence, .ocr_confidence, .normalized, .in_zone),
        INCLUDING sub-threshold ones.
        `now`: explicit timestamp (video time in the harness); wall clock
        when omitted (production).
        """
        if not self._enabled:
            return []
        if now is None:
            now = time.time()
        h, w = frame.shape[:2]
        self._frame_wh = (w, h)

        # Motion signal + boxes, EXACTLY ONCE per frame (updates the diff
        # baseline; calling twice corrupts it).
        any_motion, motion_boxes = self._motion_analysis(frame)

        # ---- 1. SDK vehicle boxes (plate fallback) -> tracks ----
        plate_boxes = []
        observation_groups = []
        for c in plate_candidates:
            if not getattr(c, "in_zone", True):
                continue
            plate_bbox = getattr(c, "bbox", None)
            vehicle_bbox = getattr(c, "vehicle_bbox", None)
            bbox = vehicle_bbox or plate_bbox
            if bbox is None:
                continue
            box = tuple(float(v) for v in bbox)
            duplicate_i = next(
                (i for i, existing in enumerate(plate_boxes)
                 if _iou(box, existing) >= 0.85),
                None,
            )
            if duplicate_i is None:
                plate_boxes.append(box)
                observation_groups.append([c])
            else:
                observation_groups[duplicate_i].append(c)

        norm_by_idx = {
            i: next(
                (getattr(c, "normalized", None) for c in group
                 if getattr(c, "normalized", None)),
                None,
            )
            for i, group in enumerate(observation_groups)
        }
        sdk_source = (
            "vehicle"
            if any(
                getattr(c, "vehicle_bbox", None) is not None
                for group in observation_groups for c in group
            ) else "plate"
        )
        assignment = self._assign(plate_boxes, now, sdk_source, norm_by_idx)

        matched_track_ids: set[int] = set()
        for i, bbox in enumerate(plate_boxes):
            group = observation_groups[i]
            c = max(
                group,
                key=lambda item: min(
                    float(getattr(item, "detection_confidence", 0.0) or 0.0),
                    float(getattr(item, "ocr_confidence", 0.0) or 0.0),
                ),
            )
            det = max(
                float(
                    getattr(item, "detection_confidence", None)
                    or getattr(item, "plate_detection_confidence", 0.0)
                    or 0.0
                )
                for item in group
            )
            ocr = max(
                float(
                    getattr(item, "ocr_confidence", None)
                    or getattr(item, "plate_ocr_confidence", 0.0)
                    or 0.0
                )
                for item in group
            )
            norms = {
                getattr(item, "normalized", None) for item in group
                if getattr(item, "normalized", None)
            }
            norm = getattr(c, "normalized", None)
            encounter_ids = {
                getattr(item, "encounter_id", None) for item in group
                if getattr(item, "encounter_id", None)
            }
            has_plate_candidate = any(
                bool(getattr(item, "saw_plate", False))
                or (
                    getattr(item, "bbox", None) is not None
                    and tuple(getattr(item, "bbox")) != (0, 0, 0, 0)
                )
                for item in group
            )
            observation_source = (
                "vehicle"
                if any(getattr(item, "vehicle_bbox", None) is not None
                       for item in group)
                else "plate"
            )
            new_sig = None
            if self._color_enabled:
                plate_for_color = next(
                    (
                        tuple(int(v) for v in getattr(item, "bbox"))
                        for item in group
                        if getattr(item, "bbox", None) is not None
                        and tuple(getattr(item, "bbox")) != (0, 0, 0, 0)
                    ),
                    None,
                )
                vehicle_for_color = (
                    tuple(int(v) for v in bbox)
                    if observation_source == "vehicle" else None
                )
                new_sig = colormod.signature(
                    frame,
                    plate_bbox=plate_for_color,
                    vehicle_bbox=vehicle_for_color,
                )

            tr = assignment[i]

            # Color split: an established track whose color CONFIDENTLY
            # changed is really two cars nose-to-tail.
            if tr is not None and self._color_enabled:
                d = colormod.distance(tr.color_sig, new_sig)
                if (d is not None and d >= self._color_split_distance
                        and tr.frames >= 2 and not tr.identified):
                    log.info(
                        "COLOR SPLIT: track=%d color-dist=%.2f — treating as "
                        "a new vehicle (nose-to-tail).", tr.track_id, d,
                    )
                    tr.split_from_color = True
                    tr.force_close = True
                    tr.last_seen_epoch = now - self._timeout - 1.0
                    tr = None

            if tr is None:
                # Held-track re-association before opening a new track.
                held = self._try_match_held(bbox, norm, new_sig)
                if held is not None:
                    held.held = False
                    held.held_since = None
                    # Motion state is stale after occlusion — reset velocity
                    # so prediction restarts from the reappearance point.
                    held.vx = held.vy = held.growth = 0.0
                    held.bbox = bbox
                    held.reinforce(
                        now, bbox, det, ocr, norm, has_plate_candidate
                    )
                    held.seen_normalized.update(norms)
                    if observation_source == "vehicle":
                        held.source = "vehicle"
                    tr = held
                    rec = getattr(self, "_recorder", None)
                    if rec is not None:
                        rec.record_event(tr.track_id, "reinforced",
                                         detail="unheld_reassociated")
                else:
                    # A plate appearing inside an existing MOTION track is the
                    # same physical car becoming readable — upgrade in place
                    # (same id, same queue position).
                    upgraded = None
                    bcx, bcy = _center(bbox)
                    for t in self._tracks.values():
                        if (t.source == "motion" and not t.held
                                and t.bbox is not None
                                and t.bbox[0] <= bcx <= t.bbox[2]
                                and t.bbox[1] <= bcy <= t.bbox[3]):
                            upgraded = t
                            break
                    if upgraded is not None:
                        upgraded.source = observation_source
                        upgraded.bbox = bbox
                        upgraded.vx = upgraded.vy = upgraded.growth = 0.0
                        upgraded.reinforce(
                            now, bbox, det, ocr, norm, has_plate_candidate
                        )
                        upgraded.seen_normalized.update(norms)
                        tr = upgraded
                    else:
                        tr = self._new_track(
                            now, bbox, det, ocr, new_sig,
                            source=observation_source,
                            had_plate_candidate=has_plate_candidate,
                        )
            else:
                tr.reinforce(
                    now, bbox, det, ocr, norm, has_plate_candidate
                )
                tr.seen_normalized.update(norms)
                if tr.source == "motion":
                    # A motion-source track just got SDK vehicle evidence:
                    # same physical car became directly detected.
                    tr.source = observation_source
                elif observation_source == "vehicle":
                    tr.source = "vehicle"

            tr.encounter_ids.update(encounter_ids)

            if self._color_enabled and tr.color_sig is None and new_sig:
                tr.color_sig = new_sig
            matched_track_ids.add(tr.track_id)

        # ---- 2. motion boxes -> motion-source tracks ----
        # Only boxes NOT explaining an already-matched plate car may open or
        # reinforce motion tracks (a moving plated car produces both a motion
        # blob and a plate box — that's one car, not two).
        if self._motion_boxes and motion_boxes:
            plate_geo = [
                (_center(b), max(8.0, b[2] - b[0])) for b in plate_boxes
            ]
            unexplained = []
            for mb in motion_boxes:
                mcx, mcy = _center(mb)
                explained = False
                for (px, py), pw in plate_geo:
                    # Contains the plate center, OR sits within a car-body
                    # radius (~3 plate widths) of it. The diff of a moving
                    # car is edge strips that flank the plate rather than
                    # covering it — those are still the same car.
                    if (mb[0] <= px <= mb[2] and mb[1] <= py <= mb[3]) or (
                            abs(mcx - px) < 3.0 * pw
                            and abs(mcy - py) < 3.0 * pw):
                        explained = True
                        break
                if not explained:
                    unexplained.append(mb)
            if unexplained:
                m_assign = self._assign(unexplained, now, "motion", {})
                for i, mb in enumerate(unexplained):
                    tr = m_assign[i]
                    if tr is not None and tr.source != "motion":
                        # Plate tracks are reinforced only by plate boxes;
                        # don't let a big motion blob drag a plate track.
                        continue
                    if tr is None:
                        held = self._try_match_held(mb, None, None)
                        if held is not None:
                            held.held = False
                            held.held_since = None
                            held.vx = held.vy = held.growth = 0.0
                            held.bbox = mb
                            held.reinforce(now, mb, 0.0, 0.0, None, False)
                            matched_track_ids.add(held.track_id)
                            continue
                        tr = self._new_track(now, mb, 0.0, 0.0, None,
                                             source="motion")
                    else:
                        tr.reinforce(now, mb, 0.0, 0.0, None, False)
                    matched_track_ids.add(tr.track_id)
        elif not matched_track_ids and any_motion:
            # Legacy boolean fallback (motion boxes disabled): sustained zone
            # motion with nothing matched refreshes the most recent track or
            # opens a presence-only one.
            recent = max(self._tracks.values(),
                         key=lambda t: t.last_seen_epoch, default=None)
            if recent is not None and (now - recent.last_seen_epoch) < self._timeout:
                recent.reinforce(now, None, 0.0, 0.0, None, False)
            else:
                self._new_track(now, None, 0.0, 0.0, None, source="motion")

        # ---- 3. board zone / trigger slot ----
        return self._update_board(now, matched_track_ids)

    # ---------- board slot ----------

    def _update_board(
        self,
        now: float,
        observed_track_ids: set[int] | None = None,
    ) -> list[BoardTrigger]:
        events: list[BoardTrigger] = []
        z = self._board_zone_px()
        if z is None:
            return events
        margin = self._hyst_pct * self._frame_diag()
        stop_speed = self._stop_speed_frac * self._frame_diag()

        # Update per-track zone state (only tracks seen recently enough to
        # have meaningful geometry).
        for t in self._tracks.values():
            if t.held or t.bbox is None:
                continue
            if (observed_track_ids is not None
                    and t.track_id not in observed_track_ids):
                # A stale box is not evidence that a vehicle remained stopped
                # for another frame. Wait for a new observation before
                # advancing dwell/settle counters.
                continue
            inside = self._in_board_zone(t.bbox)
            inside_with_margin = self._in_board_zone(t.bbox, margin_px=margin)
            if inside:
                if not t.in_board_zone:
                    t.in_board_zone = True
                    t.zone_enter_epoch = now
                    t.stopped_frames = 0
                t.frames_outside_zone = 0
                if t.inst_speed <= stop_speed:
                    t.stopped_frames += 1
                else:
                    t.stopped_frames = 0
            elif not inside_with_margin:
                # Genuinely outside (past hysteresis margin).
                if t.in_board_zone:
                    t.frames_outside_zone += 1
                    if t.frames_outside_zone >= 2:
                        t.in_board_zone = False
                        t.zone_enter_epoch = None
                        t.stopped_frames = 0
            # else: in the hysteresis band — state unchanged.

        # Slot release: occupant gone, closed, or genuinely left the zone.
        if self._slot_track_id is not None:
            occ = self._tracks.get(self._slot_track_id)
            if occ is None or (not occ.in_board_zone and not occ.held):
                self._release_slot("occupant left zone")

        # Slot acquisition + trigger.
        if self._slot_track_id is None:
            in_zone = [
                t for t in self._tracks.values()
                if t.in_board_zone and not t.held and not t.triggered
            ]
            if in_zone:
                # FIFO: the earliest-seen car in the zone is the head of the
                # queue and the only trigger candidate — "one plate at a time".
                cand = min(in_zone, key=lambda t: t.first_seen_epoch)
                settled = cand.stopped_frames >= self._stop_frames
                waited = (
                    cand.zone_enter_epoch is not None
                    and (now - cand.zone_enter_epoch) >= self._max_wait_s
                )
                if settled or waited:
                    self._slot_track_id = cand.track_id
                    cand.triggered = True
                    cand.passed_board = True
                    order = sorted(
                        (t for t in self._tracks.values()),
                        key=lambda t: t.first_seen_epoch,
                    )
                    qpos = next(
                        (i for i, t in enumerate(order)
                         if t.track_id == cand.track_id), 0,
                    )
                    ev = BoardTrigger(
                        track_id=cand.track_id,
                        fired_at_epoch=now,
                        fired_at_iso=self._now_iso(now),
                        reason="settled" if settled else "dwell_fallback",
                        had_plate=cand.had_plate_candidate,
                        plate_normalized=(
                            next(iter(cand.seen_normalized))
                            if cand.seen_normalized else None
                        ),
                        wait_frames=cand.stopped_frames,
                        queue_position=qpos,
                        queue_depth=len(self._tracks),
                        encounter_id=(
                            next(iter(cand.encounter_ids))
                            if len(cand.encounter_ids) == 1 else None
                        ),
                    )
                    events.append(ev)
                    log.info(
                        "BOARD_TRIGGER track=%d reason=%s had_plate=%s qpos=%d",
                        cand.track_id, ev.reason, ev.had_plate, qpos,
                    )
                    rec = getattr(self, "_recorder", None)
                    if rec is not None:
                        rec.record_event(cand.track_id, "reinforced",
                                         detail=f"board_trigger:{ev.reason}")
        return events

    def _release_slot(self, why: str) -> None:
        log.debug("board slot released (%s)", why)
        self._slot_track_id = None
        self._board_pass_count += 1
        # A queue advance is evidence for held tracks' give-up counters and
        # is the moment occluded cars are expected to start reappearing.

    def _slot_occupied_and_stationary(self) -> bool:
        if self._slot_track_id is None:
            return False
        occ = self._tracks.get(self._slot_track_id)
        if occ is None or occ.bbox is None:
            return False
        stop_speed = self._stop_speed_frac * self._frame_diag()
        return occ.inst_speed <= stop_speed * 2.0

    # ---------- identification reconciliation ----------

    def mark_identified(
        self,
        normalized: str,
        vehicle_bbox: tuple | None = None,
    ) -> None:
        """Called when a plate confirms. Marks the matching track (or the
        most-recent active one as fallback) IDENTIFIED so it won't be
        reported as a drive-off."""
        if not self._enabled:
            return
        matching = [
            t for t in self._tracks.values()
            if normalized in t.seen_normalized and not t.identified
        ]
        if matching:
            if vehicle_bbox is not None:
                matching.sort(
                    key=lambda t: (
                        _iou(t.bbox, vehicle_bbox) if t.bbox else 0.0,
                        t.last_seen_epoch,
                    ),
                    reverse=True,
                )
            else:
                matching.sort(key=lambda t: t.last_seen_epoch, reverse=True)
            matching[0].identified = True
            return

        if vehicle_bbox is not None:
            spatial = [
                (_iou(t.bbox, vehicle_bbox), t.last_seen_epoch, t)
                for t in self._tracks.values()
                if not t.identified and t.bbox is not None
            ]
            if spatial:
                overlap, _, best = max(spatial, key=lambda item: (item[0], item[1]))
                if overlap >= 0.1:
                    best.identified = True
                    return

        # Only use the legacy fallback when there is exactly one possible car;
        # guessing among several active tracks can attribute a customer to the
        # wrong vehicle and suppress a real drive-off.
        unresolved = [t for t in self._tracks.values() if not t.identified]
        if len(unresolved) == 1:
            unresolved[0].identified = True
        elif unresolved:
            log.warning(
                "could not safely reconcile confirmed plate with %d active "
                "vehicle tracks", len(unresolved),
            )

    # ---------- expiry / emission ----------

    def reap(self, now: Optional[float] = None) -> Iterator[DriveOff]:
        """Close tracks that have gone quiet — with occlusion awareness.

        A track that times out is NOT closed if it plausibly vanished behind
        the car at the board while the queue is stalled: the lane is FIFO, so
        while the slot occupant sits still, nobody behind it can have left.
        Such tracks go HELD (timeout paused) and only close after the queue
        has advanced `occlusion_max_advance_positions` cars past them without
        the track reappearing.
        """
        if not self._enabled:
            return
        if now is None:
            now = time.time()

        # 1. Give up on held tracks the queue has demonstrably passed by.
        for tid, t in list(self._tracks.items()):
            if not t.held:
                continue
            advanced = self._board_pass_count - t.passes_at_hold
            too_old = (t.held_since is not None
                       and (now - t.held_since) > max(self._timeout * 20, 120.0))
            if advanced >= self._hold_max_adv or too_old:
                self._tracks.pop(tid)
                yield from self._emit_close(
                    tid, t, now, end_reason="lost_occluded"
                )

        # 2. Normal expiry.
        expired = [
            (tid, t) for tid, t in self._tracks.items()
            if not t.held and (now - t.last_seen_epoch) > self._timeout
        ]
        for tid, t in expired:
            # Occlusion hold decision.
            if (self._hold_enabled
                    and not t.force_close
                    and not t.identified
                    and not t.triggered
                    and self._slot_track_id is not None
                    and tid != self._slot_track_id
                    and self._slot_occupied_and_stationary()
                    and t.frames >= self._min_frames):
                t.held = True
                t.held_since = now
                t.passes_at_hold = self._board_pass_count
                rec = getattr(self, "_recorder", None)
                if rec is not None:
                    rec.record_event(tid, "reinforced", detail="held_occluded")
                log.debug("track %d HELD (occluded behind board car)", tid)
                continue
            self._tracks.pop(tid)
            if tid == self._slot_track_id:
                self._release_slot("occupant track closed")
            yield from self._emit_close(tid, t, now, end_reason="timeout")

    def _emit_close(self, tid: int, t: _Track, now: float,
                    end_reason: str) -> Iterator[DriveOff]:
        """Shared close path: record info for reporting; emit DriveOff if the
        track ended unidentified and met the minimum-frames bar."""
        duration = round(t.last_seen_epoch - t.first_seen_epoch, 2)
        color_label = _color_label(t.color_sig)
        # Bounded closed-track info for the harness report.
        if len(self.closed_track_info) < 5000:
            self.closed_track_info[tid] = {
                "top_det": round(t.top_det, 3),
                "top_ocr": round(t.top_ocr, 3),
                "duration_s": duration,
                "color": color_label,
                "source": t.source,
                "passed_board": t.passed_board,
                "end_reason": end_reason,
            }
        rec = getattr(self, "_recorder", None)
        if t.identified:
            if rec is not None:
                rec.record_event(tid, "reaped", detail="identified")
            return
        if t.frames < self._min_frames and not t.split_from_color:
            if rec is not None:
                rec.record_event(
                    tid, "reaped",
                    detail=f"dropped_min_frames({t.frames}<{self._min_frames})",
                )
            log.debug("presence track %d dropped (only %d frames)", tid, t.frames)
            return
        # A motion-only track is not evidence of a VEHICLE. Motion-contour
        # boxes fire on pedestrians, wind-blown
        # foliage, shadows and parked cars — on real footage 87 of 119
        # "drive-offs" had no plate at any point, inflating the vehicle count
        # to 136 against a true ~30. A drive-off is a billable business claim
        # ("a customer left unidentified"), so it needs actual vehicle
        # evidence behind it. These tracks are still tracked and still hold
        # the board slot; they just aren't COUNTED as vehicles.
        has_direct_vehicle_evidence = (
            t.source == "vehicle" or t.had_plate_candidate
        )
        if self._driveoff_requires_plate and not has_direct_vehicle_evidence:
            if rec is not None:
                rec.record_event(
                    tid, "reaped",
                    detail=f"motion_only_no_plate frames={t.frames} "
                           f"reason={end_reason}",
                )
            log.debug(
                "presence track %d closed as motion-only (no plate ever seen, "
                "%d frames) — not counted as a vehicle", tid, t.frames,
            )
            return
        if rec is not None:
            rec.record_event(
                tid, "reaped",
                detail=(f"drive_off frames={t.frames} "
                        f"had_plate={t.had_plate_candidate} "
                        f"reason={end_reason}"),
            )
        end_reason_out = "color_split" if t.split_from_color else end_reason
        log.info(
            "DRIVE_OFF track=%d frames=%d dur=%.1fs had_plate=%s "
            "top_det=%.2f top_ocr=%.2f color=%s split=%s reason=%s board=%s",
            tid, t.frames, duration, t.had_plate_candidate,
            t.top_det, t.top_ocr, color_label, t.split_from_color,
            end_reason_out, t.passed_board,
        )
        yield DriveOff(
            outcome="drive_off",
            detected_at_iso=t.first_seen_iso,
            frame_count=t.frames,
            duration_seconds=duration,
            top_detection_confidence=round(t.top_det, 4),
            top_ocr_confidence=round(t.top_ocr, 4),
            had_plate_candidate=t.had_plate_candidate,
            inference_duration_ms=0,
            dominant_color=color_label,
            split_from_color=t.split_from_color,
            passed_board=t.passed_board,
            end_reason=end_reason_out,
            episode_id=t.episode_id,
            episode_start=t.first_seen_iso,
            episode_end=self._now_iso(t.last_seen_epoch),
        )

    # ---------- introspection ----------

    def active_track_count(self) -> int:
        return len(self._tracks)

    def queue_snapshot(self) -> list[dict]:
        """Current virtual queue, FIFO order, for diagnostics."""
        order = sorted(self._tracks.values(), key=lambda t: t.first_seen_epoch)
        return [
            {
                "track_id": t.track_id,
                "source": t.source,
                "held": t.held,
                "in_board_zone": t.in_board_zone,
                "triggered": t.triggered,
                "identified": t.identified,
                "frames": t.frames,
                "plates": sorted(x[:8] for x in t.seen_normalized),
            }
            for t in order
        ]

    def board_pass_count(self) -> int:
        return self._board_pass_count
