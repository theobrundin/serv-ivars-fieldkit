"""Plate inference + the two-frame voting logic.

Inference itself lives behind the `PlateEngine` protocol in `engines.py`;
this module owns everything downstream of it — thresholding, normalization,
voting, and dwell suppression — and is deliberately engine-agnostic. The only
engine-aware line in this file is the `self._engine.infer(frame)` call.

Vote semantics (per NORMALIZATION_SPEC and the empirical thresholds):
  - Each candidate frame must have detection confidence >= min_detection_confidence
    AND OCR confidence >= min_ocr_confidence, where OCR confidence is the
    MINIMUM across per-character confidences (see _to_scalar_confidence).
  - When the first valid candidate appears, we open a vote bucket keyed by its
    exact `normalized` string.
  - Confirmation requires vote_min_frames candidates (default 2) carrying the
    IDENTICAL normalized string within vote_window_seconds, with min(det) and
    min(ocr) across them clearing the group floors — OR a single frame whose
    OCR clears high_ocr_single_frame (default 0.85) when that gate is enabled.
  - An optional engine-specific rescue can confirm only when at least THREE
    sub-threshold reads from the SAME physical encounter carry the IDENTICAL
    normalized string, clear explicit rescue floors, and fail exactly one
    normal gate. It is disabled unless configured beside the engine gates.
  - PR Stream may also confirm one provider-selected result after Stream has
    already observed the vehicle for a bounded temporal window. That path is
    opt-in, encounter-bound, clears the normal gates, requires at least five
    normalized characters, and verifies rear-plate geometry inside the
    vehicle box. It is not a generic one-frame threshold reduction.
  - When the vote window expires without enough agreement, we close the bucket
    and emit a 'group_confidence_floor' or 'low_confidence' detection event
    depending on what we saw.
  - Plate reads that never confirm still produce at most ONE detection-event
    per vehicle episode. Episodes merge by near-match OCR variants (JAL/JALE)
    AND by timing: failed reads that arrive while an episode is still active
    (within the encounter gap) join that same vehicle even when the OCR text
    differs completely. Deferred until quiet; cancelled if the vehicle later
    confirms.

Design priority: PRECISION over recall. A missed car is a lost data point;
a wrong hash is a wrong customer identity that also poisons cross-visit
matching, so it is much more costly. Every threshold here is set to the
strictest value that did not cost real confirms on measured footage.

Measured on the full 17:53 clip (24 plate encounters, ~20 real plated cars):
  - old gate (mean OCR conf, 2 frames): 17 confirms, 1 WRONG hash shipped
  - this gate (min-per-char, exact match): 16 confirms, 0 wrong hashes

There is exactly one string in this pipeline. The bucket key, the voted
string, and the string that gets hashed and sent to /visit are all the same
spec-compliant `normalized` value. No folding, no translation, no second
representation to keep straight.
"""
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Iterator


# --- Why there is no OCR "fold" here anymore ----------------------------
# An earlier revision folded OCR-ambiguous characters (O->0, G->6, B->8...)
# to build a fuzzy vote key, because the same plate appeared to read
# differently across consecutive frames and exact-match voting confirmed
# nothing. That was the wrong fix for the right observation.
#
# Measuring every OCR read across the full 17:53 clip showed the flap was
# not inherent OCR nondeterminism — it was low-quality reads being trusted.
# Gating on MINIMUM per-character confidence (see _to_scalar_confidence)
# removed the disagreement outright: 67 distinct strings collapsed to 19,
# and NO encounter was left with two competing strings. Exact-match voting
# then works on its own.
#
# Folding is now actively harmful, for two reasons:
#   1. It merges genuinely DIFFERENT plates. "B09667G" and "809667G" fold
#      to the same key, so two different cars could vote as one and be
#      billed as one visit.
#   2. It kept NORMALIZATION_SPEC.md rule 7 (no character substitution) in
#      permanent tension with the code. With exact matching there is no
#      tension: the string we group by IS the spec-compliant `normalized`
#      string we hash. No spec v2.0 migration is needed.
#
# Bucket key == normalized == what gets hashed. One string, no translation.


from .camera import bbox_inside_zone
from .config import Config
from .engines import build_engine
# Re-exported: `_to_scalar_confidence` now lives in engines.py so engines can
# share it without a circular import. Its behaviour (MIN across per-character
# confidences, never mean) is unchanged and still load-bearing — see the
# docstring there for the frame-289 A951G700 case. Imported here so existing
# callers, including tools/test_dwell.py, keep working.
from .engines import _to_scalar_confidence  # noqa: F401
from .log import get
from .miss_diagnostics import (
    copy_frame,
    joint_rank,
    prune_miss_plates,
    save_miss_plate,
    PresenceFrameDumper,
)
from .normalizer import normalize

log = get(__name__)


@dataclass
class DetectionResult:
    """One frame's worth of plate detection, after thresholding."""
    raw_text: str
    normalized: str | None
    detection_confidence: float
    ocr_confidence: float
    bbox: tuple[int, int, int, int]
    inference_ms: int
    captured_at_epoch: float
    captured_at_iso: str
    in_zone: bool
    # Vehicle box from engines that provide one (ultimateALPR). None on
    # fast-alpr, which returns plates only.
    vehicle_bbox: tuple[int, int, int, int] | None = None
    vehicle_confidence: float | None = None
    # Make / model / colour from engines with MMC enabled. Descriptive only —
    # never hash input, so a misclassification cannot split a customer.
    make: str | None = None
    model: str | None = None
    color: str | None = None
    make_confidence: float | None = None
    model_confidence: float | None = None
    color_confidence: float | None = None
    # Stable physical encounter from Stream. Correlation only; never hash input.
    encounter_id: str | None = None
    # Stream settlement saw a different normalized candidate for this same
    # encounter. Never identity input; used only to block one-shot rescue.
    had_competing_variants: bool = False
    # Stable provider-observation identity. This distinguishes independently
    # selected Stream observations that happen to drain in one local call from
    # duplicate boxes produced by one source frame.
    source_observation_id: str | None = None
    # True when the dwell zone is enabled and this read's anchor point sits
    # inside it (and clears any minimum vehicle-box area). Always True when
    # the dwell zone is disabled, so behaviour is unchanged by default.
    in_dwell_zone: bool = True
    # Name of the gate that rejected this read, for calibration. None = passed.
    rejected_by: str | None = None
    # True if this box was localized but fell below the identify thresholds.
    # These never enter the vote, but they DO feed the presence tracker so a
    # weak-but-real plate still counts a car as present.
    below_threshold: bool = False
    # Full frame supplied by the engine (Stream webhook JPEG). Preferred over
    # the camera frame when saving the episode's highest-OCR still.
    engine_image: object | None = field(default=None, repr=False)


@dataclass
class ConfirmedIdentification:
    """Output of a successful vote — what gets sent to /visit."""
    normalized: str
    detected_at_iso: str            # iso of the FIRST frame in the vote
    frame_count: int                # always 2 for confirmed under current spec
    detection_confidence: float     # min across voting frames
    ocr_confidence: float           # min across voting frames
    inference_duration_ms: int      # sum across voting frames
    # --- dwell suppression ---
    # True when this is a RE-confirmation of a plate that never left the frame
    # (a car idling at the order point). The read genuinely happened — we emit
    # it so it stays visible in logs/telemetry rather than vanishing — but it
    # must NOT be counted as a new visit or sent to /visit.
    # Deliberately emitted-and-flagged rather than silently dropped, so the
    # difference between "read once" and "read 11 times" stays observable.
    suppressed: bool = False
    suppress_reason: str | None = None      # e.g. "dwell<60.0s, absent=0"
    seconds_since_first_confirm: float | None = None
    repeat_index: int = 0                   # 0 = first confirm, 1+ = re-reads
    # Vehicle geometry from the confirming frame. This is a transient
    # correlation hint, not part of the cloud identity. It lets the presence
    # tracker mark the exact vehicle rather than guessing "most recent".
    vehicle_bbox: tuple[int, int, int, int] | None = None
    # Local path of the best full frame written for this visit, if enabled.
    diagnostic_image_path: str | None = None
    # Make / model / colour of the confirming vehicle, when the engine
    # supplies them. Descriptive context for staff; never part of identity.
    make: str | None = None
    model: str | None = None
    color: str | None = None
    make_confidence: float | None = None
    model_confidence: float | None = None
    color_confidence: float | None = None
    encounter_id: str | None = None
    # Observable locally and in tests. The cloud's plateDetection remains the
    # stable "recognized" category regardless of which trusted vote path won.
    confirmation_method: str = "normal_vote"


@dataclass
class VehicleOnlyIdentification:
    """A vehicle was tracked in the dwell zone but NO plate ever confirmed.

    Deliberately a separate type rather than a flag on ConfirmedIdentification.
    A plateless vehicle has no plate string, therefore no hash, and must never
    reach hasher.py or be written as a plate-identified visit. Making it a
    distinct type means that mistake cannot be made by forgetting to check a
    boolean — there is simply no `normalized` field to hash.

    2 of the 8 vehicles in test_footage.mp4 have no readable plate; without
    this they are invisible to the pipeline (Problem 2).
    """
    detected_at_iso: str
    frame_count: int
    vehicle_bbox: tuple[int, int, int, int] | None
    vehicle_confidence: float | None
    seconds_in_zone: float
    reason: str = "no_plate_confirmed"
    # Movement evidence, carried so the cloud can audit WHY this counted as a
    # real vehicle rather than a parked car. Without a dwell zone these two
    # numbers are the entire basis for the claim, so they travel with it.
    travel_fraction: float = 0.0        # centre travel / frame diagonal
    area_growth_ratio: float = 1.0      # last box area / first box area
    # True if a plate box was localized for this vehicle at some point but
    # never confirmed, vs. never seeing a plate at all. Both are misses; the
    # distinction says whether the plate was absent or merely unreadable.
    had_plate_candidate: bool = False


@dataclass
class _ReadSample:
    """One read's contribution to an episode's telemetry.

    Deliberately carries the det/ocr pair TOGETHER: the whole point of the
    joint fields below is that a number must be attributable to a real frame.
    """
    det: float
    ocr: float
    cleared_det: bool
    cleared_ocr: bool
    admitted: bool = False          # did this read actually enter a vote?


@dataclass
class _EpisodeStats:
    """Frame-level accounting for one miss episode.

    Answers "what did this vehicle's frames actually look like?" without the
    cross-frame max() blending that makes `top_*` unusable for tuning.
    """
    reads: int = 0
    frames_admitted: int = 0
    max_bucket_size: int = 0
    frames_clearing_det: int = 0
    frames_clearing_ocr: int = 0
    frames_clearing_both: int = 0
    failed_det_only: int = 0
    failed_ocr_only: int = 0
    failed_both: int = 0
    # The best SINGLE frame, as a pair. See `_offer` for the ordering.
    best_joint_det: float = 0.0
    best_joint_ocr: float = 0.0
    best_joint_cleared_det: bool = False
    # Sort key of the current best: (cleared_det, ocr, det). Any frame that
    # cleared the detection gate outranks every frame that did not, so this
    # single comparison implements both halves of the rule: prefer the
    # highest-OCR frame among det-clearing frames, and fall back to the
    # highest-OCR frame overall (flagged) when nothing cleared det.
    _best_rank: tuple = field(default=(-1, -1.0, -1.0), repr=False)

    def _offer(self, s: "_ReadSample") -> None:
        rank = (1 if s.cleared_det else 0, s.ocr, s.det)
        if rank > self._best_rank:
            self._best_rank = rank
            self.best_joint_det = s.det
            self.best_joint_ocr = s.ocr
            self.best_joint_cleared_det = s.cleared_det

    def ingest(self, samples, bucket_size: int = 0) -> None:
        for s in samples:
            self.reads += 1
            if s.admitted:
                self.frames_admitted += 1
            if s.cleared_det:
                self.frames_clearing_det += 1
            if s.cleared_ocr:
                self.frames_clearing_ocr += 1
            if s.cleared_det and s.cleared_ocr:
                self.frames_clearing_both += 1
            elif s.cleared_det:            # ocr is what failed
                self.failed_ocr_only += 1
            elif s.cleared_ocr:            # det is what failed
                self.failed_det_only += 1
            else:
                self.failed_both += 1
            self._offer(s)
        if bucket_size > self.max_bucket_size:
            self.max_bucket_size = bucket_size


