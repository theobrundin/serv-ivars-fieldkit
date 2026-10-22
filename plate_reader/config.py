"""Load and validate config.json. Fail loud on bad config — better to refuse
to start than to run with bogus settings."""
import json
import re
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .log import get

log = get(__name__)

CONFIG_PATH = Path("/home/serv/plate_reader/config.json")
BOOTSTRAP_LOG_PATH = Path("/var/log/serv/bootstrap.log")

HEX_64 = re.compile(r"^[0-9a-f]{64}$")
DEVICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{2,63}$")
LOCATION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{2,63}$")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class ConfigError(Exception):
    """Raised on invalid or missing config. Caller should log to bootstrap
    location and exit."""


@dataclass
class Thresholds:
    min_detection_confidence: float
    min_ocr_confidence: float
    min_group_confidence: float
    vote_window_seconds: float
    min_normalized_length: int
    max_normalized_length: int
    # --- group floors, applied to min(det) / min(ocr) ACROSS a vote ---------
    # Split from the single min_group_confidence, which applied one number to
    # both scores and sat BEHIND much looser per-frame floors. That asymmetry
    # silently ate reads: a frame cleared the per-frame gate at ocr 0.76, was
    # admitted to a bucket, found agreeing frames, and was then re-judged at
    # 0.80 and dropped. The per-frame floor was decorative for anything in
    # 0.75-0.80.
    #
    # Measured on dtfootagefinal (20 plated cars, hand-labelled):
    #   group 0.80/0.80, 2 frames -> 15/20 cars, 2 with competing strings
    #   group 0.50/0.75, 2 frames -> 20/20 cars, 4 with competing strings
    #   group 0.50/0.75, 3 frames -> 20/20 cars, 1 with competing strings
    # Aligning the floors and requiring a THIRD agreeing frame beats the old
    # setting on both recall and ambiguity: a third independent frame reading
    # the identical string is stronger evidence than a higher score on two.
    #
    # None => fall back to the per-frame floor, so the group gate can never
    # again be stricter than the gate that fed it.
    min_group_detection_confidence: float | None = None
    min_group_ocr_confidence: float | None = None
    # How many frames must independently read the IDENTICAL plate string
    # before we confirm an identity. 2 is the default; a single high-OCR
    # frame can still confirm early via high_ocr_single_frame below.
    vote_min_frames: int = 2
    # If min(ocr) across the bucket is at least this, confirm with ONE frame
    # even when vote_min_frames is 2. 0 disables the fast path.
    # Short-dwell / zoomed board sites use 0.85 so a clean read ships
    # immediately while weaker 0.75–0.85 reads still need two agreements.
    high_ocr_single_frame: float = 0.85
    # --- dwell suppression (distinct from behavior.cooldown_seconds) ---
    # These solve a DIFFERENT problem than the cloud-send cooldown:
    #   behavior.cooldown_seconds  = "don't count a RETURNING customer as a new
    #                                 visit too soon" (revisit policy, ~600s)
    #   dwell_suppress_seconds     = "don't re-confirm a car that never left the
    #                                 frame" (same-car dwell, ~60s)
    # A car idling at the order point trips the second, not the first.
    dwell_suppress_seconds: float = 60.0
    # A dwell-suppressed plate is released early once this many OTHER vehicles
    # have passed through — a slow queue shouldn't wrongly suppress a genuine
    # later visit. 0 disables early release.
    dwell_release_after_other_vehicles: int = 1
    # ...but early release ALSO requires the plate to have actually vanished
    # for this many consecutive frames first (option "c"). A car that never
    # left the frame is not a new visit, no matter what else drove past.
    dwell_release_requires_absent_frames: int = 3

    @property
    def group_detection_floor(self) -> float:
        """Detection floor applied to min(det) across a vote.

        Deliberately NOT clamped to the per-frame floor. "A confirmed identity
        must clear a higher bar than a mere candidate" is a legitimate policy,
        and clamping would make this gate incapable of ever rejecting
        anything: bucket members have already passed the per-frame gate, so
        min() across them is always >= those floors.

        The original defect was that the tax was INVISIBLE, not that it
        existed — reads died here and were reported as `vote_disagreement`,
        which is why 5 of 20 cars went unexplained. Visibility is handled by
        the startup warning in load() and by the distinct
        `group_confidence_floor` failure outcome, not by removing the lever.
        """
        if self.min_group_detection_confidence is not None:
            return self.min_group_detection_confidence
        return self.min_detection_confidence

    @property
    def group_ocr_floor(self) -> float:
        """OCR floor applied to min(ocr) across a vote. See above."""
        if self.min_group_ocr_confidence is not None:
            return self.min_group_ocr_confidence
        return self.min_ocr_confidence

    @property
    def group_floor_is_stricter(self) -> bool:
        """True when the group gate can reject reads the per-frame gate let
        through. Not an error — but it must be stated out loud."""
        return (self.group_detection_floor > self.min_detection_confidence
                or self.group_ocr_floor > self.min_ocr_confidence)


@dataclass
class Behavior:
    cooldown_seconds: int
    capture_interval_ms: int
    queue_max_size: int
    queue_ttl_hours: int
    heartbeat_interval_seconds: int
    cooldown_prune_interval_seconds: int


@dataclass
class Hours:
    """When this location counts traffic and when capture should run.

    Outside the visit window we still heartbeat but do not send /visit or
    /detection-event. Absent from config.json means always open and
    capture is unmanaged (other sites unchanged). 10:00–22:00 is 10am
    inclusive to 10pm exclusive in `timezone`.

    Capture (MediaMTX + Stream) uses a wider window: it starts
    `warmup_minutes` before open so model optimization can finish, and
    stops at close. plate-reader itself stays up.
    """
    enabled: bool = False
    timezone: str = "UTC"
    open: str = "00:00"
    close: str = "00:00"
    open_minutes: int = 0
    close_minutes: int = 0
    warmup_minutes: int = 15
    # Complete weekly schedule, indexed by the day a window OPENS. None is
    # closed. Empty preserves the legacy daily open/close behaviour exactly.
    weekly: dict[str, tuple[int, int] | None] = field(default_factory=dict)

    def _local_time(self, when: datetime | None) -> datetime:
        now = when or datetime.now(ZoneInfo(self.timezone))
        if now.tzinfo is None:
            now = now.replace(tzinfo=ZoneInfo(self.timezone))
        else:
            now = now.astimezone(ZoneInfo(self.timezone))
        return now

    def _local_minutes(self, when: datetime | None) -> int:
        now = self._local_time(when)
        return now.hour * 60 + now.minute

    def _weekly_open(self, when: datetime | None, warmup: int) -> bool:
        now = self._local_time(when)
        minutes = now.hour * 60 + now.minute
        # Yesterday can still be open after midnight; tomorrow's warmup can
        # begin today. Comparing local wall-clock minutes also follows DST.
        for offset in (-1, 0, 1):
            window = self.weekly[WEEKDAYS[(now.weekday() + offset) % 7]]
            if window is None:
                continue
            opening, closing = window
            start = offset * 1440 + opening
            end = offset * 1440 + closing + (1440 if closing <= opening else 0)
            if start - warmup <= minutes < end:
                return True
        return False

    def is_open(self, when: datetime | None = None) -> bool:
        if not self.enabled:
            return True
        if self.weekly:
            return self._weekly_open(when, 0)
        return self._in_window(
            self._local_minutes(when),
            self.open_minutes,
            self.close_minutes,
        )

    def capture_should_run(self, when: datetime | None = None) -> bool:
        """Whether MediaMTX + Stream should be up.

        Hours not configured: True (caller should treat that as unmanaged
        and leave capture alone). When configured: [open - warmup, close).
        """
        if not self.enabled:
            return True
        if self.weekly:
            return self._weekly_open(when, self.warmup_minutes)
        start = (self.open_minutes - self.warmup_minutes) % (24 * 60)
        return self._in_window(
            self._local_minutes(when),
            start,
            self.close_minutes,
        )

    @staticmethod
    def _in_window(minutes: int, start: int, end: int) -> bool:
        if start == end:
            return True
        if start < end:
            return start <= minutes < end
        return minutes >= start or minutes < end


@dataclass
class DetectionZone:
    enabled: bool
    x_min_pct: float
    x_max_pct: float
    y_min_pct: float
    y_max_pct: float


@dataclass
class Camera:
    resolution_width: int
    resolution_height: int
    framerate: int


@dataclass
class Telemetry:
    include_tailscale: bool
    include_cellular: bool
    save_zone_preview_on_boot: bool


VALID_CAMERA_POSITIONS = ("at_board", "facing_cars", "behind_cars")


@dataclass
class RouteQualification:
    """Site-calibrated physical path required before a vehicle can count.

    The entry zone is deliberately separate from the board zone. A vehicle
    must be observed in the entry zone first and the board zone later. That
    ordering rejects adjacent-road traffic which only crosses the far field,
    while keeping a near/front observation open long enough to acquire its
    rear plate farther up the drive-through lane.
    """
    enabled: bool = False
    # Keep the classifier observable without changing traffic totals during a
    # new site's calibration. A reviewed live config can opt into authority
    # only after the site's actual dwell payloads validate the geometry.
    shadow_only: bool = True
    # Native Stream trajectories may reconcile two sparse webhook profiles
    # into one physical vehicle even while traffic filtering remains in
    # shadow.  This is deliberately independent from ``shadow_only``: merging
    # duplicate profiles and deciding whether a route counts are different
    # authorities and must be staged separately.
    profile_reconciliation_enabled: bool = False
    entry_zone: tuple | None = None
    # A completed provider trajectory must contain enough independent samples
    # over enough time to prove a physical lane traversal. The defaults retain
    # compatibility for uncalibrated sites; production values are site data.
    min_trajectory_points: int = 2
    min_trajectory_duration_seconds: float = 0.0
    zone_confirm_points: int = 1
    min_leftward_progress: float = 0.0
    min_upward_progress: float = 0.0
    # Some stopped menu-board vehicles first become a stable native track at
    # the boundary between entry and board, so their completed path proves the
    # ordered zones but narrowly misses the normal progress delta. A site may
    # opt into a stricter fallback that requires a long, consecutive board
    # dwell with substantially more trajectory evidence. Zero seconds keeps
    # the fallback disabled for uncalibrated sites.
    board_dwell_fallback_min_points: int = 0
    board_dwell_fallback_min_board_points: int = 0
    board_dwell_fallback_min_seconds: float = 0.0
    # Stream sometimes starts a completed vehicle track only as the rear of
    # the car reaches the board.  A short but directionally correct track can
    # therefore contain one entry sample (or begin wholly inside the board
    # zone) even though it is a real lane traversal.  This separate rescue is
    # disabled by default and retains the calibrated left/up progress and
    # terminal-exit guards; it is not a global sparse-track relaxation.
    progressive_board_rescue_min_points: int = 0
    progressive_board_rescue_min_board_points: int = 0
    progressive_board_rescue_min_seconds: float = 0.0
    terminal_exit_zone: tuple | None = None
    decision_timeout_seconds: float = 120.0
    # A completed rejected trajectory is observable immediately, but is not
    # terminal until this grace has elapsed without a later qualifying
    # fragment.  Stream can split one physical path into adjacent dwell hooks.
    rejection_grace_seconds: float = 10.0
    # A dwell webhook can race slightly ahead of its recognition webhook.
    # Retain that unmatched on-device trajectory briefly and retry it when the
    # vehicle profile arrives.  Raw plate correlation data never leaves RAM.
    unmatched_dwell_cache_seconds: float = 30.0
    # Once a qualified anonymous Visit is emitted, retain only a lightweight
    # physical-route tombstone.  It absorbs a later board-area departure from
    # the same car without keeping image bytes or changing plate identity.
    terminal_tombstone_seconds: float = 300.0


