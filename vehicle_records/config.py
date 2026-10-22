"""Configuration for the vehicle-record layer.

Deliberately separate from plate_reader.config: that one owns the thresholds,
voting and hashing rules, which this architecture does NOT change. This owns
only what is new — when a record commits, and which descriptive classifiers
are expected to contribute.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from enum import Enum
from typing import Any


def _strict_bool(value: Any, path: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{path} must be true or false, got {value!r}")
    return value


def _number(value: Any, path: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{path} must be numeric, got {value!r}")
    return float(value)


def _integer(value: Any, path: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{path} must be an integer, got {value!r}")
    return value


class SendTrigger(str, Enum):
    """When the record is finalized, transmitted, and frozen.

    ZONE_EXIT is the default, and the reason is the interaction with plate
    confirmation. A plate does not confirm until several agreeing frames have
    accumulated (vote_min_frames, inside vote_window_seconds). A trigger that
    fires earlier commits the record before the plate has had its chance to
    resolve, and the car is sent as plateless when it was about to be
    identified a moment later — a self-inflicted miss that looks exactly like
    a model failure in the metrics.

    The earlier triggers stay available because some deployments need the
    record sooner (live board signalling), but they trade identification rate
    for latency and that trade should be deliberate.
    """

    INSTANT = "instant"            # first frame the vehicle is detected
    ZONE_ENTRY = "zone_entry"      # vehicle enters the dwell zone
    ZONE_EXIT = "zone_exit"        # vehicle leaves the dwell zone  (default)
    FRAME_EXIT = "frame_exit"      # vehicle leaves frame entirely


@dataclass
class VehicleRecordConfig:
    send_trigger: SendTrigger = SendTrigger.ZONE_EXIT

    # Seconds a vehicle must be absent before its track is considered closed.
    # Mirrors presence.track_timeout_seconds; kept here so this layer can be
    # driven standalone without a full production config.
    track_timeout_seconds: float = 3.0

    # Frames a vehicle must be seen for before it earns a record at all.
    # Suppresses single-frame detector blips.
    min_track_frames: int = 3

    # Vehicle-box association: two boxes in consecutive frames are the same
    # vehicle if their IoU clears this. The SDK car box is far steadier than
    # the plate box, which is why vehicle-keyed tracking fragments less than
    # the plate-keyed tracking it replaces.
    iou_match_threshold: float = 0.3

    # App-side floor on the SDK's own car-detection confidence. 0.0 means
    # "trust the engine", which already gates internally at
    # car_noplate_detect_min_score.
    min_vehicle_confidence: float = 0.0

    # Which descriptive classifiers we EXPECT to contribute. A field whose
    # classifier is disabled stays null; this is declared rather than
    # inferred so a null can be read as "not enabled" instead of "enabled but
    # never produced a reading", which are different facts.
    color_enabled: bool = True
    make_model_enabled: bool = False       # VMMR: ~268ms/frame, over budget

    # Emit records for vehicles that never confirmed a plate. Required for the
    # throughput / identification-rate KPI: a unit that only reports what it
    # identified cannot report what it missed.
    emit_plateless: bool = True

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "VehicleRecordConfig":
        vr = raw.get("vehicle_records", raw) or {}
        if not isinstance(vr, dict):
            raise ValueError("vehicle_records must be an object")
        allowed = {
            "send_trigger", "track_timeout_seconds", "min_track_frames",
            "iou_match_threshold", "min_vehicle_confidence",
            "color_enabled", "make_model_enabled", "emit_plateless",
        }
        unknown = sorted(set(vr) - allowed)
        if unknown:
            raise ValueError(
                f"vehicle_records has unknown keys: {', '.join(unknown)}"
            )

        trigger_value = vr.get("send_trigger", "zone_exit")
        if not isinstance(trigger_value, str):
            raise ValueError("vehicle_records.send_trigger must be a string")
        trig = trigger_value.lower()
        valid = {t.value for t in SendTrigger}
        if trig not in valid:
            raise ValueError(
                f"vehicle_records.send_trigger must be one of "
                f"{sorted(valid)}, got {trig!r}")
        timeout = _number(
            vr.get("track_timeout_seconds", 3.0),
            "vehicle_records.track_timeout_seconds",
        )
        min_frames = _integer(
            vr.get("min_track_frames", 3),
            "vehicle_records.min_track_frames",
        )
        iou_threshold = _number(
            vr.get("iou_match_threshold", 0.3),
            "vehicle_records.iou_match_threshold",
        )
        vehicle_floor = _number(
            vr.get("min_vehicle_confidence", 0.0),
            "vehicle_records.min_vehicle_confidence",
        )
        if timeout <= 0:
            raise ValueError("vehicle_records.track_timeout_seconds must be > 0")
        if min_frames < 1:
            raise ValueError("vehicle_records.min_track_frames must be >= 1")
        if not 0.0 < iou_threshold <= 1.0:
            raise ValueError(
                "vehicle_records.iou_match_threshold must be in (0, 1]"
            )
        if not 0.0 <= vehicle_floor <= 1.0:
            raise ValueError(
                "vehicle_records.min_vehicle_confidence must be in [0, 1]"
            )

        return VehicleRecordConfig(
            send_trigger=SendTrigger(trig),
            track_timeout_seconds=timeout,
            min_track_frames=min_frames,
            iou_match_threshold=iou_threshold,
            min_vehicle_confidence=vehicle_floor,
            color_enabled=_strict_bool(
                vr.get("color_enabled", True), "vehicle_records.color_enabled"
            ),
            make_model_enabled=_strict_bool(
                vr.get("make_model_enabled", False),
                "vehicle_records.make_model_enabled",
            ),
            emit_plateless=_strict_bool(
                vr.get("emit_plateless", True),
                "vehicle_records.emit_plateless",
            ),
        )

    @staticmethod
    def load(path: str | pathlib.Path) -> "VehicleRecordConfig":
        raw = json.loads(pathlib.Path(path).read_text())
        return VehicleRecordConfig.from_dict(raw)