@dataclass
class FailedIdentification:
    """Output of an expired / rejected plate read — sent to /detection-event.

    `outcome` distinguishes WHICH knob to turn, so the strings must never be
    collapsed into one another:
      below_threshold        no frame ever cleared the per-frame gates —
                             an image-quality problem
      unreadable             cleared the gates but produced no legal plate
                             string (normalization)
      low_confidence         entered a vote, expired with too few frames —
                             a vote-timing problem
      group_confidence_floor enough agreeing frames, min() failed the group
                             floor — a threshold problem
    """
    outcome: str   # 'no_plate_detected' | 'low_confidence' | 'group_confidence_floor'
                   # | 'below_threshold' | 'unreadable'
    detected_at_iso: str
    frame_count: int
    # DEPRECATED (kept for backward compat with the existing cloud schema):
    # these are INDEPENDENT maxima across every read in the episode, so they
    # routinely describe two different frames — e.g. det 0.809 from one frame
    # and ocr 0.804 from another 23s later, reported as if one frame had
    # scored 0.809/0.804 when no frame ever cleared both. Use best_joint_*.
    top_detection_confidence: float
    top_ocr_confidence: float
    inference_duration_ms: int

    # --- paired best-frame telemetry (additive; all null-safe) -------------
    # One REAL frame: the highest-OCR frame among those clearing the det
    # gate. If no frame cleared det, the highest-OCR frame overall, with
    # best_joint_cleared_det=False so the cloud knows the pair is not a
    # candidate that ever passed the first gate.
    best_joint_det: float = 0.0
    best_joint_ocr: float = 0.0
    best_joint_cleared_det: bool = False
    frames_clearing_det: int = 0
    frames_clearing_ocr: int = 0
    frames_clearing_both: int = 0
    # {'failed_det_only','failed_ocr_only','failed_both'} — which gate lost.
    reject_reason_counts: dict = field(default_factory=dict)

    # --- unambiguous frame counts -----------------------------------------
    # `frame_count` above is the legacy name and equals reads_in_episode; it
    # is NOT "frames in the vote". These three say what each number means.
    reads_in_episode: int = 0       # every read folded into this episode
    frames_admitted: int = 0        # reads that actually entered a vote
    max_bucket_size: int = 0        # most agreeing frames any one vote held

    # --- episode identity --------------------------------------------------
    # Lets the cloud dedupe and count encounters. `detected_at_iso` stays the
    # FIRST sighting (unchanged), and episode_start mirrors it.
    episode_id: str = ""
    episode_start: str = ""
    episode_end: str = ""
    # Local path of the best plate crop written for this miss, if enabled.
    diagnostic_image_path: str | None = None
    encounter_id: str | None = None


@dataclass
class _PendingMiss:
    """One unconfirmed plate-vehicle episode waiting to flush to AWS.

    Stored by physical encounter plus canonical plate string. Near-match OCR
    variants merge only when encounter ownership is compatible, so one car
    does not become many detection-events and adjacent cars never share one.
    """
    key: str
    first_seen_iso: str
    first_seen_epoch: float
    last_seen_epoch: float
    frame_count: int = 1
    top_detection_confidence: float = 0.0
    top_ocr_confidence: float = 0.0
    inference_duration_ms: int = 0
    outcome: str = "below_threshold"
    # Stable identity for the whole episode, minted once when it opens and
    # carried through every merge — including a re-key to a longer plate
    # string, which must NOT look like a new encounter to the cloud.
    episode_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    last_seen_iso: str = ""
    encounter_id: str | None = None
    stats: _EpisodeStats = field(default_factory=_EpisodeStats)
    # Best full frame for this episode (same ranking as best_joint_*).
    best_crop: object | None = field(default=None, repr=False)
    best_raw_text: str = ""
    best_bbox: tuple[int, int, int, int] | None = None
    _best_crop_rank: tuple = field(
        default=(-1, -1.0, -1.0), repr=False,
    )

    def maybe_take_crop(
        self,
        *,
        cleared_det: bool,
        ocr: float,
        det: float,
        crop,
        raw_text: str = "",
        bbox: tuple[int, int, int, int] | None = None,
    ) -> None:
        if crop is None:
            return
        rank = joint_rank(cleared_det, ocr, det)
        if rank > self._best_crop_rank:
            self._best_crop_rank = rank
            self.best_crop = crop
            if raw_text:
                self.best_raw_text = raw_text
            if bbox is not None:
                self.best_bbox = bbox


@dataclass
class _DwellRecord:
    """Tracks a plate we've already confirmed, to suppress re-confirms while
    the same physical car is still sitting in frame.

    Keyed by the exact `normalized` string, the same key the vote buckets
    use, so a dwelling car is looked up consistently.

    Release semantics (option "c" — both conditions, not either):
      A suppressed plate becomes confirmable again when EITHER
        (1) dwell_suppress_seconds has elapsed, OR
        (2) enough OTHER vehicles have passed through,
      but in BOTH cases only if the plate has ALSO been absent from frame for
      dwell_release_requires_absent_frames consecutive frames.

    The absence requirement is what makes this correct: a car that never left
    the frame is not a new visit, regardless of how much time passed or how
    many other cars went by. Conversely a car that left and came back IS a new
    visit even if it returns quickly.
    """
    normalized: str                    # exact plate string; see class doc
    first_confirm_epoch: float
    last_confirm_epoch: float
    repeat_count: int = 0
    # Consecutive frames (since the last sighting) in which this plate was NOT
    # seen. Reset to 0 every time we see it again.
    absent_frames: int = 0
    # How many other distinct vehicle tracks have closed since this confirm.
    other_vehicles_since: int = 0
    # LATCH: set True the moment the absence requirement is met, and NOT
    # cleared when the plate reappears.
    #
    # Why this is necessary: confirming requires two agreeing frames, so by the
    # time a returning car actually confirms, it has been visible again for 2+
    # frames — which resets absent_frames to 0. Without this latch the release
    # condition is satisfied and then destroyed before it can ever be used, and
    # a genuinely returning car is wrongly suppressed forever. Caught by test.
    left_frame_since_confirm: bool = False
    # Wall-clock epoch when the current absence run began, or None if the
    # plate is currently visible. Backs the time-based absence requirement,
    # which is more robust than a frame count when fps varies.
    absent_since_epoch: float | None = None

    def mark_seen(self) -> None:
        self.absent_frames = 0
        self.absent_since_epoch = None

    def mark_absent(self, now: float | None = None) -> None:
        self.absent_frames += 1
        if self.absent_since_epoch is None and now is not None:
            self.absent_since_epoch = now

    def absent_seconds(self, now: float) -> float:
        if self.absent_since_epoch is None:
            return 0.0
        return now - self.absent_since_epoch

    def can_release(self, now: float, cfg_thresholds) -> tuple[bool, str]:
        """Returns (releasable, human-readable reason for the decision)."""
        absent_needed = cfg_thresholds.dwell_release_requires_absent_frames
        # Hard gate: must have actually left the frame at some point since the
        # last confirm. Uses the LATCH, not live absent_frames — see the field
        # comment; a returning car is necessarily visible again by confirm time.
        if not self.left_frame_since_confirm:
            return False, (
                f"never left frame since last confirm "
                f"(absent {self.absent_frames}/{absent_needed})"
            )

        elapsed = now - self.last_confirm_epoch
        if elapsed >= cfg_thresholds.dwell_suppress_seconds:
            return True, f"dwell elapsed {elapsed:.1f}s"

        need_others = cfg_thresholds.dwell_release_after_other_vehicles
        if need_others > 0 and self.other_vehicles_since >= need_others:
            return True, (
                f"{self.other_vehicles_since} other vehicle(s) passed "
                f"(elapsed {elapsed:.1f}s)"
            )

        return False, (
            f"dwell {elapsed:.1f}s < {cfg_thresholds.dwell_suppress_seconds}s, "
            f"others={self.other_vehicles_since}/{need_others}"
        )


@dataclass
class _VoteBucket:
    """One in-flight vote for an exact `normalized` plate string.

    Every candidate in a bucket carries the identical normalized string, so
    the bucket key IS the identity that gets hashed. See best_normalized().
    """
    normalized: str                                  # exact; groups AND is hashed
    first_seen_epoch: float
    first_seen_iso: str
    candidates: list[DetectionResult] = field(default_factory=list)
    # Best full frame among admitted reads, for miss diagnostics if the
    # vote never confirms. Same rank order as episode best_joint_*.
    best_crop: object | None = field(default=None, repr=False)
    best_raw_text: str = ""
    best_bbox: tuple[int, int, int, int] | None = None
    _best_crop_rank: tuple = field(
        default=(-1, -1.0, -1.0), repr=False,
    )

    def add(
        self, r: DetectionResult, crop=None, *, cleared_det: bool = True
    ) -> None:
        if r.captured_at_epoch < self.first_seen_epoch:
            self.first_seen_epoch = r.captured_at_epoch
            self.first_seen_iso = r.captured_at_iso
        self.candidates.append(r)
        self.maybe_take_crop(r, crop, cleared_det=cleared_det)

    def maybe_take_crop(
        self, r: DetectionResult, crop, *, cleared_det: bool = True
    ) -> None:
        if crop is None:
            return
        rank = joint_rank(
            cleared_det, r.ocr_confidence, r.detection_confidence
        )
        if rank > self._best_crop_rank:
            self._best_crop_rank = rank
            self.best_crop = crop
            self.best_raw_text = r.raw_text or ""
            self.best_bbox = r.bbox

    def is_expired(self, now: float, window_s: float) -> bool:
        return (now - self.first_seen_epoch) > window_s

    def best_normalized(self) -> str:
        """The string to hash and send to /visit.

        Bucketing is by exact string, so every candidate here already agrees;
        this is simply that string. Kept as a method (rather than inlining
        `self.normalized`) so the emission point stays explicit and auditable
        — assert the invariant rather than assume it.
        """
        assert all(c.normalized == self.normalized for c in self.candidates), (
            "vote bucket contains mixed normalized strings; bucketing is "
            "supposed to be exact-match"
        )
        return self.normalized

    def encounter_id(self) -> str | None:
        """Physical encounter shared by this bucket's Stream reads."""
        ids = [c.encounter_id for c in self.candidates if c.encounter_id]
        return max(set(ids), key=ids.count) if ids else None

    def samples(self) -> list[_ReadSample]:
        """Per-frame telemetry for this bucket's members.

        Every candidate in a bucket cleared BOTH per-frame gates by
        construction — that is what admitted it — so the flags are set True
        rather than re-derived from a config this dataclass doesn't hold.
        """
        return [
            _ReadSample(
                det=c.detection_confidence,
                ocr=c.ocr_confidence,
                cleared_det=True,
                cleared_ocr=True,
                admitted=True,
            )
            for c in self.candidates
        ]

    def to_failed(self, reason: str) -> FailedIdentification:
        top_det = max((c.detection_confidence for c in self.candidates), default=0.0)
        top_ocr = max((c.ocr_confidence for c in self.candidates), default=0.0)
        total_ms = sum(c.inference_ms for c in self.candidates)
        stats = _EpisodeStats()
        stats.ingest(self.samples(), bucket_size=len(self.candidates))
        return FailedIdentification(
            outcome=reason,
            detected_at_iso=self.first_seen_iso,
            frame_count=len(self.candidates),
            top_detection_confidence=round(top_det, 4),
            top_ocr_confidence=round(top_ocr, 4),
            inference_duration_ms=total_ms,
            best_joint_det=round(stats.best_joint_det, 4),
            best_joint_ocr=round(stats.best_joint_ocr, 4),
            best_joint_cleared_det=stats.best_joint_cleared_det,
            frames_clearing_det=stats.frames_clearing_det,
            frames_clearing_ocr=stats.frames_clearing_ocr,
            frames_clearing_both=stats.frames_clearing_both,
            reject_reason_counts={
                "failed_det_only": stats.failed_det_only,
                "failed_ocr_only": stats.failed_ocr_only,
                "failed_both": stats.failed_both,
            },
            reads_in_episode=stats.reads,
            frames_admitted=stats.frames_admitted,
            max_bucket_size=stats.max_bucket_size,
            episode_id=uuid.uuid4().hex,
            episode_start=self.first_seen_iso,
            episode_end=(
                self.candidates[-1].captured_at_iso
                if self.candidates else self.first_seen_iso
            ),
            encounter_id=self.encounter_id(),
        )