@dataclass
class Presence:
    """Vehicle-presence / drive-off tracking. Independent of plate identify."""
    enabled: bool
    iou_match_threshold: float       # legacy IoU threshold (kept for report ctx)
    track_timeout_seconds: float     # quiet period before a track is closed
    min_track_frames: int            # frames required before a track can count
    motion_area_fraction: float      # zone change fraction that signals a car
    # Color corroboration (secondary signal; splits merged nose-to-tail cars).
    color_enabled: bool              # compute vehicle-body color signatures
    color_split_distance: float      # in-track color jump that triggers a split
    # ---- v2: camera geometry ----
    # Where the camera physically sits relative to the lane. Fixed property of
    # a given install; set at install time, never inferred per-frame.
    #   at_board     — camera at/next to the menu board facing oncoming
    #                  traffic (current LJS footage). Front plates; plate
    #                  grows on approach; board zone is the NEAR field.
    #   facing_cars  — camera upstream of the board facing oncoming traffic.
    #                  Plate grows toward camera; board zone is the far side.
    #   behind_cars  — camera behind the queue watching rear plates. Plate
    #                  SHRINKS on approach; board zone is the far field.
    camera_position: str = "at_board"
    # Legacy compatibility knob. Current code deliberately does not perform a
    # speculative geometry-only plate handoff: an adjacent queue car can be
    # the one apparently unique predecessor. Existing unit configs and older
    # rollback builds still require this key to parse, so retain it as a no-op;
    # native timestamp-owned trajectories are the reconciliation authority.
    encounter_handoff_seconds: float = 5.0
    # Board zone as fractions of the frame (x_min, y_min, x_max, y_max).
    # None => derived from camera_position (placeholder defaults — calibrate
    # per install / per footage).
    board_zone: tuple | None = None
    # Vehicle-first traffic authority. Anonymous traffic must visit the entry
    # zone and then the board zone before a Visit may leave the Pi. A real
    # provider plate associated with a vehicle already inside the calibrated
    # board zone is the site-specific low-latency recognized exception.
    route_qualification: RouteQualification = field(
        default_factory=RouteQualification
    )
    # ---- v2: board trigger (arrival-and-settle, single slot) ----
    stop_speed_frac_per_s: float = 0.02   # of frame diagonal; below = stopped
    stop_confirm_frames: int = 3          # consecutive stopped frames to fire
    trigger_max_wait_seconds: float = 4.0 # dwell fallback for slow-creep queues
    depart_hysteresis_pct: float = 0.05   # of frame diagonal, zone-exit margin
    # ---- v2: occlusion hold (FIFO queue awareness) ----
    occlusion_hold_enabled: bool = True
    occlusion_max_advance_positions: int = 2
    # ---- v2: motion-contour boxes (weak vehicle signal, pre-detector) ----
    motion_boxes_enabled: bool = True
    motion_box_min_area_frac: float = 0.005   # of full frame area
    # Legacy name: require direct SDK vehicle evidence (a vehicle box or plate
    # box), not motion alone. Motion contours cannot tell a car from a
    # pedestrian or wind-blown bush. Set False only to diagnose raw motion.
    driveoff_requires_plate: bool = True
    # ---- v2: association cost (blended, replaces IoU-only gate) ----
    assoc_cost_threshold: float = 0.7
    assoc_dist_weight: float = 0.5
    assoc_scale_weight: float = 0.2
    assoc_iou_weight: float = 0.3
    # Observe and log tracks/triggers without letting drive-off reaping create
    # production Visits. Use this while a site's board geometry is being
    # calibrated; identity delivery remains immediate and unchanged.
    shadow_only: bool = False

    @property
    def plate_grows_on_approach(self) -> bool:
        """Derived from camera_position: does an approaching car's plate get
        BIGGER in frame? True for cameras facing oncoming traffic."""
        return self.camera_position in ("at_board", "facing_cars")

    def resolved_board_zone(self) -> tuple:
        """(x_min, y_min, x_max, y_max) fractions. Explicit config wins;
        otherwise a per-position placeholder default that MUST be calibrated
        against real footage before the trigger numbers are trusted."""
        if self.board_zone is not None:
            return self.board_zone
        if self.camera_position == "at_board":
            # Car arriving at the board fills the near field: low + large.
            return (0.0, 0.45, 1.0, 1.0)
        # Upstream / behind cameras: the board is in the far field.
        return (0.0, 0.0, 1.0, 0.45)


# "pr_stream" is Plate Recognizer Stream. Unlike the others it does not run
# inference in this process: the Stream container reads an RTSP feed from
# MediaMTX and posts results to a loopback webhook that plate_reader serves.
# Selecting it also switches the capture path to NullCamera (see main.py's
# ENGINES_WITH_OWN_CAPTURE), because MediaMTX owns the sensor.
VALID_ENGINES = ("fast_alpr", "ultimate_alpr", "ultimatealpr", "pr_stream")
VALID_ANCHORS = ("plate_center", "vehicle_center", "vehicle_bottom")


# ---------------------------------------------------------------------------
# Spatial config. NORMALIZED 0.0-1.0 ONLY.
#
# Never store pixels in config. The imx296 is 1456x1088 native but capture
# resolution is per-unit and can change; a pixel box written against one
# resolution silently means something else at another, and the failure mode is
# invisible — plates simply stop being detected. Normalized coordinates are
# resolution-independent and survive a capture-resolution change. Conversion
# to pixels happens once, at the point of use, from the real frame dimensions.
# ---------------------------------------------------------------------------
@dataclass
class NormBox:
    """A normalized rectangle: origin (x, y), size (w, h), all in [0, 1]."""
    enabled: bool = False
    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0

    def to_pixels(self, frame_w: int, frame_h: int) -> tuple[int, int, int, int]:
        """Convert to (x1, y1, x2, y2) pixels for a given frame size."""
        x1 = int(round(self.x * frame_w))
        y1 = int(round(self.y * frame_h))
        x2 = int(round((self.x + self.w) * frame_w))
        y2 = int(round((self.y + self.h) * frame_h))
        # Clamp: rounding at the far edge can overshoot by a pixel.
        return (max(0, x1), max(0, y1), min(frame_w, x2), min(frame_h, y2))

    def contains(self, nx: float, ny: float) -> bool:
        """Is a normalized point inside this box?"""
        return self.x <= nx <= (self.x + self.w) and self.y <= ny <= (self.y + self.h)

    def is_full_frame(self) -> bool:
        return (self.x, self.y, self.w, self.h) == (0.0, 0.0, 1.0, 1.0)


@dataclass
class DwellZone(NormBox):
    """Application-level 'is this vehicle actually ordering?' region.

    Distinct from `alpr.roi`: the ROI decides where the ENGINE looks; the dwell
    zone decides whether a detection COUNTS. Needed because the camera now sits
    close to the menu board with exiting cars and background road traffic in
    frame.
    """
    anchor: str = "plate_center"
    min_seconds_in_zone: float = 0.0
    # Normalized area (fraction of frame) the vehicle box must cover. A 2D
    # image has no true depth; box area is the usable proxy for "close enough
    # to be at the speaker" versus "driving past in the background".
    min_vehicle_box_area: float = 0.0


@dataclass
class VehicleOnly:
    """Vehicles that pass through without a plate we could confirm.

    This is the KPI: a car in the lane we could NOT identify is a car the
    FIFO queue cannot match to an order, so it has to be counted rather than
    silently dropped. Deliberately independent of `DwellZone` — a plateless
    car breaks order matching wherever it is in the lane, whereas the dwell
    zone answers the separate drive-off question ("was this car ordering?").

    Without a zone, though, something else has to keep parked cars and
    background traffic out of the count. Measured on test_footage.mp4 (2
    genuinely plateless cars) with the zone off: 20 events, one of them a
    parked car tracked for 207.68s. The replacement is a MOVEMENT gate.
    A car driving the lane traverses the frame and, on a front-facing camera,
    grows substantially as it approaches; a parked car does neither. That
    holds without any per-site calibration, which is the whole point.
    """
    enabled: bool = True
    # Require the vehicle box centre to travel at least this fraction of the
    # frame diagonal over the track's life. 0.0 disables the check.
    min_travel_fraction: float = 0.10
    # ...OR to grow by at least this ratio (last_area / first_area). Either
    # signal alone is sufficient: a car approaching head-on grows while barely
    # moving laterally, and a car crossing the frame moves while barely
    # growing. Requiring BOTH would miss real cars. 0.0 disables the check.
    min_area_growth_ratio: float = 1.5
    # App-side floor on the SDK's own car-detection confidence. The SDK
    # already gates internally at car_noplate_detect_min_score (0.8 default),
    # so 0.0 means "trust the engine" rather than "no filtering at all".
    min_vehicle_confidence: float = 0.0
    # Opt back in to the pre-KPI behaviour where a calibrated dwell zone is
    # mandatory. Off by default: requiring it would mean units without a
    # calibrated zone silently report ZERO missed vehicles, which is the
    # worst possible failure mode for a metric whose entire purpose is
    # counting what we miss.
    require_dwell_zone: bool = False


@dataclass
class Dedup:
    """Suppression of repeat confirms for the same physical vehicle."""
    # Primary defence. Independent of presence/absence tracking, so read
    # flicker cannot defeat it. See docs/STAGE2B_PIPELINE_BRINGUP.md 4.2:
    # hash d532120117e8 confirmed twice 1.0s apart because the plate dropped
    # out for >=3 frames, latching the absence gate, then reappeared.
    cooldown_seconds: float = 120.0
    release_requires_absent_frames: int = 3
    # Time-based absence is more robust than a frame count when fps varies.
    # 0.0 keeps the legacy frame-count behaviour.
    release_requires_absent_seconds: float = 0.0

    # --- near-match suppression -------------------------------------------
    # The cooldown above keys on the HASH, so it only catches a repeat of the
    # IDENTICAL string. Measured on real footage, one car produced:
    #     f02959  CHR7067  -> hash d532120117e8
    #     f02964  CHR706   -> hash 68d2065f19cd   (truncated, 1.0s later)
    # Two hashes, two "visits", one car. The hash cooldown cannot see this
    # because the strings differ, and NORMALIZATION_SPEC v1.0 rule 7 correctly
    # forbids substituting characters to make them match.
    #
    # This is a VISIT-DEDUP rule, not a normalization rule: it changes nothing
    # about how a plate is normalized or hashed, it only declines to emit a
    # second visit for a string that is one edit away from one just confirmed.
    # NORMALIZATION_SPEC.md is untouched.
    #
    # Disabled by default. Applying edit distance globally can suppress two
    # real back-to-back cars whose plates differ by one character. Re-enable
    # only when the comparison is constrained to one vehicle track.
    near_match_max_distance: int = 0


@dataclass
class AlprThresholds:
    """Two layers, deliberately separate.

    Engine floors are passed into the SDK and decide what it bothers to
    return. App floors are applied in detector.py AFTER the engine returns and
    decide what we trust. Keeping them apart lets us see weak reads (for
    calibration) without trusting them.
    """
    detect_minscore: float = 0.1
    recogn_minscore: float = 0.3
    recogn_score_type: str = "min"
    app_detect_min: float = 0.50
    app_ocr_min: float = 0.75
    # Optional second path for an encounter that repeatedly produces the
    # EXACT same normalized string just below one app gate. Disabled at 0.
    # These live beside the app gates because confidence scales are
    # engine-specific; changing engines without changing this whole block is
    # invalid for the same reason copying PR's 0.85 OCR gate to ultimateALPR
    # is invalid.
    consensus_rescue_min_frames: int = 0
    consensus_rescue_detect_min: float = 0.0
    consensus_rescue_ocr_min: float = 0.0
    # PR Stream has already evaluated many source frames before it emits one
    # provider-selected result. A clean, encounter-bound result may therefore
    # be accepted as temporal evidence even though this process sees one
    # webhook event. Disabled by default and deliberately constrained by
    # plate length plus rear-of-vehicle geometry.
    provider_selected_single_enabled: bool = False
    provider_selected_single_min_length: int = 5
    provider_selected_single_min_relative_x: float = 0.45
    # PR sometimes returns a correct, provider-selected plate with effectively
    # perfect OCR but a low plate-localization score. Recover that one narrow
    # class without lowering the ordinary detection gate. The existing
    # provider-selected length/geometry safeguards also apply.
    provider_high_ocr_rescue_enabled: bool = False
    provider_high_ocr_rescue_detect_min: float = 0.0
    provider_high_ocr_rescue_ocr_min: float = 1.0
    # Plate Recognizer Stream can report a truncated candidate just before a
    # fuller read of the same physical vehicle. Hold encounter-bound reads for
    # a short, bounded interval before the detector is allowed to vote/hash.
    # 0 disables the hold (useful for legacy engines and isolated tests).
    identity_settle_quiet_seconds: float = 1.0
    identity_settle_max_seconds: float = 3.0


