import json
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
events = dynamodb.Table(os.environ.get("EVENTS_TABLE", "DetectionEvents"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "90"))

VALID_OUTCOMES = {
    "no_plate_detected",
    "low_confidence",
    "vote_disagreement",
    "group_confidence_floor",
    "below_threshold",
    "unreadable",
    "drive_off",
    "unknown",
}


def handler(event, context):
    auth_ctx = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("lambda", {})
    )
    location_id = auth_ctx.get("locationId") or auth_ctx.get("location_id")
    if not isinstance(location_id, str) or not (1 <= len(location_id) <= 128):
        return resp(403, {"error": "authenticated device is missing location scope"})

    try:
        body = json.loads(event.get("body") or "{}", parse_float=Decimal)
    except Exception:
        return resp(400, {"error": "invalid json"})
    if not isinstance(body, dict):
        return resp(400, {"error": "json body must be an object"})

    now = datetime.now(timezone.utc)
    timestamp = _bounded_text(
        body.get("detectedAt"),
        64,
        default=now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    )

    outcome = body.get("outcome", "unknown")
    if not isinstance(outcome, str) or outcome not in VALID_OUTCOMES:
        return resp(400, {"error": f"unknown outcome: {outcome}"})

    item = {
        "location_id": location_id,
        "timestamp": timestamp,
        "outcome": outcome,
        "frame_count": _bounded_int(body.get("frameCount")),
        "top_detection_confidence": _confidence(
            body.get("topDetectionConfidence")
        ),
        "top_ocr_confidence": _confidence(body.get("topOcrConfidence")),
        "inference_duration_ms": _bounded_int(
            body.get("inferenceDurationMs"), maximum=86_400_000
        ),
        "sw_version": _bounded_text(body.get("swVersion"), 64, default=""),
        "expires_at": int((now + timedelta(days=RETENTION_DAYS)).timestamp()),
    }

    # Paired best-frame telemetry from one real frame. All optional fields are
    # explicitly named, bounded, and type-normalized; never persist arbitrary
    # maps supplied by a device.
    item.update({
        "best_joint_detection_confidence": _confidence(
            body.get("bestJointDet")
        ),
        "best_joint_ocr_confidence": _confidence(body.get("bestJointOcr")),
        "best_joint_cleared_detection": _bool_or_none(
            body.get("bestJointClearedDet")
        ),
        "frames_clearing_detection": _bounded_int(
            body.get("framesClearingDet")
        ),
        "frames_clearing_ocr": _bounded_int(body.get("framesClearingOcr")),
        "frames_clearing_both": _bounded_int(body.get("framesClearingBoth")),
        "reads_in_episode": _bounded_int(body.get("readsInEpisode")),
        "frames_admitted": _bounded_int(body.get("framesAdmitted")),
        "max_bucket_size": _bounded_int(body.get("maxBucketSize")),
        "episode_id": _episode_id(body.get("episodeId")),
        "encounter_id": _episode_id(body.get("encounterId")),
        "episode_start": _bounded_text(body.get("episodeStart"), 64),
        "episode_end": _bounded_text(body.get("episodeEnd"), 64),
    })

    reject_counts = body.get("rejectReasonCounts")
    if isinstance(reject_counts, dict):
        sanitized_counts = {
            name: count
            for name in ("failed_det_only", "failed_ocr_only", "failed_both")
            if (count := _bounded_int(reject_counts.get(name))) is not None
        }
        if sanitized_counts:
            item["reject_reason_counts"] = sanitized_counts

    # Only a terminal vehicle track is allowed to carry vehicle-level evidence.
    # group_confidence_floor remains a recognition diagnostic and therefore
    # cannot become a countable vehicle or visit in this handler.
    if outcome == "drive_off":
        item["duration_seconds"] = _nonnegative_decimal(
            body.get("durationSeconds"), maximum=86_400
        )
        item["had_plate_candidate"] = _bool_or_none(
            body.get("hadPlateCandidate")
        )
        item["dominant_color"] = _bounded_text(body.get("dominantColor"), 32)
        item["split_from_color"] = _bool_or_none(body.get("splitFromColor"))

    item = {key: value for key, value in item.items() if value is not None}

    events.put_item(Item=item)

    return resp(200, {"ok": True})


def _bounded_int(value, minimum=0, maximum=1_000_000):
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if minimum <= parsed <= maximum else None


def _nonnegative_decimal(value, maximum):
    if isinstance(value, bool):
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0 or parsed > maximum:
        return None
    return parsed


def _confidence(value):
    return _nonnegative_decimal(value, maximum=1)


def _bool_or_none(value):
    return value if isinstance(value, bool) else None


def _bounded_text(value, maximum, default=None):
    if not isinstance(value, str) or not value or len(value) > maximum:
        return default
    return value


def _episode_id(value):
    if not isinstance(value, str) or len(value) != 32:
        return None
    try:
        int(value, 16)
    except ValueError:
        return None
    return value.lower()


def resp(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }
