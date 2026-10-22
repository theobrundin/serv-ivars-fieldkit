"""Plate Recognizer Stream as a PlateEngine.

Stream owns the camera (via an RTSP feed from MediaMTX) and pushes results to
a webhook. The rest of this codebase is pull-based: `detector.process_frame`
calls `engine.infer(frame)` once per tick. This module bridges the two by
running a small localhost HTTP listener that parks incoming webhook results in
a queue, which `infer()` then drains.

Why this shape rather than a standalone forwarder:

  Everything downstream of recognition stays byte-for-byte unchanged — the
  vote/dwell/cooldown logic in detector.py, NORMALIZATION_SPEC hashing in
  normalizer.py + hasher.py, the SQLite retry/dead-letter queue in sender.py,
  and the heartbeat that feeds the Devices table and DeviceMonitor alerting.
  Only the thing that reads plates changes, which is the whole point.

  In particular the plate NEVER leaves this device as text: Stream posts to
  127.0.0.1, and the existing pipeline hashes before anything is sent to AWS.
  That property is a patent claim and a SECURITY.md requirement, so the
  listener deliberately binds loopback-only and refuses to start otherwise.

Threading: the HTTP server runs on its own daemon thread. `_pending` is
guarded by `_lock`; encounter-bound reads first spend a short bounded interval
in `_identity_stages` so a truncated first OCR result cannot become a terminal
hash before a fuller view of that same car arrives. `infer()` drains only the
settled canonical candidate, so a slow consumer can never block the webhook
handler.
"""
import hashlib
import json
import math
import re
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default as _default_policy
from http.server import BaseHTTPRequestHandler, HTTPServer

from .engines import RawPlateRead
from .audit_evidence import AuditFrameCache
from .log import get
from .normalizer import normalize


@dataclass(frozen=True)
class StreamVehicle:
    """Plate-free vehicle metadata used to own one physical encounter.

    Deliberately a separate type from RawPlateRead rather than a flag on it:
    there is never a plate string here, even when ``saw_plate`` records that
    the paired RawPlateRead existed. Therefore there is no hash and no way for
    this to be mistaken for a customer identity by forgetting a boolean.
    """
    vehicle_bbox: tuple[int, int, int, int] | None
    vehicle_confidence: float
    vehicle_type: str | None = None
    make: str | None = None
    model: str | None = None
    color: str | None = None
    make_confidence: float | None = None
    model_confidence: float | None = None
    color_confidence: float | None = None
    # Populated only when an encounter reaches its one terminal /visit.
    # None on raw observations constructed by the webhook parser.
    encounter_id: str | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    saw_plate: bool = False
    diagnostic_episode_id: str | None = None
    plate_detection_confidence: float | None = None
    plate_ocr_confidence: float | None = None
    # Physical-route audit fields.  These contain no plate text and are
    # populated only on a terminal profile or route metadata snapshot.
    route_status: str | None = None
    route_reason: str | None = None
    # Local audit taxonomy only. Rejected route evidence never becomes a
    # Visit, customer identity, or KPI input.
    traffic_classification: str | None = None
    route_track_fingerprint: str | None = None
    trajectory_points: int = 0
    dwell_seconds: float | None = None
    # Local-only route evidence used to audit classifier decisions.  The
    # completed dwell payload carries boxes/timestamps but no image bytes;
    # retain that safe geometry alongside the latest recognition frame so an
    # operator can distinguish a true exit from a provider track split.
    route_trajectory_bboxes: tuple[
        tuple[int, int, int, int], ...
    ] = field(default=(), compare=False, repr=False)
    route_trajectory_timestamps: tuple[
        str | None, ...
    ] = field(default=(), compare=False, repr=False)
    route_entry_zone: tuple[float, float, float, float] | None = field(
        default=None, compare=False, repr=False
    )
    route_board_zone: tuple[float, float, float, float] | None = field(
        default=None, compare=False, repr=False
    )
    route_terminal_exit_zone: tuple[
        float, float, float, float
    ] | None = field(default=None, compare=False, repr=False)
    # Compressed original webhook frame retained only while this encounter is
    # unresolved. It never enters identity or an outbound payload.
    # compare=False also keeps large binary evidence out of dataclass
    # equality/repr paths.
    diagnostic_image_jpeg: bytes | None = field(
        default=None, compare=False, repr=False
    )
    # The vehicle box from the same webhook frame as diagnostic_image_jpeg.
    # vehicle_bbox intentionally keeps moving with the live encounter, so it
    # cannot also describe an earlier retained audit frame.
    diagnostic_image_bbox: tuple[int, int, int, int] | None = field(
        default=None, compare=False, repr=False
    )
    # Provider timestamp owning the retained JPEG. This prevents a delayed
    # webhook or older reconciled front fragment from rewinding evidence that
    # already reached the rear/menu-board view.
    diagnostic_image_observed_at: str | None = field(
        default=None, compare=False, repr=False
    )


@dataclass(frozen=True)
class StreamDwellTrack:
    """One completed native Stream vehicle trajectory.

    `plate_key` is the canonical on-device correlation value only. It never
    leaves this class, is never logged, and is never accepted as identity: the
    dwell hook carries no OCR/detection confidence pair, so only the normal
    recognition webhook may create a plate hash.
    """
    first_seen_at: str | None
    last_seen_at: str | None
    dwell_seconds: float
    plate_key: str | None
    samples: tuple[StreamVehicle, ...]
    # Timestamp for each parsed trajectory sample. Stream may provide an
    # explicit timestamp or only position_sec; the latter is resolved against
    # first_seen_at. This lets the board trigger retain the actual first
    # crossing instead of incorrectly using the end of a long dwell.
    sample_observed_at: tuple[str | None, ...] = ()
    # Only used to scope local image evidence, never identity/route authority.
    camera_id: str | None = None

log = get(__name__)

_DRIVE_OFF_ROUTE_REASONS = frozenset({"terminal_upper_right_exit"})
_NON_DRIVE_THROUGH_ROUTE_REASONS = frozenset({
    "never_entered_drive_through",
})


def route_diagnostic_classification(reason: str | None) -> str:
    """Classify rejected route evidence without overstating weak telemetry."""
    if reason in _DRIVE_OFF_ROUTE_REASONS:
        return "drive_off"
    if reason in _NON_DRIVE_THROUGH_ROUTE_REASONS:
        return "non_drive_through"
    return "route_unresolved"

# Stream posts a JPEG when a webhook image mode is enabled. Bound the
# listener so a misconfigured config.ini cannot OOM the Pi.
MAX_BODY_BYTES = 8 * 1024 * 1024
# A decoded native frame is several MiB. OCR can flap through many strings in
# three seconds, so "one image per variant" alone is not a real memory bound.
MAX_IDENTITY_STAGE_IMAGES = 4
MAX_ENCOUNTER_ALIASES = 1024
MAX_TRAJECTORY_SAMPLE_SKEW_SECONDS = 1.0
# Stream retries a webhook when the local response is lost. Keep a bounded
# process-local receipt cache so a retry cannot become another observation or
# another vote. Restart-durable exactly-once handling requires persisting the
# entire inbound encounter/vote transaction, not merely a "seen" bit; doing
# only the latter would lose a car if the process died after claiming it.
MAX_RECOGNITION_RECEIPTS = 4096
RECOGNITION_RECEIPT_TTL_SECONDS = 6 * 3600.0


def _clamp_unit(value) -> float:
    """Coerce a Stream score to the 0.0-1.0 contract RawPlateRead requires."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:                      # NaN
        return 0.0
    return max(0.0, min(1.0, number))


def _bbox(box) -> tuple[int, int, int, int]:
    """Stream's {xmin,ymin,xmax,ymax} -> the (x1,y1,x2,y2) tuple we use."""
    if not isinstance(box, dict):
        return (0, 0, 0, 0)
    try:
        return (
            int(box.get("xmin", 0)), int(box.get("ymin", 0)),
            int(box.get("xmax", 0)), int(box.get("ymax", 0)),
        )
    except (TypeError, ValueError):
        return (0, 0, 0, 0)


def _prop_score(entry: dict) -> float | None:
    value = entry.get("score")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return _clamp_unit(value)


def _top_scored(entries, *keys):
    """Values plus score from the actual highest-confidence prop candidate."""
    if not isinstance(entries, list):
        return None
    candidates = [
        entry for entry in entries
        if isinstance(entry, dict)
        and all(entry.get(key) not in (None, "") for key in keys)
    ]
    if not candidates:
        return None
    best = max(
        enumerate(candidates),
        key=lambda item: (
            _prop_score(item[1])
            if _prop_score(item[1]) is not None else -1.0,
            -item[0],
        ),
    )[1]
    values = tuple(best.get(key) or None for key in keys)
    return (*values, _prop_score(best))


def _best_mmc(result: dict):
    """Best-scored make/model pair and colour from a Stream result.

    Stream normally returns ranked lists, but selecting by the supplied score
    makes the invariant explicit and robust to an out-of-order payload.
    These are descriptive attributes only — NEVER hash input. The vehicle
    identity is the plate hash and nothing else.
    """
    make = model = colour = None
    pair_score = colour_score = None
    mm = result.get("model_make")
    best_mm = _top_scored(mm, "make", "model")
    if best_mm:
        make, model, pair_score = best_mm
    col = result.get("color")
    best_colour = _top_scored(col, "color")
    if best_colour:
        colour, colour_score = best_colour
    return make, model, colour, pair_score, colour_score


def parse_webhook_payload(payload: dict) -> list[RawPlateRead]:
    """Convert one Stream webhook body into engine-neutral reads.

    Handles BOTH payload schemas — see `_plate_fields` for why there are two.
    The two scores are mapped deliberately and are NOT interchangeable:
      detection score -> detection_confidence   (gated by app_detect_min)
      OCR score       -> ocr_confidence         (gated by app_ocr_min)

    Results with NO plate are skipped here on purpose: this feeds the
    identify pipeline, which is plate-only by design. In `detection_mode =
    vehicle`, cars whose plate was never read are picked up by
    `parse_vehicle_only` instead, so they still count as vehicles.

    Never raises: a malformed result is skipped, not fatal, matching the
    PlateEngine contract that one bad input can't kill the detection loop.
    """
    out: list[RawPlateRead] = []
    observed_at = _payload_timestamp(payload)
    observation_id = _source_observation_id(payload)
    for result in _results(payload):
        read = _read_from(
            result,
            captured_at_iso=observed_at,
            source_observation_id=observation_id,
        )
        if read is not None:
            out.append(read)
    return out


def _top(entries, *keys):
    """Highest-scoring entry from one of Stream's ranked prop lists."""
    if not isinstance(entries, list) or not entries:
        return None
    first = entries[0]
    if not isinstance(first, dict):
        return None
    if len(keys) == 1:
        return first.get(keys[0]) or None
    return tuple(first.get(k) or None for k in keys)


def _plate_fields(result: dict):
    """(text, det_score, ocr_score, plate_bbox) from EITHER payload schema.

    Stream emits two structurally different shapes depending on
    `detection_mode`, and this is not documented as a schema change:

      detection_mode = plate    result["plate"] is a STRING, with the OCR
                                score in result["score"] and the detection
                                score in result["dscore"].

      detection_mode = vehicle  result["plate"] is a DICT. Its "score" is the
                                DETECTION score, and the OCR candidates live
                                at ["props"]["plate"], best first, each with
                                its own score.

    Reading only the first shape makes `plate` a dict, fails the isinstance
    check, and drops every plate silently -- no vote ever opens, and each car
    is logged as a plateless vehicle. That is exactly what happened live.
    """
    plate = result.get("plate")

    if isinstance(plate, str):                      # detection_mode = plate
        return (plate or None,
                _clamp_unit(result.get("dscore")),
                _clamp_unit(result.get("score")),
                _bbox(result.get("box")))

    if isinstance(plate, dict):                     # detection_mode = vehicle
        props = plate.get("props") or {}
        best = _top(props.get("plate"), "value", "score")
        if not best or not best[0]:
            return None, 0.0, 0.0, _bbox(plate.get("box"))
        text, ocr = best
        return (str(text),
                _clamp_unit(plate.get("score")),
                _clamp_unit(ocr),
                _bbox(plate.get("box")))

    return None, 0.0, 0.0, (0, 0, 0, 0)


def _vehicle_fields(result: dict):
    """Vehicle geometry, labels and MMC scores from either Stream schema."""
    vehicle = result.get("vehicle")
    if not isinstance(vehicle, dict):
        return (None, 0.0, None, None, None, None, None, None, None)

    bbox = _bbox(vehicle["box"]) if isinstance(vehicle.get("box"), dict) else None
    score = _clamp_unit(vehicle.get("score"))
    vtype = vehicle.get("type") or None

    props = vehicle.get("props")
    if isinstance(props, dict):                     # detection_mode = vehicle
        mm = _top_scored(
            props.get("make_model"), "make", "model"
        ) or (None, None, None)
        colour = _top_scored(
            props.get("color"), "value"
        ) or (None, None)
        return (
            bbox, score, vtype, mm[0], mm[1], colour[0],
            mm[2], mm[2], colour[1],
        )

    # detection_mode = plate keeps MMC at the top level under other names.
    make, model, colour, pair_score, colour_score = _best_mmc(result)
    return (
        bbox, score, vtype, make, model, colour,
        pair_score, pair_score, colour_score,
    )


def _jurisdiction(result: dict) -> str | None:
    """Region code, e.g. 'us-ky'. Diagnostics only -- NEVER hash input."""
    plate = result.get("plate")
    if isinstance(plate, dict):
        code = _top((plate.get("props") or {}).get("region"), "value")
        return None if code in (None, "unknown") else str(code)
    region = result.get("region")
    if isinstance(region, dict):
        return region.get("code") or None
    return None


def _read_from(
    result: dict,
    *,
    captured_at_iso: str | None = None,
    source_observation_id: str | None = None,
) -> RawPlateRead | None:
    """One result -> a plate read, or None when it carries no plate."""
    text, det, ocr, bbox = _plate_fields(result)
    if not text:
        return None
    (
        vbox, vscore, _vtype, make, model, colour,
        make_confidence, model_confidence, color_confidence,
    ) = _vehicle_fields(result)
    return RawPlateRead(
        text=text,
        detection_confidence=det,
        ocr_confidence=ocr,
        bbox=bbox,
        vehicle_bbox=vbox,
        vehicle_confidence=vscore if vbox is not None else None,
        jurisdiction=_jurisdiction(result),
        make=make, model=model, color=colour,
        make_confidence=make_confidence,
        model_confidence=model_confidence,
        color_confidence=color_confidence,
        captured_at_iso=captured_at_iso,
        source_observation_id=source_observation_id,
    )


def _vehicle_from(
    result: dict,
    diagnostic_image_jpeg: bytes | None = None,
) -> "StreamVehicle | None":
    """One result -> a countable vehicle, or None when no vehicle was seen."""
    (
        vbox, vscore, vtype, make, model, colour,
        make_confidence, model_confidence, color_confidence,
    ) = _vehicle_fields(result)
    if vbox is None and vtype is None:
        return None
    return StreamVehicle(
        vehicle_bbox=vbox,
        vehicle_confidence=vscore,
        vehicle_type=vtype,
        make=make, model=model, color=colour,
        make_confidence=make_confidence,
        model_confidence=model_confidence,
        color_confidence=color_confidence,
        diagnostic_image_jpeg=diagnostic_image_jpeg,
        diagnostic_image_bbox=(vbox if diagnostic_image_jpeg is not None else None),
    )


def vehicle_from_read(read: RawPlateRead) -> StreamVehicle | None:
    """Box + MMC from a plated read, for encounter tracking.

    A gate-clearing plate still has a vehicle. We do not count it anonymously,
    but we DO keep its box and make/model/colour so later weak frames of the
    same truck can join this encounter instead of opening a new /visit.
    """
    if (read.vehicle_bbox is None
            and not (read.make or read.model or read.color)):
        return None
    return StreamVehicle(
        vehicle_bbox=read.vehicle_bbox,
        vehicle_confidence=float(read.vehicle_confidence or 0.0),
        make=read.make,
        model=read.model,
        color=read.color,
        make_confidence=getattr(read, "make_confidence", None),
        model_confidence=getattr(read, "model_confidence", None),
        color_confidence=getattr(read, "color_confidence", None),
    )


def _norm_attr(value: str | None) -> str | None:
    if not value or not isinstance(value, str):
        return None
    stripped = value.strip().lower()
    return stripped or None


def _type_family(vehicle_type: str | None) -> str | None:
    """Collapse Stream's 'Pickup Truck' / 'Big Truck' into one family."""
    t = _norm_attr(vehicle_type)
    if not t:
        return None
    if "pickup" in t or "truck" in t:
        return "truck"
    if "suv" in t:
        return "suv"
    if "sedan" in t or "car" in t:
        return "sedan"
    return t


def mmc_same_vehicle(a: StreamVehicle, b: StreamVehicle) -> bool:
    """True when make/model/colour say these are the same physical car.

    MMC is descriptive only — never hash input. Two or more of make, model,
    colour must agree, and none of those three may conflict. One attribute
    is too weak (the lane is full of white cars).
    """
    agrees = 0
    for attr in ("make", "model", "color"):
        left = _norm_attr(getattr(a, attr, None))
        right = _norm_attr(getattr(b, attr, None))
        if not left or not right:
            continue
        if left == right:
            agrees += 1
        else:
            return False
    return agrees >= 2


def classify_results(payload: dict, det_min: float, ocr_min: float):
    """Split one webhook into plate reads and countable vehicles.

    Three cases, and the third is the one that is easy to get wrong:

      plate clears the gates  -> identity candidate; classify_results does
                                 NOT return a StreamVehicle. The handler
                                 still records the box + MMC as an identified
                                 encounter so later weak frames of this car
                                 cannot open a separate anonymous /visit.
      no plate at all         -> a vehicle we can count but never identify
      plate BELOW the gates   -> BOTH. The read is still returned so it is
                                 logged as a recognition diagnostic, AND the
                                 vehicle is counted -- a car whose plate was
                                 too blurry to trust is still a real car that
                                 drove through. Returning only the read would
                                 silently drop it from traffic counts, which
                                 is exactly the blind spot vehicle mode was
                                 turned on to close. The encounter layer then
                                 drops that count if this box / MMC already
                                 had a gate-clearing plate (ROG260, 18 Aug).
    """
    reads: list[RawPlateRead] = []
    vehicles: list[StreamVehicle] = []
    observed_at = _payload_timestamp(payload)
    observation_id = _source_observation_id(payload)
    for result in _results(payload):
        read = _read_from(
            result,
            captured_at_iso=observed_at,
            source_observation_id=observation_id,
        )
        if read is not None:
            reads.append(read)
            if (read.detection_confidence >= det_min
                    and read.ocr_confidence >= ocr_min):
                continue                     # identity path owns this car
        veh = _vehicle_from(result)
        if veh is not None:
            vehicles.append(veh)
    return reads, vehicles