@dataclass
class Classifiers:
    vehicle_detect_enabled: bool = False
    color_enabled: bool = False
    # These three together cost ~268 ms/frame and blow the 200 ms budget.
    lpci_enabled: bool = False
    vmmr_enabled: bool = False
    vbsr_enabled: bool = False


@dataclass
class EngineAdvanced:
    # DO NOT enable. Read order may derive from post-rectification geometry,
    # which would reorder characters on the same physical plate and change its
    # hash. Hash-stability flag, not a tuning knob. Open with Doubango.
    rectify_enabled: bool = False
    # Night-vision enhancement; also shifts car box coordinates.
    ienv_enabled: bool = False
    pyramidal_search_enabled: bool = False
    pyramidal_search_sensitivity: float = 0.28
    num_threads: int = -1
    openvino_enabled: bool = False
    npu_enabled: bool = False


@dataclass
class Alpr:
    engine: str = "fast_alpr"
    # PATH to the token file, never the token string itself.
    license_token_file: str | None = None
    assets_folder: str | None = None
    roi: NormBox = field(default_factory=NormBox)
    thresholds: AlprThresholds = field(default_factory=AlprThresholds)
    classifiers: Classifiers = field(default_factory=Classifiers)
    engine_advanced: EngineAdvanced = field(default_factory=EngineAdvanced)


@dataclass
class Capture:
    target_fps: float = 5.0
    width: int = 1456
    height: int = 1088


@dataclass
class Diagnostics:
    log_per_frame_timing: bool = False
    zone_preview_overlay: bool = True
    # Plate strings are PII-adjacent. This gates them to DEBUG; never INFO in
    # production.
    log_rejected_reads: bool = False
    # Keep one local JPEG of the best full frame per plate episode so
    # operators can scp the folder for OCR eval.
    # Images stay on-device; raw OCR text is only in the sidecar JSON.
    # The detector bbox is drawn on the frame (not cropped).
    save_miss_plates: bool = True
    miss_plate_dir: str = "/home/serv/plate_reader/miss_plates"
    # Delete images older than this. 0 disables pruning.
    miss_plate_retain_hours: float = 72.0
    # 0 preserves the legacy time-only policy. Production uses a case cap so
    # a diagnostic collection can grow to a useful sample without filling
    # disk; a JPEG and its JSON sidecar count as one case.
    miss_plate_max_cases: int = 0
    # Existing installs keep the historical behaviour unless they opt out.
    # The LJS audit dataset is misses-only, so its shipping config is false.
    save_confirmed_plates: bool = True
    # Plate-free Stream encounters carry a compressed original frame only for
    # local audit. Disabled by default for backward compatibility.
    save_unidentified_vehicle_frames: bool = False
    # Unused (kept for config compatibility). Miss diagnostics save full frames.
    miss_plate_pad: float = 0.5
    # Test-window dump: every frame with a plate box or vehicle box.
    save_presence_frames: bool = False
    presence_frame_dir: str = "/home/serv/plate_reader/presence_frames"


@dataclass
class Config:
    device_id: str
    api_key: str
    location_id: str
    api_base: str
    sw_version: str
    thresholds: Thresholds
    behavior: Behavior
    detection_zone: DetectionZone
    camera: Camera
    telemetry: Telemetry
    presence: Presence
    # Which inference engine backs the detector. Absent from config.json means
    # fast_alpr, so every existing deployment is byte-identical after the
    # engine refactor. See plate_reader/engines.py.
    engine: str = "fast_alpr"
    # --- ultimateALPR only; ignored by fast_alpr ---
    # PATH to the licence token file — never the token itself. See
    # docs/LICENSING.md. Absent means trial mode, which redacts plate text.
    license_token_file: str | None = None
    ualpr_assets_folder: str | None = None
    # Chosen production profile is plates + car box + VCR: 106 ms/frame on a
    # Pi 5, inside the 200 ms budget. LPCI/VMMR/VBSR do not fit and are not
    # exposed here on purpose.
    ualpr_car_detect_enabled: bool = False
    ualpr_vcr_enabled: bool = False
    # --- deployment-driven blocks (all default to today's behaviour) ---
    alpr: Alpr = field(default_factory=Alpr)
    dwell_zone: DwellZone = field(default_factory=DwellZone)
    vehicle_only: VehicleOnly = field(default_factory=VehicleOnly)
    dedup: Dedup = field(default_factory=Dedup)
    capture: Capture = field(default_factory=Capture)
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    hours: Hours = field(default_factory=Hours)
    # Bench runs retain bounded local events without any backend HTTP calls.
    # Sender uses a separate database so enabling delivery cannot replay them.
    backend_writes_enabled: bool = True


def _bootstrap_log(msg: str) -> None:
    """Write to a fallback location even if main logging isn't set up.
    Critical because config errors happen before logging is ready."""
    try:
        BOOTSTRAP_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(BOOTSTRAP_LOG_PATH, "a") as f:
            f.write(msg + "\n")
    except Exception:
        # If even this fails, stderr will get it via systemd
        print(f"BOOTSTRAP: {msg}", flush=True)


def _num(d: dict, key: str, default: float) -> float:
    v = d.get(key, default)
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ConfigError(f"{key} must be a number, got {type(v).__name__}")
    return float(v)


def _int(d: dict, key: str, default: int, *, label: str | None = None) -> int:
    """Strict integer parser; bool and numeric strings are rejected."""
    v = d.get(key, default)
    if not isinstance(v, int) or isinstance(v, bool):
        raise ConfigError(
            f"{label or key} must be an integer, got {type(v).__name__}"
        )
    return v


def _flag(d: dict, key: str, default: bool, *, label: str | None = None) -> bool:
    """Strict JSON boolean parser.

    Python's ``bool('false')`` is True, which previously allowed a quoted
    config value to turn classifiers, zones, or diagnostics on accidentally.
    """
    v = d.get(key, default)
    if not isinstance(v, bool):
        raise ConfigError(
            f"{label or key} must be true or false, got {type(v).__name__}"
        )
    return v


def _reject_unknown(d: dict, allowed: set[str], label: str) -> None:
    """Reject misspelled config keys while allowing explanatory // fields."""
    unknown = sorted(
        key for key in d
        if not key.startswith("//") and key not in allowed
    )
    if unknown:
        raise ConfigError(
            f"{label} has unknown field(s): {', '.join(unknown)}"
        )


def _unit(d: dict, key: str, default: float, *, label: str) -> float:
    """A number that must lie in [0, 1]."""
    v = _num(d, key, default)
    if not 0.0 <= v <= 1.0:
        raise ConfigError(f"{label} must be in [0.0, 1.0], got {v}")
    return v


def _parse_norm_box(raw: dict, label: str, cls=NormBox, **extra):
    """Parse a normalized box, validating it lands inside the unit square."""
    if not isinstance(raw, dict):
        raise ConfigError(f"{label} must be an object")
    allowed = {"enabled", "x", "y", "w", "h"}
    if cls is DwellZone:
        allowed.update({"anchor", "min_seconds_in_zone",
                        "min_vehicle_box_area"})
    _reject_unknown(raw, allowed, label)
    enabled = _flag(raw, "enabled", False, label=f"{label}.enabled")
    x = _num(raw, "x", 0.0)
    y = _num(raw, "y", 0.0)
    w = _num(raw, "w", 1.0)
    h = _num(raw, "h", 1.0)

    for name, v in (("x", x), ("y", y)):
        if not 0.0 <= v <= 1.0:
            raise ConfigError(
                f"{label}.{name} must be in [0.0, 1.0] (normalized, NOT "
                f"pixels), got {v}"
            )
    for name, v in (("w", w), ("h", h)):
        if v <= 0.0:
            raise ConfigError(f"{label}.{name} must be > 0, got {v}")
    if x + w > 1.0 + 1e-9:
        raise ConfigError(
            f"{label} extends past the right edge: x({x}) + w({w}) = {x + w} > 1.0"
        )
    if y + h > 1.0 + 1e-9:
        raise ConfigError(
            f"{label} extends past the bottom edge: y({y}) + h({h}) = {y + h} > 1.0"
        )
    if enabled and (w <= 1e-6 or h <= 1e-6):
        raise ConfigError(f"{label}.enabled is true but the box is degenerate")
    return cls(enabled=enabled, x=x, y=y, w=w, h=h, **extra)