class Detector:
    """Stateful: holds open vote buckets across frames."""

    def __init__(self, cfg: Config,
                 enable_vehicle_only_tracking: bool = True) -> None:
        self.cfg = cfg
        self._engine = None
        # The production service uses PresenceTracker as the single
        # authoritative vehicle lifecycle. This legacy tracker remains
        # available to standalone replay/calibration tools only.
        self._enable_vehicle_only_tracking = enable_vehicle_only_tracking
        # --- dedup: last confirm time per hash-key -----------------------
        # Primary defence against one car counting as several visits. Keyed by
        # the same exact normalized string everything else uses. Deliberately
        # INDEPENDENT of presence/absence tracking, because read flicker (a
        # plate dropping out for a few frames and returning) is exactly what
        # defeated the absence latch and produced the duplicate confirm at
        # f02961/f02966 in the Stage 2b run.
        self._last_confirm_epoch: dict[str, float] = {}
        self._last_confirm_encounter: dict[str, str | None] = {}
        # Unconfirmed plate episodes (below-threshold / expired votes), merged
        # by near-match so OCR variants of one car become one AWS event.
        self._pending_miss: dict[tuple[str | None, str], _PendingMiss] = {}
        # Last time we posted a miss detection-event for a plate family.
        self._last_miss_emit_epoch: dict[tuple[str | None, str], float] = {}
        # Continuous dwell-zone entry time per plate, for min_seconds_in_zone.
        self._zone_entry_epoch: dict[tuple[str | None, str], float] = {}
        # Vehicle boxes in the dwell zone with no usable plate yet, keyed by a
        # monotonic id and associated frame-to-frame by box overlap.
        # Drained by collect_vehicle_only(). See VehicleOnlyIdentification.
        self._vehicle_only_pending: dict = {}
        self._vehicle_only_seq: int = 0
        self._warned_vehicle_only_no_zone: bool = False
        self._capture_seq: int = 0
        # Frame size, refreshed each frame. Needed to express vehicle travel
        # as a fraction of the frame diagonal rather than in pixels, which
        # would silently change meaning with capture resolution.
        self._frame_wh: tuple[int, int] = (
            cfg.capture.width or 1456, cfg.capture.height or 1088,
        )
        # KPI accounting, read by the harness and by main.py's counters.
        # `raw` counts every unidentified vehicle before the movement gate;
        # `static_dropped` counts those the gate rejected. raw - dropped is
        # what gets emitted, so a hand count of the same footage can be
        # compared against both numbers and the gate tuned from the gap.
        self.vehicle_only_seen_raw: int = 0
        self.vehicle_only_static_dropped: int = 0
        # Open vote buckets, keyed by physical encounter plus exact normalized
        # plate. Missing encounter IDs use None and retain legacy behavior.
        self._buckets: dict[tuple[str | None, str], _VoteBucket] = {}
        # Optional conservative recovery path. The encounter id is part of
        # the key so repeated weak text from two different cars can never
        # combine into one identity vote.
        self._rescue_buckets: dict[tuple[str, str], _VoteBucket] = {}
        # One physical Stream encounter can produce OCR variants. Once any
        # variant confirms, no later variant from that same encounter may
        # become a second visit.
        self._confirmed_encounter_epoch: dict[str, float] = {}
        # Plates already confirmed, held to suppress re-confirms while the same
        # car is still dwelling in frame. Keyed by exact normalized string
        # (matches bucket keying so lookups stay consistent).
        self._dwell: dict[str, _DwellRecord] = {}
        self._presence_dumper: PresenceFrameDumper | None = None

    def open(self) -> None:
        self._engine = build_engine(self.cfg)
        self._engine.open()
        dg = self.cfg.diagnostics
        if getattr(dg, "save_presence_frames", False):
            self._presence_dumper = PresenceFrameDumper(
                getattr(dg, "presence_frame_dir", "")
                or "/home/serv/plate_reader/presence_frames"
            )
            log.info(
                "presence-frame dump ON -> %s (every plate/vehicle frame)",
                self._presence_dumper.directory,
            )

    def close(self) -> None:
        """Release the inference engine. Idempotent.

        fast-alpr never needed teardown, but ultimateALPR's engine is a
        process-global singleton whose deInit() must actually run before
        another init() is legal. Call sites exist so a later engine swap
        doesn't have to touch main.py or the harness.
        """
        if self._presence_dumper is not None:
            self._presence_dumper.close()
            self._presence_dumper = None
        if self._engine is not None:
            self._engine.close()
            self._engine = None

    @staticmethod
    def _edit_distance_within(a: str, b: str, limit: int) -> bool:
        """True if Levenshtein(a, b) <= limit. Cheap early exits.

        Only ever used to decide whether to EMIT a second visit — never to
        alter a plate string or its hash. NORMALIZATION_SPEC v1.0 rule 7 (no
        character substitution) is about producing the identity; this is about
        counting visits, which is a separate concern.
        """
        if a == b:
            return True
        if abs(len(a) - len(b)) > limit:
            return False
        # Standard DP, bounded by the short strings involved (<= 12 chars).
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            cur = [i]
            for j, cb in enumerate(b, 1):
                cur.append(min(
                    prev[j] + 1,          # deletion
                    cur[j - 1] + 1,       # insertion
                    prev[j - 1] + (ca != cb),   # substitution
                ))
            if min(cur) > limit:          # whole row already too far
                return False
            prev = cur
        return prev[-1] <= limit

    def _find_near_match(
        self,
        key: str,
        now: float,
        encounter_id: str | None = None,
    ) -> tuple[str, float] | None:
        """A recently-confirmed plate within the configured edit distance.

        Returns (other_key, seconds_ago) or None. Only considers confirms
        still inside the cooldown window — outside it, a genuinely returning
        car should count again.
        """
        limit = self.cfg.dedup.near_match_max_distance
        cooldown = self.cfg.dedup.cooldown_seconds
        if limit <= 0 or cooldown <= 0:
            return None
        for other, ts in self._last_confirm_epoch.items():
            if other == key:
                continue                    # exact repeat: the hash cooldown
            # A known physical owner is stronger evidence than edit distance.
            # Two simultaneous/adjacent cars may legitimately have plates one
            # character apart; near-match suppression is only a temporal
            # fallback when either side lacks encounter ownership.
            if not self._encounter_compatible(
                self._last_confirm_encounter.get(other), encounter_id
            ):
                continue
            age = now - ts
            if age >= cooldown:
                continue
            if self._edit_distance_within(key, other, limit):
                return other, age
        return None

    def _miss_edit_limit(self) -> int:
        """Edit distance for grouping miss events as one vehicle.

        At least 1 so truncations (JAL vs JALE) share a detection-event.
        Honors a higher configured near_match_max_distance when set.
        """
        return max(1, int(self.cfg.dedup.near_match_max_distance))

    def _miss_family_cooldown(self) -> float:
        """How long one vehicle miss stays 'counted' after we post it."""
        return float(max(self.cfg.dedup.cooldown_seconds, 60.0))

    def _miss_encounter_gap(self) -> float:
        """Seconds of plate activity treated as one physical vehicle.

        Weak OCR often flips to an unrelated string on the next frame; without
        a time window those would each open a miss episode and inflate vehicle
        counts. The physical encounter ID is authoritative when present; this
        window is a fallback only for reads missing that ownership. It remains
        longer than flush silence so one unknown-owner car can settle.
        """
        return max(self.cfg.thresholds.vote_window_seconds * 4.0, 12.0)

    def _miss_flush_silence(self) -> float:
        """How long an episode must be quiet before we post it to AWS."""
        return max(self.cfg.thresholds.vote_window_seconds * 2.0, 3.0)

    def _find_family_key(self, key: str, keys) -> str | None:
        """Existing key in `keys` within miss edit distance, else None."""
        limit = self._miss_edit_limit()
        for other in keys:
            if other == key or self._edit_distance_within(key, other, limit):
                return other
        return None

    @staticmethod
    def _encounter_compatible(
        existing: str | None, incoming: str | None
    ) -> bool:
        """Temporal fallback is allowed only when either owner is unknown."""
        return not existing or not incoming or existing == incoming

    def _pending_family_key(
        self, key: str, encounter_id: str | None
    ) -> tuple[str | None, str] | None:
        limit = self._miss_edit_limit()
        for storage_key, miss in self._pending_miss.items():
            if not self._encounter_compatible(
                miss.encounter_id, encounter_id
            ):
                continue
            if miss.key == key or self._edit_distance_within(
                miss.key, key, limit
            ):
                return storage_key
        return None

    def _confirmed_family_key(
        self, key: str, encounter_id: str | None
    ) -> str | None:
        """Near/exact confirmed family compatible with this physical owner."""
        limit = self._miss_edit_limit()
        for confirmed_key in self._last_confirm_epoch:
            if not self._encounter_compatible(
                self._last_confirm_encounter.get(confirmed_key),
                encounter_id,
            ):
                continue
            if confirmed_key == key or self._edit_distance_within(
                confirmed_key, key, limit
            ):
                return confirmed_key
        return None

    def _active_pending_miss(
        self, now: float, encounter_id: str | None = None
    ) -> "_PendingMiss | None":
        """Most recently updated pending episode still inside the encounter gap."""
        gap = self._miss_encounter_gap()
        best: _PendingMiss | None = None
        for miss in self._pending_miss.values():
            if (now - miss.last_seen_epoch) >= gap:
                continue
            if not self._encounter_compatible(
                miss.encounter_id, encounter_id
            ):
                continue
            if best is None or miss.last_seen_epoch > best.last_seen_epoch:
                best = miss
        return best

    def _merge_into_miss(
        self,
        miss: _PendingMiss,
        *,
        key: str,
        now: float,
        det: float,
        ocr: float,
        inference_ms: int,
        outcome: str,
        frames: int,
        samples: "list[_ReadSample] | None" = None,
        bucket_size: int = 0,
        last_seen_iso: str = "",
        crop=None,
        raw_text: str = "",
        crop_cleared_det: bool | None = None,
        bbox: tuple[int, int, int, int] | None = None,
        encounter_id: str | None = None,
    ) -> None:
        """Fold a new sighting into an existing episode; re-key if useful."""
        old_storage_key = next(
            storage_key for storage_key, pending in self._pending_miss.items()
            if pending is miss
        )
        old_key = miss.key
        miss.last_seen_epoch = now
        miss.frame_count += frames
        miss.top_detection_confidence = max(miss.top_detection_confidence, det)
        miss.top_ocr_confidence = max(miss.top_ocr_confidence, ocr)
        miss.inference_duration_ms += inference_ms
        if last_seen_iso:
            miss.last_seen_iso = last_seen_iso
        if encounter_id and not miss.encounter_id:
            miss.encounter_id = encounter_id
        miss.stats.ingest(samples or (), bucket_size=bucket_size)
        self._offer_miss_crop(
            miss,
            samples=samples,
            crop=crop,
            raw_text=raw_text,
            crop_cleared_det=crop_cleared_det,
            det=det,
            ocr=ocr,
            bbox=bbox,
        )
        rank = {
            "below_threshold": 0,
            "unreadable": 1,
            "low_confidence": 2,
            "group_confidence_floor": 3,
        }
        if rank.get(outcome, 0) >= rank.get(miss.outcome, 0):
            miss.outcome = outcome
        # Prefer a longer/more complete plate string as the family key.
        if len(key) > len(old_key) and key != old_key:
            miss.key = key
        new_storage_key = (miss.encounter_id, miss.key)
        if new_storage_key != old_storage_key:
            self._pending_miss.pop(old_storage_key, None)
            self._pending_miss[new_storage_key] = miss

    @staticmethod
    def _offer_miss_crop(
        miss: _PendingMiss,
        *,
        samples: "list[_ReadSample] | None",
        crop,
        raw_text: str,
        crop_cleared_det: bool | None,
        det: float,
        ocr: float,
        bbox: tuple[int, int, int, int] | None = None,
    ) -> None:
        if crop is None:
            return
        if samples:
            best = max(
                samples,
                key=lambda s: joint_rank(s.cleared_det, s.ocr, s.det),
            )
            miss.maybe_take_crop(
                cleared_det=best.cleared_det,
                ocr=best.ocr,
                det=best.det,
                crop=crop,
                raw_text=raw_text,
                bbox=bbox,
            )
            return
        cleared = True if crop_cleared_det is None else crop_cleared_det
        miss.maybe_take_crop(
            cleared_det=cleared,
            ocr=ocr,
            det=det,
            crop=crop,
            raw_text=raw_text,
            bbox=bbox,
        )

    def _note_pending_miss(
        self,
        key: str,
        *,
        now: float,
        detected_at_iso: str,
        det: float,
        ocr: float,
        inference_ms: int,
        outcome: str,
        frames: int = 1,
        samples: "list[_ReadSample] | None" = None,
        bucket_size: int = 0,
        last_seen_iso: str = "",
        crop=None,
        raw_text: str = "",
        crop_cleared_det: bool | None = None,
        bbox: tuple[int, int, int, int] | None = None,
        encounter_id: str | None = None,
    ) -> None:
        """Merge a plate sighting into a vehicle miss episode (no AWS yet).

        Joins an existing episode when the plate near-matches OR when an
        episode is still time-active (same physical car, different OCR).

        `samples` carries the per-frame det/ocr PAIRS behind this sighting so
        the episode can report a real frame rather than two independent
        maxima. Omitting it costs only the joint fields, never correctness.

        `crop` is the full camera frame for the best read in this sighting; the
        episode keeps only the single best frame across merges.
        """
        if not key:
            return
        if self._miss_recently_emitted(key, now, encounter_id):
            return
        # Already confirmed this vehicle recently — do not open a miss episode.
        conf = self._confirmed_family_key(key, encounter_id)
        if conf is not None:
            last = self._last_confirm_epoch[conf]
            if (now - last) < self._miss_family_cooldown():
                return
        # Recent confirm of ANY plate during this encounter: the car was
        # identified; don't start a parallel miss for a garbage OCR variant.
        if self._recent_confirm_in_encounter(now, encounter_id):
            return

        existing = self._pending_family_key(key, encounter_id)
        if existing is None:
            active = self._active_pending_miss(now, encounter_id)
            if active is not None:
                existing = next(
                    storage_key for storage_key, pending
                    in self._pending_miss.items()
                    if pending is active
                )
        if existing is None:
            opened = _PendingMiss(
                key=key,
                first_seen_iso=detected_at_iso,
                first_seen_epoch=now,
                last_seen_epoch=now,
                frame_count=frames,
                top_detection_confidence=det,
                top_ocr_confidence=ocr,
                inference_duration_ms=inference_ms,
                outcome=outcome,
                last_seen_iso=last_seen_iso or detected_at_iso,
                encounter_id=encounter_id,
            )
            opened.stats.ingest(samples or (), bucket_size=bucket_size)
            self._offer_miss_crop(
                opened,
                samples=samples,
                crop=crop,
                raw_text=raw_text,
                crop_cleared_det=crop_cleared_det,
                det=det,
                ocr=ocr,
                bbox=bbox,
            )
            self._pending_miss[(encounter_id, key)] = opened
            return
        self._merge_into_miss(
            self._pending_miss[existing],
            key=key,
            now=now,
            det=det,
            ocr=ocr,
            inference_ms=inference_ms,
            outcome=outcome,
            frames=frames,
            samples=samples,
            bucket_size=bucket_size,
            last_seen_iso=last_seen_iso,
            crop=crop,
            raw_text=raw_text,
            crop_cleared_det=crop_cleared_det,
            bbox=bbox,
            encounter_id=encounter_id,
        )

    def _recent_confirm_in_encounter(
        self, now: float, encounter_id: str | None = None
    ) -> bool:
        """True if any plate confirmed within the encounter gap."""
        gap = self._miss_encounter_gap()
        if encounter_id:
            confirmed_at = self._confirmed_encounter_epoch.get(encounter_id)
            return bool(
                confirmed_at is not None and (now - confirmed_at) < gap
            )
        for ts in self._last_confirm_epoch.values():
            if (now - ts) < gap:
                return True
        return False

    def _drop_pending_miss_family(
        self,
        key: str,
        now: float | None = None,
        encounter_id: str | None = None,
    ) -> None:
        """Drop near-match pending keys, and any time-active episode if `now`."""
        limit = self._miss_edit_limit()
        for storage_key, miss in list(self._pending_miss.items()):
            if not self._encounter_compatible(
                miss.encounter_id, encounter_id
            ):
                continue
            if miss.key == key or self._edit_distance_within(
                miss.key, key, limit
            ):
                self._pending_miss.pop(storage_key, None)
        if now is None:
            return
        # Same physical car often produces unrelated OCR junk alongside a
        # confirm; wipe the whole active encounter, not just near-matches.
        gap = self._miss_encounter_gap()
        for storage_key, miss in list(self._pending_miss.items()):
            if (
                self._encounter_compatible(miss.encounter_id, encounter_id)
                and (now - miss.last_seen_epoch) < gap
            ):
                self._pending_miss.pop(storage_key, None)

    def _miss_recently_emitted(
        self, key: str, now: float, encounter_id: str | None = None
    ) -> bool:
        cooldown = self._miss_family_cooldown()
        limit = self._miss_edit_limit()
        for (emitted_encounter, emitted_key), emitted_at in (
            self._last_miss_emit_epoch.items()
        ):
            if not self._encounter_compatible(
                emitted_encounter, encounter_id
            ):
                continue
            if (
                emitted_key == key
                or self._edit_distance_within(emitted_key, key, limit)
            ) and (now - emitted_at) < cooldown:
                return True
        # Short time fuse after ANY miss post: different OCR of the same car
        # that resumes right after a flush must not open a second episode.
        # Use flush-silence (not the longer encounter gap) so a real next car
        # arriving ~10s later still counts.
        post_emit = self._miss_flush_silence()
        for (emitted_encounter, _), ts in self._last_miss_emit_epoch.items():
            if (
                self._encounter_compatible(emitted_encounter, encounter_id)
                and (now - ts) < post_emit
            ):
                return True
        return False

    def _mark_miss_emitted(
        self, key: str, now: float, encounter_id: str | None = None
    ) -> None:
        # Collapse near-match keys onto one emit timestamp.
        limit = self._miss_edit_limit()
        for storage_key in list(self._last_miss_emit_epoch):
            emitted_encounter, emitted_key = storage_key
            if not self._encounter_compatible(
                emitted_encounter, encounter_id
            ):
                continue
            if emitted_key != key and self._edit_distance_within(
                emitted_key, key, limit
            ):
                self._last_miss_emit_epoch.pop(storage_key, None)
        self._last_miss_emit_epoch[(encounter_id, key)] = now
        self._drop_pending_miss_family(
            key, now=now, encounter_id=encounter_id
        )

    def _flush_quiet_misses(
        self, now: float
    ) -> list[FailedIdentification]:
        """Emit one detection-event per quiet unconfirmed vehicle episode."""
        silence = self._miss_flush_silence()
        out: list[FailedIdentification] = []
        # Stream may still be posting this car (even plateless). Do not give
        # up on a weak-det first look; a later sampled frame may clear the
        # gates. Freshness is encounter-specific: another car arriving must
        # not freeze this miss indefinitely in a busy lane.
        still_here = getattr(self._engine, "has_fresh_vehicle", None)
        for storage_key, miss in list(self._pending_miss.items()):
            # A live vote for THIS encounter means it is still being
            # identified. Another car's vote must not freeze an old miss in a
            # busy lane. With no encounter ID, preserve the conservative
            # legacy behavior because ownership cannot be proven.
            if not miss.encounter_id:
                active_vote = bool(self._buckets or self._rescue_buckets)
            else:
                active_vote = any(
                    bucket.encounter_id() == miss.encounter_id
                    for bucket in self._buckets.values()
                ) or any(
                    rescue_key[0] == miss.encounter_id
                    for rescue_key in self._rescue_buckets
                )
            if active_vote:
                continue
            if callable(still_here):
                try:
                    if still_here(encounter_id=miss.encounter_id):
                        continue
                except TypeError:
                    # Engine plugins predating encounter correlation expose
                    # the legacy no-argument method.
                    try:
                        if still_here():
                            continue
                    except Exception:
                        pass
                except Exception:
                    pass
            if (now - miss.last_seen_epoch) < silence:
                continue
            if self._miss_recently_emitted(
                miss.key, now, miss.encounter_id
            ):
                self._pending_miss.pop(storage_key, None)
                continue
            # Confirmed in the meantime (exact, near-match, or same encounter).
            if self._recent_confirm_in_encounter(now, miss.encounter_id):
                self._pending_miss.pop(storage_key, None)
                continue
            conf = self._confirmed_family_key(
                miss.key, miss.encounter_id
            )
            if conf is not None:
                age = now - self._last_confirm_epoch[conf]
                if age < self._miss_family_cooldown():
                    self._pending_miss.pop(storage_key, None)
                    continue
            st = miss.stats
            image_path = self._persist_miss_crop(miss)
            out.append(FailedIdentification(
                outcome=miss.outcome,
                # Unchanged: first sighting of the episode. The cloud's
                # detectedAt keeps meaning exactly what it always meant.
                detected_at_iso=miss.first_seen_iso,
                # Legacy alias for reads_in_episode — see the dataclass.
                frame_count=miss.frame_count,
                top_detection_confidence=round(miss.top_detection_confidence, 4),
                top_ocr_confidence=round(miss.top_ocr_confidence, 4),
                inference_duration_ms=miss.inference_duration_ms,
                best_joint_det=round(st.best_joint_det, 4),
                best_joint_ocr=round(st.best_joint_ocr, 4),
                best_joint_cleared_det=st.best_joint_cleared_det,
                frames_clearing_det=st.frames_clearing_det,
                frames_clearing_ocr=st.frames_clearing_ocr,
                frames_clearing_both=st.frames_clearing_both,
                reject_reason_counts={
                    "failed_det_only": st.failed_det_only,
                    "failed_ocr_only": st.failed_ocr_only,
                    "failed_both": st.failed_both,
                },
                reads_in_episode=st.reads,
                frames_admitted=st.frames_admitted,
                max_bucket_size=st.max_bucket_size,
                episode_id=miss.episode_id,
                episode_start=miss.first_seen_iso,
                episode_end=miss.last_seen_iso or miss.first_seen_iso,
                diagnostic_image_path=image_path,
                encounter_id=miss.encounter_id,
            ))
            self._mark_miss_emitted(
                miss.key, now, miss.encounter_id
            )
        # Prune stale emit records.
        cooldown = self._miss_family_cooldown()
        for k, ts in list(self._last_miss_emit_epoch.items()):
            if now - ts > max(cooldown * 10.0, 600.0):
                self._last_miss_emit_epoch.pop(k, None)
        return out

    def _write_episode_frame(
        self,
        *,
        image,
        episode_id: str,
        outcome: str,
        det: float,
        ocr: float,
        cleared_det: bool,
        raw_text: str,
        detected_at_iso: str,
        episode_end: str,
        reads: int,
        bbox,
        normalized: str | None = None,
        encounter_id: str | None = None,
    ) -> str | None:
        """Write one full-frame JPEG+JSON; prune old files. Shared by miss and confirm."""
        dg = self.cfg.diagnostics
        if not getattr(dg, "save_miss_plates", False) or image is None:
            return None
        path = save_miss_plate(
            directory=dg.miss_plate_dir,
            image=image,
            episode_id=episode_id,
            outcome=outcome,
            det=det,
            ocr=ocr,
            cleared_det=cleared_det,
            raw_text=raw_text,
            detected_at_iso=detected_at_iso,
            episode_end=episode_end,
            reads=reads,
            bbox=bbox,
            normalized=normalized,
            encounter_id=encounter_id,
        )
        try:
            prune_miss_plates(
                dg.miss_plate_dir,
                dg.miss_plate_retain_hours,
                getattr(dg, "miss_plate_max_cases", 0),
            )
        except Exception:
            pass
        return path

    def _persist_miss_crop(self, miss: _PendingMiss) -> str | None:
        """Write the episode's best full frame to disk when diagnostics allow."""
        path = self._write_episode_frame(
            image=miss.best_crop,
            episode_id=miss.episode_id,
            outcome=miss.outcome,
            det=miss.stats.best_joint_det,
            ocr=miss.stats.best_joint_ocr,
            cleared_det=miss.stats.best_joint_cleared_det,
            raw_text=miss.best_raw_text,
            detected_at_iso=miss.first_seen_iso,
            episode_end=miss.last_seen_iso or miss.first_seen_iso,
            reads=miss.stats.reads,
            bbox=miss.best_bbox,
            normalized=miss.key or None,
        )
        miss.best_crop = None
        return path

    def _persist_confirm_frame(self, bucket: _VoteBucket) -> str | None:
        """Save the vote's best full frame for a confirmed visit."""
        if not getattr(
            self.cfg.diagnostics, "save_confirmed_plates", True
        ):
            bucket.best_crop = None
            return None
        rank = bucket._best_crop_rank
        cleared = bool(rank[0]) if rank[0] >= 0 else True
        ocr = float(rank[1]) if rank[1] >= 0 else 0.0
        det = float(rank[2]) if rank[2] >= 0 else 0.0
        episode_end = (
            bucket.candidates[-1].captured_at_iso
            if bucket.candidates else bucket.first_seen_iso
        )
        path = self._write_episode_frame(
            image=bucket.best_crop,
            episode_id=uuid.uuid4().hex,
            outcome="confirmed",
            det=det,
            ocr=ocr,
            cleared_det=cleared,
            raw_text=bucket.best_raw_text or bucket.normalized,
            detected_at_iso=bucket.first_seen_iso,
            episode_end=episode_end,
            reads=len(bucket.candidates),
            bbox=bucket.best_bbox,
            normalized=bucket.normalized,
            encounter_id=bucket.encounter_id(),
        )
        bucket.best_crop = None
        return path

    def _plate_crop(self, frame, bbox=None, engine_image=None):
        """Copy the full frame for miss diagnostics (no crop).

        Stream owns capture, so the camera `frame` is empty; prefer the
        webhook JPEG on the read when present. One copy is kept per episode,
        ranked by OCR, so disk gets one still per vehicle.
        """
        dg = self.cfg.diagnostics
        if not getattr(dg, "save_miss_plates", False):
            return None
        src = engine_image if engine_image is not None else frame
        return copy_frame(src)

    @staticmethod
    def _bucket_mmc(bucket: _VoteBucket):
        """Highest-confidence make/model pair and independent colour."""
        make = model = color = None
        make_confidence = model_confidence = color_confidence = None
        best_pair_score = best_color_score = -1.0
        for c in bucket.candidates:
            candidate_make = getattr(c, "make", None)
            candidate_model = getattr(c, "model", None)
            candidate_make_conf = getattr(c, "make_confidence", None)
            candidate_model_conf = getattr(c, "model_confidence", None)
            pair_is_scored = (
                candidate_make_conf is not None
                and candidate_model_conf is not None
            )
            pair_score = (
                min(float(candidate_make_conf), float(candidate_model_conf))
                if pair_is_scored else -1.0
            )
            if candidate_make and candidate_model and (
                not (make and model) or pair_score > best_pair_score
            ):
                make = candidate_make
                model = candidate_model
                make_confidence = (
                    candidate_make_conf if pair_is_scored else None
                )
                model_confidence = (
                    candidate_model_conf if pair_is_scored else None
                )
                best_pair_score = pair_score

            candidate_color = getattr(c, "color", None)
            candidate_color_conf = getattr(c, "color_confidence", None)
            candidate_color_score = (
                float(candidate_color_conf)
                if candidate_color_conf is not None else -1.0
            )
            if candidate_color and (
                not color or candidate_color_score > best_color_score
            ):
                color = candidate_color
                color_confidence = candidate_color_conf
                best_color_score = candidate_color_score
        return (
            make, model, color,
            make_confidence, model_confidence, color_confidence,
        )

    @staticmethod
    def _bucket_vehicle_bbox(bucket: _VoteBucket):
        """Most recent vehicle box associated with a successful vote."""
        return next(
            (c.vehicle_bbox for c in reversed(bucket.candidates)
             if c.vehicle_bbox is not None),
            None,
        )

    def _anchor_point(self, r, frame_w: int, frame_h: int) -> tuple[float, float]:
        """Normalized (x, y) anchor for the dwell-zone test.

        `vehicle_bottom` approximates ground contact, so it is the most stable
        choice for "where the car actually is" — it doesn't move with plate
        read jitter and doesn't drift as the plate box tightens or loosens.
        """
        anchor = self.cfg.dwell_zone.anchor
        vb = r.vehicle_bbox
        if anchor in ("vehicle_center", "vehicle_bottom") and vb:
            x1, y1, x2, y2 = vb
            cx = (x1 + x2) / 2.0
            cy = y2 if anchor == "vehicle_bottom" else (y1 + y2) / 2.0
            return (cx / frame_w, cy / frame_h)
        # plate_center, or a vehicle anchor with no vehicle box available.
        x1, y1, x2, y2 = r.bbox
        return ((x1 + x2) / 2.0 / frame_w, (y1 + y2) / 2.0 / frame_h)

    def _evaluate_dwell_zone(self, r, frame_w, frame_h) -> tuple[bool, str | None]:
        """(in_zone, rejecting_gate). Always (True, None) when disabled."""
        dz = self.cfg.dwell_zone
        if not dz.enabled:
            return True, None

        nx, ny = self._anchor_point(r, frame_w, frame_h)
        if not dz.contains(nx, ny):
            return False, "dwell_zone_position"

        if dz.min_vehicle_box_area > 0.0:
            vb = r.vehicle_bbox
            if not vb:
                # No vehicle box to measure. Don't silently pass a check the
                # operator explicitly asked for.
                return False, "dwell_zone_no_vehicle_box"
            x1, y1, x2, y2 = vb
            area = (abs(x2 - x1) * abs(y2 - y1)) / float(frame_w * frame_h)
            if area < dz.min_vehicle_box_area:
                return False, "dwell_zone_area"
        return True, None

    def _run_one(self, frame) -> list[DetectionResult]:
        """Run inference on one frame, return all detections that PASSED
        the per-frame threshold check. Detections below threshold are dropped here.
        Detections outside the zone get in_zone=False but are still returned so
        we can emit telemetry about them."""
        if self._engine is None:
            raise RuntimeError("Detector.open() not called")

        t0 = time.monotonic()
        results = self._engine.infer(frame)   # the ONLY engine-aware line
        inference_ms = int((time.monotonic() - t0) * 1000)

        # Track the REAL frame size rather than trusting config: the movement
        # gate normalizes by the frame diagonal, and a config that disagrees
        # with the actual capture would skew every travel fraction.
        _fh0, _fw0 = frame.shape[:2]
        self._frame_wh = (_fw0, _fh0)

        import datetime as _dt
        _now = _dt.datetime.now(_dt.timezone.utc)

        def provider_capture_time(raw):
            """Return provider time when valid, otherwise local capture time."""
            supplied = getattr(raw, "captured_at_iso", None)
            if supplied:
                try:
                    parsed = _dt.datetime.fromisoformat(
                        str(supplied).strip().replace("Z", "+00:00")
                    )
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
                    return parsed.timestamp(), str(supplied)
                except (TypeError, ValueError, OverflowError):
                    log.warning(
                        "invalid provider capture timestamp; using local time"
                    )
            return (
                _now.timestamp(),
                _now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            )

        out: list[DetectionResult] = []
        for r in results or []:
            # Engine-neutral RawPlateRead: already coerced to 0.0-1.0
            # confidences and an axis-aligned bbox by the adapter. The
            # thresholding / normalization / zone logic below is unchanged and
            # is deliberately the same for every engine.
            det_conf = r.detection_confidence
            ocr_text = r.text
            ocr_conf = r.ocr_confidence
            bbox = r.bbox
            captured_at_epoch, captured_at_iso = provider_capture_time(r)

            # Determine whether this box clears the per-frame identify floors.
            below = (
                det_conf < self.cfg.thresholds.min_detection_confidence
                or ocr_conf < self.cfg.thresholds.min_ocr_confidence
            )

            normalized = normalize(
                ocr_text,
                min_length=self.cfg.thresholds.min_normalized_length,
                max_length=self.cfg.thresholds.max_normalized_length,
            )
            # A box that fails normalization is still a real detection for
            # presence purposes; it just carries no usable plate string.

            # A genuine plateless vehicle carries a useful vehicle box and a
            # degenerate plate box. Zone-testing (0,0,0,0) drops that car at
            # the edge of the frame before presence tracking can count it.
            # Prefer real plate geometry; otherwise use the SDK vehicle box.
            zone_bbox = bbox
            if (zone_bbox is None or tuple(zone_bbox) == (0, 0, 0, 0)):
                zone_bbox = r.vehicle_bbox or (0, 0, 0, 0)
            in_zone = bbox_inside_zone(
                zone_bbox, frame.shape, self.cfg.detection_zone
            )

            fh, fw = frame.shape[:2]
            in_dwell, dwell_gate = self._evaluate_dwell_zone(r, fw, fh)

            # Which gate rejected this read — recorded for calibration so the
            # on-site question "why didn't that confirm?" has an answer.
            rejected_by = None
            if det_conf < self.cfg.thresholds.min_detection_confidence:
                rejected_by = "app_detect_min"
            elif ocr_conf < self.cfg.thresholds.min_ocr_confidence:
                rejected_by = "app_ocr_min"
            elif normalized is None:
                rejected_by = "normalize"
            elif not in_zone:
                rejected_by = "detection_zone"
            elif dwell_gate:
                rejected_by = dwell_gate

            if rejected_by and self.cfg.diagnostics.log_rejected_reads:
                # Opt-in calibration flag: INFO so journalctl -u plate-reader
                # actually shows why a read missed. Keep this off in steady
                # production — plate text is PII-adjacent.
                log.info(
                    "read rejected by %s: text=%r det=%.3f ocr=%.3f bbox=%s",
                    rejected_by, ocr_text, det_conf, ocr_conf, bbox,
                )

            out.append(DetectionResult(
                raw_text=ocr_text,
                normalized=normalized,
                detection_confidence=det_conf,
                ocr_confidence=ocr_conf,
                bbox=bbox,
                inference_ms=inference_ms,
                captured_at_epoch=captured_at_epoch,
                captured_at_iso=captured_at_iso,
                in_zone=in_zone,
                vehicle_bbox=r.vehicle_bbox,
                vehicle_confidence=r.vehicle_confidence,
                make=getattr(r, "make", None),
                model=getattr(r, "model", None),
                color=getattr(r, "color", None),
                make_confidence=getattr(r, "make_confidence", None),
                model_confidence=getattr(r, "model_confidence", None),
                color_confidence=getattr(r, "color_confidence", None),
                encounter_id=getattr(r, "encounter_id", None),
                had_competing_variants=getattr(
                    r, "had_competing_variants", False
                ),
                source_observation_id=getattr(
                    r, "source_observation_id", None
                ),
                in_dwell_zone=in_dwell,
                rejected_by=rejected_by,
                # Below threshold OR unnormalizable → identify pipeline ignores it.
                below_threshold=below or normalized is None,
                engine_image=getattr(r, "image", None),
            ))
        return out

    def collect_vehicle_only(
        self,
        now: float | None = None,
        timeout_seconds: float = 3.0,
        min_frames: int = 2,
        flush: bool = False,
    ) -> list[VehicleOnlyIdentification]:
        """Vehicles that were tracked but never produced a usable plate.

        Emits only once a vehicle has been GONE for `timeout_seconds` — while
        it is still in frame a plate may yet be read, and emitting early would
        report a car that is about to identify itself. `flush=True` drains
        everything regardless, for end-of-stream.

        `min_frames` suppresses one-frame detector blips, mirroring the
        presence tracker's own min_track_frames idea.

        These carry NO plate string and NO hash, BY TYPE. A plateless vehicle
        must never be written as a plate-identified visit — there is no
        `normalized` field to hash even by accident.
        """
        now = time.time() if now is None else now
        vo_cfg = self.cfg.vehicle_only
        out: list[VehicleOnlyIdentification] = []
        for key, rec in list(self._vehicle_only_pending.items()):
            gone_for = now - rec["last_epoch"]
            if not flush and gone_for < timeout_seconds:
                continue                       # still around; may yet identify
            self._vehicle_only_pending.pop(key, None)
            if rec["identified"]:
                continue                       # got a plate after all
            if rec["frames"] < min_frames:
                continue                       # single-frame blip

            travel, growth = self._movement_evidence(rec)
            # Every candidate that reaches here is a genuinely unidentified
            # vehicle box. Count it BEFORE the movement gate so the raw number
            # is always available to compare against a hand count of the same
            # footage — that comparison is how the gate gets tuned, and it is
            # impossible if the pre-gate number is never recorded.
            self.vehicle_only_seen_raw += 1

            need_travel = vo_cfg.min_travel_fraction
            need_growth = vo_cfg.min_area_growth_ratio
            moved = need_travel > 0.0 and travel >= need_travel
            grew = need_growth > 0.0 and growth >= need_growth
            gates_off = need_travel == 0.0 and need_growth == 0.0
            if not (gates_off or moved or grew):
                # Static box: a parked car, roadside furniture, or a detector
                # artifact. Not a customer who drove through.
                self.vehicle_only_static_dropped += 1
                log.debug(
                    "vehicle-only candidate dropped as static: frames=%d "
                    "travel=%.3f (need %.3f) growth=%.2f (need %.2f)",
                    rec["frames"], travel, need_travel, growth, need_growth,
                )
                continue

            out.append(VehicleOnlyIdentification(
                detected_at_iso=rec["first_iso"],
                frame_count=rec["frames"],
                vehicle_bbox=rec["bbox"],
                vehicle_confidence=rec["conf"],
                seconds_in_zone=round(rec["last_epoch"] - rec["first_epoch"], 2),
                travel_fraction=round(travel, 4),
                area_growth_ratio=round(growth, 3),
                had_plate_candidate=rec["had_plate"],
            ))
        return out

    @staticmethod
    def _boxes_overlap(a, b) -> bool:
        """Cheap association test: do two vehicle boxes plausibly overlap?

        A full tracker lives in presence.py; this only needs to decide whether
        two consecutive frames show the SAME car, so centre-inside-the-other
        is sufficient and costs nothing.
        """
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        acx, acy = (ax1 + ax2) / 2.0, (ay1 + ay2) / 2.0
        bcx, bcy = (bx1 + bx2) / 2.0, (by1 + by2) / 2.0
        return ((bx1 <= acx <= bx2 and by1 <= acy <= by2)
                or (ax1 <= bcx <= ax2 and ay1 <= bcy <= ay2))

    @staticmethod
    def _box_center_area(box) -> tuple[float, float, float]:
        x1, y1, x2, y2 = box
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0,
                max(0.0, float(x2 - x1)) * max(0.0, float(y2 - y1)))

    def _movement_evidence(self, rec) -> tuple[float, float]:
        """(travel as a fraction of the frame diagonal, area growth ratio).

        These are what stand in for a dwell zone. A car driving the lane
        either crosses the frame or swells as it approaches the camera; a
        parked car in the background does neither, however many frames it is
        visible for.
        """
        fw, fh = self._frame_wh
        diag = math.hypot(float(fw), float(fh)) or 1.0
        travel = math.hypot(rec["last_cx"] - rec["first_cx"],
                            rec["last_cy"] - rec["first_cy"]) / diag
        first_area = rec["first_area"]
        growth = (rec["last_area"] / first_area) if first_area > 0 else 0.0
        return travel, growth

    def _note_vehicle_only(self, candidates, now: float) -> None:
        """Track vehicle boxes, and whether each ever yielded a usable plate.

        Associates by box overlap rather than a fixed grid: a moving car
        crosses grid cells, and grid-keying would emit several vehicle-only
        events for one vehicle.
        """
        if not self.cfg.alpr.classifiers.vehicle_detect_enabled:
            return
        vo_cfg = self.cfg.vehicle_only
        if not vo_cfg.enabled:
            return

        # A vehicle-only event asserts "a car went through and we could not
        # identify it" — which is exactly the KPI, since an unidentified car
        # is one the FIFO queue cannot match to an order.
        #
        # This used to require a calibrated dwell zone and emit NOTHING
        # without one. That is the wrong default for a miss-counter: a unit
        # with no zone would report zero misses and look perfect. The zone
        # answers a different question (was this car ORDERING?), which
        # belongs to drive-off tracking. Noise is handled by the movement
        # gate in _movement_evidence() instead, which needs no calibration.
        if vo_cfg.require_dwell_zone and not self.cfg.dwell_zone.enabled:
            if not self._warned_vehicle_only_no_zone:
                self._warned_vehicle_only_no_zone = True
                log.warning(
                    "presence.vehicle_only.require_dwell_zone is set but "
                    "presence.dwell_zone is DISABLED — vehicle-only events "
                    "are suppressed and missed vehicles will NOT be counted. "
                    "Either calibrate the zone (tools/calibrate.py) or clear "
                    "require_dwell_zone."
                )
            return

        for c in candidates:
            if c.vehicle_bbox is None:
                continue
            # The dwell zone still applies when one IS configured, so the
            # later drive-off work keeps its filtering; it just is not
            # required for the count to happen at all.
            if self.cfg.dwell_zone.enabled and not c.in_dwell_zone:
                continue
            if (vo_cfg.min_vehicle_confidence > 0.0
                    and (c.vehicle_confidence or 0.0)
                    < vo_cfg.min_vehicle_confidence):
                continue

            cx, cy, area = self._box_center_area(c.vehicle_bbox)

            match = None
            for key, rec in self._vehicle_only_pending.items():
                if self._boxes_overlap(c.vehicle_bbox, rec["bbox"]):
                    match = key
                    break
            if match is None:
                match = self._vehicle_only_seq
                self._vehicle_only_seq += 1
                self._vehicle_only_pending[match] = {
                    "first_epoch": now,
                    "first_iso": c.captured_at_iso,
                    "frames": 0,
                    "identified": False,
                    "bbox": c.vehicle_bbox,
                    "conf": c.vehicle_confidence,
                    "first_cx": cx, "first_cy": cy, "first_area": area,
                    "had_plate": False,
                }

            rec = self._vehicle_only_pending[match]
            rec["frames"] += 1
            rec["last_epoch"] = now
            rec["bbox"] = c.vehicle_bbox
            rec["last_cx"], rec["last_cy"], rec["last_area"] = cx, cy, area
            if c.vehicle_confidence is not None:
                rec["conf"] = c.vehicle_confidence
            # A plate BOX was localized here even if it was never readable.
            # Engines emit (0, 0, 0, 0) for "car, no plate found", so a
            # degenerate box is not evidence of a plate.
            if c.bbox is not None and c.bbox != (0, 0, 0, 0):
                rec["had_plate"] = True
            if c.normalized is not None:
                rec.setdefault("seen_normalized", set()).add(c.normalized)

    def mark_vehicle_identified(
        self,
        normalized: str,
        vehicle_bbox: tuple[int, int, int, int] | None = None,
    ) -> None:
        """Reconcile a *confirmed* plate with the vehicle-only tracker.

        A single qualifying read no longer marks a car identified. We wait for
        the vote to complete, then prefer a record that both saw the exact
        normalized string and overlaps the confirming vehicle box.
        """
        matches = []
        for key, rec in self._vehicle_only_pending.items():
            if normalized not in rec.get("seen_normalized", set()):
                continue
            overlap = (
                self._boxes_overlap(vehicle_bbox, rec["bbox"])
                if vehicle_bbox is not None else False
            )
            matches.append((1 if overlap else 0, rec.get("last_epoch", 0.0), key))
        if not matches:
            return
        _, _, best_key = max(matches)
        self._vehicle_only_pending[best_key]["identified"] = True

    def _consensus_rescue_eligible(self, c: DetectionResult) -> bool:
        """Whether one rejected read may enter the conservative rescue vote.

        This is intentionally narrower than lowering the normal gates:
        normalization, zone checks and a stable physical encounter are all
        mandatory; exactly one normal confidence gate may fail; and both
        scores must still clear the engine-specific rescue floors.
        """
        rt = self.cfg.alpr.thresholds
        if rt.consensus_rescue_min_frames < 3:
            return False
        if (
            not c.below_threshold
            or c.normalized is None
            or not c.encounter_id
            or not c.in_zone
            or not c.in_dwell_zone
        ):
            return False
        cleared_det = (
            c.detection_confidence
            >= self.cfg.thresholds.min_detection_confidence
        )
        cleared_ocr = (
            c.ocr_confidence >= self.cfg.thresholds.min_ocr_confidence
        )
        # Both passed belongs in the normal vote. Both failed is too weak to
        # use for identity, no matter how many times the engine repeats it.
        if cleared_det == cleared_ocr:
            return False
        return (
            c.detection_confidence >= rt.consensus_rescue_detect_min
            and c.ocr_confidence >= rt.consensus_rescue_ocr_min
        )

    def _offer_consensus_rescue(
        self, c: DetectionResult, crop
    ) -> _VoteBucket | None:
        """Add one exact, encounter-bound weak read; return a ready vote."""
        if not self._consensus_rescue_eligible(c):
            return None
        assert c.encounter_id is not None and c.normalized is not None
        rescue_key = (c.encounter_id, c.normalized)
        bucket = self._rescue_buckets.get(rescue_key)
        cleared_det = (
            c.detection_confidence
            >= self.cfg.thresholds.min_detection_confidence
        )
        if bucket is None:
            bucket = _VoteBucket(
                normalized=c.normalized,
                first_seen_epoch=c.captured_at_epoch,
                first_seen_iso=c.captured_at_iso,
                candidates=[c],
            )
            bucket.maybe_take_crop(c, crop, cleared_det=cleared_det)
            self._rescue_buckets[rescue_key] = bucket
        else:
            bucket.add(c, crop, cleared_det=cleared_det)
        if (
            len(bucket.candidates)
            < self.cfg.alpr.thresholds.consensus_rescue_min_frames
        ):
            return None
        self._rescue_buckets.pop(rescue_key, None)
        return bucket

    def _provider_geometry_ready(self, c: DetectionResult, rt) -> bool:
        """Shared stable-encounter, length and rear-plate safeguards."""
        if not c.encounter_id or c.normalized is None:
            return False
        if len(c.normalized) < int(getattr(
            rt, "provider_selected_single_min_length", 5
        )):
            return False
        if not c.bbox or not c.vehicle_bbox:
            return False
        px1, _py1, px2, _py2 = c.bbox
        vx1, _vy1, vx2, _vy2 = c.vehicle_bbox
        vehicle_width = vx2 - vx1
        if px2 <= px1 or vehicle_width <= 0:
            return False
        relative_x = (((px1 + px2) / 2.0) - vx1) / vehicle_width
        return relative_x >= float(getattr(
            rt, "provider_selected_single_min_relative_x", 0.45
        ))

    def _provider_selected_single_ready(self, bucket: _VoteBucket) -> bool:
        """Trust one PR result only after provider-side temporal selection.

        Stream's ``max_prediction_delay`` makes one webhook the selected best
        result from many analyzed source frames. The detector cannot count
        those hidden frames as independent votes, so this site-calibrated path
        treats the selected result as temporal evidence while retaining the
        ordinary confidence gates and adding physical safeguards that reject
        measured short fragments and bumper/window text.
        """
        rt = getattr(getattr(self.cfg, "alpr", None), "thresholds", None)
        if not bool(getattr(rt, "provider_selected_single_enabled", False)):
            return False
        if getattr(getattr(self.cfg, "alpr", None), "engine", None) != "pr_stream":
            return False
        if len(bucket.candidates) != 1:
            return False
        c = bucket.candidates[0]
        if c.below_threshold:
            return False
        return self._provider_geometry_ready(c, rt)

    def _provider_high_ocr_rescue_ready(self, c: DetectionResult) -> bool:
        """Recover one PR-selected, near-certain OCR with weak localization.

        This is deliberately asymmetric: only the normal detection gate may
        fail. OCR must clear a stricter rescue floor, the settled encounter
        must contain no competing normalized variant, and the existing
        provider-selected length/vehicle-geometry safeguards still apply.
        """
        rt = getattr(getattr(self.cfg, "alpr", None), "thresholds", None)
        if not bool(getattr(rt, "provider_high_ocr_rescue_enabled", False)):
            return False
        if getattr(getattr(self.cfg, "alpr", None), "engine", None) != "pr_stream":
            return False
        if not c.below_threshold or c.rejected_by != "app_detect_min":
            return False
        if c.had_competing_variants:
            return False
        if c.detection_confidence < float(getattr(
            rt, "provider_high_ocr_rescue_detect_min", 0.0
        )):
            return False
        if c.ocr_confidence < float(getattr(
            rt, "provider_high_ocr_rescue_ocr_min", 1.0
        )):
            return False
        return self._provider_geometry_ready(c, rt)

    def _drop_consensus_rescue_family(
        self, normalized: str, encounter_id: str | None
    ) -> None:
        """Discard every weak vote that could represent the confirmed car."""
        for rescue_key in list(self._rescue_buckets):
            enc, key = rescue_key
            if key == normalized or (encounter_id and enc == encounter_id):
                self._rescue_buckets.pop(rescue_key, None)

    def process_frame(
        self, frame, now: float | None = None
    ) -> tuple[list[DetectionResult], list[ConfirmedIdentification | FailedIdentification]]:
        """Run a frame through inference and update vote buckets.

        Returns a 2-tuple:
          - candidates: ALL localized plate boxes this frame (including
            sub-threshold ones), for the presence tracker to consume.
          - outcomes: zero or more ConfirmedIdentification / FailedIdentification
            from completed or expired votes.

        Voting semantics (now actually enforced):
          * A candidate that clears the per-frame identify floors opens a vote
            bucket keyed by physical encounter plus exact normalized string,
            OR reinforces one. Legacy reads without an encounter share None.
          * Confirmation requires vote_min_frames qualifying frames whose
            min(det)/min(ocr) clear the group floors, OR one frame whose OCR
            clears high_ocr_single_frame (when that gate is > 0).
          * If configured, three or more exact sub-threshold reads can confirm
            only when they share one encounter, clear the rescue floors, and
            fail exactly one normal confidence gate.
          * When a bucket's window expires: fewer agreeing frames than needed
            → 'low_confidence'; enough agreeing frames that never cleared the
            group floor → 'group_confidence_floor'.
          * Plate reads rejected before the vote (OCR/det floors, normalize)
            join a near-match miss episode and flush as one detection-event
            only after the plate goes quiet — never per OCR variant, and never
            if that vehicle later confirms.
          * A confirmation emits the bucket's exact `normalized` string, which
            is the same string every candidate in it read.
        """
        supplied_now = now is not None
        candidates = self._run_one(frame)
        if self._presence_dumper is not None:
            self._presence_dumper.submit(frame, candidates)

        # Replay and acceptance tools can supply a capture timestamp so dwell,
        # cooldown, and track behavior are deterministic. Production callers
        # omit it and use the camera-time wall clock as before.
        if now is None:
            now = (
                max(c.captured_at_epoch for c in candidates)
                if candidates else time.time()
            )
        else:
            now = float(now)
        if supplied_now and candidates:
            import datetime as _dt
            captured_iso = (
                _dt.datetime.fromtimestamp(now, _dt.timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            for candidate in candidates:
                candidate.captured_at_epoch = now
                candidate.captured_at_iso = captured_iso

        # For in-process engines, one SDK call is one independent capture,
        # regardless of how many duplicate/pyramidal boxes it returns. Stream
        # is different: its infer() call drains a queue and can therefore carry
        # several independently selected provider observations at once. Keep
        # one strongest string per source observation, not per local drain.
        self._capture_seq += 1
        vote_by_observation: dict[
            tuple[str | None, str, str], DetectionResult
        ] = {}
        for candidate in candidates:
            if candidate.normalized is None:
                continue
            observation = (
                candidate.source_observation_id
                or f"local-capture:{self._capture_seq}"
            )
            vote_key = (
                candidate.encounter_id,
                candidate.normalized,
                observation,
            )
            incumbent = vote_by_observation.get(vote_key)
            if incumbent is None or min(
                candidate.detection_confidence, candidate.ocr_confidence
            ) > min(incumbent.detection_confidence, incumbent.ocr_confidence):
                vote_by_observation[vote_key] = candidate
        # A queue drain is not contractually ordered. Process independent
        # evidence chronologically so the bucket's detected_at remains the
        # earliest provider frame while `now` above advances to the latest.
        vote_candidates = sorted(
            vote_by_observation.values(),
            key=lambda candidate: candidate.captured_at_epoch,
        )

        if getattr(self, "_enable_vehicle_only_tracking", True):
            self._note_vehicle_only(candidates, now)
        window_s = self.cfg.thresholds.vote_window_seconds
        outcomes: list[ConfirmedIdentification | FailedIdentification] = []

        # Dwell time must be continuous. If a normalized plate is absent from
        # this capture, discard its entry time instead of counting the gap.
        visible_in_dwell = {
            (c.encounter_id, c.normalized) for c in candidates
            if c.normalized is not None and c.in_zone and c.in_dwell_zone
        }
        for key in list(self._zone_entry_epoch):
            if key not in visible_in_dwell:
                self._zone_entry_epoch.pop(key, None)

        # Bound the exact/near-match cache. Without pruning, every historical
        # plate stayed resident and every later confirm scanned the full set.
        confirm_ttl = max(self.cfg.dedup.cooldown_seconds, 600.0)
        for key, confirmed_at in list(self._last_confirm_epoch.items()):
            if now - confirmed_at > confirm_ttl:
                self._last_confirm_epoch.pop(key, None)
                self._last_confirm_encounter.pop(key, None)
        for encounter_id, confirmed_at in list(
            self._confirmed_encounter_epoch.items()
        ):
            if now - confirmed_at > confirm_ttl:
                self._confirmed_encounter_epoch.pop(encounter_id, None)

        # 0. Dwell bookkeeping: which known plates are visible THIS frame?
        #    Uses every localized box (including sub-threshold ones) — a weak
        #    read still proves the car hasn't left, which is exactly what the
        #    absence gate needs to know. Using only qualifying reads here would
        #    let a briefly-blurry plate look "absent" and wrongly release.
        #    Compared against dwell keys, which are exact normalized strings.
        seen_this_frame = {
            c.normalized
            for c in candidates if c.normalized is not None
        }
        absent_needed = self.cfg.thresholds.dwell_release_requires_absent_frames
        for norm, rec in self._dwell.items():
            if norm in seen_this_frame:
                rec.mark_seen()
            else:
                rec.mark_absent(now)
                # Latch as soon as the absence bar is cleared. Once latched it
                # stays latched until the next confirm — so a car that leaves
                # and comes back is still recognized as having left, even
                # though it's visible again by the time it re-confirms.
                #
                # When a time-based requirement is configured it REPLACES the
                # frame count, because a frame count means different things at
                # different fps. 0.0 keeps the legacy frame behaviour.
                absent_secs_needed = self.cfg.dedup.release_requires_absent_seconds
                if absent_secs_needed > 0.0:
                    if rec.absent_seconds(now) >= absent_secs_needed:
                        rec.left_frame_since_confirm = True
                elif rec.absent_frames >= absent_needed:
                    rec.left_frame_since_confirm = True

        # Expire dwell records that are long past any possible relevance, so
        # the table can't grow unbounded on a busy lane.
        dwell_ttl = max(
            self.cfg.thresholds.dwell_suppress_seconds * 10.0, 600.0
        )
        stale = [
            k for k, r in self._dwell.items()
            if (now - r.last_confirm_epoch) > dwell_ttl
        ]
        for k in stale:
            self._dwell.pop(k, None)

        # 1. Reap expired buckets into near-match miss episodes.
        #    Do NOT post AWS immediately: a later confirming read of the same
        #    car (or OCR variant) must cancel the miss, otherwise one vehicle
        #    becomes both a detection-event and a visit.
        expired_keys = [
            k for k, b in self._buckets.items() if b.is_expired(now, window_s)
        ]
        for storage_key in expired_keys:
            bucket = self._buckets.pop(storage_key)
            if self._vote_frames_ready(bucket):
                # Enough agreeing frames existed for either the multi-frame
                # path or the high-OCR single-frame path, so the ONLY thing
                # that can have stopped this confirming is the group floor.
                #
                # This used to be reported as "vote_disagreement", which is
                # impossible by construction: buckets are keyed by the EXACT
                # normalized string, so every candidate in one already agrees
                # character-for-character. Genuine disagreement shows up as
                # SEPARATE buckets, not as a mixed one. That misnomer is why
                # 5 of 20 cars looked like an OCR problem for so long — the
                # logs blamed the reads when the threshold was at fault.
                failed = bucket.to_failed("group_confidence_floor")
            else:
                failed = bucket.to_failed("low_confidence")
            self._note_pending_miss(
                bucket.normalized,
                now=now,
                detected_at_iso=failed.detected_at_iso,
                det=failed.top_detection_confidence,
                ocr=failed.top_ocr_confidence,
                inference_ms=failed.inference_duration_ms,
                outcome=failed.outcome,
                frames=failed.frame_count,
                # Per-frame pairs, not the bucket's independent maxima, and
                # the bucket's own size so `max_bucket_size` reports the
                # largest agreement this episode ever achieved.
                samples=bucket.samples(),
                bucket_size=len(bucket.candidates),
                last_seen_iso=failed.episode_end,
                crop=bucket.best_crop,
                raw_text=bucket.best_raw_text,
                crop_cleared_det=True,
                bbox=bucket.best_bbox,
                encounter_id=failed.encounter_id,
            )
        # Weak rescue reads already feed the ordinary pending-miss episode
        # below, so expiry only drops their auxiliary exact-agreement state;
        # emitting here would count the same vehicle twice.
        for rescue_key, bucket in list(self._rescue_buckets.items()):
            if bucket.is_expired(now, window_s):
                self._rescue_buckets.pop(rescue_key, None)

        # 2. Process identity candidates. Normal reads use the ordinary vote;
        #    narrowly eligible sub-threshold reads use a separate bucket that
        #    can never combine evidence from different physical encounters.
        for c in vote_candidates:
            if c.normalized is None:
                continue
            if (
                c.encounter_id
                and c.encounter_id in self._confirmed_encounter_epoch
            ):
                # A physical encounter has one terminal identity. OCR variants
                # after its first confirm are attributes of that car, not new
                # cars and not new customer hashes.
                continue
            if not c.in_zone:
                log.debug("candidate out of zone, ignoring: %s", c.normalized)
                continue
            if not c.in_dwell_zone:
                # Detected, readable, but not an ordering vehicle — an exiting
                # car or background road traffic. Never enters the vote.
                log.debug("candidate outside dwell zone (%s), ignoring",
                          c.rejected_by)
                self._zone_entry_epoch.pop(
                    (c.encounter_id, c.normalized), None
                )
                continue

            # Continuous time inside the zone. Time-based, not frame-count:
            # frame counts mean different things at different fps.
            entry = self._zone_entry_epoch.setdefault(
                (c.encounter_id, c.normalized), c.captured_at_epoch
            )
            secs_in_zone = c.captured_at_epoch - entry
            if secs_in_zone < self.cfg.dwell_zone.min_seconds_in_zone:
                log.debug(
                    "candidate in zone only %.2fs of %.2fs required",
                    secs_in_zone, self.cfg.dwell_zone.min_seconds_in_zone,
                )
                continue

            # Group by the EXACT normalized string. After the min-per-char
            # quality gate above, genuine reads of one plate agree exactly,
            # so exact matching costs nothing and cannot merge two different
            # plates the way a fuzzy fold could.
            key = c.normalized
            vote_storage_key = (c.encounter_id, key)
            crop = self._plate_crop(frame, c.bbox, c.engine_image)
            confirmation_method = "normal_vote"
            if c.below_threshold:
                if self._provider_high_ocr_rescue_ready(c):
                    bucket = _VoteBucket(
                        normalized=key,
                        first_seen_epoch=c.captured_at_epoch,
                        first_seen_iso=c.captured_at_iso,
                        candidates=[c],
                    )
                    bucket.maybe_take_crop(c, crop, cleared_det=False)
                    confirmation_ready = True
                    confirmation_method = "provider_high_ocr_rescue"
                else:
                    bucket = self._offer_consensus_rescue(c, crop)
                    if bucket is None:
                        continue
                    confirmation_ready = True
                    confirmation_method = "consensus_rescue"
            else:
                bucket = self._buckets.get(vote_storage_key)
                if bucket is None:
                    # First qualifying sighting. May confirm immediately via
                    # the high-OCR path; otherwise wait for agreement.
                    bucket = _VoteBucket(
                        normalized=key,
                        first_seen_epoch=c.captured_at_epoch,
                        first_seen_iso=c.captured_at_iso,
                        candidates=[c],
                    )
                    bucket.maybe_take_crop(c, crop)
                    self._buckets[vote_storage_key] = bucket
                else:
                    # Same plate again — add to bucket and try to confirm.
                    bucket.add(c, crop)
                confirmation_ready = self._vote_frames_ready(bucket)
                if (
                    not confirmation_ready
                    and self._provider_selected_single_ready(bucket)
                ):
                    confirmation_ready = True
                    confirmation_method = "provider_selected_single"

            min_det = min(
                x.detection_confidence for x in bucket.candidates
            )
            min_ocr = min(x.ocr_confidence for x in bucket.candidates)
            if confirmation_method not in (
                "consensus_rescue", "provider_high_ocr_rescue"
            ):
                # Normal votes retain the existing group gate. Rescue members
                # were individually checked against their dedicated floors
                # before entering their encounter-bound bucket.
                confirmation_ready = (
                    confirmation_ready
                    and min_det >= self.cfg.thresholds.group_detection_floor
                    and min_ocr >= self.cfg.thresholds.group_ocr_floor
                )

            if confirmation_ready:
                total_ms = sum(x.inference_ms for x in bucket.candidates)
                # Make/model/colour of the confirming vehicle, if the engine
                # classifies. Descriptive only — never identity input.
                _mmc = self._bucket_mmc(bucket)
                self._buckets.pop(vote_storage_key, None)
                encounter_id = bucket.encounter_id()
                self._drop_consensus_rescue_family(key, encounter_id)
                if encounter_id:
                    self._confirmed_encounter_epoch[encounter_id] = now
                # This vehicle identified — never also count it as a miss
                # (including unrelated OCR junk from the same encounter).
                self._drop_pending_miss_family(
                    key, now=now, encounter_id=encounter_id
                )

                # --- dedup: per-hash cooldown (PRIMARY defence) ------------
                # Checked BEFORE the dwell logic, and independent of it. The
                # absence latch can be defeated by read flicker: a plate that
                # drops out for >= release_requires_absent_frames and returns
                # looks like a departed-and-returned car, which is how
                # d532120117e8 confirmed twice 1.0s apart in the Stage 2b run.
                # A wall-clock cooldown on the hash itself cannot be defeated
                # that way.
                cooldown = self.cfg.dedup.cooldown_seconds
                last = self._last_confirm_epoch.get(key)
                if cooldown > 0 and last is not None and (now - last) < cooldown:
                    elapsed = now - last
                    rec = self._dwell.get(key)
                    if rec is not None:
                        rec.repeat_count += 1
                        rec.last_confirm_epoch = now
                    repeat_index = rec.repeat_count if rec else 1
                    since_first = (
                        round(now - rec.first_confirm_epoch, 2) if rec else None
                    )
                    log.debug(
                        "re-confirm SUPPRESSED by cooldown for %s (%.1fs of %.1fs)",
                        key[:8], elapsed, cooldown,
                    )
                    outcomes.append(ConfirmedIdentification(
                        normalized=bucket.best_normalized(),
                        detected_at_iso=bucket.first_seen_iso,
                        frame_count=len(bucket.candidates),
                        detection_confidence=round(min_det, 4),
                        ocr_confidence=round(min_ocr, 4),
                        inference_duration_ms=total_ms,
                        suppressed=True,
                        suppress_reason=(
                            f"cooldown {elapsed:.1f}s < {cooldown:.1f}s"
                        ),
                        seconds_since_first_confirm=since_first,
                        repeat_index=repeat_index,
                        vehicle_bbox=self._bucket_vehicle_bbox(bucket),
                        make=_mmc[0], model=_mmc[1], color=_mmc[2],
                        make_confidence=_mmc[3],
                        model_confidence=_mmc[4],
                        color_confidence=_mmc[5],
                        encounter_id=encounter_id,
                        confirmation_method=confirmation_method,
                    ))
                    continue

                # --- dedup: near-match (a MISREAD of a car just confirmed) --
                # The hash cooldown above only catches an identical string.
                # A truncated or single-character-off read of the same physical
                # plate produces a DIFFERENT hash and slips straight through:
                #     f02959 CHR7067 -> d532120117e8
                #     f02964 CHR706  -> 68d2065f19cd   (1.0s later, same car)
                # Two visits, one customer. Checked only inside the cooldown
                # window, so a genuinely returning car still counts later.
                near = self._find_near_match(key, now, encounter_id)
                if near is not None:
                    other, age = near
                    log.debug(
                        "re-confirm SUPPRESSED as near-match: %s ~ %s (%.1fs)",
                        key, other, age,
                    )
                    outcomes.append(ConfirmedIdentification(
                        normalized=bucket.best_normalized(),
                        detected_at_iso=bucket.first_seen_iso,
                        frame_count=len(bucket.candidates),
                        detection_confidence=round(min_det, 4),
                        ocr_confidence=round(min_ocr, 4),
                        inference_duration_ms=total_ms,
                        suppressed=True,
                        suppress_reason=(
                            f"near_match of {other} confirmed {age:.1f}s ago "
                            f"(edit distance <= "
                            f"{self.cfg.dedup.near_match_max_distance})"
                        ),
                        seconds_since_first_confirm=round(age, 2),
                        repeat_index=1,
                        vehicle_bbox=self._bucket_vehicle_bbox(bucket),
                        make=_mmc[0], model=_mmc[1], color=_mmc[2],
                        make_confidence=_mmc[3],
                        model_confidence=_mmc[4],
                        color_confidence=_mmc[5],
                        encounter_id=encounter_id,
                        confirmation_method=confirmation_method,
                    ))
                    continue

                # --- dwell suppression decision ---
                # Dwell records use the same exact-string key as the buckets,
                # so lookups are consistent.
                rec = self._dwell.get(key)
                suppressed = False
                reason: str | None = None
                since_first: float | None = None
                repeat_index = 0

                if rec is None:
                    # First time we've ever confirmed this plate — always counts.
                    self._dwell[key] = _DwellRecord(
                        normalized=key,
                        first_confirm_epoch=now,
                        last_confirm_epoch=now,
                    )
                else:
                    releasable, why = rec.can_release(now, self.cfg.thresholds)
                    since_first = round(now - rec.first_confirm_epoch, 2)
                    if releasable:
                        # Genuinely a new visit — reset the record's clock and
                        # counters so the next dwell cycle starts fresh.
                        rec.last_confirm_epoch = now
                        rec.other_vehicles_since = 0
                        rec.absent_frames = 0
                        # Re-arm: this car must leave again before it can
                        # count as yet another visit.
                        rec.left_frame_since_confirm = False
                        rec.repeat_count += 1
                        repeat_index = rec.repeat_count
                        log.info(
                            "re-confirm ALLOWED for %s (%s)", key[:8], why
                        )
                    else:
                        suppressed = True
                        reason = why
                        rec.repeat_count += 1
                        repeat_index = rec.repeat_count
                        rec.last_confirm_epoch = now
                        log.debug(
                            "re-confirm SUPPRESSED for %s (%s)", key[:8], why
                        )

                # Record the confirm time for the cooldown gate above. Only
                # unsuppressed confirms arm it, so a suppressed re-read does
                # not keep extending its own cooldown indefinitely.
                if not suppressed:
                    self._last_confirm_epoch[key] = now
                    self._last_confirm_encounter[key] = encounter_id

                image_path = None
                if not suppressed:
                    terminal_already_emitted = False
                    terminal_emitted = getattr(
                        self._engine, "terminal_visit_emitted", None
                    )
                    if callable(terminal_emitted):
                        try:
                            terminal_already_emitted = bool(
                                terminal_emitted(encounter_id)
                            )
                        except Exception:
                            pass
                    if terminal_already_emitted:
                        # main.py removes the earlier anonymous audit case.
                        # Do not write a second confirmed case for a Visit that
                        # is intentionally suppressed as already terminal.
                        bucket.best_crop = None
                    else:
                        image_path = self._persist_confirm_frame(bucket)

                # Emit the SPEC-COMPLIANT normalized string of the best
                # candidate. This is what gets hashed and sent to /visit.
                outcomes.append(ConfirmedIdentification(
                    normalized=bucket.best_normalized(),
                    detected_at_iso=bucket.first_seen_iso,
                    frame_count=len(bucket.candidates),
                    detection_confidence=round(min_det, 4),
                    ocr_confidence=round(min_ocr, 4),
                    inference_duration_ms=total_ms,
                    suppressed=suppressed,
                    suppress_reason=reason,
                    seconds_since_first_confirm=since_first,
                    repeat_index=repeat_index,
                    vehicle_bbox=self._bucket_vehicle_bbox(bucket),
                    make=_mmc[0], model=_mmc[1], color=_mmc[2],
                    make_confidence=_mmc[3],
                    model_confidence=_mmc[4],
                    color_confidence=_mmc[5],
                    diagnostic_image_path=image_path,
                    encounter_id=encounter_id,
                    confirmation_method=confirmation_method,
                ))

        # 3. Accumulate below-vote plate reads into vehicle miss episodes.
        #    Merge by near-match AND by timing (active encounter). Flush at
        #    most one detection-event after silence; a later confirm cancels.
        for c in candidates:
            if not c.in_zone or not c.in_dwell_zone:
                continue
            if not c.below_threshold:
                continue
            if c.rejected_by in (
                "detection_zone",
                "dwell_zone_position",
                "dwell_zone_area",
                "dwell_zone_no_vehicle_box",
            ):
                continue
            dedup_key = c.normalized or (c.raw_text or "").strip().upper()
            if not dedup_key:
                continue
            cleared_det = (
                c.detection_confidence
                >= self.cfg.thresholds.min_detection_confidence
            )
            cleared_ocr = (
                c.ocr_confidence
                >= self.cfg.thresholds.min_ocr_confidence
            )
            self._note_pending_miss(
                dedup_key,
                now=now,
                detected_at_iso=c.captured_at_iso,
                det=c.detection_confidence,
                ocr=c.ocr_confidence,
                inference_ms=c.inference_ms,
                outcome=(
                    "unreadable" if c.rejected_by == "normalize"
                    else "below_threshold"
                ),
                # Which gate this read lost at, recorded against the SAME
                # per-frame floors that rejected it — not re-derived later.
                # A read here never entered a vote, so admitted stays False;
                # note it can still clear both gates and land here on a
                # normalization failure ('unreadable').
                samples=[_ReadSample(
                    det=c.detection_confidence,
                    ocr=c.ocr_confidence,
                    cleared_det=cleared_det,
                    cleared_ocr=cleared_ocr,
                    admitted=False,
                )],
                last_seen_iso=c.captured_at_iso,
                crop=self._plate_crop(frame, c.bbox, c.engine_image),
                raw_text=c.raw_text or "",
                crop_cleared_det=cleared_det,
                bbox=c.bbox,
                encounter_id=c.encounter_id,
            )

        outcomes.extend(self._flush_quiet_misses(now))

        return candidates, outcomes

    def _vote_frames_ready(self, bucket: "_VoteBucket") -> bool:
        """Whether the bucket has enough frames for either confirm path."""
        n = len(bucket.candidates)
        if n >= self.cfg.thresholds.vote_min_frames:
            return True
        high = self.cfg.thresholds.high_ocr_single_frame
        if high > 0.0 and n >= 1:
            min_ocr = min(c.ocr_confidence for c in bucket.candidates)
            if min_ocr >= high:
                return True
        return False

    @property
    def engine(self):
        """The active recognition engine, for callers that need engine-specific
        extras (e.g. draining plateless vehicles from Stream). None until
        open() runs."""
        return self._engine

    def open_vote_count(self) -> int:
        return len(self._buckets) + len(self._rescue_buckets)

    @property
    def last_inference_ok(self) -> bool:
        """Whether the engine completed and decoded its most recent call."""
        if self._engine is None:
            return False
        return bool(getattr(self._engine, "last_call_ok", True))

    def clear_confirm_cooldown_except(self, normalized: str) -> None:
        """Drop hash-cooldowns for every plate except `normalized`.

        Called when a distinct customer confirms, so a prior plate can return
        without waiting out the full revisit cooldown.
        """
        drop = [k for k in self._last_confirm_epoch if k != normalized]
        for k in drop:
            del self._last_confirm_epoch[k]
            self._last_confirm_encounter.pop(k, None)

    def note_vehicle_departed(self, normalized: str | None = None) -> None:
        """Called by the caller (main loop / harness) when a vehicle track
        closes, so dwell records know another car has passed through.

        `normalized` is the plate of the departing vehicle if known — that
        vehicle doesn't count as an "other" against its own record.
        (Compared against dwell keys, which are exact normalized strings.)

        This is what makes a slow queue behave correctly: a plate suppressed
        60s ago is releasable once real traffic has moved through, rather than
        waiting out a fixed timer while the lane is obviously cycling.
        """
        own_key = normalized
        for key, rec in self._dwell.items():
            if own_key is not None and key == own_key:
                continue
            rec.other_vehicles_since += 1

    def dwell_debug_snapshot(self) -> list[dict]:
        """Current dwell table, for the harness's per-track reporting."""
        now = time.time()
        return [
            {
                "plate": k[:8],
                "repeats": r.repeat_count,
                "absent_frames": r.absent_frames,
                "others_since": r.other_vehicles_since,
                "age_s": round(now - r.first_confirm_epoch, 1),
            }
            for k, r in self._dwell.items()
        ]