def _results(payload: dict) -> list[dict]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    results = data.get("results")
    if not isinstance(results, list):
        return []
    return [r for r in results if isinstance(r, dict)]


def _hook_event(payload: dict) -> str:
    hook = payload.get("hook")
    if not isinstance(hook, dict):
        return "recognition"
    value = hook.get("event")
    return str(value or "recognition").strip().lower()


def _payload_timestamp(payload: dict) -> str | None:
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    value = data.get("timestamp")
    return str(value) if value else None


def _payload_camera(payload: dict) -> str | None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    hook = payload.get("hook") if isinstance(payload.get("hook"), dict) else {}
    value = data.get("camera_id") or hook.get("id")
    return str(value) if value else None


def _recognition_fingerprint(payload: dict) -> str:
    """Stable local identity for one exact provider recognition event.

    Stream does not expose a documented event UUID. Hash the canonical body so
    an HTTP retry receives the same identity while two genuine observations at
    different provider timestamps remain independent. Only the digest is kept;
    raw plate text remains inside the existing loopback process.
    """
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_observation_id(payload: dict) -> str:
    """Stable identity for one provider-timestamped source observation."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    hook = payload.get("hook") if isinstance(payload.get("hook"), dict) else {}
    timestamp = data.get("timestamp")
    if not timestamp:
        # Malformed/legacy events have no better observation boundary. Exact
        # body identity remains conservative: it never combines two payloads.
        return _recognition_fingerprint(payload)
    material = {
        "event": hook.get("event") or "recognition",
        "camera": data.get("camera_id") or hook.get("id"),
        "timestamp": timestamp,
        "filename": hook.get("filename"),
    }
    encoded = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _iso_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        cleaned = str(value).strip().replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _dwell_fingerprint(track: StreamDwellTrack) -> str:
    """Privacy-safe stable label for one provider track.

    Raw plate text is deliberately excluded. Provider timestamps plus the
    ordered vehicle geometry are enough to recognize a retried webhook in
    logs without creating another identity surface.
    """
    material = {
        "first": track.first_seen_at,
        "last": track.last_seen_at,
        "dwell": round(track.dwell_seconds, 3),
        "boxes": [sample.vehicle_bbox for sample in track.samples],
    }
    encoded = json.dumps(
        material, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _trajectory_vehicle(sample: dict) -> StreamVehicle | None:
    """Parse the vehicle portion of a dwell trajectory sample.

    Stream's dwell schema uses `dscore`; recognition uses `score`. Accept both
    because v1.64 has already differed from the public webhook schema in other
    places and route authority must fail closed, not silently invent a box.
    """
    vehicle = sample.get("vehicle")
    if not isinstance(vehicle, dict):
        return None
    box = _bbox(vehicle.get("box"))
    if box == (0, 0, 0, 0):
        return None
    return StreamVehicle(
        vehicle_bbox=box,
        vehicle_confidence=_clamp_unit(
            vehicle.get("dscore", vehicle.get("score"))
        ),
        vehicle_type=vehicle.get("type") or None,
    )


def parse_vehicle_only(payload: dict) -> list[StreamVehicle]:
    """Vehicles Stream saw but never read a plate for.

    Only produced when Stream runs with `detection_mode = vehicle`. This is
    strictly better evidence than the DetectionEvents episode heuristic the
    cloud currently infers vehicle counts from: it is the recogniser itself
    saying "there was a car here and it had no readable plate", rather than
    us guessing from clusters of failed reads.

    Carries NO plate and therefore no hash, by construction — a plateless
    vehicle must never be written as a plate-identified visit.
    """
    out: list[StreamVehicle] = []
    for result in _results(payload):
        if _read_from(result) is not None:
            continue                    # has a plate; the identify path owns it
        vehicle = _vehicle_from(result)
        if vehicle is not None:
            out.append(vehicle)
    return out


def _loads_dict(raw) -> dict | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def extract_json_field(body: bytes, content_type: str) -> dict | None:
    """Pull the JSON body out of a Stream webhook POST.

    Stream wraps the same JSON document in one of three encodings depending
    on how it is configured, so all three are handled:

      application/x-www-form-urlencoded
          What Stream v1.64 actually sends when `image = no` — a single
          urlencoded `json=` field, with no file parts to justify multipart.
          This is NOT the documented shape; verified by capturing a live
          webhook. Must keep working: if `image` is accidentally flipped
          off, parsing only multipart would silently drop every result.
      multipart/form-data
          What Stream sends when webhook images are on (`json` plus
          `upload`). Production uses this so plate_reader can keep one
          highest-OCR still per vehicle. Parsed via `email.parser` rather
          than `cgi`, which Python 3.13 removed.
      application/json
          Not sent by Stream; accepted so the endpoint can be exercised with
          curl during setup and testing.
    """
    ctype = (content_type or "").lower()

    if ctype.startswith("application/json"):
        return _loads_dict(body)

    if ctype.startswith("application/x-www-form-urlencoded"):
        fields = urllib.parse.parse_qs(
            body.decode("utf-8", errors="replace"), keep_blank_values=False
        )
        values = fields.get("json")
        if not values:
            return None
        return _loads_dict(values[0])

    if ctype.startswith("multipart/"):
        message = BytesParser(policy=_default_policy).parsebytes(
            b"Content-Type: " + (content_type or "").encode("utf-8", "replace")
            + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
        )
        for part in message.iter_parts():
            disposition = part.get("content-disposition", "")
            if 'name="json"' in disposition or "name=json" in disposition:
                return _loads_dict(part.get_payload(decode=True) or b"")
        return None

    return None


_IMAGE_PART_NAMES = ("original", "upload", "image", "vehicle", "screenshot", "camera")


def _iter_multipart_parts(body: bytes, content_type: str):
    """Split a multipart body into (headers, payload) without decoding.

    The stdlib email parser treats unspecified Content-Transfer-Encoding as
    7bit and can mangle JPEG payloads. Stream sends raw binary; split on the
    boundary ourselves so the bytes stay intact.
    """
    match = re.search(
        r'boundary=(?:"([^"]+)"|([^;]+))', content_type or "", re.I
    )
    if not match:
        return
    boundary = (match.group(1) or match.group(2)).strip()
    sep = b"--" + boundary.encode("latin-1", "replace")
    for raw in body.split(sep)[1:]:
        if raw.startswith(b"--"):
            continue
        if raw.startswith(b"\r\n"):
            raw = raw[2:]
        elif raw.startswith(b"\n"):
            raw = raw[1:]
        split = raw.split(b"\r\n\r\n", 1)
        if len(split) != 2:
            split = raw.split(b"\n\n", 1)
        if len(split) != 2:
            continue
        headers, payload = split
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        elif payload.endswith(b"\n"):
            payload = payload[:-1]
        yield headers.decode("latin-1", "replace"), payload


def extract_webhook_image(body: bytes, content_type: str) -> bytes | None:
    """JPEG bytes from a Stream webhook, or None when images are disabled.

    Prefer Stream's explicitly requested ``original`` frame over vehicle
    crops or legacy ``upload``/``image`` names. Loopback only — this function
    never writes or transmits the bytes.
    """
    ctype = (content_type or "").lower()
    if not ctype.startswith("multipart/"):
        return None
    parts = list(_iter_multipart_parts(body, content_type))
    for name in _IMAGE_PART_NAMES:
        for headers, payload in parts:
            lowered = headers.lower()
            if f'name="{name}"' in lowered or f"name={name}" in lowered:
                return payload or None
    return None


def _decode_jpeg(data: bytes):
    """numpy BGR image, or None."""
    if not data:
        return None
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return img
    except Exception:
        return None


class _Handler(BaseHTTPRequestHandler):
    # Set by the engine that owns this server.
    engine = None

    def do_POST(self):                                   # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self.send_error(413 if length else 400)
            return

        body = self.rfile.read(length)
        payload = extract_json_field(body, self.headers.get("Content-Type", ""))
        if payload is None:
            self.send_error(400)
            return

        if self.engine is not None:
            event = _hook_event(payload)
            if event == "dwell_time":
                # Native Stream trajectories are the completed physical track
                # used to decide whether this vehicle actually followed the
                # drive-through path. The dwell hook contains raw plate text,
                # so it is handled only inside this loopback process.
                self.engine.submit_dwell_payload(payload)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                return
            if event != "recognition":
                log.debug("ignoring Stream webhook event=%s", event)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                return

            jpeg = extract_webhook_image(
                body, self.headers.get("Content-Type", "")
            )
            frame = _decode_jpeg(jpeg) if jpeg else None
            self.engine.submit_recognition_payload(
                payload, jpeg=jpeg, frame=frame
            )

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_GET(self):                                    # noqa: N802
        """Liveness probe, so setup scripts can verify the listener is up."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true,"engine":"pr_stream"}')

    def log_message(self, fmt, *args):
        # Never let the default handler write request lines to stderr: a
        # webhook URL can contain a plate and this would put it in the journal.
        log.debug("stream webhook: %s", fmt % args)