def _parse_alpr(raw: dict, legacy: dict) -> Alpr:
    """Parse the `alpr` block, falling back to legacy top-level keys.

    Backward compatibility matters here: a config written before this schema
    existed must produce byte-identical behaviour.
    """
    a = raw.get("alpr")
    if not isinstance(a, dict):
        a = {}
        # Legacy flat keys (pre-schema configs).
        if legacy.get("engine"):
            a["engine"] = legacy["engine"]
        if legacy.get("license_token_file"):
            a["license_token_file"] = legacy["license_token_file"]
        if legacy.get("ualpr_assets_folder"):
            a["assets_folder"] = legacy["ualpr_assets_folder"]
        a["classifiers"] = {
            "vehicle_detect_enabled": _flag(
                legacy, "ualpr_car_detect_enabled", False),
            "color_enabled": _flag(
                legacy, "ualpr_vcr_enabled", False),
        }

    _reject_unknown(
        a,
        {"engine", "license_token_file", "assets_folder", "roi",
         "thresholds", "classifiers", "engine_advanced"},
        "alpr",
    )

    engine = str(a.get("engine", legacy.get("engine", "fast_alpr")))
    if engine not in VALID_ENGINES:
        raise ConfigError(f"alpr.engine must be one of {VALID_ENGINES}, got {engine!r}")
    # "ultimatealpr" is accepted as an alias for the canonical name.
    if engine == "ultimatealpr":
        engine = "ultimate_alpr"

    t = a.get("thresholds", {}) or {}
    if not isinstance(t, dict):
        raise ConfigError("alpr.thresholds must be an object")
    _reject_unknown(
        t,
        {"detect_minscore", "recogn_minscore", "recogn_score_type",
         "app_detect_min", "app_ocr_min",
         "consensus_rescue_min_frames", "consensus_rescue_detect_min",
         "consensus_rescue_ocr_min", "provider_selected_single_enabled",
         "provider_selected_single_min_length",
         "provider_selected_single_min_relative_x",
         "provider_high_ocr_rescue_enabled",
         "provider_high_ocr_rescue_detect_min",
         "provider_high_ocr_rescue_ocr_min",
         "identity_settle_quiet_seconds",
         "identity_settle_max_seconds"},
        "alpr.thresholds",
    )
    score_type = str(t.get("recogn_score_type", "min"))
    if score_type != "min":
        # Not a hard error, but it silently breaks our precision argument:
        # confidences[0] would stop being the per-character minimum.
        log.warning(
            "alpr.thresholds.recogn_score_type=%r — expected 'min'. "
            "confidences[0] will NOT be the per-character minimum and the "
            "min-per-character quality gate is defeated.", score_type,
        )
    thresholds = AlprThresholds(
        detect_minscore=_unit(t, "detect_minscore", 0.1,
                              label="alpr.thresholds.detect_minscore"),
        recogn_minscore=_unit(t, "recogn_minscore", 0.3,
                              label="alpr.thresholds.recogn_minscore"),
        recogn_score_type=score_type,
        app_detect_min=_unit(t, "app_detect_min", 0.50,
                             label="alpr.thresholds.app_detect_min"),
        app_ocr_min=_unit(t, "app_ocr_min", 0.75,
                          label="alpr.thresholds.app_ocr_min"),
        consensus_rescue_min_frames=_int(
            t, "consensus_rescue_min_frames", 0,
            label="alpr.thresholds.consensus_rescue_min_frames"),
        consensus_rescue_detect_min=_unit(
            t, "consensus_rescue_detect_min", 0.0,
            label="alpr.thresholds.consensus_rescue_detect_min"),
        consensus_rescue_ocr_min=_unit(
            t, "consensus_rescue_ocr_min", 0.0,
            label="alpr.thresholds.consensus_rescue_ocr_min"),
        provider_selected_single_enabled=_flag(
            t, "provider_selected_single_enabled", False,
            label="alpr.thresholds.provider_selected_single_enabled"),
        provider_selected_single_min_length=_int(
            t, "provider_selected_single_min_length", 5,
            label="alpr.thresholds.provider_selected_single_min_length"),
        provider_selected_single_min_relative_x=_unit(
            t, "provider_selected_single_min_relative_x", 0.45,
            label=("alpr.thresholds."
                   "provider_selected_single_min_relative_x")),
        provider_high_ocr_rescue_enabled=_flag(
            t, "provider_high_ocr_rescue_enabled", False,
            label="alpr.thresholds.provider_high_ocr_rescue_enabled"),
        provider_high_ocr_rescue_detect_min=_unit(
            t, "provider_high_ocr_rescue_detect_min", 0.0,
            label="alpr.thresholds.provider_high_ocr_rescue_detect_min"),
        provider_high_ocr_rescue_ocr_min=_unit(
            t, "provider_high_ocr_rescue_ocr_min", 1.0,
            label="alpr.thresholds.provider_high_ocr_rescue_ocr_min"),
        identity_settle_quiet_seconds=_num(
            t, "identity_settle_quiet_seconds", 1.0),
        identity_settle_max_seconds=_num(
            t, "identity_settle_max_seconds", 3.0),
    )
    settle_quiet = thresholds.identity_settle_quiet_seconds
    settle_max = thresholds.identity_settle_max_seconds
    if settle_quiet < 0.0 or settle_max < 0.0:
        raise ConfigError(
            "alpr.thresholds identity settlement seconds must be >= 0"
        )
    if settle_quiet > settle_max:
        raise ConfigError(
            "alpr.thresholds.identity_settle_quiet_seconds cannot exceed "
            "identity_settle_max_seconds"
        )
    if engine != "pr_stream" and (settle_quiet, settle_max) != (1.0, 3.0):
        raise ConfigError(
            "identity settlement is a pr_stream encounter feature; remove "
            "its overrides before changing engines"
        )
    provider_single = thresholds.provider_selected_single_enabled
    if provider_single and engine != "pr_stream":
        raise ConfigError(
            "provider-selected single confirmation is calibrated only for "
            "alpr.engine='pr_stream'"
        )
    if not 3 <= thresholds.provider_selected_single_min_length <= 12:
        raise ConfigError(
            "provider_selected_single_min_length must be between 3 and 12"
        )
    high_ocr_rescue = thresholds.provider_high_ocr_rescue_enabled
    if high_ocr_rescue and engine != "pr_stream":
        raise ConfigError(
            "provider high-OCR rescue is calibrated only for "
            "alpr.engine='pr_stream'"
        )
    if high_ocr_rescue and not {"app_detect_min", "app_ocr_min"}.issubset(t):
        raise ConfigError(
            "enabled provider high-OCR rescue requires explicit "
            "app_detect_min and app_ocr_min in the same threshold block"
        )
    if high_ocr_rescue:
        if thresholds.provider_high_ocr_rescue_detect_min <= 0.0:
            raise ConfigError(
                "enabled provider high-OCR rescue requires a positive "
                "provider_high_ocr_rescue_detect_min"
            )
        if (thresholds.provider_high_ocr_rescue_detect_min
                >= thresholds.app_detect_min):
            raise ConfigError(
                "provider_high_ocr_rescue_detect_min must be below "
                "app_detect_min"
            )
        if (thresholds.provider_high_ocr_rescue_ocr_min
                < thresholds.app_ocr_min):
            raise ConfigError(
                "provider_high_ocr_rescue_ocr_min cannot be below "
                "app_ocr_min"
            )
    rescue_frames = thresholds.consensus_rescue_min_frames
    if rescue_frames and not {"app_detect_min", "app_ocr_min"}.issubset(t):
        raise ConfigError(
            "enabled consensus rescue requires explicit app_detect_min and "
            "app_ocr_min in the same engine threshold block"
        )
    if rescue_frames and engine != "pr_stream":
        raise ConfigError(
            "configured consensus rescue floors are calibrated for "
            "alpr.engine='pr_stream'; disable or retune the entire threshold "
            "block before changing engines"
        )
    if rescue_frames != 0 and not 3 <= rescue_frames <= 10:
        raise ConfigError(
            "alpr.thresholds.consensus_rescue_min_frames must be 0 "
            "(disabled) or between 3 and 10"
        )
    if rescue_frames:
        if (
            thresholds.consensus_rescue_detect_min <= 0.0
            or thresholds.consensus_rescue_ocr_min <= 0.0
        ):
            raise ConfigError(
                "enabled consensus rescue requires explicit positive "
                "consensus_rescue_detect_min and consensus_rescue_ocr_min"
            )
        if thresholds.consensus_rescue_detect_min > thresholds.app_detect_min:
            raise ConfigError(
                "consensus_rescue_detect_min cannot exceed app_detect_min"
            )
        if thresholds.consensus_rescue_ocr_min > thresholds.app_ocr_min:
            raise ConfigError(
                "consensus_rescue_ocr_min cannot exceed app_ocr_min"
            )
        if (thresholds.consensus_rescue_detect_min
                == thresholds.app_detect_min
                and thresholds.consensus_rescue_ocr_min
                == thresholds.app_ocr_min):
            raise ConfigError(
                "consensus rescue must relax at least one app confidence gate"
            )

    c = a.get("classifiers", {}) or {}
    if not isinstance(c, dict):
        raise ConfigError("alpr.classifiers must be an object")
    _reject_unknown(
        c,
        {"vehicle_detect_enabled", "color_enabled", "lpci_enabled",
         "vmmr_enabled", "vbsr_enabled"},
        "alpr.classifiers",
    )
    classifiers = Classifiers(
        vehicle_detect_enabled=_flag(
            c, "vehicle_detect_enabled", False,
            label="alpr.classifiers.vehicle_detect_enabled"),
        color_enabled=_flag(
            c, "color_enabled", False,
            label="alpr.classifiers.color_enabled"),
        lpci_enabled=_flag(c, "lpci_enabled", False,
                           label="alpr.classifiers.lpci_enabled"),
        vmmr_enabled=_flag(c, "vmmr_enabled", False,
                           label="alpr.classifiers.vmmr_enabled"),
        vbsr_enabled=_flag(c, "vbsr_enabled", False,
                           label="alpr.classifiers.vbsr_enabled"),
    )
    heavy = [n for n, on in (("lpci", classifiers.lpci_enabled),
                             ("vmmr", classifiers.vmmr_enabled),
                             ("vbsr", classifiers.vbsr_enabled)) if on]
    if heavy:
        log.warning(
            "alpr.classifiers enables %s — measured at ~268 ms/frame together, "
            "which EXCEEDS the 200 ms budget at 5 fps. Re-measure before "
            "trusting this configuration.", "+".join(heavy),
        )

    e = a.get("engine_advanced", {}) or {}
    if not isinstance(e, dict):
        raise ConfigError("alpr.engine_advanced must be an object")
    _reject_unknown(
        e,
        {"rectify_enabled", "ienv_enabled", "pyramidal_search_enabled",
         "pyramidal_search_sensitivity", "num_threads",
         "openvino_enabled", "npu_enabled"},
        "alpr.engine_advanced",
    )
    advanced = EngineAdvanced(
        rectify_enabled=_flag(
            e, "rectify_enabled", False,
            label="alpr.engine_advanced.rectify_enabled"),
        ienv_enabled=_flag(e, "ienv_enabled", False,
                           label="alpr.engine_advanced.ienv_enabled"),
        pyramidal_search_enabled=_flag(
            e, "pyramidal_search_enabled", False,
            label="alpr.engine_advanced.pyramidal_search_enabled"),
        pyramidal_search_sensitivity=_unit(
            e, "pyramidal_search_sensitivity", 0.28,
            label="alpr.engine_advanced.pyramidal_search_sensitivity"),
        num_threads=_int(e, "num_threads", -1,
                         label="alpr.engine_advanced.num_threads"),
        openvino_enabled=_flag(
            e, "openvino_enabled", False,
            label="alpr.engine_advanced.openvino_enabled"),
        npu_enabled=_flag(e, "npu_enabled", False,
                          label="alpr.engine_advanced.npu_enabled"),
    )
    if advanced.rectify_enabled:
        log.warning(
            "=" * 70 + "\n"
            "alpr.engine_advanced.rectify_enabled is TRUE.\n"
            "Read order under rectification is UNVERIFIED with Doubango: it "
            "may reorder characters on the same physical plate, which changes "
            "its SHA-256 and therefore its vehicle identity. This is a "
            "hash-stability flag, not a tuning knob. Hashes produced in this "
            "configuration may not be comparable with any others.\n" + "=" * 70
        )

    token = a.get("license_token_file") or legacy.get("license_token_file")
    return Alpr(
        engine=engine,
        license_token_file=str(token) if token else None,
        assets_folder=(
            str(a["assets_folder"]) if a.get("assets_folder")
            else (str(legacy["ualpr_assets_folder"])
                  if legacy.get("ualpr_assets_folder") else None)
        ),
        roi=_parse_norm_box(a.get("roi", {}) or {}, "alpr.roi"),
        thresholds=thresholds,
        classifiers=classifiers,
        engine_advanced=advanced,
    )


def _require(d: dict, key: str, type_: type) -> Any:
    if key not in d:
        raise ConfigError(f"missing required field: {key}")
    v = d[key]
    if isinstance(v, bool) and type_ in (int, float):
        raise ConfigError(
            f"field {key} must be {type_.__name__}, got bool"
        )
    if type_ is float and isinstance(v, int):
        v = float(v)
    if not isinstance(v, type_):
        raise ConfigError(
            f"field {key} must be {type_.__name__}, got {type(v).__name__}"
        )
    return v


_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _parse_hhmm(value: str, label: str) -> int:
    """'10:00' -> minutes from midnight. Rejects 24:00."""
    if not isinstance(value, str) or not _HHMM.match(value.strip()):
        raise ConfigError(
            f"{label} must be HH:MM on a 24-hour clock, got {value!r}"
        )
    hour, minute = value.strip().split(":")
    return int(hour) * 60 + int(minute)


def parse_hours(hraw) -> Hours:
    """Parse the optional `hours` block. None/absent means always open."""
    if hraw is None:
        return Hours()
    if not isinstance(hraw, dict):
        raise ConfigError("hours must be an object")
    _reject_unknown(
        hraw,
        {"timezone", "open", "close", "weekly", "warmup_minutes"},
        "hours",
    )
    tz_name = _require(hraw, "timezone", str)
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as e:
        raise ConfigError(
            f"hours.timezone is not a valid IANA zone: {tz_name!r}"
        ) from e
    warmup = _int(hraw, "warmup_minutes", 15, label="hours.warmup_minutes")
    if warmup < 0 or warmup > 12 * 60:
        raise ConfigError(
            f"hours.warmup_minutes must be 0–720, got {warmup}"
        )
    if "weekly" in hraw:
        if "open" in hraw or "close" in hraw:
            raise ConfigError("hours.weekly cannot be mixed with daily open/close")
        raw_week = _require(hraw, "weekly", dict)
        _reject_unknown(raw_week, set(WEEKDAYS), "hours.weekly")
        if any(day not in raw_week for day in WEEKDAYS):
            raise ConfigError("hours.weekly must specify all seven days; use null for closed days")
        weekly = {}
        for day in WEEKDAYS:
            window = raw_week[day]
            if window is None:
                weekly[day] = None
                continue
            if not isinstance(window, dict):
                raise ConfigError(f"hours.weekly.{day} must be an open/close object or null")
            _reject_unknown(window, {"open", "close"}, f"hours.weekly.{day}")
            weekly[day] = (
                _parse_hhmm(_require(window, "open", str), f"hours.weekly.{day}.open"),
                _parse_hhmm(_require(window, "close", str), f"hours.weekly.{day}.close"),
            )
        return Hours(enabled=True, timezone=tz_name, open="weekly", close="weekly",
                     warmup_minutes=warmup, weekly=weekly)
    open_s = _require(hraw, "open", str)
    close_s = _require(hraw, "close", str)
    return Hours(
        enabled=True,
        timezone=tz_name,
        open=open_s,
        close=close_s,
        open_minutes=_parse_hhmm(open_s, "hours.open"),
        close_minutes=_parse_hhmm(close_s, "hours.close"),
        warmup_minutes=warmup,
    )


def capture_desired_state(
    path: Path = CONFIG_PATH,
    when: datetime | None = None,
) -> str:
    """What the hours watchdog should do: unmanaged, on, or off.

    Only `pr_stream` is managed: starting MediaMTX on an in-process engine
    would steal the CSI sensor (libcamera is one consumer). Sites without
    an `hours` block are also unmanaged.

    Does not log. Invalid JSON or hours raise ConfigError so the watchdog
    leaves capture alone rather than guessing.
    """
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a JSON object")
    alpr = raw.get("alpr")
    engine = alpr.get("engine") if isinstance(alpr, dict) else None
    if engine != "pr_stream":
        return "unmanaged"
    hours = parse_hours(raw.get("hours"))
    if not hours.enabled:
        return "unmanaged"
    return "on" if hours.capture_should_run(when) else "off"