class StreamWebhookEngine:
    """Receives Plate Recognizer Stream webhooks and serves them as reads."""

    name = "pr_stream"

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        stream_cfg = getattr(cfg, "stream", None)
        self._host = str(getattr(stream_cfg, "webhook_host", "127.0.0.1"))
        self._port = int(getattr(stream_cfg, "webhook_port", 8129))
        self._pending: list[RawPlateRead] = []
        # Exact webhook retries must not count as independent OCR evidence.
        # This is intentionally process-local: persisting only a receipt bit
        # while encounter/vote state remains in RAM creates an unrecoverable
        # loss window after a crash. A durable inbox is a separate transaction
        # design, not a safe one-table patch.
        self._recognition_receipts: dict[str, float] = {}
        self._recognition_receipt_lock = threading.Lock()
        # Real-time vehicle observations for the menu-board tracker. This is
        # distinct from drain_vehicles(): observations describe a still-live
        # encounter, while drain_vehicles emits one terminal unidentified
        # traffic outcome after the encounter closes.
        self._vehicle_observations: list[StreamVehicle] = []
        # Route-rejected profiles are not traffic, but one bounded terminal
        # record remains locally reviewable during the route canary. This
        # queue carries no plate text and is drained by the main loop.
        self._route_diagnostics: list[StreamVehicle] = []
        self._route_diagnostic_retry_after: dict[str, float] = {}
        # Identity is an ATTRIBUTE of the vehicle encounter, not a parallel
        # per-frame event stream. PR sometimes emits a short fragment first
        # and a complete plate 0.2--1.6s later. The detector's terminal guard
        # correctly permits only one hash, but without this settlement the
        # fragment wins merely because it arrived first.
        alpr = getattr(cfg, "alpr", None)
        alpr_th = getattr(alpr, "thresholds", None)
        self._identity_settle_quiet_seconds = float(getattr(
            alpr_th, "identity_settle_quiet_seconds", 1.0
        ))
        self._identity_settle_max_seconds = float(getattr(
            alpr_th, "identity_settle_max_seconds", 3.0
        ))
        self._identity_stages: dict[str, dict] = {}
        # Open vehicle encounters, coalesced; see drain_vehicles.
        self._encounters: list[dict] = []
        self._last_plate_read_at = 0.0
        # How long a vehicle must be gone before its encounter is emitted.
        # 12s, not 3s: Stream's webhook gaps on a dwelling car were 5–8s
        # (ROG260, 18 Aug), and 3s split one truck into four /visit rows.
        # A following car after 12s of silence is a new encounter.
        self._vehicle_quiet_seconds = float(getattr(
            stream_cfg, "vehicle_quiet_seconds", 12.0))
        # Keep a quiet, unresolved encounter locally long enough for a late
        # clean read to resolve it. The cloud Visits key cannot safely mutate
        # an anonymous vehicle into a hashed one, so the terminal record is
        # held here rather than emitting a premature anonymous Visit and a
        # second recognized Visit 30–90 seconds later.
        self._unidentified_resolution_seconds = max(
            self._vehicle_quiet_seconds,
            float(getattr(
                stream_cfg, "unidentified_resolution_seconds", 90.0
            )),
        )
        # After a gate-clearing plate, keep the encounter so later plateless
        # frames of the same car (dwell, Stream gaps, a plate-reader restart
        # is a separate problem) are not emitted as anonymous visits. 5 min
        # covers a slow order. Rejoin after the 12s quiet is geometry only;
        # descriptive MMC never owns a physical profile.
        self._identified_hold_seconds = float(getattr(
            stream_cfg, "identified_hold_seconds", 300.0))
        presence = getattr(cfg, "presence", None)
        route = getattr(presence, "route_qualification", None)
        self._route_enabled = bool(getattr(route, "enabled", False))
        self._route_shadow_only = bool(getattr(route, "shadow_only", True))
        self._profile_reconciliation_enabled = bool(getattr(
            route, "profile_reconciliation_enabled", False
        ))
        self._route_entry_zone = getattr(route, "entry_zone", None)
        self._route_min_trajectory_points = int(getattr(
            route, "min_trajectory_points", 2
        ))
        self._route_min_trajectory_duration_seconds = float(getattr(
            route, "min_trajectory_duration_seconds", 0.0
        ))
        self._route_zone_confirm_points = int(getattr(
            route, "zone_confirm_points", 1
        ))
        self._route_min_leftward_progress = float(getattr(
            route, "min_leftward_progress", 0.0
        ))
        self._route_min_upward_progress = float(getattr(
            route, "min_upward_progress", 0.0
        ))
        self._route_board_dwell_fallback_min_points = int(getattr(
            route, "board_dwell_fallback_min_points", 0
        ))
        self._route_board_dwell_fallback_min_board_points = int(getattr(
            route, "board_dwell_fallback_min_board_points", 0
        ))
        self._route_board_dwell_fallback_min_seconds = float(getattr(
            route, "board_dwell_fallback_min_seconds", 0.0
        ))
        self._route_progressive_board_rescue_min_points = int(getattr(
            route, "progressive_board_rescue_min_points", 0
        ))
        self._route_progressive_board_rescue_min_board_points = int(getattr(
            route, "progressive_board_rescue_min_board_points", 0
        ))
        self._route_progressive_board_rescue_min_seconds = float(getattr(
            route, "progressive_board_rescue_min_seconds", 0.0
        ))
        self._route_terminal_exit_zone = getattr(
            route, "terminal_exit_zone", None
        )
        board_resolver = getattr(presence, "resolved_board_zone", None)
        self._route_board_zone = (
            board_resolver() if callable(board_resolver)
            else getattr(presence, "board_zone", None)
        )
        self._route_decision_timeout_seconds = float(getattr(
            route, "decision_timeout_seconds", 120.0
        ))
        self._route_rejection_grace_seconds = float(getattr(
            route, "rejection_grace_seconds", 10.0
        ))
        self._unmatched_dwell_cache_seconds = float(getattr(
            route, "unmatched_dwell_cache_seconds", 30.0
        ))
        self._terminal_tombstone_seconds = float(getattr(
            route, "terminal_tombstone_seconds", 300.0
        ))
        capture = getattr(cfg, "capture", None)
        camera = getattr(cfg, "camera", None)
        self._frame_width = int(getattr(
            capture, "width", getattr(camera, "resolution_width", 1456)
        ))
        self._frame_height = int(getattr(
            capture, "height", getattr(camera, "resolution_height", 1088)
        ))
        # This is a reviewed LJS-only exception, not a new fleet default or
        # a relaxation of the ordinary route/identity floors. A changed site,
        # zone, or shorter grace period must be reviewed separately.
        self._short_plated_route_enabled = bool(
            getattr(cfg, "location_id", None) == "ljs-louisville-001"
            and self._route_enabled and not self._route_shadow_only
            and self._route_entry_zone == (0.52, 0.26, 1.0, 1.0)
            and self._route_board_zone == (0.0, 0.0, 0.72, 0.25)
            and self._route_terminal_exit_zone == (0.72, 0.0, 1.0, 0.28)
            and self._route_min_trajectory_points == 8
            and self._route_rejection_grace_seconds >= 60.0
        )
        # A completed dwell trajectory can prove that two sparse recognition
        # encounters were one vehicle. Keep aliases so an already-deferred
        # detector outcome follows the merged canonical profile.
        self._encounter_aliases: dict[str, str] = {}
        # Anonymous traffic may emit before all delayed recognition work is
        # exhausted. Remember that one terminal Visit independently of the
        # live profile so a very late detector outcome cannot create a second
        # Visit after cleanup.
        # Values retain the canonical owner for audit-case coalescing.
        self._terminal_visit_owners: dict[str, tuple[str, float]] = {}
        self._terminal_visit_owner_ttl_seconds = max(
            600.0,
            self._route_decision_timeout_seconds * 2.0,
            self._terminal_tombstone_seconds,
        )
        # Dwell and recognition webhooks can arrive out of order. Cache only
        # qualified unmatched tracks, on-device and bounded by a short TTL.
        self._pending_dwell_tracks: dict[str, tuple[StreamDwellTrack, float]] = {}
        # Emitted qualified unidentified profiles retain a metadata-only
        # tombstone so a late board-area departure cannot become a second car.
        self._terminal_tombstones: list[dict] = []
        self._audit_frames = AuditFrameCache()
        # Effective per-frame gates, so the engine can tell an identity
        # candidate from a car it can only count. These are the same values
        # detector.py admits on, after alpr.thresholds.app_* overrides.
        th = getattr(cfg, "thresholds", None)
        self.det_min = float(getattr(th, "min_detection_confidence", 0.0))
        self.ocr_min = float(getattr(th, "min_ocr_confidence", 0.0))
        self._lock = threading.Lock()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.last_call_ok = True

    # ---------- PlateEngine contract ----------

    def open(self) -> None:
        if self._host not in ("127.0.0.1", "localhost", "::1"):
            # Raw plate text crosses this socket. Binding it to a routable
            # interface would put unhashed plates on the network, which
            # SECURITY.md forbids outright.
            raise ValueError(
                f"stream.webhook_host must be loopback, got {self._host!r}"
            )
        handler = type("_BoundHandler", (_Handler,), {"engine": self})
        self._server = HTTPServer((self._host, self._port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="stream-webhook",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "Plate Recognizer Stream webhook listening on http://%s:%d/",
            self._host, self._port,
        )
        if self._route_enabled:
            log.info(
                "vehicle route qualification ON shadow=%s reconcile=%s "
                "entry=%s board=%s min_points=%d min_seconds=%.1f "
                "confirm_points=%d min_progress=(%.2f,%.2f) "
                "board_fallback=(%d total,%d board,%.1fs) exit=%s "
                "progress_rescue=(%d total,%d board,%.1fs) "
                "rejection_grace=%.1fs",
                self._route_shadow_only,
                self._profile_reconciliation_enabled,
                self._route_entry_zone,
                self._route_board_zone,
                self._route_min_trajectory_points,
                self._route_min_trajectory_duration_seconds,
                self._route_zone_confirm_points,
                self._route_min_leftward_progress,
                self._route_min_upward_progress,
                self._route_board_dwell_fallback_min_points,
                self._route_board_dwell_fallback_min_board_points,
                self._route_board_dwell_fallback_min_seconds,
                self._route_terminal_exit_zone,
                self._route_progressive_board_rescue_min_points,
                self._route_progressive_board_rescue_min_board_points,
                self._route_progressive_board_rescue_min_seconds,
                self._route_rejection_grace_seconds,
            )

    def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3.0)
        with self._lock:
            self._audit_frames.frames.clear()

    def infer(self, frame, now: float | None = None) -> list[RawPlateRead]:
        """Return everything received since the last call.

        `frame` is ignored — Stream already did the capture and inference.
        The parameter stays for protocol compatibility so detector.py needs
        no special case. Encounter-bound reads are released only after a
        short quiet period, or the hard maximum wait, whichever comes first.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            for encounter_id, stage in list(self._identity_stages.items()):
                quiet = now - stage["last_seen"]
                age = now - stage["first_seen"]
                if (
                    quiet < self._identity_settle_quiet_seconds
                    and age < self._identity_settle_max_seconds
                ):
                    continue
                self._pending.extend(
                    self._select_encounter_reads(stage["reads"])
                )
                self._identity_stages.pop(encounter_id, None)
            if not self._pending:
                return []
            batch, self._pending = self._pending, []
        return batch

    def _claim_recognition(
        self, fingerprint: str, now: float
    ) -> bool:
        """Claim one exact webhook body, pruning the bounded retry cache."""
        with self._recognition_receipt_lock:
            stale = [
                key for key, seen_at in self._recognition_receipts.items()
                if now - seen_at > RECOGNITION_RECEIPT_TTL_SECONDS
            ]
            for key in stale:
                self._recognition_receipts.pop(key, None)
            if fingerprint in self._recognition_receipts:
                return False
            self._recognition_receipts[fingerprint] = now
            while len(self._recognition_receipts) > MAX_RECOGNITION_RECEIPTS:
                oldest = min(
                    self._recognition_receipts,
                    key=self._recognition_receipts.get,
                )
                self._recognition_receipts.pop(oldest, None)
            return True

    def submit_recognition_payload(
        self,
        payload: dict,
        *,
        jpeg: bytes | None = None,
        frame=None,
        now: float | None = None,
    ) -> bool:
        """Ingest one recognition webhook exactly once in this process.

        Returns True when newly ingested and False for an exact retry. Results
        with an exactly shared associated-vehicle box are Plate Recognizer's
        multi-plate representation of one vehicle and therefore share one
        encounter. Distinct boxes in the same provider event remain distinct
        simultaneous vehicles.
        """
        # Internal encounter/settlement lifetimes use the monotonic clock
        # everywhere in this class. Provider wall time remains separate in
        # observed_at / RawPlateRead.captured_at_iso for ordering and output.
        monotonic_now = time.monotonic() if now is None else float(now)
        fingerprint = _recognition_fingerprint(payload)
        if not self._claim_recognition(fingerprint, monotonic_now):
            log.info(
                "duplicate Stream recognition webhook suppressed: %s",
                fingerprint[:12],
            )
            return False

        try:
            observed_at = _payload_timestamp(payload)
            # The extractor supports crop-shaped legacy image parts too.
            # Only share originals whose already-decoded dimensions match
            # the full capture; do not spend another decode on audit work.
            if (self._route_enabled and jpeg and frame is not None
                    and tuple(getattr(frame, "shape", ())[:2])
                    == (self._frame_height, self._frame_width)):
                with self._lock:
                    self._audit_frames.add(
                        _payload_camera(payload), observed_at, jpeg, monotonic_now
                    )
            observation_id = _source_observation_id(payload)
            reads: list[RawPlateRead] = []
            unattached_reads: list[RawPlateRead] = []
            grouped: dict[tuple, dict] = {}

            # Correlate at the source, while each plate and its associated
            # vehicle still live in the same Stream result. source_batch_id
            # keeps distinct boxes simultaneous; the exact-box map handles
            # Stream's plates_per_vehicle>1 representation.
            for result_index, result in enumerate(_results(payload)):
                read = _read_from(
                    result,
                    captured_at_iso=observed_at,
                    source_observation_id=observation_id,
                )
                vehicle = _vehicle_from(
                    result, diagnostic_image_jpeg=jpeg
                )
                if vehicle is None:
                    if read is not None:
                        unattached_reads.append(read)
                    continue
                # A missing vehicle box cannot prove two result rows refer to
                # one vehicle, so keep such rows distinct. Exact non-empty box
                # equality is intentionally stricter than ordinary overlap.
                group_key = (
                    ("box", vehicle.vehicle_bbox)
                    if vehicle.vehicle_bbox is not None
                    else ("result", result_index)
                )
                group = grouped.get(group_key)
                if group is None:
                    grouped[group_key] = {
                        "vehicle": vehicle,
                        "reads": [read] if read is not None else [],
                    }
                else:
                    group["vehicle"] = self._merge_vehicle(
                        group["vehicle"], vehicle
                    )
                    if read is not None:
                        group["reads"].append(read)

            for group in grouped.values():
                group_reads: list[RawPlateRead] = group["reads"]
                strongest = max(
                    group_reads,
                    key=lambda item: (
                        min(item.detection_confidence, item.ocr_confidence),
                        item.ocr_confidence,
                        item.detection_confidence,
                    ),
                    default=None,
                )
                candidate_key = (
                    self.normalize_candidate(strongest.text)
                    if strongest is not None else None
                )
                encounter_id = self.observe_vehicle(
                    group["vehicle"],
                    now=monotonic_now,
                    saw_plate=bool(group_reads),
                    plated_attempt=bool(group_reads),
                    candidate_key=candidate_key,
                    # Keep a real same-result confidence pair; independent
                    # maxima could describe evidence that never existed.
                    plate_detection_confidence=(
                        strongest.detection_confidence
                        if strongest is not None else None
                    ),
                    plate_ocr_confidence=(
                        strongest.ocr_confidence
                        if strongest is not None else None
                    ),
                    observed_at=observed_at,
                    source_batch_id=fingerprint,
                )
                # Retain every alternate key for a later keyed dwell hook, but
                # never treat those alternatives as independent observations.
                if group_reads:
                    with self._lock:
                        encounter = self._encounter_locked(encounter_id)
                        if encounter is not None:
                            encounter.setdefault("candidate_keys", set()).update(
                                key for item in group_reads
                                if (key := self.normalize_candidate(item.text))
                            )
                reads.extend(
                    replace(
                        item,
                        encounter_id=encounter_id,
                        image=frame if frame is not None else item.image,
                    )
                    for item in group_reads
                )

            reads.extend(
                replace(
                    item,
                    image=frame if frame is not None else item.image,
                )
                for item in unattached_reads
            )
            if reads:
                self.submit(reads, now=monotonic_now)
            return True
        except Exception:
            # Permit Stream to retry after a failed partial parse/ingest. The
            # HTTP server is single-threaded, so no concurrent request can
            # observe this temporary claim.
            with self._recognition_receipt_lock:
                self._recognition_receipts.pop(fingerprint, None)
            raise

    def drain_vehicles(self, now: float | None = None) -> list[StreamVehicle]:
        """Vehicles whose encounter has ended, one per unidentified vehicle.

        Separate from infer() because these must never enter the identify
        pipeline — they have no plate to normalize or hash.

        COALESCING IS NOT OPTIONAL HERE. Stream analyzes sampled frames but
        emits sparse, irregular prediction events; a plateless vehicle has no
        string with which to join those events. Detections are merged into one
        encounter while the car is present using geometry and native
        trajectory evidence, then route-gated sites wait for Stream's
        completed physical path before one terminal outcome can be emitted.

        A confirmed detector vote on the same encounter marks it identified.
        Emitting that encounter as well would count one physical vehicle
        twice — once as a customer and once as an anonymous car. That is
        what happened to ROG260 on 18 Aug (1 confirm + 4 VEHICLE_ONLY).
        """
        now = time.monotonic() if now is None else now
        out: list[StreamVehicle] = []
        with self._lock:
            self._audit_frames.prune(now)
            self._prune_terminal_tombstones_locked(now)
            self._prune_terminal_visit_owners_locked(time.monotonic())
            self._retry_pending_dwell_locked(now)

            def terminal_vehicle(enc: dict) -> StreamVehicle:
                shadow_status = enc.get("route_shadow_status")
                use_shadow = self._route_shadow_only and shadow_status
                route_status = (
                    shadow_status if use_shadow else enc.get("route_status")
                )
                route_reason = (
                    enc.get("route_shadow_reason") if use_shadow
                    else enc.get("route_reason")
                )
                return replace(
                    enc["vehicle"],
                    encounter_id=enc["encounter_id"],
                    first_seen_at=enc["first_seen_at"],
                    last_seen_at=enc["last_seen_at"],
                    saw_plate=bool(enc.get("saw_plate")),
                    diagnostic_episode_id=enc.get("diagnostic_episode_id"),
                    plate_detection_confidence=enc.get(
                        "plate_detection_confidence"
                    ),
                    plate_ocr_confidence=enc.get("plate_ocr_confidence"),
                    route_status=route_status,
                    route_reason=route_reason,
                    traffic_classification=(
                        route_diagnostic_classification(route_reason)
                        if route_status == "rejected" else None
                    ),
                    route_track_fingerprint=enc.get(
                        "route_track_fingerprint"
                    ),
                    trajectory_points=int(
                        enc.get("trajectory_points", 0)
                    ),
                    dwell_seconds=enc.get("dwell_seconds"),
                    route_trajectory_bboxes=tuple(
                        enc.get("route_trajectory_bboxes", ())
                    ),
                    route_trajectory_timestamps=tuple(
                        enc.get("route_trajectory_timestamps", ())
                    ),
                    route_entry_zone=self._route_entry_zone,
                    route_board_zone=self._route_board_zone,
                    route_terminal_exit_zone=(
                        self._route_terminal_exit_zone
                    ),
                )

            def queue_route_diagnostic(enc: dict) -> None:
                if enc.get("route_diagnostic_emitted"):
                    return
                vehicle = terminal_vehicle(enc)
                if vehicle.route_status != "rejected":
                    return
                self._route_diagnostics.append(vehicle)
                if len(self._route_diagnostics) > 64:
                    dropped = self._route_diagnostics[:-64]
                    del self._route_diagnostics[:-64]
                    for item in dropped:
                        self._route_diagnostic_retry_after.pop(
                            item.encounter_id or "", None
                        )
                    log.warning(
                        "route diagnostic queue full; dropped %d oldest "
                        "in-process case(s)",
                        len(dropped),
                    )
                enc["route_diagnostic_emitted"] = True

            still_open = []
            for enc in self._encounters:
                self._maybe_qualify_short_plated_route_locked(enc, now)
                if (
                    self._route_enabled
                    and not self._route_shadow_only
                    and enc.get("identified")
                ):
                    raw_route_status = enc.get("route_status", "pending")
                    route_status = raw_route_status
                    if (
                        route_status == "qualified"
                        and not enc.get("route_complete")
                    ):
                        route_status = "pending"
                    if route_status == "rejected":
                        if not self._route_rejection_final_locked(enc, now):
                            still_open.append(enc)
                        else:
                            queue_route_diagnostic(enc)
                        continue
                    if (
                        route_status == "pending"
                        and (now - enc["last_seen"])
                        >= self._route_decision_timeout_seconds
                    ):
                        enc["route_status"] = "rejected"
                        enc["route_reason"] = (
                            "profile_without_completed_route"
                            if raw_route_status == "qualified"
                            else "decision_timeout"
                        )
                        queue_route_diagnostic(enc)
                        continue
                if (
                    self._route_enabled
                    and not self._route_shadow_only
                    and not enc.get("identified")
                ):
                    route_status = enc.get("route_status", "pending")
                    if route_status == "qualified":
                        # A completed route is physical traffic proof, not proof
                        # that identity processing has finished. Recognition and
                        # dwell hooks are independent HTTP requests and have been
                        # observed arriving in the opposite order. Keep the live
                        # profile available for the ordinary late-resolution
                        # window so a board plate (or a deferred detector miss)
                        # cannot become a second vehicle 100 ms later.
                        if (now - enc["last_seen"]) < \
                                self._unidentified_resolution_seconds:
                            still_open.append(enc)
                            continue
                        if not enc.get("route_complete"):
                            # Mixed-version/stale state must not preserve the
                            # retired live board qualification. Plated and
                            # anonymous profiles both require a completed
                            # physical route before becoming a Visit.
                            log.warning(
                                "VEHICLE_ROUTE encounter=%s status=rejected "
                                "reason=profile_without_completed_route",
                                enc["encounter_id"][:8],
                            )
                            enc["route_status"] = "rejected"
                            enc["route_reason"] = \
                                "profile_without_completed_route"
                            queue_route_diagnostic(enc)
                            continue
                        out.append(terminal_vehicle(enc))
                        self._remember_terminal_locked(enc, now)
                        self._remember_terminal_visit_locked(
                            enc, time.monotonic()
                        )
                        continue
                    if route_status == "rejected":
                        # Adjacent-road traffic and entry-lane drive-offs are
                        # diagnostics, not restaurant traffic. Give a split
                        # provider trajectory a short chance to upgrade this
                        # profile to qualified before final suppression.
                        # A below-threshold plate miss is persisted by
                        # Detector after its own quiet flush. Keep that profile
                        # long enough for note_detection_event() to attach the
                        # episode, so route diagnostics enrich the existing
                        # case rather than racing it and writing a duplicate.
                        awaiting_miss_case = bool(
                            enc.get("saw_plate")
                            and not enc.get("diagnostic_episode_id")
                            and (now - enc["last_seen"])
                            < self._unidentified_resolution_seconds
                        )
                        if (
                            awaiting_miss_case
                            or not self._route_rejection_final_locked(enc, now)
                        ):
                            still_open.append(enc)
                        else:
                            queue_route_diagnostic(enc)
                        continue
                    if (now - enc["last_seen"]) >= \
                            self._route_decision_timeout_seconds:
                        log.warning(
                            "VEHICLE_ROUTE encounter=%s status=rejected "
                            "reason=decision_timeout",
                            enc["encounter_id"][:8],
                        )
                        enc["route_status"] = "rejected"
                        enc["route_reason"] = "decision_timeout"
                        queue_route_diagnostic(enc)
                        continue
                    still_open.append(enc)
                    continue

                if (
                    self._route_enabled
                    and self._route_shadow_only
                    and not enc.get("identified")
                ):
                    age = now - enc["last_seen"]
                    if age < self._unidentified_resolution_seconds:
                        still_open.append(enc)
                        continue
                    # Shadow classification may not create, suppress, or
                    # delay traffic. Emit the legacy anonymous Visit at its
                    # ordinary 90-second boundary, then retain only the
                    # in-process profile until late trajectory evidence can
                    # label it. The emitted flag prevents a second Visit.
                    if not enc.get("route_shadow_visit_emitted"):
                        if enc["last_seen"] <= self._last_plate_read_at:
                            continue
                        out.append(terminal_vehicle(enc))
                        enc["route_shadow_visit_emitted"] = True
                        self._remember_terminal_visit_locked(
                            enc, time.monotonic()
                        )
                    shadow_status = enc.get("route_shadow_status", "pending")
                    if shadow_status == "pending":
                        if age < self._route_decision_timeout_seconds:
                            still_open.append(enc)
                            continue
                        enc["route_shadow_status"] = "rejected"
                        enc["route_shadow_reason"] = "decision_timeout"
                        enc["route_shadow_complete"] = True
                        shadow_status = "rejected"
                    if shadow_status == "rejected":
                        queue_route_diagnostic(enc)
                    continue

                if (
                    self._route_enabled
                    and self._route_shadow_only
                    and enc.get("identified")
                    and enc.get("route_shadow_status", "pending") == "pending"
                ):
                    age = now - enc["last_seen"]
                    if age < self._route_decision_timeout_seconds:
                        still_open.append(enc)
                        continue
                    enc["route_shadow_status"] = "rejected"
                    enc["route_shadow_reason"] = "decision_timeout"
                    enc["route_shadow_complete"] = True

                hold = (
                    self._identified_hold_seconds
                    if enc.get("identified")
                    else self._unidentified_resolution_seconds
                )
                if (now - enc["last_seen"]) < hold:
                    still_open.append(enc)
                    continue
                if enc.get("identified"):
                    if (
                        self._route_enabled
                        and self._route_shadow_only
                        and enc.get("route_shadow_status") == "rejected"
                    ):
                        queue_route_diagnostic(enc)
                    continue
                # Legacy: note_plate_read() without a box still suppresses
                # anything last seen at or before that plate.
                if enc["last_seen"] <= self._last_plate_read_at:
                    continue
                vehicle = terminal_vehicle(enc)
                if vehicle.route_status == "rejected":
                    queue_route_diagnostic(enc)
                out.append(vehicle)
                self._remember_terminal_locked(enc, now)
                self._remember_terminal_visit_locked(
                    enc, time.monotonic()
                )
            self._encounters = still_open
            self._prune_encounter_aliases_locked()
        return out

    def drain_vehicle_observations(self) -> list[StreamVehicle]:
        """Drain live vehicle boxes for presence/board-trigger tracking."""
        with self._lock:
            batch, self._vehicle_observations = self._vehicle_observations, []
        return batch

    def drain_route_diagnostics(
        self, now: float | None = None
    ) -> list[StreamVehicle]:
        """Retry due route evidence in-process until persistence is acknowledged."""
        now = time.monotonic() if now is None else now
        with self._lock:
            batch = [
                vehicle for vehicle in self._route_diagnostics
                if self._route_diagnostic_retry_after.get(
                    vehicle.encounter_id or "", 0.0
                ) <= now
            ]
            for vehicle in batch:
                self._route_diagnostic_retry_after[
                    vehicle.encounter_id or ""
                ] = now + 30.0
        return batch

    def ack_route_diagnostic(self, encounter_id: str | None) -> None:
        """Remove local route evidence only after its sidecar is durable."""
        if not encounter_id:
            return
        with self._lock:
            self._route_diagnostics = [
                vehicle for vehicle in self._route_diagnostics
                if vehicle.encounter_id != encounter_id
            ]
            self._route_diagnostic_retry_after.pop(encounter_id, None)

    # ---------- Vehicle profile / physical route authority ----------

    def route_gate_enabled(self) -> bool:
        return self._route_enabled

    def route_gate_shadow_only(self) -> bool:
        return self._route_shadow_only

    def route_decision_timeout_seconds(self) -> float:
        return self._route_decision_timeout_seconds

    def terminal_visit_emitted(self, encounter_id: str | None) -> bool:
        """Whether this encounter already emitted its one traffic Visit."""
        if not encounter_id:
            return False
        with self._lock:
            now = time.monotonic()
            self._prune_terminal_visit_owners_locked(now)
            if encounter_id in self._terminal_visit_owners:
                return True
            enc = self._encounter_locked(encounter_id)
            emitted = bool(
                enc is not None
                and enc.get("route_shadow_visit_emitted")
            )
            if emitted:
                self._remember_terminal_visit_locked(enc, now)
            return emitted

    def terminal_visit_owner(self, encounter_id: str | None) -> str | None:
        """Canonical audit owner for a terminal encounter or alias."""
        if not encounter_id:
            return None
        with self._lock:
            now = time.monotonic()
            self._prune_terminal_visit_owners_locked(now)
            remembered = self._terminal_visit_owners.get(encounter_id)
            if remembered is not None:
                return remembered[0]
            enc = self._encounter_locked(encounter_id)
            if enc is None or not enc.get("route_shadow_visit_emitted"):
                return None
            self._remember_terminal_visit_locked(enc, now)
            return enc["encounter_id"]

    def _prune_terminal_visit_owners_locked(self, now: float) -> None:
        ttl = self._terminal_visit_owner_ttl_seconds
        for owner, (_, emitted_at) in list(
            self._terminal_visit_owners.items()
        ):
            if (now - emitted_at) > ttl:
                self._terminal_visit_owners.pop(owner, None)
        overflow = len(self._terminal_visit_owners) - 4096
        if overflow > 0:
            oldest = sorted(
                self._terminal_visit_owners,
                key=lambda owner: self._terminal_visit_owners[owner][1],
            )[:overflow]
            for owner in oldest:
                self._terminal_visit_owners.pop(owner, None)

    def _remember_terminal_visit_locked(self, enc: dict, now: float) -> None:
        canonical = enc.get("encounter_id")
        if not canonical:
            return
        owners = {canonical}
        for alias in self._encounter_aliases:
            if self._canonical_id_locked(alias) == canonical:
                owners.add(alias)
        # A Visit's encounter ID is immutable once sent. Reconciliation may
        # later choose a different live profile as canonical, but audit and
        # DetectionEvent linkage must continue to use the historical owner
        # that actually appeared in the terminal Visit payload.
        remembered = [
            self._terminal_visit_owners[owner]
            for owner in owners
            if owner in self._terminal_visit_owners
        ]
        if remembered:
            terminal_owner, emitted_at = min(
                remembered, key=lambda item: item[1]
            )
        else:
            terminal_owner, emitted_at = canonical, now
        for owner in owners:
            self._terminal_visit_owners[owner] = (
                terminal_owner, emitted_at
            )
        self._terminal_visit_owners[terminal_owner] = (
            terminal_owner, emitted_at
        )
        self._prune_terminal_visit_owners_locked(now)

    def route_profile_active(
        self,
        encounter_id: str | None,
        now: float | None = None,
    ) -> bool:
        """Whether a profile is still receiving observations inside timeout."""
        now = time.monotonic() if now is None else now
        with self._lock:
            enc = self._encounter_locked(encounter_id)
            return bool(
                enc is not None
                and (now - enc["last_seen"])
                < self._route_decision_timeout_seconds
            )

    def _route_rejection_final_locked(self, enc: dict, now: float) -> bool:
        self._maybe_qualify_short_plated_route_locked(enc, now)
        if enc.get("route_status") != "rejected":
            return False
        anchor = max(
            float(enc.get("route_rejected_at") or 0.0),
            float(enc.get("last_seen") or 0.0),
        )
        return (now - anchor) >= self._route_rejection_grace_seconds

    def route_rejection_final(
        self,
        encounter_id: str | None,
        now: float | None = None,
    ) -> bool:
        """Whether a rejected route survived the split-track grace period."""
        now = time.monotonic() if now is None else now
        with self._lock:
            enc = self._encounter_locked(encounter_id)
            return bool(
                enc is not None
                and self._route_rejection_final_locked(enc, now)
            )

    def _canonical_id_locked(self, encounter_id: str | None) -> str | None:
        current = encounter_id
        seen = set()
        while current and current in self._encounter_aliases and current not in seen:
            seen.add(current)
            current = self._encounter_aliases[current]
        # Flatten chains while their canonical profile is alive. This keeps
        # lookup bounded even when a provider splits one car repeatedly.
        if current and current not in seen:
            for alias in seen:
                self._encounter_aliases[alias] = current
        return current

    def _prune_encounter_aliases_locked(self) -> None:
        """Retain aliases only while their canonical in-memory owner exists."""
        live_ids = {
            enc.get("encounter_id") for enc in self._encounters
            if enc.get("encounter_id")
        }
        live_ids.update(self._identity_stages)
        for alias in list(self._encounter_aliases):
            canonical = self._canonical_id_locked(alias)
            if not canonical or canonical not in live_ids:
                self._encounter_aliases.pop(alias, None)
            else:
                self._encounter_aliases[alias] = canonical
        # A live profile should never have this many genuine fragments. The
        # cap protects multi-day uptime from malformed provider payloads while
        # retaining the newest aliases most likely to own delayed outcomes.
        overflow = len(self._encounter_aliases) - MAX_ENCOUNTER_ALIASES
        if overflow > 0:
            for alias in list(self._encounter_aliases)[:overflow]:
                self._encounter_aliases.pop(alias, None)

    def canonical_encounter_id(self, encounter_id: str | None) -> str | None:
        with self._lock:
            return self._canonical_id_locked(encounter_id)

    def _encounter_locked(self, encounter_id: str | None) -> dict | None:
        canonical = self._canonical_id_locked(encounter_id)
        if not canonical:
            return None
        return next(
            (enc for enc in self._encounters
             if enc.get("encounter_id") == canonical),
            None,
        )

    def route_status(self, encounter_id: str | None) -> str:
        """disabled | pending | qualified | rejected for one vehicle profile."""
        if not self._route_enabled:
            return "disabled"
        with self._lock:
            enc = self._encounter_locked(encounter_id)
            if enc is None:
                return "pending"
            self._maybe_qualify_short_plated_route_locked(enc, time.monotonic())
            status = str(enc.get("route_status") or "pending")
            # Qualification is physical evidence from a completed provider
            # trajectory. Mixed-version or partially merged state must fail
            # closed until that completion bit is present.
            if status == "qualified" and not enc.get("route_complete"):
                return "pending"
            return status

    def route_metadata(self, encounter_id: str | None) -> dict | None:
        """Non-identity profile metadata safe for constructing a Visit."""
        with self._lock:
            enc = self._encounter_locked(encounter_id)
            if enc is None:
                return None
            vehicle = enc["vehicle"]
            return {
                "encounter_id": enc["encounter_id"],
                "first_seen_at": enc.get("first_seen_at"),
                "last_seen_at": enc.get("last_seen_at"),
                "route_status": enc.get("route_status", "pending"),
                "route_reason": enc.get("route_reason"),
                "route_qualified_at": enc.get("route_qualified_at"),
                "route_track_fingerprint": enc.get(
                    "route_track_fingerprint"
                ),
                "trajectory_points": enc.get("trajectory_points", 0),
                "dwell_seconds": enc.get("dwell_seconds"),
                "route_trajectory_bboxes": tuple(
                    enc.get("route_trajectory_bboxes", ())
                ),
                "route_trajectory_timestamps": tuple(
                    enc.get("route_trajectory_timestamps", ())
                ),
                "route_entry_zone": self._route_entry_zone,
                "route_board_zone": self._route_board_zone,
                "route_terminal_exit_zone": self._route_terminal_exit_zone,
                "vehicle_bbox": vehicle.vehicle_bbox,
                "color": vehicle.color,
                "make": vehicle.make,
                "model": vehicle.model,
                "vehicle_type": vehicle.vehicle_type,
                "color_confidence": vehicle.color_confidence,
                "make_confidence": vehicle.make_confidence,
                "model_confidence": vehicle.model_confidence,
            }

    @staticmethod
    def _point_in_zone(point: tuple[float, float], zone) -> bool:
        if not zone:
            return False
        x, y = point
        return zone[0] <= x <= zone[2] and zone[1] <= y <= zone[3]

    def _box_center_normalized(
        self, box: tuple[int, int, int, int] | None
    ) -> tuple[float, float] | None:
        if not box or self._frame_width <= 0 or self._frame_height <= 0:
            return None
        x1, y1, x2, y2 = box
        if x2 <= x1 or y2 <= y1:
            return None
        return (
            ((x1 + x2) / 2.0) / self._frame_width,
            ((y1 + y2) / 2.0) / self._frame_height,
        )

    def _qualify_route_locked(
        self,
        enc: dict,
        reason: str,
        qualified_at: str | None,
    ) -> None:
        # Qualification is monotonic. A later short provider fragment can add
        # evidence but can never erase a completed entry -> board path.
        enc["route_status"] = "qualified"
        enc["route_reason"] = reason
        enc["route_rejected_at"] = None
        if not enc.get("route_qualified_at"):
            enc["route_qualified_at"] = qualified_at or self._now_iso()

    def _update_route_observation_locked(
        self,
        enc: dict,
        box,
        observed_at: str | None = None,
        *,
        plated_attempt: bool = False,
        candidate_key: str | None = None,
        now: float | None = None,
    ) -> None:
        if not self._route_enabled:
            return
        point = self._box_center_normalized(box)
        if point is None:
            return
        enc["route_observations"] = int(enc.get("route_observations", 0)) + 1
        entered_before = bool(enc.get("route_entered"))
        if self._point_in_zone(point, self._route_entry_zone):
            enc["route_entered"] = True
        at_board = self._point_in_zone(point, self._route_board_zone)
        now = time.monotonic() if now is None else now
        conflicting_recent_plate = bool(
            plated_attempt
            and at_board
            and not entered_before
            and any(
                other is not enc
                and (
                    (
                        other.get("identified")
                        and (
                            not candidate_key
                            or other.get("confirmed_candidate_key")
                            != candidate_key
                        )
                    )
                    or (
                        not other.get("identified")
                        and other.get("saw_plate")
                        and (
                            not candidate_key
                            or candidate_key not in other.get(
                                "candidate_keys", set()
                            )
                        )
                    )
                )
                and (now - other["last_seen"]) < self._vehicle_quiet_seconds
                and self._boxes_overlap(
                    other["vehicle"].vehicle_bbox, box
                )
                for other in self._encounters
            )
        )
        # A provider plate result belongs to the vehicle box in this same
        # webhook, so a board observation is useful trigger evidence. It is
        # not final traffic authority: the completed trajectory may still
        # show that this vehicle exited through the upper-right drive-off.
        # Keep the profile pending until the ordered full route resolves.
        if plated_attempt and at_board and conflicting_recent_plate:
            # Keep the different candidate as a separate profile, but do not
            # let one overlapping OCR variant immediately become a second
            # menu-board identity. A genuine follower can still qualify by its
            # own entry observation or completed keyed trajectory.
            if self._route_shadow_only:
                enc["route_shadow_reason"] = (
                    "recent_plate_candidate_conflict"
                )
            else:
                enc["route_reason"] = "recent_plate_candidate_conflict"
        elif plated_attempt and at_board:
            if self._route_shadow_only:
                enc["route_shadow_reason"] = (
                    "plate_at_board_pending_trajectory"
                )
            else:
                enc["route_reason"] = "plate_at_board_pending_trajectory"
        # There is intentionally no live entry->board qualification. Sparse
        # recognition boxes are profile evidence, not a physical trajectory.
        # Every profile waits for completed native dwell evidence before it
        # can become a Visit; a future voice-context channel may consume the
        # provisional board observation without inflating traffic.

    def _route_timestamp_rows(
        self,
        track: StreamDwellTrack,
        rows: list[tuple[int, tuple[float, float]]],
    ) -> list[tuple[int, tuple[float, float]]] | None:
        """Resolve only unambiguous, coincident route samples.

        The 16 Sep completed track included an unrelated lower-left box at
        the exact same timestamp as its terminal board box. Keep the native
        track untouched for audit and identity association. For short-rescue
        proof only, one instant may supply one sample: choose a unique continuation
        with >=70% overlap against the prior sample only when every competing
        box is completely disjoint from that prior sample. Ambiguity remains
        unresolved, rather than selecting whichever box would qualify.
        """
        result: list[tuple[int, tuple[float, float]]] = []
        cursor = 0
        while cursor < len(rows):
            index = rows[cursor][0]
            epoch = (
                _iso_epoch(track.sample_observed_at[index])
                if index < len(track.sample_observed_at) else None
            )
            end = cursor + 1
            if epoch is not None:
                while end < len(rows):
                    other_index = rows[end][0]
                    other_epoch = (
                        _iso_epoch(track.sample_observed_at[other_index])
                        if other_index < len(track.sample_observed_at) else None
                    )
                    if other_epoch != epoch:
                        break
                    end += 1
            group = rows[cursor:end]
            distinct = {}
            for row in group:
                box = tuple(track.samples[row[0]].vehicle_bbox)
                distinct.setdefault(box, row)
            if len(distinct) == 1:
                result.append(next(iter(distinct.values())))
            else:
                if not result:
                    return None
                prior_box = track.samples[result[-1][0]].vehicle_bbox
                scored = [
                    (self._overlap_fraction(prior_box, box), row)
                    for box, row in distinct.items()
                ]
                continuations = [row for score, row in scored if score >= 0.70]
                if len(continuations) != 1 or any(
                    score > 0.0 and row != continuations[0]
                    for score, row in scored
                ):
                    return None
                result.append(continuations[0])
            cursor = end
        return result

    def _trajectory_route_evidence(
        self, track: StreamDwellTrack
    ) -> tuple[str, str, int | None]:
        """Classify a completed physical track and return first board index."""
        rows = []
        for index, sample in enumerate(track.samples):
            point = self._box_center_normalized(sample.vehicle_bbox)
            if point is not None:
                rows.append((index, point))
        if not rows:
            return "rejected", "trajectory_insufficient_points", None
        epochs = [
            _iso_epoch(track.sample_observed_at[index])
            if index < len(track.sample_observed_at) else None
            for index, _ in rows
        ]
        timed = [value for value in epochs if value is not None]
        duration = max(timed) - min(timed) if len(timed) >= 2 else None
        terminal_epoch = epochs[-1]
        terminals = [
            point for (_index, point), epoch in zip(rows, epochs)
            if terminal_epoch is not None and epoch == terminal_epoch
        ] or [rows[-1][1]]
        # Terminal direction is stronger evidence than sparsity.  The two
        # audited upper-right drive-offs contained only three and seven
        # provider samples; returning "insufficient" before examining their
        # terminal position kept them in the ambiguous bucket even though the
        # completed trajectory proved where they left.
        if (
            self._route_terminal_exit_zone is not None
            and any(
                self._route_terminal_exit_zone[0] < terminal[0]
                <= self._route_terminal_exit_zone[2]
                and self._route_terminal_exit_zone[1] <= terminal[1]
                <= self._route_terminal_exit_zone[3]
                for terminal in terminals
            )
        ):
            return "rejected", "terminal_upper_right_exit", None

        progressive_rescue = self._progressive_board_rescue_start(
            track, rows, duration
        )

        def rescued_or(reason: str):
            if progressive_rescue is not None:
                rescue_reason, board_start = progressive_rescue
                return "qualified", rescue_reason, board_start
            return "rejected", reason, None

        if len(rows) < self._route_min_trajectory_points:
            return rescued_or("trajectory_insufficient_points")

        if self._route_min_trajectory_duration_seconds > 0.0:
            if duration is None:
                return rescued_or("trajectory_missing_timing")
            # Stream timestamps are sampled independently from its rounded
            # dwell duration.  Fifty milliseconds prevents a displayed 2.0s
            # route from failing at 1.999s without turning the threshold into
            # a broad short-track relaxation.
            if duration + 0.05 < self._route_min_trajectory_duration_seconds:
                return rescued_or("trajectory_insufficient_duration")

        confirm = self._route_zone_confirm_points

        def confirmed_zone(start: int, zone) -> tuple[int, list[tuple[float, float]]] | None:
            streak: list[tuple[float, float]] = []
            for row_index in range(start, len(rows)):
                point = rows[row_index][1]
                if self._point_in_zone(point, zone):
                    streak.append(point)
                    if len(streak) >= confirm:
                        return row_index, streak[-confirm:]
                else:
                    streak = []
            return None

        entry = confirmed_zone(0, self._route_entry_zone)
        if entry is None:
            return rescued_or("never_entered_drive_through")
        entry_end, entry_points = entry
        board = confirmed_zone(entry_end + 1, self._route_board_zone)
        if board is None:
            return rescued_or("left_before_board_trigger")
        board_end, board_points = board

        entry_x = sum(point[0] for point in entry_points) / len(entry_points)
        entry_y = sum(point[1] for point in entry_points) / len(entry_points)
        board_x = sum(point[0] for point in board_points) / len(board_points)
        board_y = sum(point[1] for point in board_points) / len(board_points)
        # First sample in the confirmed board streak, not the completion time.
        board_start = rows[board_end - confirm + 1][0]
        leftward_ok = (
            entry_x - board_x + 1e-9
            >= self._route_min_leftward_progress
        )
        upward_ok = (
            entry_y - board_y + 1e-9
            >= self._route_min_upward_progress
        )
        if not (leftward_ok and upward_ok):
            fallback_start = self._sustained_board_fallback_start(
                track, rows, entry_end + 1
            )
            if fallback_start is not None:
                return (
                    "qualified",
                    "entry_then_sustained_board_dwell",
                    fallback_start,
                )
            if progressive_rescue is not None:
                rescue_reason, rescue_start = progressive_rescue
                return "qualified", rescue_reason, rescue_start
            if not leftward_ok:
                return "rejected", "insufficient_leftward_progress", None
            return "rejected", "insufficient_upward_progress", None
        return "qualified", "entry_then_board_trajectory", board_start

    def _progressive_board_rescue_start(
        self,
        track: StreamDwellTrack,
        rows: list[tuple[int, tuple[float, float]]],
        duration: float | None,
    ) -> tuple[str, int] | None:
        """Recover a short but directional arrival at the menu board.

        Provider tracking can begin after most of a long vehicle has already
        crossed the entry/board boundary.  The audited false negatives then
        contain either one entry sample followed by board samples, or a track
        wholly inside the board zone moving farther left/up.  This rescue is
        an OR evidence path, not a relaxed ordinary path: it retains a point
        and time floor, requires a consecutive terminal board streak and the
        normal calibrated progress, and runs only after terminal upper-right
        traffic has been rejected.
        """
        # Ambiguous coincident boxes cannot establish this short OR path.
        # They do not revoke independent ordinary entry/board or sustained
        # dwell proof elsewhere on a long native track. The Sept16 full-track
        # replay caught two real long stops that such a global veto rejected.
        # Terminal exit checks still inspect every tied terminal candidate
        # before either qualification path runs.
        rows = self._route_timestamp_rows(track, rows)
        if rows is None:
            return None

        min_points = self._route_progressive_board_rescue_min_points
        min_board = self._route_progressive_board_rescue_min_board_points
        min_seconds = self._route_progressive_board_rescue_min_seconds
        if (
            min_points <= 0
            or min_board <= 0
            or min_seconds <= 0.0
            or len(rows) < min_points
            or duration is None
            or duration + 0.05 < min_seconds
        ):
            return None

        terminal_board_streak: list[
            tuple[int, int, tuple[float, float]]
        ] = []
        for row_index, (sample_index, point) in enumerate(rows):
            if self._point_in_zone(point, self._route_board_zone):
                terminal_board_streak.append((row_index, sample_index, point))
            else:
                terminal_board_streak = []
        if len(terminal_board_streak) < min_board:
            return None

        board_row, board_sample, _ = terminal_board_streak[0]
        entries_before_board = [
            (row_index, point)
            for row_index, (_sample_index, point) in enumerate(rows[:board_row])
            if self._point_in_zone(point, self._route_entry_zone)
        ]
        if entries_before_board:
            _entry_row, anchor = entries_before_board[-1]
            reason = "entry_then_progressive_board_trajectory"
        else:
            # A board-only rescue must begin and remain at the menu-board
            # side.  A disconnected mid-frame fragment cannot skip entry and
            # later touch the board merely once.
            if board_row != 0 or len(terminal_board_streak) != len(rows):
                return None
            anchor = rows[0][1]
            reason = "board_only_progressive_trajectory"

        target = rows[-1][1]
        if (
            anchor[0] - target[0] + 1e-9
            < self._route_min_leftward_progress
            or anchor[1] - target[1] + 1e-9
            < self._route_min_upward_progress
        ):
            # 16 Sep: a coherent 9-point lane arrival took 1.81s. Measuring
            # progress only from its LAST entry sample discarded the earlier
            # approach and falsely rejected it. A short track with the full
            # ordinary point count and confirmed entry streak may instead use
            # that stable entry anchor. The 2s ordinary floor, terminal board
            # streak, direction floors and upper-right veto stay unchanged.
            if (
                entries_before_board
                and len(rows) >= max(8, self._route_min_trajectory_points)
            ):
                confirm = max(2, self._route_zone_confirm_points)
                entry_streak = []
                for _sample_index, point in rows[:board_row]:
                    if self._point_in_zone(point, self._route_entry_zone):
                        entry_streak.append(point)
                    else:
                        entry_streak = []
                    if len(entry_streak) >= confirm:
                        stable = entry_streak[-confirm:]
                        stable_x = sum(p[0] for p in stable) / confirm
                        stable_y = sum(p[1] for p in stable) / confirm
                        if (
                            stable_x - target[0] + 1e-9
                            >= self._route_min_leftward_progress
                            and stable_y - target[1] + 1e-9
                            >= self._route_min_upward_progress
                        ):
                            return (
                                "confirmed_entry_then_progressive_board_trajectory",
                                board_sample,
                            )
                        break
            return None
        return reason, board_sample

    def _sustained_board_fallback_start(
        self,
        track: StreamDwellTrack,
        rows: list[tuple[int, tuple[float, float]]],
        start: int,
    ) -> int | None:
        """Return the first sample of a long consecutive board-zone dwell.

        This is intentionally not a general route rescue. The caller has
        already proven ordered entry and board zones and rejected terminal
        upper-right traffic. The extra point and time floors recover a car
        whose provider track began too near the zone boundary to demonstrate
        the normal calibrated progress delta.
        """
        min_points = self._route_board_dwell_fallback_min_points
        min_board_points = self._route_board_dwell_fallback_min_board_points
        min_seconds = self._route_board_dwell_fallback_min_seconds
        if (
            min_points <= 0
            or min_board_points <= 0
            or min_seconds <= 0.0
            or len(rows) < min_points
        ):
            return None

        streak: list[tuple[int, float]] = []
        for row_index in range(start, len(rows)):
            sample_index, point = rows[row_index]
            if not self._point_in_zone(point, self._route_board_zone):
                streak = []
                continue
            observed_at = (
                track.sample_observed_at[sample_index]
                if sample_index < len(track.sample_observed_at) else None
            )
            epoch = _iso_epoch(observed_at)
            if epoch is None:
                streak = []
                continue
            if streak and epoch < streak[-1][1]:
                streak = []
            streak.append((sample_index, epoch))
            if (
                len(streak) >= min_board_points
                and epoch - streak[0][1] + 1e-6 >= min_seconds
            ):
                return streak[0][0]
        return None

    def _short_plated_route_board_index(
        self, track: StreamDwellTrack
    ) -> int | None:
        """Evidence shape for one reviewed short, deep lane arrival.

        The Sept25 Jeep has three entry samples and one board sample over
        0.8s. This is insufficient ordinary route proof. Only the separately
        confirmed, uniquely owned plate and the existing rejection grace can
        authorize the narrow exception below; this helper alone never counts.
        No camera reads, image retention or extra inference are involved.
        """
        if (
            not self._short_plated_route_enabled or not track.plate_key
            or not 4 <= len(track.plate_key) <= 12
            or not 4 <= len(track.samples) <= 7
            or len(track.sample_observed_at) != len(track.samples)
        ):
            return None
        epochs = [_iso_epoch(value) for value in track.sample_observed_at]
        if any(value is None or not math.isfinite(value) for value in epochs):
            return None
        if any(b <= a for a, b in zip(epochs, epochs[1:])):
            return None
        if not 0.5 <= epochs[-1] - epochs[0] <= 2.0:
            return None
        for value, epoch in ((track.first_seen_at, epochs[0]),
                             (track.last_seen_at, epochs[-1])):
            endpoint = _iso_epoch(value)
            if endpoint is None or abs(endpoint - epoch) > 0.05:
                return None
        points = [
            self._box_center_normalized(sample.vehicle_bbox)
            for sample in track.samples
        ]
        if any(
            not sample.vehicle_bbox
            or not (0 <= sample.vehicle_bbox[0] < sample.vehicle_bbox[2] <= self._frame_width)
            or not (0 <= sample.vehicle_bbox[1] < sample.vehicle_bbox[3] <= self._frame_height)
            for sample in track.samples
        ):
            return None
        if any(
            point is None or any(not math.isfinite(value) for value in point)
            for point in points
        ):
            return None
        # Even an intermediate upper-right observation denies this exception:
        # it must be a clean lane approach, not an exit that clips the board.
        if any(self._point_in_zone(p, self._route_terminal_exit_zone) for p in points):
            return None
        if any(
            b[0] > a[0] or b[1] > a[1]
            for a, b in zip(points, points[1:])
        ):
            return None
        if any(
            self._overlap_fraction(a.vehicle_bbox, b.vehicle_bbox) < 0.70
            for a, b in zip(track.samples, track.samples[1:])
        ):
            return None
        board_index = next(
            (i for i, point in enumerate(points)
             if self._point_in_zone(point, self._route_board_zone)),
            None,
        )
        if (
            board_index is None or board_index < 3
            or any(not self._point_in_zone(p, self._route_entry_zone)
                   for p in points[:board_index])
            or any(not self._point_in_zone(p, self._route_board_zone)
                   for p in points[board_index:])
        ):
            return None
        first, terminal = points[0], points[-1]
        if (
            first[1] < 0.40 or terminal[0] > 0.60 or terminal[1] > 0.24
            or first[0] - terminal[0] < 0.06
            or first[1] - terminal[1] < 0.15
        ):
            return None
        return board_index

    def _short_plated_route_owner_locked(
        self, enc: dict, track: StreamDwellTrack, *, confirmed: bool
    ) -> bool:
        """Require one exact, same-frame strong plate on this native endpoint."""
        if (
            not self._short_plated_route_enabled
            or not track.plate_key
            or enc.get("candidate_keys") != {track.plate_key}
            or int(enc.get("profile_fragment_count", 1)) != 1
            or enc.get("plate_attached_to_existing")
            or enc.get("route_shadow_visit_emitted")
            or enc.get("route_diagnostic_emitted")
            or enc.get("short_route_exit_veto")
            or any(
                alias == enc.get("encounter_id")
                or owner == enc.get("encounter_id")
                for alias, owner in self._encounter_aliases.items()
            )
        ):
            return False
        if confirmed and (
            not enc.get("short_route_detector_confirmed")
            or not enc.get("identified")
            or enc.get("confirmed_candidate_key") != track.plate_key
        ):
            return False
        evidence = enc.get("short_route_plate_evidence")
        if not evidence or evidence[0] != track.plate_key:
            return False
        _key, detection, ocr, box, observed_at = evidence
        observed_epoch = _iso_epoch(observed_at)
        terminal_epoch = _iso_epoch(track.sample_observed_at[-1])
        if (
            not math.isfinite(detection) or not math.isfinite(ocr)
            or detection < max(0.70, self.det_min)
            or ocr < max(0.98, self.ocr_min)
            or observed_epoch is None or terminal_epoch is None
            or abs(observed_epoch - terminal_epoch) > 0.05
            or self._overlap_fraction(box, track.samples[-1].vehicle_bbox) < 0.70
        ):
            return False
        # Do not infer ownership from colour/MMC. An overlapping, simultaneous
        # second profile (even plateless) makes this abbreviated proof unsafe.
        return not any(
            other is not enc and self._time_matches_dwell(other, track)
            and self._trajectory_assignment(other, track) is not None
            for other in self._encounters
        )

    def _maybe_qualify_short_plated_route_locked(self, enc: dict, now: float) -> None:
        """Settle a metadata-only candidate after the existing rejection grace."""
        track = enc.get("short_plated_route_candidate")
        if track is None or enc.get("route_status") != "rejected":
            return
        if (
            enc.get("route_reason") != "trajectory_insufficient_points"
            or not enc.get("route_complete")
            or enc.get("route_track_fingerprint") != _dwell_fingerprint(track)
        ):
            enc.pop("short_plated_route_candidate", None)
            return
        board_index = self._short_plated_route_board_index(track)
        if board_index is None or not self._short_plated_route_owner_locked(
            enc, track, confirmed=False
        ):
            enc.pop("short_plated_route_candidate", None)
            return
        anchor = max(
            float(enc.get("route_rejected_at") or 0.0),
            float(enc.get("last_seen") or 0.0),
        )
        if now - anchor < self._route_rejection_grace_seconds:
            return
        if not self._short_plated_route_owner_locked(enc, track, confirmed=True):
            return
        enc.pop("short_plated_route_candidate", None)
        enc["route_board_sample_at"] = track.sample_observed_at[board_index]
        enc["route_board_sample_bbox"] = track.samples[board_index].vehicle_bbox
        self._qualify_route_locked(
            enc, "confirmed_plate_deep_entry_short_board",
            track.sample_observed_at[board_index],
        )
        enc["vehicle"] = replace(
            enc["vehicle"], diagnostic_image_jpeg=None,
            diagnostic_image_bbox=None, diagnostic_image_observed_at=None,
        )
        log.info(
            "VEHICLE_ROUTE encounter=%s status=qualified "
            "reason=confirmed_plate_deep_entry_short_board points=%d "
            "grace=%.1fs",
            enc["encounter_id"][:8], len(track.samples), now - anchor,
        )

    def _classify_trajectory(
        self, track: StreamDwellTrack
    ) -> tuple[str, str]:
        status, reason, _ = self._trajectory_route_evidence(track)
        return status, reason

    def _trajectory_qualified_at(
        self, track: StreamDwellTrack
    ) -> str | None:
        """First timestamp at which an ordered track reaches the board."""
        status, _, index = self._trajectory_route_evidence(track)
        if status != "qualified" or index is None:
            return None
        return (
            track.sample_observed_at[index]
            if index < len(track.sample_observed_at) else None
        )

    def _parse_dwell_tracks(self, payload: dict) -> list[StreamDwellTrack]:
        tracks = []
        for result in _results(payload):
            first_seen_at = (
                str(result.get("first_seen"))
                if result.get("first_seen") else None
            )
            last_seen_at = (
                str(result.get("last_seen"))
                if result.get("last_seen") else None
            )
            trajectory = result.get("trajectory")
            raw_samples = [
                sample for sample in trajectory if isinstance(sample, dict)
            ] if isinstance(trajectory, list) else []
            timestamp_order = [
                _iso_epoch(sample.get("timestamp")) for sample in raw_samples
            ]
            if raw_samples and all(value is not None for value in timestamp_order):
                raw_samples = [
                    sample for _, sample in sorted(
                        zip(timestamp_order, raw_samples),
                        key=lambda item: item[0],
                    )
                ]
            elif raw_samples:
                try:
                    positions = [
                        float(sample["position_sec"]) for sample in raw_samples
                    ]
                    if any(value != value for value in positions):
                        raise ValueError("NaN trajectory position")
                    raw_samples = [
                        sample for _, sample in sorted(
                            zip(positions, raw_samples),
                            key=lambda item: item[0],
                        )
                    ]
                except (KeyError, TypeError, ValueError, OverflowError):
                    # Preserve provider order if it supplies neither a complete
                    # timestamp sequence nor sortable position values.
                    pass
            parsed_samples = []
            sample_observed_at = []
            first_epoch = _iso_epoch(first_seen_at)
            for raw_sample in raw_samples:
                vehicle = _trajectory_vehicle(raw_sample)
                if vehicle is None:
                    continue
                observed_at = raw_sample.get("timestamp")
                if _iso_epoch(observed_at) is None:
                    observed_at = None
                    try:
                        position = float(raw_sample.get("position_sec"))
                        if first_epoch is not None and math.isfinite(position):
                            observed_at = datetime.fromtimestamp(
                                first_epoch + position, timezone.utc
                            ).isoformat(timespec="milliseconds").replace(
                                "+00:00", "Z"
                            )
                    except (TypeError, ValueError, OverflowError):
                        pass
                else:
                    observed_at = str(observed_at)
                parsed_samples.append(vehicle)
                sample_observed_at.append(observed_at)
            samples = tuple(parsed_samples)
            raw_plate = result.get("plate")
            plate_key = (
                self.normalize_candidate(raw_plate)
                if isinstance(raw_plate, str) and raw_plate else None
            )
            try:
                dwell_seconds = max(0.0, float(result.get("dwell_time") or 0.0))
            except (TypeError, ValueError):
                dwell_seconds = 0.0
            tracks.append(StreamDwellTrack(
                first_seen_at=first_seen_at,
                last_seen_at=last_seen_at,
                dwell_seconds=dwell_seconds,
                plate_key=plate_key,
                samples=samples,
                sample_observed_at=tuple(sample_observed_at),
                camera_id=_payload_camera(payload),
            ))
        return tracks

    def _time_matches_dwell(self, enc: dict, track: StreamDwellTrack) -> bool:
        start = _iso_epoch(track.first_seen_at)
        end = _iso_epoch(track.last_seen_at) or start
        enc_start = _iso_epoch(enc.get("first_seen_at"))
        enc_end = _iso_epoch(enc.get("last_seen_at")) or enc_start
        if None in (start, end, enc_start, enc_end):
            # Geometry alone is not an identity. A missing timestamp must not
            # make every overlapping profile eligible for an authoritative
            # merge; the caller may still use one unique exact plate key.
            return False
        if end < start or enc_end < enc_start:
            return False
        return enc_end >= start - 5.0 and enc_start <= end + 5.0

    def _trajectory_overlap(self, enc: dict, track: StreamDwellTrack) -> float:
        box = enc["vehicle"].vehicle_bbox
        if not box:
            return 0.0
        return max(
            (self._overlap_fraction(box, sample.vehicle_bbox)
             for sample in track.samples if sample.vehicle_bbox),
            default=0.0,
        )

    def _trajectory_assignment(
        self, enc: dict, track: StreamDwellTrack
    ) -> tuple[float, int] | None:
        """Best physical trajectory sample for one sparse profile.

        Geometry establishes eligibility. When more than one sample overlaps
        nearly equally, the observation timestamp chooses the contemporaneous
        sample. This does not identify a vehicle; it is used only as part of
        the stricter ordered-fragment proof below.
        """
        box = enc["vehicle"].vehicle_bbox
        if not box:
            return None
        scores = [
            self._overlap_fraction(box, sample.vehicle_bbox)
            if sample.vehicle_bbox else 0.0
            for sample in track.samples
        ]
        if not scores:
            return None
        best_score = max(scores)
        if best_score < 0.30:
            return None
        eligible = [
            index for index, score in enumerate(scores)
            if score >= best_score - 0.05
        ]
        # `enc["vehicle"]` contains the latest merged geometry, so its owner is
        # `last_seen_at`, not the midpoint of the profile's whole lifetime. A
        # real t0 entry -> t6 board profile must therefore map its current board
        # box to the t6 trajectory sample. Conversely, a following car first
        # seen in the same entry box at t3 must not steal the lead car's t0
        # sample through the track-level +/-5 second correlation window.
        geometry_observed_at = _iso_epoch(enc.get("last_seen_at"))
        sample_epochs = [
            _iso_epoch(value) for value in track.sample_observed_at
        ]
        if geometry_observed_at is not None and any(
            index < len(sample_epochs) and sample_epochs[index] is not None
            for index in eligible
        ):
            index = min(
                (
                    index for index in eligible
                    if index < len(sample_epochs)
                    and sample_epochs[index] is not None
                ),
                key=lambda candidate: abs(
                    sample_epochs[candidate] - geometry_observed_at
                ),
            )
        else:
            index = eligible[0]
        if geometry_observed_at is not None:
            if index >= len(sample_epochs) or sample_epochs[index] is None:
                return None
            if abs(sample_epochs[index] - geometry_observed_at) > \
                    MAX_TRAJECTORY_SAMPLE_SKEW_SECONDS:
                return None
        return best_score, index

    def _has_viable_plate_evidence(self, enc: dict) -> bool:
        """Whether this profile has plate evidence worth owning separately.

        A false localization that clears neither engine gate is still saved as
        diagnostic evidence, but it must not prevent a later real rear plate
        from reconciling with the same physical trajectory.  Evidence clearing
        either gate stays conservative because the configured consensus rescue
        may legitimately confirm it on later frames.
        """
        if enc.get("identified"):
            return True
        if not enc.get("saw_plate"):
            return False
        detection = enc.get("plate_detection_confidence")
        ocr = enc.get("plate_ocr_confidence")
        if detection is None or ocr is None:
            # Legacy/manual observations without score provenance retain the
            # historical conservative behavior.
            return True
        return bool(
            float(detection) >= self.det_min
            or float(ocr) >= self.ocr_min
        )

    def _ordered_trajectory_fragments(
        self,
        encounters: list[dict],
        track: StreamDwellTrack,
        *,
        require_unplated: bool,
    ) -> list[dict]:
        """Prove that sparse profiles are sequential pieces of one track.

        Multiple profiles are mergeable only when their own timestamp windows
        do not overlap and their uniquely associated trajectory samples move
        strictly forward in provider order. Simultaneous queue occupants and
        profiles already built from earlier fragments fail closed.
        """
        if not 2 <= len(encounters) <= 4:
            return []
        rows = []
        for enc in encounters:
            if (
                int(enc.get("profile_fragment_count", 1)) != 1
                or enc.get("plate_attached_to_existing")
            ):
                return []
            if require_unplated and self._has_viable_plate_evidence(enc):
                return []
            start = _iso_epoch(enc.get("first_seen_at"))
            end = _iso_epoch(enc.get("last_seen_at")) or start
            assignment = self._trajectory_assignment(enc, track)
            if start is None or end is None or end < start or assignment is None:
                return []
            score, sample_index = assignment
            sample_epoch = (
                _iso_epoch(track.sample_observed_at[sample_index])
                if sample_index < len(track.sample_observed_at) else None
            )
            geometry_observed_at = end
            # Sample ownership must be substantially tighter than the broad
            # track-window correlation. At three seconds, the next queue car
            # can already occupy the prior car's entry box.
            if sample_epoch is None or abs(
                sample_epoch - geometry_observed_at
            ) > \
                    MAX_TRAJECTORY_SAMPLE_SKEW_SECONDS:
                return []
            rows.append((start, end, sample_index, score, enc))
        rows.sort(key=lambda row: (row[0], row[1]))
        for previous, current in zip(rows, rows[1:]):
            if previous[1] >= current[0]:
                return []
            if previous[2] >= current[2]:
                return []
        return [row[4] for row in rows]

    def _match_dwell_locked(
        self,
        track: StreamDwellTrack,
        excluded_ids: set[str] | None = None,
        *,
        allow_reconciliation: bool = False,
    ) -> list[dict]:
        excluded_ids = excluded_ids or set()
        candidates = [
            enc for enc in self._encounters
            if enc.get("encounter_id") not in excluded_ids
        ]
        recent = [
            enc for enc in candidates
            if self._time_matches_dwell(enc, track)
        ]
        keyed = [
            enc for enc in recent
            if track.plate_key
            and track.plate_key in enc.get("candidate_keys", set())
        ]
        if keyed:
            if len(keyed) == 1:
                matches = list(keyed)
            else:
                on_track = [
                    enc for enc in keyed
                    if self._trajectory_assignment(enc, track) is not None
                ]
                if len(on_track) == 1:
                    # The identical candidate on another profile is an OCR
                    # collision unless it also belongs to this trajectory.
                    matches = on_track
                elif allow_reconciliation:
                    matches = self._ordered_trajectory_fragments(
                        on_track, track, require_unplated=False
                    )
                else:
                    matches = []
                if not matches:
                    log.warning(
                        "multiple exact plate candidates lack unique physical "
                        "trajectory proof; keeping profiles separate"
                    )
                    return []
            # A native trajectory is strong enough to bridge exactly one
            # earlier front/side profile into the keyed rear-plate profile.
            # Ambiguity fails closed so adjacent same-colour queue occupants
            # cannot be merged.
            # Never bridge a second front profile after the keyed encounter
            # has already absorbed another fragment. That would let one
            # confused provider trajectory collapse adjacent queue cars.
            if allow_reconciliation and len(matches) == 1 and all(
                int(enc.get("profile_fragment_count", 1)) == 1
                and not enc.get("plate_attached_to_existing")
                for enc in matches
            ):
                front = [
                    enc for enc in recent
                    if enc not in matches
                    and not self._has_viable_plate_evidence(enc)
                    and self._trajectory_overlap(enc, track) >= 0.30
                ]
                if len(front) == 1:
                    ordered = self._ordered_trajectory_fragments(
                        [front[0], matches[0]],
                        track,
                        require_unplated=False,
                    )
                    if ordered:
                        matches.append(front[0])
            return matches

        # A provider/legacy payload with incomplete timestamps may correlate
        # only through one unique exact on-device plate candidate. Never use
        # geometry alone in that case and never bridge a second front profile.
        if track.plate_key:
            exact_any_time = [
                enc for enc in candidates
                if track.plate_key in enc.get("candidate_keys", set())
            ]
            if len(exact_any_time) == 1:
                exact = exact_any_time[0]
                track_start = _iso_epoch(track.first_seen_at)
                track_end = _iso_epoch(track.last_seen_at)
                enc_start = _iso_epoch(exact.get("first_seen_at"))
                enc_end = _iso_epoch(exact.get("last_seen_at"))
                if None in (track_start, track_end, enc_start, enc_end):
                    return exact_any_time
            # A dwell hook's raw plate is correlation evidence only. Until an
            # ordinary recognition webhook supplies that exact candidate, it
            # must never geometry-qualify or finalize a plateless front car.
            return []

        assignments = [
            (assignment, enc)
            for enc in recent
            if (assignment := self._trajectory_assignment(enc, track))
            is not None
        ]
        ranked = sorted(
            ((assignment[0], enc) for assignment, enc in assignments),
            key=lambda item: item[0],
            reverse=True,
        )
        if not ranked or ranked[0][0] < 0.30:
            return []
        if allow_reconciliation:
            eligible = [enc for score, enc in ranked if score >= 0.30]
            if len(eligible) > 1:
                ordered = self._ordered_trajectory_fragments(
                    eligible, track, require_unplated=True
                )
                if ordered:
                    return ordered
        if len(ranked) > 1 and ranked[1][0] >= ranked[0][0] - 0.05:
            log.warning(
                "dwell trajectory association ambiguous; keeping profiles separate"
            )
            return []
        return [ranked[0][1]]

    def _merge_encounters_locked(self, primary: dict, other: dict) -> None:
        if primary is other:
            return
        # A short-route exception never grants authority across aliases or
        # merged queue fragments, even if stronger ordinary proof may do so.
        primary.pop("short_plated_route_candidate", None)
        other.pop("short_plated_route_candidate", None)
        # Exit evidence is sticky for this abbreviated exception. A delayed
        # older short-board hook cannot erase a later native upper-right exit.
        primary["short_route_exit_veto"] = bool(
            primary.get("short_route_exit_veto")
            or other.get("short_route_exit_veto")
        )
        primary_epoch = _iso_epoch(primary.get("last_seen_at"))
        other_epoch = _iso_epoch(other.get("last_seen_at"))
        if primary_epoch is not None and other_epoch is not None:
            primary_has_latest_geometry = primary_epoch >= other_epoch
        else:
            primary_has_latest_geometry = float(
                primary.get("last_seen") or 0.0
            ) >= float(other.get("last_seen") or 0.0)
        latest_vehicle = (
            primary["vehicle"] if primary_has_latest_geometry
            else other["vehicle"]
        )
        old_id = other["encounter_id"]
        new_id = primary["encounter_id"]
        self._encounter_aliases[old_id] = new_id
        primary["first_seen"] = min(primary["first_seen"], other["first_seen"])
        primary["last_seen"] = max(primary["last_seen"], other["last_seen"])
        if (_iso_epoch(other.get("first_seen_at")) or float("inf")) < (
            _iso_epoch(primary.get("first_seen_at")) or float("inf")
        ):
            primary["first_seen_at"] = other.get("first_seen_at")
        if (_iso_epoch(other.get("last_seen_at")) or 0.0) > (
            _iso_epoch(primary.get("last_seen_at")) or 0.0
        ):
            primary["last_seen_at"] = other.get("last_seen_at")
        primary["identified"] = bool(
            primary.get("identified") or other.get("identified")
        )
        primary["confirmed_candidate_key"] = (
            primary.get("confirmed_candidate_key")
            or other.get("confirmed_candidate_key")
        )
        primary["saw_plate"] = bool(
            primary.get("saw_plate") or other.get("saw_plate")
        )
        primary["plate_attached_to_existing"] = bool(
            primary.get("plate_attached_to_existing")
            or other.get("plate_attached_to_existing")
        )
        primary["route_shadow_visit_emitted"] = bool(
            primary.get("route_shadow_visit_emitted")
            or other.get("route_shadow_visit_emitted")
        )
        if primary["route_shadow_visit_emitted"]:
            self._remember_terminal_visit_locked(primary, time.monotonic())
        primary["route_diagnostic_emitted"] = bool(
            primary.get("route_diagnostic_emitted")
            or other.get("route_diagnostic_emitted")
        )
        primary.setdefault("candidate_keys", set()).update(
            other.get("candidate_keys", set())
        )
        merged_vehicle = self._merge_vehicle(
            primary["vehicle"], other["vehicle"]
        )
        # Descriptive MMC is confidence-selected, but geometry represents the
        # profile's current physical position and must come from the newer
        # fragment. Rewinding a keyed board profile to an older entry bbox can
        # hide overlap with a later OCR variant and create a second vehicle.
        primary["vehicle"] = replace(
            merged_vehicle,
            vehicle_bbox=(
                latest_vehicle.vehicle_bbox or merged_vehicle.vehicle_bbox
            ),
        )
        if (
            primary.get("diagnostic_episode_id") is None
            and other.get("diagnostic_episode_id") is not None
        ):
            primary["diagnostic_episode_id"] = other["diagnostic_episode_id"]
        for key in ("plate_detection_confidence", "plate_ocr_confidence"):
            values = [
                float(value) for value in (primary.get(key), other.get(key))
                if value is not None
            ]
            primary[key] = max(values) if values else None
        primary["route_entered"] = bool(
            primary.get("route_entered") or other.get("route_entered")
        )
        primary["route_observations"] = int(
            primary.get("route_observations", 0)
        ) + int(other.get("route_observations", 0))
        primary["profile_fragment_count"] = int(
            primary.get("profile_fragment_count", 1)
        ) + int(other.get("profile_fragment_count", 1))

        def merge_status(prefix: str = "route") -> dict:
            status_key = f"{prefix}_status"
            reason_key = f"{prefix}_reason"
            statuses = {
                primary.get(status_key, "pending"),
                other.get(status_key, "pending"),
            }
            if "qualified" in statuses:
                source = (
                    primary if primary.get(status_key) == "qualified" else other
                )
                primary[status_key] = "qualified"
                primary[reason_key] = source.get(reason_key)
            elif "rejected" in statuses:
                source = (
                    primary if primary.get(status_key) == "rejected" else other
                )
                primary[status_key] = "rejected"
                primary[reason_key] = source.get(reason_key)
            else:
                primary[status_key] = "pending"
                source = primary
            return source

        route_source = merge_status("route")
        shadow_source = merge_status("route_shadow")
        audit_source = shadow_source if self._route_shadow_only else route_source
        primary["route_complete"] = bool(
            primary.get("route_complete") or other.get("route_complete")
        )
        primary["route_shadow_complete"] = bool(
            primary.get("route_shadow_complete")
            or other.get("route_shadow_complete")
        )
        if not primary.get("route_board_sample_at") and other.get(
            "route_board_sample_at"
        ):
            primary["route_board_sample_at"] = other["route_board_sample_at"]
            primary["route_board_sample_bbox"] = other.get(
                "route_board_sample_bbox"
            )
        primary["route_rejected_at"] = (
            primary.get("route_rejected_at")
            or other.get("route_rejected_at")
        )
        qualified_times = [
            value for value in (
                primary.get("route_qualified_at"),
                other.get("route_qualified_at"),
            )
            if value
        ]
        if qualified_times:
            primary["route_qualified_at"] = min(
                qualified_times,
                key=lambda value: _iso_epoch(value) or float("inf"),
            )
        # Route status/reason and the supporting track are one proof. Choosing
        # the longer track independently can label a rejected exit trajectory
        # with a shorter qualifying continuation's reason (or vice versa).
        # This changes audit provenance only, not the existing monotonic
        # qualification or physical-profile reconciliation rules.
        if audit_source is other:
            primary["trajectory_points"] = int(
                other.get("trajectory_points", 0)
            )
            primary["dwell_seconds"] = other.get("dwell_seconds")
            primary["route_track_fingerprint"] = other.get(
                "route_track_fingerprint"
            )
            primary["route_trajectory_bboxes"] = tuple(
                other.get("route_trajectory_bboxes", ())
            )
            primary["route_trajectory_timestamps"] = tuple(
                other.get("route_trajectory_timestamps", ())
            )

        other_stage = self._identity_stages.pop(old_id, None)
        if other_stage is not None:
            primary_stage = self._identity_stages.get(new_id)
            if primary_stage is None:
                primary_stage = {
                    "first_seen": other_stage["first_seen"],
                    "last_seen": other_stage["last_seen"],
                    "reads": [],
                    "best_image_by_key": {},
                }
                self._identity_stages[new_id] = primary_stage
            else:
                primary_stage["first_seen"] = min(
                    primary_stage["first_seen"], other_stage["first_seen"]
                )
                primary_stage["last_seen"] = max(
                    primary_stage["last_seen"], other_stage["last_seen"]
                )
            for read in other_stage.get("reads", []):
                self._stage_read(
                    primary_stage, replace(read, encounter_id=new_id)
                )
        self._encounters = [enc for enc in self._encounters if enc is not other]
        self._prune_encounter_aliases_locked()

    def _cache_unmatched_dwell_locked(
        self, track: StreamDwellTrack, now: float
    ) -> None:
        fingerprint = _dwell_fingerprint(track)
        self._pending_dwell_tracks[fingerprint] = (
            track, now + self._unmatched_dwell_cache_seconds
        )
        # A disconnected or malformed provider must not grow RAM without
        # bound. The retained correlation values never leave this process.
        if len(self._pending_dwell_tracks) > 128:
            oldest = min(
                self._pending_dwell_tracks,
                key=lambda key: self._pending_dwell_tracks[key][1],
            )
            self._pending_dwell_tracks.pop(oldest, None)

    def _awaiting_track_fragment(
        self,
        matches: list[dict],
        track: StreamDwellTrack,
        status: str,
    ) -> bool:
        """Whether one sparse match should wait briefly for its other half."""
        if (
            status != "qualified"
            or not self._profile_reconciliation_enabled
            or len(matches) != 1
        ):
            return False
        enc = matches[0]
        if (
            int(enc.get("profile_fragment_count", 1)) != 1
            or enc.get("plate_attached_to_existing")
        ):
            return False
        if (
            track.plate_key
            and track.plate_key in enc.get("candidate_keys", set())
        ):
            # The provider's completed keyed trajectory and the recognition
            # profile agree exactly. That is sufficient route ownership even
            # when Stream emitted no separate entry recognition fragment.
            return False
        latest_point = self._box_center_normalized(
            enc["vehicle"].vehicle_bbox
        )
        if (
            enc.get("route_entered")
            and self._point_in_zone(latest_point, self._route_board_zone)
        ):
            # This one live profile supplied both endpoint observations. They
            # do not qualify traffic by themselves, but the completed native
            # track can now be applied without waiting for a split profile.
            return False
        box = enc["vehicle"].vehicle_bbox
        return bool(box) and any(
            sample.vehicle_bbox
            and self._overlap_fraction(box, sample.vehicle_bbox) < 0.30
            for sample in track.samples
        )

    def _apply_track_evidence_locked(
        self,
        primary: dict,
        track: StreamDwellTrack,
        status: str,
        reason: str,
        now: float,
    ) -> None:
        fingerprint = _dwell_fingerprint(track)
        points = len(track.samples)
        status_key = "route_shadow_status" if self._route_shadow_only \
            else "route_status"
        prior_status = primary.get(status_key, "pending")
        # The decision below accepts each new classification until qualified,
        # then keeps qualification monotonic. Retain exactly the track whose
        # classification it accepts, even when it has fewer points. A later
        # rejected fragment must not overwrite a qualified proof's geometry.
        if status == "qualified" or prior_status != "qualified":
            primary["trajectory_points"] = points
            primary["dwell_seconds"] = track.dwell_seconds
            primary["route_track_fingerprint"] = fingerprint
            audit_rows = [
                (sample.vehicle_bbox, (
                    track.sample_observed_at[index]
                    if index < len(track.sample_observed_at) else None
                ))
                for index, sample in enumerate(track.samples)
                if sample.vehicle_bbox is not None
            ]
            primary["route_trajectory_bboxes"] = tuple(
                bbox for bbox, _ in audit_rows
            )
            primary["route_trajectory_timestamps"] = tuple(
                observed_at for _, observed_at in audit_rows
            )

        if self._route_shadow_only:
            prior = primary.get("route_shadow_status", "pending")
            if status == "qualified" or prior != "qualified":
                primary["route_shadow_status"] = status
                primary["route_shadow_reason"] = reason
            primary["route_shadow_complete"] = True
            if status == "qualified" and primary.get("identified"):
                primary["vehicle"] = replace(
                    primary["vehicle"],
                    diagnostic_image_jpeg=None,
                    diagnostic_image_bbox=None,
                    diagnostic_image_observed_at=None,
                )
            return

        for key, value, choose_min in (
            ("first_seen_at", track.first_seen_at, True),
            ("last_seen_at", track.last_seen_at, False),
        ):
            if not value:
                continue
            old = primary.get(key)
            old_epoch = _iso_epoch(old)
            new_epoch = _iso_epoch(value)
            if old_epoch is None or new_epoch is None:
                if not old:
                    primary[key] = value
            elif (choose_min and new_epoch < old_epoch) or (
                not choose_min and new_epoch > old_epoch
            ):
                primary[key] = value

        primary["last_seen"] = now
        primary["route_complete"] = True
        if status == "qualified":
            _, _, board_index = self._trajectory_route_evidence(track)
            if board_index is not None:
                primary["route_board_sample_at"] = (
                    track.sample_observed_at[board_index]
                    if board_index < len(track.sample_observed_at) else None
                )
                primary["route_board_sample_bbox"] = (
                    track.samples[board_index].vehicle_bbox
                    if board_index < len(track.samples) else None
                )
            self._qualify_route_locked(
                primary,
                reason,
                self._trajectory_qualified_at(track)
                or track.last_seen_at
                or track.first_seen_at,
            )
            if primary.get("identified"):
                primary["vehicle"] = replace(
                    primary["vehicle"],
                    diagnostic_image_jpeg=None,
                    diagnostic_image_bbox=None,
                    diagnostic_image_observed_at=None,
                )
        elif primary.get("route_status") != "qualified":
            primary["route_status"] = "rejected"
            primary["route_reason"] = reason
            if primary.get("route_rejected_at") is None:
                primary["route_rejected_at"] = now

    def _process_dwell_track_locked(
        self,
        track: StreamDwellTrack,
        now: float,
        *,
        consumed: set[str] | None = None,
        cache_unmatched: bool,
        log_unmatched: bool = True,
    ) -> bool:
        status, reason = self._classify_trajectory(track)
        matches = self._match_dwell_locked(
            track,
            consumed,
            allow_reconciliation=(
                status == "qualified"
                and self._profile_reconciliation_enabled
            ),
        )
        fingerprint = _dwell_fingerprint(track)
        if not matches:
            # Only a physically qualified path is worth retrying. Rejected
            # unmatched cross traffic has no profile to suppress or enrich.
            if cache_unmatched and status == "qualified":
                self._cache_unmatched_dwell_locked(track, now)
                log.info(
                    "VEHICLE_ROUTE track=%s status=cached "
                    "reason=recognition_webhook_pending points=%d",
                    fingerprint[:12], len(track.samples),
                )
            elif log_unmatched:
                log.warning(
                    "VEHICLE_ROUTE track=%s status=ignored "
                    "reason=no_unique_candidate points=%d first=%s last=%s",
                    fingerprint[:12], len(track.samples),
                    track.first_seen_at or "missing",
                    track.last_seen_at or "missing",
                )
            return False

        if cache_unmatched and self._awaiting_track_fragment(
            matches, track, status
        ):
            self._cache_unmatched_dwell_locked(track, now)
            log.info(
                "VEHICLE_ROUTE track=%s status=cached "
                "reason=profile_fragment_pending points=%d",
                fingerprint[:12], len(track.samples),
            )
            return False

        primary = next(
            (enc for enc in matches if enc.get("identified")),
            next(
                (
                    enc for enc in matches
                    if track.plate_key
                    and track.plate_key in enc.get("candidate_keys", set())
                ),
                matches[0],
            ),
        )
        if len(matches) > 1:
            if not self._profile_reconciliation_enabled:
                log.warning(
                    "VEHICLE_ROUTE track=%s status=ignored "
                    "reason=profile_reconciliation_disabled matches=%d",
                    fingerprint[:12], len(matches),
                )
                return False
            for other in list(matches):
                self._merge_encounters_locked(primary, other)

        if consumed is not None:
            consumed.update(enc["encounter_id"] for enc in matches)
            consumed.add(primary["encounter_id"])
        # Keep at most this one short native metadata track on its existing
        # owner. Later completed evidence replaces it: an upper-right exit
        # arriving during grace must cancel the possible exception.
        primary.pop("short_plated_route_candidate", None)
        if status == "rejected" and reason == "terminal_upper_right_exit":
            primary["short_route_exit_veto"] = True
        if (
            status == "rejected" and reason == "trajectory_insufficient_points"
            and len(matches) == 1
            and self._short_plated_route_board_index(track) is not None
            and self._short_plated_route_owner_locked(primary, track, confirmed=False)
        ):
            primary["short_plated_route_candidate"] = track
        self._apply_track_evidence_locked(
            primary, track, status, reason, now
        )
        # A later full frame may have arrived on another result's webhook.
        # Reuse it only at an actual same-camera trajectory sample time. This
        # changes audit evidence only: no new observation, vote, association,
        # route authority, or clock extension is manufactured.
        if not primary.get("identified"):
            evidence = self._audit_frames.latest_on_track(
                track.camera_id, track.sample_observed_at,
                [sample.vehicle_bbox for sample in track.samples],
                primary["vehicle"].diagnostic_image_observed_at, now,
            )
            if evidence is not None:
                frame, box = evidence
                primary["vehicle"] = replace(
                    primary["vehicle"], diagnostic_image_jpeg=frame.jpeg,
                    diagnostic_image_bbox=box,
                    diagnostic_image_observed_at=frame.observed_at,
                )
        log.info(
            "%s track=%s encounter=%s status=%s reason=%s "
            "points=%d dwell=%.1fs match=%s",
            "VEHICLE_ROUTE_SHADOW"
            if self._route_shadow_only else "VEHICLE_ROUTE",
            fingerprint[:12], primary["encounter_id"][:8], status, reason,
            len(track.samples), track.dwell_seconds,
            "multi-profile" if len(matches) > 1 else "single-profile",
        )
        return True

    def _retry_pending_dwell_locked(self, now: float) -> None:
        for fingerprint, (track, expires_at) in list(
            self._pending_dwell_tracks.items()
        ):
            expired = now >= expires_at
            if not expired:
                status, _ = self._classify_trajectory(track)
                matches = self._match_dwell_locked(
                    track,
                    allow_reconciliation=(
                        status == "qualified"
                        and self._profile_reconciliation_enabled
                    ),
                )
                # Do not consume a qualified track after only one sparse hook
                # when its geometry proves another segment is still missing.
                # A second sequential hook may still arrive.
                if self._awaiting_track_fragment(matches, track, status):
                    continue
            matched = self._process_dwell_track_locked(
                track,
                now,
                cache_unmatched=False,
                log_unmatched=False,
            )
            if matched or expired:
                self._pending_dwell_tracks.pop(fingerprint, None)

    def submit_dwell_payload(self, payload: dict) -> None:
        """Resolve completed native Stream tracks into vehicle profiles."""
        if not self._route_enabled:
            return
        tracks = self._parse_dwell_tracks(payload)
        if not tracks:
            log.warning("dwell_time webhook contained no vehicle results")
            return
        now = time.monotonic()
        with self._lock:
            consumed: set[str] = set()
            for track in tracks:
                self._process_dwell_track_locked(
                    track,
                    now,
                    consumed=consumed,
                    cache_unmatched=True,
                )

    @staticmethod
    def _boxes_overlap(a, b) -> bool:
        """Same physical vehicle across frames, approximately."""
        return StreamWebhookEngine._overlap_fraction(a, b) >= 0.3

    @staticmethod
    def _overlap_fraction(a, b) -> float:
        """Intersection divided by the smaller box area."""
        if not a or not b:
            return 0.0
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        ix = min(ax2, bx2) - max(ax1, bx1)
        iy = min(ay2, by2) - max(ay1, by1)
        if ix <= 0 or iy <= 0:
            return 0.0
        inter = ix * iy
        smaller = min(max((ax2 - ax1) * (ay2 - ay1), 1),
                      max((bx2 - bx1) * (by2 - by1), 1))
        return inter / smaller

    def _same_vehicle(self, enc: dict, v: StreamVehicle) -> bool:
        """Join this detection to an open encounter?

        Physical geometry is the key. Make/model/colour are intentionally
        excluded: the same field-reviewed SUV changed make and model between
        provider fragments, while unrelated cars routinely share colour and
        class. MMC enriches the completed profile but never manufactures a
        physical-vehicle association.
        """
        current = enc["vehicle"]
        return self._boxes_overlap(current.vehicle_bbox, v.vehicle_bbox)

    @staticmethod
    def _sanitize_mmc_pair(vehicle: StreamVehicle) -> StreamVehicle:
        """Keep make/model labels and their confidence provenance atomic."""
        if not (vehicle.make and vehicle.model):
            return replace(
                vehicle,
                make=None,
                model=None,
                make_confidence=None,
                model_confidence=None,
            )
        if (
            (vehicle.make_confidence is None)
            != (vehicle.model_confidence is None)
        ):
            return replace(
                vehicle,
                make_confidence=None,
                model_confidence=None,
            )
        return vehicle

    @staticmethod
    def _merge_vehicle(best: StreamVehicle, v: StreamVehicle) -> StreamVehicle:
        """Merge geometry and keep highest-confidence descriptive evidence."""
        best = StreamWebhookEngine._sanitize_mmc_pair(best)
        v = StreamWebhookEngine._sanitize_mmc_pair(v)
        best_jpeg = best.diagnostic_image_jpeg
        best_image_bbox = (
            best.diagnostic_image_bbox
            or (best.vehicle_bbox if best_jpeg is not None else None)
        )
        best_image_observed_at = best.diagnostic_image_observed_at
        # Recognition evaluates every candidate independently. Audit evidence
        # should instead follow the encounter toward its later rear/menu-board
        # view, not freeze on an early frame merely because the whole vehicle
        # had a higher confidence there.
        best_image_epoch = _iso_epoch(best_image_observed_at)
        new_image_epoch = _iso_epoch(v.diagnostic_image_observed_at)
        take_new_image = bool(
            v.diagnostic_image_jpeg is not None
            and (
                best_jpeg is None
                or (
                    new_image_epoch is not None
                    and (
                        best_image_epoch is None
                        or new_image_epoch >= best_image_epoch
                    )
                )
                or (
                    new_image_epoch is None
                    and best_image_epoch is None
                )
            )
        )
        if take_new_image:
            best_jpeg = v.diagnostic_image_jpeg
            best_image_bbox = v.diagnostic_image_bbox or v.vehicle_bbox
            best_image_observed_at = v.diagnostic_image_observed_at

        def score(value) -> float:
            return -1.0 if value is None else float(value)

        # Plate Recognizer scores make/model as one atomic pair. Never build a
        # synthetic pair by choosing a make from one observation and a model
        # from another.
        def pair_score(vehicle: StreamVehicle) -> float:
            if (
                vehicle.make_confidence is None
                or vehicle.model_confidence is None
            ):
                return -1.0
            return min(
                float(vehicle.make_confidence),
                float(vehicle.model_confidence),
            )

        best_has_pair = bool(best.make and best.model)
        new_has_pair = bool(v.make and v.model)
        best_pair_score = pair_score(best)
        new_pair_score = pair_score(v)
        take_new_pair = new_has_pair and (
            not best_has_pair
            or new_pair_score > best_pair_score
        )
        make = v.make if take_new_pair else (best.make if best_has_pair else None)
        model = v.model if take_new_pair else (best.model if best_has_pair else None)
        make_confidence = (
            v.make_confidence
            if take_new_pair
            else (best.make_confidence if best_has_pair else None)
        )
        model_confidence = (
            v.model_confidence
            if take_new_pair
            else (best.model_confidence if best_has_pair else None)
        )

        take_new_color = bool(v.color) and (
            not best.color
            or score(v.color_confidence) > score(best.color_confidence)
        )
        color = v.color if take_new_color else best.color
        color_confidence = (
            v.color_confidence if take_new_color else best.color_confidence
        )

        take_new_type = bool(v.vehicle_type) and (
            not best.vehicle_type
            or v.vehicle_confidence > best.vehicle_confidence
        )
        return StreamVehicle(
            vehicle_bbox=v.vehicle_bbox or best.vehicle_bbox,
            vehicle_confidence=max(v.vehicle_confidence, best.vehicle_confidence),
            vehicle_type=(v.vehicle_type if take_new_type else best.vehicle_type),
            make=make,
            model=model,
            color=color,
            make_confidence=make_confidence,
            model_confidence=model_confidence,
            color_confidence=color_confidence,
            diagnostic_image_jpeg=best_jpeg,
            diagnostic_image_bbox=best_image_bbox,
            diagnostic_image_observed_at=best_image_observed_at,
        )

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")

    def _hold_seconds(self, enc: dict) -> float:
        if enc.get("identified"):
            return self._identified_hold_seconds
        return self._unidentified_resolution_seconds

    def _prune_terminal_tombstones_locked(self, now: float) -> None:
        self._terminal_tombstones = [
            tomb for tomb in self._terminal_tombstones
            if now < float(tomb.get("expires_at") or 0.0)
        ]

    def _remember_terminal_locked(self, enc: dict, now: float) -> None:
        # A shadow classifier must be observably read-only. Tombstones suppress
        # later profiles, so they belong only to authoritative route mode.
        if (
            not self._route_enabled
            or self._route_shadow_only
            or self._terminal_tombstone_seconds <= 0.0
        ):
            return
        if enc.get("route_status") != "qualified":
            return
        self._prune_terminal_tombstones_locked(now)
        self._terminal_tombstones = [
            tomb for tomb in self._terminal_tombstones
            if tomb.get("encounter_id") != enc.get("encounter_id")
        ]
        tomb = dict(enc)
        tomb["vehicle"] = replace(
            enc["vehicle"],
            diagnostic_image_jpeg=None,
            diagnostic_image_bbox=None,
            diagnostic_image_observed_at=None,
        )
        # Plate correlation candidates are unnecessary for a plateless
        # departure and must not be retained longer than the live encounter.
        tomb["candidate_keys"] = set()
        tomb["confirmed_candidate_key"] = None
        tomb["terminal"] = True
        tomb["terminal_at"] = now
        tomb["expires_at"] = now + self._terminal_tombstone_seconds
        tomb["departure_claimed_at"] = None
        tomb["departure_claim_until"] = None
        self._terminal_tombstones.append(tomb)
        if len(self._terminal_tombstones) > 64:
            self._terminal_tombstones.sort(
                key=lambda item: float(item.get("expires_at") or 0.0)
            )
            del self._terminal_tombstones[:-64]

    def _match_terminal_tombstone_locked(
        self,
        v: StreamVehicle,
        now: float,
        *,
        plated_attempt: bool,
        candidate_key: str | None,
        source_batch_id: str | None,
    ) -> dict | None:
        if (
            not self._route_enabled
            or self._route_shadow_only
            or plated_attempt
            or candidate_key
            or self._terminal_tombstone_seconds <= 0.0
        ):
            return None
        point = self._box_center_normalized(v.vehicle_bbox)
        if (
            point is None
            or self._point_in_zone(point, self._route_entry_zone)
            or not self._point_in_zone(point, self._route_board_zone)
        ):
            return None
        # A car that has newly entered the lane owns any forthcoming board
        # observation more plausibly than an old anonymous tombstone does.
        # Fail open to a new sparse profile so its completed trajectory can
        # reconcile it; never give that observation to the previous car.
        fresh_route_owners = [
            enc for enc in self._encounters
            if enc.get("route_entered")
            and (now - float(enc.get("last_seen") or 0.0))
            < self._route_decision_timeout_seconds
        ]
        if fresh_route_owners:
            log.info(
                "terminal departure not absorbed: %d fresh route owner(s)",
                len(fresh_route_owners),
            )
            return None
        self._prune_terminal_tombstones_locked(now)
        claimed = [
            tomb for tomb in self._terminal_tombstones
            if not source_batch_id
            or tomb.get("last_source_batch_id") != source_batch_id
            if tomb.get("departure_claimed_at") is not None
            and now < float(tomb.get("departure_claim_until") or 0.0)
            if self._overlap_fraction(
                tomb["vehicle"].vehicle_bbox, v.vehicle_bbox
            ) >= 0.70
        ]
        unclaimed = [
            tomb for tomb in self._terminal_tombstones
            if not source_batch_id
            or tomb.get("last_source_batch_id") != source_batch_id
            if tomb.get("departure_claimed_at") is None
            and self._overlap_fraction(
                tomb["vehicle"].vehicle_bbox, v.vehicle_bbox
            ) >= 0.70
        ]
        # Once claimed, this tombstone owns only one short, continuous
        # departure episode. It wins over generic unclaimed tombstones but
        # expires after one quiet window and can never absorb the next car.
        matches = claimed if claimed else unclaimed
        if len(matches) != 1:
            if len(matches) > 1:
                log.warning(
                    "terminal vehicle departure ambiguous across %d profiles",
                    len(matches),
                )
            return None
        tomb = matches[0]
        if tomb.get("departure_claimed_at") is None:
            tomb["departure_claimed_at"] = now
            tomb["departure_claim_until"] = now + self._vehicle_quiet_seconds
            tomb["expires_at"] = min(
                float(tomb.get("expires_at") or now),
                float(tomb["departure_claim_until"]),
            )
        log.info(
            "terminal vehicle departure absorbed encounter=%s age=%.1fs",
            tomb["encounter_id"][:8], now - float(tomb["terminal_at"]),
        )
        return tomb

    def _match_encounter(self, v: StreamVehicle, now: float,
                         plated_attempt: bool = False,
                         candidate_key: str | None = None,
                         source_batch_id: str | None = None,
                         observed_at: str | None = None):
        active = [
            enc for enc in self._encounters
            if not (
                source_batch_id is not None
                and enc.get("last_source_batch_id") == source_batch_id
            )
            and (now - enc["last_seen"]) < self._hold_seconds(enc)
        ]

        def unique(matches: list[dict], reason: str) -> dict | None:
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                log.warning(
                    "vehicle encounter %s ambiguous across %d profiles; "
                    "keeping encounters separate",
                    reason, len(matches),
                )
            return None

        is_plate_observation = bool(plated_attempt or candidate_key)
        if is_plate_observation:
            # Canonical identity evidence always outranks geometry. An
            # identified profile may accept only its detector-confirmed key,
            # never another provisional variant that happened to be staged on
            # the same box before settlement.
            confirmed_exact = []
            if candidate_key:
                confirmed_exact = [
                    enc for enc in active
                    if enc.get("identified")
                    and enc.get("confirmed_candidate_key") == candidate_key
                ]
            if confirmed_exact:
                return unique(confirmed_exact, "confirmed exact candidate")

            provisional_exact = []
            if candidate_key:
                provisional_exact = [
                    enc for enc in active
                    if not enc.get("identified")
                    and candidate_key in enc.get("candidate_keys", set())
                    and (now - enc["last_seen"])
                    < self._vehicle_quiet_seconds
                    and self._same_vehicle(enc, v)
                ]
            if provisional_exact:
                return unique(
                    provisional_exact, "provisional exact candidate"
                )

            # Recognition and dwell are separate provider requests. Preserve
            # one narrowly proven ordering race: an unkeyed completed track may
            # be followed immediately by its first keyed BOARD observation.
            # This is native trajectory endpoint ownership, not a generic
            # geometry merge. Provider sample timestamps must agree within one
            # second, the endpoint boxes must overlap, and the owner must be
            # unique. A follower arriving three seconds later therefore cannot
            # acquire the lead car's completed route.
            observed_epoch = _iso_epoch(observed_at)
            completed_endpoint = [
                enc for enc in active
                if candidate_key
                and not enc.get("identified")
                and not enc.get("saw_plate")
                and enc.get("route_status") == "qualified"
                and enc.get("route_complete")
                and observed_epoch is not None
                and _iso_epoch(enc.get("route_board_sample_at")) is not None
                and abs(
                    observed_epoch
                    - _iso_epoch(enc.get("route_board_sample_at"))
                ) <= MAX_TRAJECTORY_SAMPLE_SKEW_SECONDS
                and self._overlap_fraction(
                    enc.get("route_board_sample_bbox"), v.vehicle_bbox
                ) >= 0.30
            ]
            if completed_endpoint:
                return unique(completed_endpoint, "completed route endpoint")

            # OCR variants from one still-unsettled plated profile must remain
            # together so detector.py can compare support. Never use geometry
            # to place a first plate on a purely plateless profile, and never
            # place a different plate on an already identified profile.
            variants = [
                enc for enc in active
                if not enc.get("identified")
                and enc.get("saw_plate")
                and (
                    stage := self._identity_stages.get(
                        enc.get("encounter_id")
                    )
                ) is not None
                and (now - stage["first_seen"])
                <= self._identity_settle_max_seconds
                and self._same_vehicle(enc, v)
            ]
            if variants:
                return unique(variants, "unsettled plate geometry")
        else:
            # Candidate-free geometry can continue only one uninterrupted
            # episode. It cannot resume after quiet, and an identified profile
            # whose route is complete is immutable; its later departure is
            # handled by the narrow terminal mechanism rather than a five-minute
            # geometry hold.
            geometry = [
                enc for enc in active
                if (now - enc["last_seen"]) < self._vehicle_quiet_seconds
                and not (
                    enc.get("identified")
                    and (
                        enc.get("route_complete")
                        or (
                            self._route_enabled
                            and not self._route_shadow_only
                        )
                    )
                )
                and self._same_vehicle(enc, v)
            ]
            if geometry:
                return unique(geometry, "candidate-free geometry")

        return self._match_terminal_tombstone_locked(
            v,
            now,
            plated_attempt=plated_attempt,
            candidate_key=candidate_key,
            source_batch_id=source_batch_id,
        )

    def has_fresh_vehicle(self, now: float | None = None,
                          within: float | None = None,
                          encounter_id: str | None = None) -> bool:
        """True while Stream is still posting this car (plate or not).

        Miss episodes stay open for that window so a weak-det first look
        can still confirm if a later sampled frame clears the gates.
        Uses last webhook time, not the 5-minute identified hold — a
        confirmed Camry sitting in hold must not freeze every miss on site.
        """
        now = time.monotonic() if now is None else now
        within = (
            self._vehicle_quiet_seconds if within is None else within
        )
        with self._lock:
            encounters = self._encounters
            if encounter_id:
                canonical = self._canonical_id_locked(encounter_id)
                encounters = [
                    enc for enc in encounters
                    if enc.get("encounter_id") == canonical
                ]
            return any(
                (now - enc["last_seen"]) < within for enc in encounters
            )

    # ---------- used by the request handler ----------

    @staticmethod
    def _without_image(read: RawPlateRead) -> RawPlateRead:
        return read if read.image is None else replace(read, image=None)

    def _stage_read(self, stage: dict, read: RawPlateRead) -> None:
        """Append metadata while retaining at most one JPEG per text variant."""
        key = self.normalize_candidate(read.text)
        previous_best = stage["best_image_by_key"].get(key)
        quality = (
            min(read.detection_confidence, read.ocr_confidence),
            read.ocr_confidence,
            read.detection_confidence,
        )
        if read.image is None or (
            previous_best is not None and quality <= previous_best[0]
        ):
            stage["reads"].append(self._without_image(read))
            return

        # A decoded 1456x1088 frame is several MiB. At 10 eFPS, retaining all
        # images for a 4s settlement would create a needless memory spike.
        # Keep every score observation but only the best frame for each
        # normalized variant.
        prior_index = previous_best[1] if previous_best is not None else None
        if prior_index is not None:
            stage["reads"][prior_index] = self._without_image(
                stage["reads"][prior_index]
            )
        stage["reads"].append(read)
        stage["best_image_by_key"][key] = (
            quality, len(stage["reads"]) - 1
        )
        if len(stage["best_image_by_key"]) > MAX_IDENTITY_STAGE_IMAGES:
            weakest_key, (_, weakest_index) = min(
                stage["best_image_by_key"].items(),
                key=lambda item: item[1][0],
            )
            stage["reads"][weakest_index] = self._without_image(
                stage["reads"][weakest_index]
            )
            stage["best_image_by_key"].pop(weakest_key, None)

    def _candidate_rank(self, reads: list[RawPlateRead]) -> tuple:
        """Evidence rank for one exact normalized candidate, strongest first."""
        # Multiple result rows from one provider event can describe one source
        # frame (notably plates_per_vehicle > 1). They are useful alternative
        # candidates, but repetition inside that event is not independent OCR
        # evidence. Legacy/manual callers without an observation ID retain the
        # historical one-read-is-one-observation behavior.
        independent: list[RawPlateRead] = []
        best_by_observation: dict[str, RawPlateRead] = {}
        for read in reads:
            observation = read.source_observation_id
            if observation is None:
                independent.append(read)
                continue
            incumbent = best_by_observation.get(observation)
            if incumbent is None or (
                min(read.detection_confidence, read.ocr_confidence),
                read.ocr_confidence,
                read.detection_confidence,
            ) > (
                min(incumbent.detection_confidence, incumbent.ocr_confidence),
                incumbent.ocr_confidence,
                incumbent.detection_confidence,
            ):
                best_by_observation[observation] = read
        independent.extend(best_by_observation.values())

        vote_min = int(getattr(
            getattr(self.cfg, "thresholds", None), "vote_min_frames", 2
        ))
        high_single = float(getattr(
            getattr(self.cfg, "thresholds", None),
            "high_ocr_single_frame", 0.99,
        ))
        rescue = getattr(getattr(self.cfg, "alpr", None), "thresholds", None)
        rescue_n = int(getattr(rescue, "consensus_rescue_min_frames", 0))
        rescue_det = float(getattr(
            rescue, "consensus_rescue_detect_min", 0.0
        ))
        rescue_ocr = float(getattr(
            rescue, "consensus_rescue_ocr_min", 0.0
        ))

        normal = [
            r for r in independent
            if r.detection_confidence >= self.det_min
            and r.ocr_confidence >= self.ocr_min
        ]
        rescue_reads = [
            r for r in independent
            if r.detection_confidence >= rescue_det
            and r.ocr_confidence >= rescue_ocr
            and ((r.detection_confidence >= self.det_min)
                 != (r.ocr_confidence >= self.ocr_min))
        ]
        confirmable = (
            len(normal) >= vote_min
            or any(r.ocr_confidence >= high_single for r in normal)
            or (rescue_n > 0 and len(rescue_reads) >= rescue_n)
        )
        best_joint = max(
            (
                min(r.detection_confidence, r.ocr_confidence)
                for r in independent
            ),
            default=0.0,
        )
        return (
            int(confirmable), len(normal), len(rescue_reads), len(independent),
            best_joint,
        )

    def _select_encounter_reads(
        self, reads: list[RawPlateRead]
    ) -> list[RawPlateRead]:
        """Choose one canonical plate candidate for one physical vehicle.

        Exact agreement is primary. When two independently confirmable
        candidates are substring variants, prefer the fuller string: this is
        the measured failure where a 3--4 character fragment arrived before
        the complete 6--7 character result. Unrelated candidates do not get
        this length preference; their actual agreement/confidence wins.
        """
        groups: dict[str, list[RawPlateRead]] = {}
        invalid: list[RawPlateRead] = []
        for read in reads:
            key = self.normalize_candidate(read.text)
            if key is None:
                invalid.append(read)
            else:
                groups.setdefault(key, []).append(read)
        if not groups:
            return invalid

        ranks = {key: self._candidate_rank(group)
                 for key, group in groups.items()}
        winner = max(groups, key=lambda key: (ranks[key], len(key)))

        # Length only breaks a real first-fragment race when both variants
        # already satisfy an accepted confirmation path and the fuller one is
        # at least as well supported. That last condition protects genuine
        # 3-character vanity plates from a one-off appended OCR character.
        if ranks[winner][0]:
            expansions = [
                key for key in groups
                if (
                    ranks[key][0]
                    and winner in key
                    and len(key) > len(winner)
                    and ranks[key][1] >= ranks[winner][1]
                    and ranks[key][3] >= ranks[winner][3]
                )
            ]
            if expansions:
                winner = max(
                    expansions,
                    key=lambda key: (len(key), ranks[key]),
                )

        discarded = len(reads) - len(groups[winner])
        if discarded:
            log.info(
                "vehicle identity settled: kept %d exact read(s), "
                "discarded %d competing read(s) across %d variant(s)",
                len(groups[winner]), discarded, len(groups) - 1,
            )
        had_competing_variants = len(groups) > 1
        return [
            replace(
                read,
                had_competing_variants=had_competing_variants,
            )
            for read in groups[winner]
        ]

    def submit(self, reads: list[RawPlateRead],
               now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            for read in reads:
                if not read.encounter_id:
                    self._pending.append(read)
                    continue
                canonical_id = self._canonical_id_locked(read.encounter_id)
                if canonical_id != read.encounter_id:
                    read = replace(read, encounter_id=canonical_id)
                stage = self._identity_stages.get(canonical_id)
                if stage is None:
                    stage = {
                        "first_seen": now,
                        "last_seen": now,
                        "reads": [],
                        "best_image_by_key": {},
                    }
                    self._identity_stages[canonical_id] = stage
                else:
                    stage["last_seen"] = now
                self._stage_read(stage, read)

    def submit_vehicles(self, vehicles: list[StreamVehicle],
                        now: float | None = None,
                        identified: bool = False,
                        plated_attempt: bool = False,
                        saw_plate: bool = False,
                        candidate_key: str | None = None,
                        plate_detection_confidence: float | None = None,
                        plate_ocr_confidence: float | None = None,
                        observed_at: str | None = None,
                        source_batch_id: str | None = None) -> list[str]:
        """Fold vehicle detections into open encounters.

        A detection joins an existing encounter through exact candidate
        continuity, one uninterrupted geometry episode, or completed native
        trajectory proof; MMC never owns the association. The encounter retains
        the highest-confidence descriptive
        make/model pair and colour as those resolve. `identified=True` means a
        gate-clearing plate owned this box; drain_vehicles will not emit it.
        `plated_attempt=True` is a below-gate plate: after quiet, do not
        join a prior identified hold.
        """
        now = time.monotonic() if now is None else now
        if source_batch_id is None and len(vehicles) > 1:
            # The public batch API has the same simultaneity guarantee as the
            # HTTP handler: two result entries from one provider frame cannot
            # consume the same encounter merely because this loop is ordered.
            source_batch_id = uuid.uuid4().hex
        encounter_ids = []
        with self._lock:
            for v in vehicles:
                v = self._sanitize_mmc_pair(v)
                observation_time = observed_at or self._now_iso()
                if (
                    v.diagnostic_image_jpeg is not None
                    and v.diagnostic_image_observed_at is None
                ):
                    v = replace(
                        v,
                        diagnostic_image_observed_at=observation_time,
                    )
                match = self._match_encounter(
                    v, now, plated_attempt=plated_attempt,
                    candidate_key=candidate_key,
                    source_batch_id=source_batch_id,
                    observed_at=observation_time,
                )
                if match is None:
                    match = {
                        "encounter_id": uuid.uuid4().hex,
                        "vehicle": v,
                        "last_seen": now,
                        "first_seen": now,
                        "first_seen_at": observation_time,
                        "last_seen_at": observation_time,
                        "identified": identified,
                        "confirmed_candidate_key": (
                            candidate_key if identified else None
                        ),
                        "saw_plate": saw_plate,
                        "plate_attached_to_existing": False,
                        "candidate_keys": (
                            {candidate_key} if candidate_key else set()
                        ),
                        "diagnostic_episode_id": None,
                        "plate_detection_confidence": plate_detection_confidence,
                        "plate_ocr_confidence": plate_ocr_confidence,
                        "route_status": "pending",
                        "route_reason": None,
                        "route_qualified_at": None,
                        "route_rejected_at": None,
                        "route_shadow_status": "pending",
                        "route_shadow_reason": None,
                        "route_shadow_complete": False,
                        "route_shadow_visit_emitted": False,
                        "route_diagnostic_emitted": False,
                        "route_entered": False,
                        "route_observations": 0,
                        "route_complete": False,
                        "route_board_sample_at": None,
                        "route_board_sample_bbox": None,
                        "route_track_fingerprint": None,
                        "trajectory_points": 0,
                        "dwell_seconds": None,
                        "profile_fragment_count": 1,
                        "last_source_batch_id": source_batch_id,
                    }
                    self._encounters.append(match)
                else:
                    match["last_seen"] = now
                    match["last_source_batch_id"] = source_batch_id
                    observed_epoch = _iso_epoch(observation_time)
                    first_epoch = _iso_epoch(match.get("first_seen_at"))
                    last_epoch = _iso_epoch(match.get("last_seen_at"))
                    if (
                        first_epoch is None
                        or (observed_epoch is not None
                            and observed_epoch < first_epoch)
                    ):
                        match["first_seen_at"] = observation_time
                    if (
                        last_epoch is None
                        or (observed_epoch is not None
                            and observed_epoch > last_epoch)
                    ):
                        match["last_seen_at"] = observation_time
                    if identified:
                        match["identified"] = True
                        if candidate_key:
                            match["confirmed_candidate_key"] = candidate_key
                    if saw_plate and not match.get("saw_plate"):
                        match["plate_attached_to_existing"] = True
                    if saw_plate:
                        match["saw_plate"] = True
                    if candidate_key:
                        match.setdefault("candidate_keys", set()).add(
                            candidate_key
                        )
                    if plate_detection_confidence is not None:
                        match["plate_detection_confidence"] = max(
                            float(match.get("plate_detection_confidence") or 0.0),
                            float(plate_detection_confidence),
                        )
                    if plate_ocr_confidence is not None:
                        match["plate_ocr_confidence"] = max(
                            float(match.get("plate_ocr_confidence") or 0.0),
                            float(plate_ocr_confidence),
                        )
                    match["vehicle"] = self._merge_vehicle(match["vehicle"], v)
                # Keep one atomic plate/vehicle/time observation, not the
                # independent encounter score maxima, for the narrow route
                # exception. Detector confirmation remains separately required.
                if (
                    self._short_plated_route_enabled and candidate_key
                    and plate_detection_confidence is not None
                    and plate_ocr_confidence is not None
                    and math.isfinite(float(plate_detection_confidence))
                    and math.isfinite(float(plate_ocr_confidence))
                    and float(plate_detection_confidence) >= max(0.70, self.det_min)
                    and float(plate_ocr_confidence) >= max(0.98, self.ocr_min)
                ):
                    match["short_route_plate_evidence"] = (
                        candidate_key, float(plate_detection_confidence),
                        float(plate_ocr_confidence), v.vehicle_bbox, observation_time,
                    )
                self._update_route_observation_locked(
                    match,
                    v.vehicle_bbox,
                    observation_time,
                    plated_attempt=plated_attempt,
                    candidate_key=candidate_key,
                    now=now,
                )
                encounter_ids.append(match["encounter_id"])
                self._vehicle_observations.append(replace(
                    match["vehicle"],
                    encounter_id=match["encounter_id"],
                    first_seen_at=match["first_seen_at"],
                    last_seen_at=match["last_seen_at"],
                    saw_plate=bool(match.get("saw_plate")),
                    plate_detection_confidence=match.get(
                        "plate_detection_confidence"
                    ),
                    plate_ocr_confidence=match.get("plate_ocr_confidence"),
                    # Presence/board tracking consumes metadata immediately;
                    # retaining up to 512 original JPEGs here would be an
                    # avoidable memory leak. The encounter keeps one best.
                    diagnostic_image_jpeg=None,
                    diagnostic_image_bbox=None,
                    diagnostic_image_observed_at=None,
                ))
                # If the main loop stalls, retain recent geometry without
                # allowing a webhook burst to grow memory without bound.
                if len(self._vehicle_observations) > 512:
                    del self._vehicle_observations[:-512]
            self._retry_pending_dwell_locked(now)
            encounter_ids = [
                self._canonical_id_locked(encounter_id) or encounter_id
                for encounter_id in encounter_ids
            ]
        return encounter_ids

    def observe_vehicle(self, vehicle: StreamVehicle, **kwargs) -> str:
        """Fold one Stream result into its physical vehicle encounter."""
        return self.submit_vehicles([vehicle], **kwargs)[0]

    def normalize_candidate(self, text: str) -> str | None:
        """Use the canonical on-device representation for correlation only."""
        th = getattr(self.cfg, "thresholds", None)
        return normalize(
            text,
            min_length=int(getattr(th, "min_normalized_length", 3)),
            max_length=int(getattr(th, "max_normalized_length", 12)),
        )

    def mark_encounter_identified(
        self,
        encounter_id: str | None,
        confirmed_candidate_key: str | None = None,
    ) -> dict | None:
        """Mark only the encounter whose detector vote truly confirmed."""
        if not encounter_id:
            return None
        with self._lock:
            enc = self._encounter_locked(encounter_id)
            if enc is not None:
                enc["identified"] = True
                canonical_key = (
                    self.normalize_candidate(confirmed_candidate_key)
                    if confirmed_candidate_key else None
                )
                enc["confirmed_candidate_key"] = canonical_key
                enc["short_route_detector_confirmed"] = bool(canonical_key)
                if canonical_key:
                    enc.setdefault("candidate_keys", set()).add(canonical_key)
                self._maybe_qualify_short_plated_route_locked(enc, time.monotonic())
                # A route-gated identified vehicle remains provisional until
                # its completed path qualifies or rejects. Keep one compressed
                # original only while that decision is unresolved so rejected
                # cross traffic remains locally auditable without becoming a
                # Visit. Qualified and non-route sites release it immediately.
                effective_route_status = (
                    enc.get("route_shadow_status", "pending")
                    if self._route_shadow_only
                    else enc.get("route_status", "pending")
                )
                if (
                    not self._route_enabled
                    or effective_route_status == "qualified"
                ):
                    enc["vehicle"] = replace(
                        enc["vehicle"],
                        diagnostic_image_jpeg=None,
                        diagnostic_image_bbox=None,
                        diagnostic_image_observed_at=None,
                    )
                vehicle = enc["vehicle"]
                return {
                    "encounter_id": enc["encounter_id"],
                    "first_seen_at": enc.get("first_seen_at"),
                    "last_seen_at": enc.get("last_seen_at"),
                    "color": vehicle.color,
                    "make": vehicle.make,
                    "model": vehicle.model,
                    "vehicle_type": vehicle.vehicle_type,
                    "color_confidence": vehicle.color_confidence,
                    "make_confidence": vehicle.make_confidence,
                    "model_confidence": vehicle.model_confidence,
                    "route_status": enc.get("route_status", "pending"),
                    "route_reason": enc.get("route_reason"),
                    "route_qualified_at": enc.get("route_qualified_at"),
                    "route_track_fingerprint": enc.get(
                        "route_track_fingerprint"
                    ),
                    "trajectory_points": int(
                        enc.get("trajectory_points", 0)
                    ),
                    "dwell_seconds": enc.get("dwell_seconds"),
                    "terminal_visit_emitted": bool(
                        enc.get("route_shadow_visit_emitted")
                    ),
                }
        return None

    def note_detection_event(self, encounter_id: str | None,
                             episode_id: str | None) -> None:
        """Link diagnostic evidence to its vehicle without counting it."""
        if not encounter_id:
            return
        with self._lock:
            enc = self._encounter_locked(encounter_id)
            if enc is not None:
                enc["saw_plate"] = True
                if episode_id:
                    enc["diagnostic_episode_id"] = episode_id

    def note_plate_read(self, now: float | None = None,
                        vehicles: list[StreamVehicle] | None = None) -> None:
        """A gate-clearing plate: this car is the identity path's.

        `vehicles` carries the box + MMC from those reads so later weak
        frames of the same truck join this encounter. Without them (legacy
        callers, or a plate with no vehicle box) we still stamp
        `_last_plate_read_at` so a concurrent plateless encounter last seen
        before the plate is not also emitted.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            self._last_plate_read_at = now
        if vehicles:
            self.submit_vehicles(vehicles, now=now, identified=True)