def load(path: Path = CONFIG_PATH) -> Config:
    """Read and validate config. On any error, log to bootstrap log and raise."""
    if not path.exists():
        msg = f"config file does not exist at {path}"
        _bootstrap_log(msg)
        raise ConfigError(msg)

    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        msg = f"config file is not valid JSON: {e}"
        _bootstrap_log(msg)
        raise ConfigError(msg) from e

    try:
        if not isinstance(raw, dict):
            raise ConfigError("config root must be a JSON object")
        _reject_unknown(
            raw,
            {"device_id", "api_key", "location_id", "api_base",
             "sw_version", "alpr", "presence", "capture", "diagnostics",
             "thresholds", "behavior", "detection_zone", "camera",
             "telemetry", "engine", "license_token_file",
             "ualpr_assets_folder", "ualpr_car_detect_enabled",
             "ualpr_vcr_enabled", "hours", "backend_writes_enabled"},
            "config",
        )
        device_id = _require(raw, "device_id", str)
        if "REPLACE_WITH" in device_id:
            raise ConfigError("device_id is still the placeholder — fill in the real device_id")
        if not DEVICE_ID_RE.match(device_id):
            raise ConfigError(
                f"device_id must be lowercase alphanumeric+hyphens, 3-64 chars: {device_id!r}"
            )

        api_key = _require(raw, "api_key", str)
        if "REPLACE_WITH" in api_key:
            raise ConfigError("api_key is still the placeholder — fill in the real key")
        if not HEX_64.fullmatch(api_key):
            raise ConfigError(
                "api_key must be exactly 64 lowercase hexadecimal characters"
            )

        location_id = _require(raw, "location_id", str)
        if "REPLACE_WITH" in location_id:
            raise ConfigError("location_id is still the placeholder")
        if not LOCATION_ID_RE.match(location_id):
            raise ConfigError(
                f"location_id must be lowercase alphanumeric+hyphens, 3-64 chars: {location_id!r}"
            )

        api_base = _require(raw, "api_base", str).rstrip("/")
        parsed_api = urlsplit(api_base)
        if (parsed_api.scheme != "https" or not parsed_api.hostname
                or parsed_api.username or parsed_api.password
                or parsed_api.query or parsed_api.fragment):
            raise ConfigError(
                "api_base must be an HTTPS origin/path without credentials, "
                f"query, or fragment: {api_base!r}"
            )

        configured_sw_version = _require(raw, "sw_version", str)
        if configured_sw_version != __version__:
            log.warning(
                "config sw_version=%s does not match installed package=%s; "
                "reporting the installed package version",
                configured_sw_version, __version__,
            )
        # The running code is the source of truth.  Keeping a stale value in a
        # per-unit config after an OTA update must not make the cloud believe
        # an old release is still executing.
        sw_version = __version__

        t = _require(raw, "thresholds", dict)
        _reject_unknown(
            t,
            {"min_detection_confidence", "min_ocr_confidence",
             "min_group_confidence", "vote_window_seconds",
             "min_normalized_length", "max_normalized_length",
             "vote_min_frames", "dwell_suppress_seconds",
             "dwell_release_after_other_vehicles",
             "dwell_release_requires_absent_frames",
             "min_group_detection_confidence",
             "min_group_ocr_confidence",
             "high_ocr_single_frame"},
            "thresholds",
        )
        thresholds = Thresholds(
            min_detection_confidence=_require(t, "min_detection_confidence", float),
            min_ocr_confidence=_require(t, "min_ocr_confidence", float),
            min_group_confidence=_require(t, "min_group_confidence", float),
            vote_window_seconds=_require(t, "vote_window_seconds", float),
            min_normalized_length=_require(t, "min_normalized_length", int),
            max_normalized_length=_require(t, "max_normalized_length", int),
            # Optional with defaults — legacy configs written before dwell
            # suppression existed must keep loading unchanged.
            vote_min_frames=_int(t, "vote_min_frames", 2,
                                 label="thresholds.vote_min_frames"),
            high_ocr_single_frame=_unit(
                t, "high_ocr_single_frame", 0.85,
                label="thresholds.high_ocr_single_frame"),
            dwell_suppress_seconds=_num(
                t, "dwell_suppress_seconds", 60.0),
            dwell_release_after_other_vehicles=_int(
                t, "dwell_release_after_other_vehicles", 1,
                label="thresholds.dwell_release_after_other_vehicles"),
            dwell_release_requires_absent_frames=_int(
                t, "dwell_release_requires_absent_frames", 3,
                label="thresholds.dwell_release_requires_absent_frames"),
            # Absent => fall back to min_group_confidence for compatibility
            # with configs written before the floors were split.
            min_group_detection_confidence=(
                _unit(
                    t, "min_group_detection_confidence",
                    float(t["min_group_confidence"]),
                    label="thresholds.min_group_detection_confidence")
                if "min_group_detection_confidence" in t
                else float(t["min_group_confidence"])
            ),
            min_group_ocr_confidence=(
                _unit(
                    t, "min_group_ocr_confidence",
                    float(t["min_group_confidence"]),
                    label="thresholds.min_group_ocr_confidence")
                if "min_group_ocr_confidence" in t
                else float(t["min_group_confidence"])
            ),
        )
        for name, val in [
            ("min_detection_confidence", thresholds.min_detection_confidence),
            ("min_ocr_confidence", thresholds.min_ocr_confidence),
            ("min_group_confidence", thresholds.min_group_confidence),
        ]:
            if not 0.0 <= val <= 1.0:
                raise ConfigError(f"{name} must be between 0 and 1, got {val}")
        if thresholds.vote_window_seconds <= 0 or thresholds.vote_window_seconds > 30:
            raise ConfigError(
                f"vote_window_seconds must be (0, 30], got {thresholds.vote_window_seconds}"
            )
        # vote_min_frames=1 remains allowed only with a high OCR floor
        # (validated on effective gates below). Prefer vote_min_frames=2 with
        # high_ocr_single_frame for tiered short-dwell sites.
        if thresholds.vote_min_frames < 1:
            raise ConfigError(
                f"vote_min_frames must be >= 1, got {thresholds.vote_min_frames}"
            )
        if not 0.0 <= thresholds.high_ocr_single_frame <= 1.0:
            raise ConfigError(
                "high_ocr_single_frame must be in [0, 1], "
                f"got {thresholds.high_ocr_single_frame}"
            )
        if thresholds.min_normalized_length < 3:
            raise ConfigError(
                "min_normalized_length must be >= 3 to remain compatible "
                "with NORMALIZATION_SPEC v1.0"
            )
        if thresholds.max_normalized_length < thresholds.min_normalized_length:
            raise ConfigError("max_normalized_length must be >= min_normalized_length")
        if thresholds.max_normalized_length > 12:
            raise ConfigError(
                "max_normalized_length must be <= 12 to remain compatible "
                "with NORMALIZATION_SPEC v1.0"
            )

        b = _require(raw, "behavior", dict)
        _reject_unknown(
            b,
            {"cooldown_seconds", "capture_interval_ms", "queue_max_size",
             "queue_ttl_hours", "heartbeat_interval_seconds",
             "cooldown_prune_interval_seconds"},
            "behavior",
        )
        behavior = Behavior(
            cooldown_seconds=_require(b, "cooldown_seconds", int),
            capture_interval_ms=_require(b, "capture_interval_ms", int),
            queue_max_size=_require(b, "queue_max_size", int),
            queue_ttl_hours=_require(b, "queue_ttl_hours", int),
            heartbeat_interval_seconds=_require(b, "heartbeat_interval_seconds", int),
            cooldown_prune_interval_seconds=_require(
                b, "cooldown_prune_interval_seconds", int
            ),
        )
        if behavior.capture_interval_ms < 0:
            raise ConfigError("capture_interval_ms must be >= 0")
        if behavior.cooldown_seconds < 0:
            raise ConfigError("behavior.cooldown_seconds must be >= 0")
        if behavior.queue_max_size < 1:
            raise ConfigError("behavior.queue_max_size must be >= 1")
        if behavior.queue_ttl_hours < 1:
            raise ConfigError("behavior.queue_ttl_hours must be >= 1")

        hours = parse_hours(raw.get("hours"))
        if behavior.heartbeat_interval_seconds < 30:
            raise ConfigError("heartbeat_interval_seconds must be >= 30")
        if behavior.cooldown_prune_interval_seconds < 1:
            raise ConfigError(
                "behavior.cooldown_prune_interval_seconds must be >= 1"
            )

        z = _require(raw, "detection_zone", dict)
        _reject_unknown(
            z,
            {"enabled", "x_min_pct", "x_max_pct", "y_min_pct", "y_max_pct"},
            "detection_zone",
        )
        detection_zone = DetectionZone(
            enabled=_require(z, "enabled", bool),
            x_min_pct=_require(z, "x_min_pct", float),
            x_max_pct=_require(z, "x_max_pct", float),
            y_min_pct=_require(z, "y_min_pct", float),
            y_max_pct=_require(z, "y_max_pct", float),
        )
        for name, val in [
            ("x_min_pct", detection_zone.x_min_pct),
            ("x_max_pct", detection_zone.x_max_pct),
            ("y_min_pct", detection_zone.y_min_pct),
            ("y_max_pct", detection_zone.y_max_pct),
        ]:
            if not 0.0 <= val <= 1.0:
                raise ConfigError(f"detection_zone.{name} must be [0,1], got {val}")
        if detection_zone.x_max_pct <= detection_zone.x_min_pct:
            raise ConfigError("detection_zone x_max_pct must be > x_min_pct")
        if detection_zone.y_max_pct <= detection_zone.y_min_pct:
            raise ConfigError("detection_zone y_max_pct must be > y_min_pct")

        c = _require(raw, "camera", dict)
        _reject_unknown(
            c, {"resolution_width", "resolution_height", "framerate"},
            "camera",
        )
        camera = Camera(
            resolution_width=_require(c, "resolution_width", int),
            resolution_height=_require(c, "resolution_height", int),
            framerate=_require(c, "framerate", int),
        )
        if (camera.resolution_width <= 0 or camera.resolution_height <= 0
                or camera.framerate <= 0):
            raise ConfigError(
                "camera resolution_width, resolution_height, and framerate "
                "must all be > 0"
            )

        tl = _require(raw, "telemetry", dict)
        _reject_unknown(
            tl,
            {"include_tailscale", "include_cellular",
             "save_zone_preview_on_boot"},
            "telemetry",
        )
        telemetry = Telemetry(
            include_tailscale=_require(tl, "include_tailscale", bool),
            include_cellular=_require(tl, "include_cellular", bool),
            save_zone_preview_on_boot=_require(tl, "save_zone_preview_on_boot", bool),
        )

        # Presence block is optional for backward compatibility. Units on old
        # config.json default to enabled with conservative parameters.
        pr = raw.get("presence", {})
        if not isinstance(pr, dict):
            raise ConfigError("presence must be an object if present")
        _reject_unknown(
            pr,
            {"enabled", "iou_match_threshold", "track_timeout_seconds",
             "min_track_frames", "motion_area_fraction", "color_enabled",
             "color_split_distance", "camera_position",
             "encounter_handoff_seconds", "board_zone",
             "route_qualification",
             "stop_speed_frac_per_s", "stop_confirm_frames",
             "trigger_max_wait_seconds", "depart_hysteresis_pct",
             "occlusion_hold_enabled", "occlusion_max_advance_positions",
             "motion_boxes_enabled", "driveoff_requires_plate",
             "motion_box_min_area_frac", "assoc_cost_threshold",
             "assoc_dist_weight", "assoc_scale_weight", "assoc_iou_weight",
             "shadow_only",
             "vote_min_frames", "vote_window_seconds", "dwell_zone",
             "vehicle_only", "dedup"},
            "presence",
        )

        def _parse_presence_box(value, label: str):
            if value is None:
                return None
            if not isinstance(value, dict):
                raise ConfigError(f"{label} must be an object")
            _reject_unknown(
                value,
                {"x_min_pct", "y_min_pct", "x_max_pct", "y_max_pct"},
                label,
            )
            try:
                values = [
                    value["x_min_pct"], value["y_min_pct"],
                    value["x_max_pct"], value["y_max_pct"],
                ]
            except KeyError as e:
                raise ConfigError(f"{label} missing field: {e}") from e
            if any(type(item) not in (int, float) for item in values):
                raise ConfigError(f"{label} values must be numeric, not strings")
            box = tuple(float(item) for item in values)
            if any(not 0.0 <= item <= 1.0 for item in box):
                raise ConfigError(f"{label} values must be [0,1]")
            if box[2] <= box[0] or box[3] <= box[1]:
                raise ConfigError(f"{label} max must be > min")
            return box

        bz_raw = pr.get("board_zone")
        board_zone = _parse_presence_box(bz_raw, "presence.board_zone")

        route_raw = pr.get("route_qualification", {}) or {}
        if not isinstance(route_raw, dict):
            raise ConfigError("presence.route_qualification must be an object")
        _reject_unknown(
            route_raw,
            {"enabled", "shadow_only", "profile_reconciliation_enabled",
             "entry_zone", "min_trajectory_points",
             "min_trajectory_duration_seconds", "zone_confirm_points",
             "min_leftward_progress", "min_upward_progress",
             "board_dwell_fallback_min_points",
             "board_dwell_fallback_min_board_points",
             "board_dwell_fallback_min_seconds",
             "progressive_board_rescue_min_points",
             "progressive_board_rescue_min_board_points",
             "progressive_board_rescue_min_seconds",
             "terminal_exit_zone", "decision_timeout_seconds",
             "rejection_grace_seconds", "unmatched_dwell_cache_seconds",
             "terminal_tombstone_seconds"},
            "presence.route_qualification",
        )
        route_qualification = RouteQualification(
            enabled=_flag(
                route_raw, "enabled", False,
                label="presence.route_qualification.enabled",
            ),
            shadow_only=_flag(
                route_raw, "shadow_only", True,
                label="presence.route_qualification.shadow_only",
            ),
            profile_reconciliation_enabled=_flag(
                route_raw, "profile_reconciliation_enabled", False,
                label=(
                    "presence.route_qualification."
                    "profile_reconciliation_enabled"
                ),
            ),
            entry_zone=_parse_presence_box(
                route_raw.get("entry_zone"),
                "presence.route_qualification.entry_zone",
            ),
            min_trajectory_points=_int(
                route_raw, "min_trajectory_points", 2
            ),
            min_trajectory_duration_seconds=_num(
                route_raw, "min_trajectory_duration_seconds", 0.0
            ),
            zone_confirm_points=_int(
                route_raw, "zone_confirm_points", 1
            ),
            min_leftward_progress=_num(
                route_raw, "min_leftward_progress", 0.0
            ),
            min_upward_progress=_num(
                route_raw, "min_upward_progress", 0.0
            ),
            board_dwell_fallback_min_points=_int(
                route_raw, "board_dwell_fallback_min_points", 0
            ),
            board_dwell_fallback_min_board_points=_int(
                route_raw, "board_dwell_fallback_min_board_points", 0
            ),
            board_dwell_fallback_min_seconds=_num(
                route_raw, "board_dwell_fallback_min_seconds", 0.0
            ),
            progressive_board_rescue_min_points=_int(
                route_raw, "progressive_board_rescue_min_points", 0
            ),
            progressive_board_rescue_min_board_points=_int(
                route_raw, "progressive_board_rescue_min_board_points", 0
            ),
            progressive_board_rescue_min_seconds=_num(
                route_raw, "progressive_board_rescue_min_seconds", 0.0
            ),
            terminal_exit_zone=_parse_presence_box(
                route_raw.get("terminal_exit_zone"),
                "presence.route_qualification.terminal_exit_zone",
            ),
            decision_timeout_seconds=_num(
                route_raw, "decision_timeout_seconds", 120.0
            ),
            rejection_grace_seconds=_num(
                route_raw, "rejection_grace_seconds", 10.0
            ),
            unmatched_dwell_cache_seconds=_num(
                route_raw, "unmatched_dwell_cache_seconds", 30.0
            ),
            terminal_tombstone_seconds=_num(
                route_raw, "terminal_tombstone_seconds", 300.0
            ),
        )
        if route_qualification.enabled and route_qualification.entry_zone is None:
            raise ConfigError(
                "presence.route_qualification.entry_zone is required when "
                "route qualification is enabled"
            )
        if route_qualification.enabled and board_zone is None:
            raise ConfigError(
                "presence.board_zone must be explicit when route "
                "qualification is enabled"
            )
        if (
            route_qualification.enabled
            and not route_qualification.shadow_only
            and not route_qualification.profile_reconciliation_enabled
        ):
            raise ConfigError(
                "authoritative route qualification requires "
                "profile_reconciliation_enabled=true"
            )
        if route_qualification.enabled:
            entry = route_qualification.entry_zone
            overlap_x = min(entry[2], board_zone[2]) - max(
                entry[0], board_zone[0]
            )
            overlap_y = min(entry[3], board_zone[3]) - max(
                entry[1], board_zone[1]
            )
            if overlap_x >= 0.0 and overlap_y >= 0.0:
                raise ConfigError(
                    "presence.route_qualification.entry_zone must not "
                    "overlap presence.board_zone"
                )
        if not 2 <= route_qualification.min_trajectory_points <= 120:
            raise ConfigError(
                "presence.route_qualification.min_trajectory_points must "
                "be in [2, 120]"
            )
        if not 0.0 <= route_qualification.min_trajectory_duration_seconds <= 30.0:
            raise ConfigError(
                "presence.route_qualification.min_trajectory_duration_seconds must "
                "be in [0, 30]"
            )
        if not 1 <= route_qualification.zone_confirm_points <= 20:
            raise ConfigError(
                "presence.route_qualification.zone_confirm_points must be "
                "in [1, 20]"
            )
        if not 0.0 <= route_qualification.min_leftward_progress <= 1.0:
            raise ConfigError(
                "presence.route_qualification.min_leftward_progress must be "
                "in [0, 1]"
            )
        if not 0.0 <= route_qualification.min_upward_progress <= 1.0:
            raise ConfigError(
                "presence.route_qualification.min_upward_progress must be "
                "in [0, 1]"
            )
        if not 0 <= route_qualification.board_dwell_fallback_min_points <= 120:
            raise ConfigError(
                "presence.route_qualification."
                "board_dwell_fallback_min_points must be in [0, 120]"
            )
        if not 0 <= route_qualification.board_dwell_fallback_min_board_points <= 120:
            raise ConfigError(
                "presence.route_qualification."
                "board_dwell_fallback_min_board_points must be in [0, 120]"
            )
        if not 0.0 <= route_qualification.board_dwell_fallback_min_seconds <= 30.0:
            raise ConfigError(
                "presence.route_qualification."
                "board_dwell_fallback_min_seconds must be in [0, 30]"
            )
        fallback_points = route_qualification.board_dwell_fallback_min_points
        fallback_board_points = (
            route_qualification.board_dwell_fallback_min_board_points
        )
        fallback_seconds = route_qualification.board_dwell_fallback_min_seconds
        fallback_enabled = (
            fallback_points > 0
            or fallback_board_points > 0
            or fallback_seconds > 0.0
        )
        if fallback_enabled and not (
            fallback_points > 0
            and fallback_board_points > 0
            and fallback_seconds > 0.0
        ):
            raise ConfigError(
                "presence.route_qualification board-dwell fallback requires "
                "total points, board points and seconds together, or all zero"
            )
        if (
            fallback_points
            and fallback_points < route_qualification.min_trajectory_points
        ):
            raise ConfigError(
                "presence.route_qualification."
                "board_dwell_fallback_min_points must be at least "
                "min_trajectory_points"
            )
        if fallback_board_points > fallback_points:
            raise ConfigError(
                "presence.route_qualification."
                "board_dwell_fallback_min_board_points cannot exceed "
                "board_dwell_fallback_min_points"
            )
        if (
            fallback_board_points
            and fallback_board_points < route_qualification.zone_confirm_points
        ):
            raise ConfigError(
                "presence.route_qualification."
                "board_dwell_fallback_min_board_points must be at least "
                "zone_confirm_points"
            )
        rescue_points = route_qualification.progressive_board_rescue_min_points
        rescue_board_points = (
            route_qualification.progressive_board_rescue_min_board_points
        )
        rescue_seconds = route_qualification.progressive_board_rescue_min_seconds
        rescue_enabled = (
            rescue_points > 0
            or rescue_board_points > 0
            or rescue_seconds > 0.0
        )
        if rescue_enabled and not (
            rescue_points >= 2
            and rescue_board_points >= 1
            and rescue_seconds > 0.0
        ):
            raise ConfigError(
                "presence.route_qualification progressive-board rescue "
                "requires at least two total points, one board point and "
                "positive seconds together, or all zero"
            )
        if not 0 <= rescue_points <= 120:
            raise ConfigError(
                "presence.route_qualification."
                "progressive_board_rescue_min_points must be in [0, 120]"
            )
        if not 0 <= rescue_board_points <= 120:
            raise ConfigError(
                "presence.route_qualification."
                "progressive_board_rescue_min_board_points must be in [0, 120]"
            )
        if not 0.0 <= rescue_seconds <= 30.0:
            raise ConfigError(
                "presence.route_qualification."
                "progressive_board_rescue_min_seconds must be in [0, 30]"
            )
        if rescue_board_points > rescue_points:
            raise ConfigError(
                "presence.route_qualification."
                "progressive_board_rescue_min_board_points cannot exceed "
                "progressive_board_rescue_min_points"
            )
        if not 10.0 <= route_qualification.decision_timeout_seconds <= 600.0:
            raise ConfigError(
                "presence.route_qualification.decision_timeout_seconds must "
                "be in [10, 600]"
            )
        if not 0.0 <= route_qualification.rejection_grace_seconds <= 60.0:
            raise ConfigError(
                "presence.route_qualification.rejection_grace_seconds must "
                "be in [0, 60]"
            )
        if not 1.0 <= route_qualification.unmatched_dwell_cache_seconds <= 120.0:
            raise ConfigError(
                "presence.route_qualification.unmatched_dwell_cache_seconds "
                "must be in [1, 120]"
            )
        if not 0.0 <= route_qualification.terminal_tombstone_seconds <= 900.0:
            raise ConfigError(
                "presence.route_qualification.terminal_tombstone_seconds must "
                "be in [0, 900]"
            )

        presence = Presence(
            enabled=_flag(pr, "enabled", True, label="presence.enabled"),
            iou_match_threshold=_num(pr, "iou_match_threshold", 0.3),
            track_timeout_seconds=_num(pr, "track_timeout_seconds", 3.0),
            min_track_frames=_int(
                pr, "min_track_frames", 3,
                label="presence.min_track_frames"),
            motion_area_fraction=_num(pr, "motion_area_fraction", 0.12),
            color_enabled=_flag(
                pr, "color_enabled", True, label="presence.color_enabled"),
            color_split_distance=_num(pr, "color_split_distance", 0.55),
            camera_position=str(pr.get("camera_position", "at_board")),
            encounter_handoff_seconds=_num(
                pr, "encounter_handoff_seconds", 5.0),
            board_zone=board_zone,
            route_qualification=route_qualification,
            stop_speed_frac_per_s=_num(pr, "stop_speed_frac_per_s", 0.02),
            stop_confirm_frames=_int(
                pr, "stop_confirm_frames", 3,
                label="presence.stop_confirm_frames"),
            trigger_max_wait_seconds=_num(
                pr, "trigger_max_wait_seconds", 4.0),
            depart_hysteresis_pct=_num(pr, "depart_hysteresis_pct", 0.05),
            occlusion_hold_enabled=_flag(
                pr, "occlusion_hold_enabled", True,
                label="presence.occlusion_hold_enabled"),
            occlusion_max_advance_positions=_int(
                pr, "occlusion_max_advance_positions", 2,
                label="presence.occlusion_max_advance_positions"),
            motion_boxes_enabled=_flag(
                pr, "motion_boxes_enabled", True,
                label="presence.motion_boxes_enabled"),
            driveoff_requires_plate=_flag(
                pr, "driveoff_requires_plate", True,
                label="presence.driveoff_requires_plate"),
            motion_box_min_area_frac=_num(
                pr, "motion_box_min_area_frac", 0.005),
            assoc_cost_threshold=_num(pr, "assoc_cost_threshold", 0.7),
            assoc_dist_weight=_num(pr, "assoc_dist_weight", 0.5),
            assoc_scale_weight=_num(pr, "assoc_scale_weight", 0.2),
            assoc_iou_weight=_num(pr, "assoc_iou_weight", 0.3),
            shadow_only=_flag(
                pr, "shadow_only", False, label="presence.shadow_only"),
        )
        if presence.camera_position not in VALID_CAMERA_POSITIONS:
            raise ConfigError(
                f"presence.camera_position must be one of "
                f"{VALID_CAMERA_POSITIONS}, got {presence.camera_position!r}"
            )
        if not 0.0 <= presence.encounter_handoff_seconds <= 15.0:
            raise ConfigError(
                "presence.encounter_handoff_seconds must be [0, 15]"
            )
        if presence.stop_confirm_frames < 1:
            raise ConfigError("presence.stop_confirm_frames must be >= 1")
        if presence.stop_speed_frac_per_s <= 0:
            raise ConfigError("presence.stop_speed_frac_per_s must be > 0")
        if presence.trigger_max_wait_seconds <= 0:
            raise ConfigError("presence.trigger_max_wait_seconds must be > 0")
        if not 0.0 <= presence.depart_hysteresis_pct <= 0.5:
            raise ConfigError("presence.depart_hysteresis_pct must be [0, 0.5]")
        if presence.occlusion_max_advance_positions < 1:
            raise ConfigError(
                "presence.occlusion_max_advance_positions must be >= 1"
            )
        if not 0.0 < presence.motion_box_min_area_frac <= 0.5:
            raise ConfigError(
                "presence.motion_box_min_area_frac must be (0, 0.5]"
            )
        if presence.assoc_cost_threshold <= 0:
            raise ConfigError("presence.assoc_cost_threshold must be > 0")
        weights = (
            presence.assoc_dist_weight,
            presence.assoc_scale_weight,
            presence.assoc_iou_weight,
        )
        if any(weight < 0 for weight in weights) or sum(weights) <= 0:
            raise ConfigError(
                "presence association weights must be >= 0 and sum to > 0"
            )
        if not 0.0 <= presence.iou_match_threshold <= 1.0:
            raise ConfigError("presence.iou_match_threshold must be [0,1]")
        if presence.track_timeout_seconds <= 0 or presence.track_timeout_seconds > 60:
            raise ConfigError("presence.track_timeout_seconds must be (0, 60]")
        if presence.min_track_frames < 1:
            raise ConfigError("presence.min_track_frames must be >= 1")
        if not 0.0 < presence.motion_area_fraction <= 1.0:
            raise ConfigError("presence.motion_area_fraction must be (0, 1]")
        if not 0.0 < presence.color_split_distance <= 1.0:
            raise ConfigError("presence.color_split_distance must be (0, 1]")

        # --- new deployment-driven blocks -----------------------------
        alpr = _parse_alpr(raw, raw)
        engine = alpr.engine

        pr_raw = raw.get("presence", {}) or {}

        dz_raw = pr_raw.get("dwell_zone", {}) or {}
        anchor = str(dz_raw.get("anchor", "plate_center"))
        if anchor not in VALID_ANCHORS:
            raise ConfigError(
                f"presence.dwell_zone.anchor must be one of {VALID_ANCHORS}, "
                f"got {anchor!r}"
            )
        dwell_zone = _parse_norm_box(
            dz_raw, "presence.dwell_zone", cls=DwellZone,
            anchor=anchor,
            min_seconds_in_zone=_num(dz_raw, "min_seconds_in_zone", 0.0),
            min_vehicle_box_area=_unit(
                dz_raw, "min_vehicle_box_area", 0.0,
                label="presence.dwell_zone.min_vehicle_box_area"),
        )
        if dwell_zone.min_seconds_in_zone < 0:
            raise ConfigError(
                "presence.dwell_zone.min_seconds_in_zone must be >= 0")

        vo_raw = pr_raw.get("vehicle_only", {}) or {}
        if not isinstance(vo_raw, dict):
            raise ConfigError("presence.vehicle_only must be an object")
        _reject_unknown(
            vo_raw,
            {"enabled", "min_travel_fraction", "min_area_growth_ratio",
             "min_vehicle_confidence", "require_dwell_zone"},
            "presence.vehicle_only",
        )
        vehicle_only = VehicleOnly(
            enabled=_flag(
                vo_raw, "enabled", True,
                label="presence.vehicle_only.enabled"),
            min_travel_fraction=_num(vo_raw, "min_travel_fraction", 0.10),
            min_area_growth_ratio=_num(vo_raw, "min_area_growth_ratio", 1.5),
            min_vehicle_confidence=_unit(
                vo_raw, "min_vehicle_confidence", 0.0,
                label="presence.vehicle_only.min_vehicle_confidence"),
            require_dwell_zone=_flag(
                vo_raw, "require_dwell_zone", False,
                label="presence.vehicle_only.require_dwell_zone"),
        )
        if not 0.0 <= vehicle_only.min_travel_fraction <= 1.5:
            raise ConfigError(
                "presence.vehicle_only.min_travel_fraction must be [0, 1.5] "
                "(fraction of the frame diagonal)")
        # 1.0 would mean "any growth at all", which is noise, not evidence.
        if vehicle_only.min_area_growth_ratio != 0.0 and \
                vehicle_only.min_area_growth_ratio < 1.0:
            raise ConfigError(
                "presence.vehicle_only.min_area_growth_ratio must be 0 "
                "(disabled) or >= 1.0")
        # Both movement gates off means every static box counts — that is the
        # measured 20-events-for-2-cars case. Allowed, but only deliberately.
        if (vehicle_only.enabled
                and not vehicle_only.require_dwell_zone
                and vehicle_only.min_travel_fraction == 0.0
                and vehicle_only.min_area_growth_ratio == 0.0):
            _bootstrap_log(
                "WARNING: presence.vehicle_only has BOTH movement gates "
                "disabled and no dwell zone requirement. Parked cars and "
                "background traffic will be counted as missed vehicles."
            )

        dd_raw = pr_raw.get("dedup", {}) or {}
        if not isinstance(dd_raw, dict):
            raise ConfigError("presence.dedup must be an object")
        _reject_unknown(
            dd_raw,
            {"cooldown_seconds", "release_requires_absent_frames",
             "release_requires_absent_seconds", "near_match_max_distance"},
            "presence.dedup",
        )
        dedup = Dedup(
            cooldown_seconds=_num(dd_raw, "cooldown_seconds", 120.0),
            release_requires_absent_frames=_int(
                dd_raw, "release_requires_absent_frames",
                thresholds.dwell_release_requires_absent_frames,
                label="presence.dedup.release_requires_absent_frames"),
            release_requires_absent_seconds=_num(
                dd_raw, "release_requires_absent_seconds", 0.0),
            near_match_max_distance=_int(
                dd_raw, "near_match_max_distance", 0,
                label="presence.dedup.near_match_max_distance"),
        )
        if dedup.near_match_max_distance < 0:
            raise ConfigError(
                "presence.dedup.near_match_max_distance must be >= 0")
        if dedup.near_match_max_distance > 2:
            log.warning(
                "presence.dedup.near_match_max_distance=%d is high — two "
                "genuinely different plates that far apart would be merged "
                "into one visit. 1 is the measured setting.",
                dedup.near_match_max_distance,
            )
        if dedup.cooldown_seconds < 0:
            raise ConfigError("presence.dedup.cooldown_seconds must be >= 0")
        if dedup.release_requires_absent_seconds < 0:
            raise ConfigError(
                "presence.dedup.release_requires_absent_seconds must be >= 0")
        if dedup.release_requires_absent_frames < 1:
            raise ConfigError(
                "presence.dedup.release_requires_absent_frames must be >= 1")

        # presence.vote_* override the legacy thresholds block when present,
        # so there is exactly one effective value for each.
        if "vote_min_frames" in pr_raw:
            thresholds.vote_min_frames = _int(
                pr_raw, "vote_min_frames", thresholds.vote_min_frames,
                label="presence.vote_min_frames")
        if "vote_window_seconds" in pr_raw:
            thresholds.vote_window_seconds = _num(
                pr_raw, "vote_window_seconds", thresholds.vote_window_seconds)
        # App-level confidence floors are the authoritative gate. Only fold
        # them in when the caller actually supplied an alpr.thresholds block,
        # so legacy configs keep their own numbers.
        if isinstance(raw.get("alpr"), dict) and \
                isinstance(raw["alpr"].get("thresholds"), dict):
            at = raw["alpr"]["thresholds"]
            if "app_detect_min" in at:
                thresholds.min_detection_confidence = alpr.thresholds.app_detect_min
            if "app_ocr_min" in at:
                thresholds.min_ocr_confidence = alpr.thresholds.app_ocr_min

        # Overrides are authoritative, so validate the final effective values,
        # not only the legacy block that existed before those overrides.
        if thresholds.vote_min_frames < 1:
            raise ConfigError("effective vote_min_frames must be >= 1")
        # One-frame confirm via vote_min_frames=1 ships an identity from a
        # single read. Only allow it when the OCR floors are high enough that
        # the quality gate is doing the work the second frame used to do.
        # Preferred alternative: vote_min_frames=2 + high_ocr_single_frame.
        if thresholds.vote_min_frames == 1 and (
            thresholds.min_ocr_confidence < 0.85
            or thresholds.group_ocr_floor < 0.85
        ):
            raise ConfigError(
                "vote_min_frames=1 requires effective min_ocr_confidence "
                "and group_ocr_floor both >= 0.85"
            )
        if not 0.0 <= thresholds.high_ocr_single_frame <= 1.0:
            raise ConfigError(
                "effective high_ocr_single_frame must be in [0, 1], "
                f"got {thresholds.high_ocr_single_frame}"
            )
        if (
            thresholds.high_ocr_single_frame > 0.0
            and thresholds.high_ocr_single_frame < thresholds.min_ocr_confidence
        ):
            raise ConfigError(
                "high_ocr_single_frame must be >= min_ocr_confidence "
                f"({thresholds.high_ocr_single_frame} < "
                f"{thresholds.min_ocr_confidence}); otherwise the fast path "
                "can never fire"
            )
        if not 0.0 < thresholds.vote_window_seconds <= 30.0:
            raise ConfigError(
                "effective vote_window_seconds must be in (0, 30]"
            )
        for label, value in (
            ("effective min_detection_confidence",
             thresholds.min_detection_confidence),
            ("effective min_ocr_confidence", thresholds.min_ocr_confidence),
            ("effective group_detection_floor",
             thresholds.group_detection_floor),
            ("effective group_ocr_floor", thresholds.group_ocr_floor),
        ):
            if not 0.0 <= value <= 1.0:
                raise ConfigError(f"{label} must be in [0, 1], got {value}")
        # The override above rewrites the per-frame floors, so the numbers in
        # the legacy thresholds block are NOT necessarily what runs. Log what
        # actually applies, including any clamping of the group floors —
        # a gate nobody can see the value of is a gate nobody can tune.
        log.info(
            "effective gates: per-frame det>=%.2f ocr>=%.2f | "
            "group det>=%.2f ocr>=%.2f | vote %d frames within %.1fs | "
            "high_ocr_1f>=%.2f",
            thresholds.min_detection_confidence,
            thresholds.min_ocr_confidence,
            thresholds.group_detection_floor,
            thresholds.group_ocr_floor,
            thresholds.vote_min_frames,
            thresholds.vote_window_seconds,
            thresholds.high_ocr_single_frame,
        )
        if alpr.thresholds.consensus_rescue_min_frames:
            log.warning(
                "consensus rescue ON for %s: %d exact reads from one "
                "encounter, det>=%.2f ocr>=%.2f, exactly one normal gate "
                "may fail",
                alpr.engine,
                alpr.thresholds.consensus_rescue_min_frames,
                alpr.thresholds.consensus_rescue_detect_min,
                alpr.thresholds.consensus_rescue_ocr_min,
            )
        if alpr.thresholds.provider_selected_single_enabled:
            log.warning(
                "provider-selected single confirmation ON for %s: "
                "normal gates, length >=%d, relative plate x >=%.2f",
                alpr.engine,
                alpr.thresholds.provider_selected_single_min_length,
                alpr.thresholds.provider_selected_single_min_relative_x,
            )
        if alpr.thresholds.provider_high_ocr_rescue_enabled:
            log.warning(
                "provider high-OCR rescue ON for %s: det>=%.2f, "
                "ocr>=%.2f, no competing encounter variant, length >=%d, "
                "relative plate x >=%.2f",
                alpr.engine,
                alpr.thresholds.provider_high_ocr_rescue_detect_min,
                alpr.thresholds.provider_high_ocr_rescue_ocr_min,
                alpr.thresholds.provider_selected_single_min_length,
                alpr.thresholds.provider_selected_single_min_relative_x,
            )
        if (
            alpr.engine == "pr_stream"
            and alpr.thresholds.identity_settle_max_seconds > 0.0
        ):
            log.info(
                "vehicle identity settlement ON: %.2fs OCR quiet, %.2fs "
                "hard maximum",
                alpr.thresholds.identity_settle_quiet_seconds,
                alpr.thresholds.identity_settle_max_seconds,
            )
        if thresholds.group_floor_is_stricter:
            log.warning(
                "group confidence floor (det>=%.2f ocr>=%.2f) is STRICTER "
                "than the per-frame floor (det>=%.2f ocr>=%.2f). Reads "
                "between the two are admitted to a vote and then rejected. "
                "This is allowed, but it is what silently cost 5 of 20 cars "
                "on measured footage — such votes are reported as "
                "'group_confidence_floor', so check that count before "
                "concluding the OCR is at fault.",
                thresholds.group_detection_floor,
                thresholds.group_ocr_floor,
                thresholds.min_detection_confidence,
                thresholds.min_ocr_confidence,
            )
        # Keep the dwell absence latch consistent with the dedup block.
        thresholds.dwell_release_requires_absent_frames = \
            dedup.release_requires_absent_frames

        cap_raw = raw.get("capture", {}) or {}
        if not isinstance(cap_raw, dict):
            raise ConfigError("capture must be an object")
        _reject_unknown(cap_raw, {"target_fps", "width", "height"}, "capture")
        capture = Capture(
            target_fps=_num(cap_raw, "target_fps", 5.0),
            width=_int(cap_raw, "width", camera.resolution_width,
                       label="capture.width"),
            height=_int(cap_raw, "height", camera.resolution_height,
                        label="capture.height"),
        )
        if capture.target_fps <= 0:
            raise ConfigError("capture.target_fps must be > 0")
        if capture.width <= 0 or capture.height <= 0:
            raise ConfigError("capture.width/height must be > 0")

        dg_raw = raw.get("diagnostics", {}) or {}
        if not isinstance(dg_raw, dict):
            raise ConfigError("diagnostics must be an object")
        _reject_unknown(
            dg_raw,
            {"log_per_frame_timing", "zone_preview_overlay",
             "log_rejected_reads", "save_miss_plates", "miss_plate_dir",
             "miss_plate_retain_hours", "miss_plate_max_cases",
             "save_confirmed_plates", "save_unidentified_vehicle_frames",
             "miss_plate_pad",
             "save_presence_frames", "presence_frame_dir"},
            "diagnostics",
        )
        miss_dir = dg_raw.get(
            "miss_plate_dir", "/home/serv/plate_reader/miss_plates")
        if not isinstance(miss_dir, str) or not miss_dir.strip():
            raise ConfigError(
                "diagnostics.miss_plate_dir must be a non-empty string")
        diagnostics = Diagnostics(
            log_per_frame_timing=_flag(
                dg_raw, "log_per_frame_timing", False,
                label="diagnostics.log_per_frame_timing"),
            zone_preview_overlay=_flag(
                dg_raw, "zone_preview_overlay", True,
                label="diagnostics.zone_preview_overlay"),
            log_rejected_reads=_flag(
                dg_raw, "log_rejected_reads", False,
                label="diagnostics.log_rejected_reads"),
            save_miss_plates=_flag(
                dg_raw, "save_miss_plates", True,
                label="diagnostics.save_miss_plates"),
            miss_plate_dir=miss_dir.strip(),
            miss_plate_retain_hours=_num(
                dg_raw, "miss_plate_retain_hours", 72.0),
            miss_plate_max_cases=_int(
                dg_raw, "miss_plate_max_cases", 0,
                label="diagnostics.miss_plate_max_cases"),
            save_confirmed_plates=_flag(
                dg_raw, "save_confirmed_plates", True,
                label="diagnostics.save_confirmed_plates"),
            save_unidentified_vehicle_frames=_flag(
                dg_raw, "save_unidentified_vehicle_frames", False,
                label="diagnostics.save_unidentified_vehicle_frames"),
            miss_plate_pad=_num(dg_raw, "miss_plate_pad", 0.5),
            save_presence_frames=_flag(
                dg_raw, "save_presence_frames", False,
                label="diagnostics.save_presence_frames"),
            presence_frame_dir=str(
                dg_raw.get(
                    "presence_frame_dir",
                    "/home/serv/plate_reader/presence_frames",
                )
                or "/home/serv/plate_reader/presence_frames"
            ).strip(),
        )
        if diagnostics.miss_plate_retain_hours < 0:
            raise ConfigError(
                "diagnostics.miss_plate_retain_hours must be >= 0")
        if diagnostics.miss_plate_max_cases < 0:
            raise ConfigError(
                "diagnostics.miss_plate_max_cases must be >= 0")
        if diagnostics.miss_plate_pad < 0:
            raise ConfigError("diagnostics.miss_plate_pad must be >= 0")

        # A token path that is configured but unreadable must NEVER fall
        # through to trial mode: the SDK would redact plate text, normalize()
        # would strip the '*', and the remainder would hash into a confident
        # wrong identity with no error anywhere.
        #
        # Only applies to engines that actually load the ultimateALPR SDK.
        # pr_stream does its recognition in the Stream container and is
        # licensed separately (hardware-bound key in the container's env), so
        # a stale ultimateALPR token path must not block boot there — that
        # would strand a unit that never ran ultimateALPR in the first place.
        if alpr.license_token_file and alpr.engine in (
            "ultimate_alpr", "ultimatealpr"
        ):
            tok = Path(alpr.license_token_file).expanduser()
            if not tok.is_file():
                raise ConfigError(
                    f"alpr.license_token_file is set but not readable: {tok}. "
                    "Refusing to start — the SDK would silently run in trial "
                    "mode and emit '*'-redacted plates that hash into "
                    "confident wrong identities."
                )

        cfg = Config(
            device_id=device_id,
            api_key=api_key,
            location_id=location_id,
            api_base=api_base,
            sw_version=sw_version,
            thresholds=thresholds,
            behavior=behavior,
            detection_zone=detection_zone,
            camera=camera,
            telemetry=telemetry,
            presence=presence,
            engine=engine,
            # Mirrors of the alpr block, kept so pre-schema callers and test
            # stubs still see a value. alpr.* is authoritative.
            license_token_file=alpr.license_token_file,
            ualpr_assets_folder=alpr.assets_folder,
            ualpr_car_detect_enabled=alpr.classifiers.vehicle_detect_enabled,
            ualpr_vcr_enabled=alpr.classifiers.color_enabled,
            alpr=alpr,
            dwell_zone=dwell_zone,
            vehicle_only=vehicle_only,
            dedup=dedup,
            capture=capture,
            diagnostics=diagnostics,
            hours=hours,
            backend_writes_enabled=_flag(raw, "backend_writes_enabled", True),
        )
        log.info(
            "config loaded: device_id=%s location_id=%s sw_version=%s api_base=%s",
            cfg.device_id, cfg.location_id, cfg.sw_version, cfg.api_base,
        )
        if cfg.hours.enabled:
            log.info(
                "open hours: %s %s–%s (capture warmup %d min)",
                cfg.hours.timezone, cfg.hours.open, cfg.hours.close,
                cfg.hours.warmup_minutes,
            )
        return cfg

    except ConfigError as e:
        _bootstrap_log(f"config validation failed: {e}")
        raise
    except Exception as e:
        _bootstrap_log(f"unexpected config error: {type(e).__name__}: {e}")
        raise ConfigError(f"unexpected config error: {e}") from e
