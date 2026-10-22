"""Apply the reviewed LJS route/profile rollout to the site-local config.

The pull updater intentionally never overwrites ``config.json`` because it
contains device credentials and site calibration. This narrow utility changes
only the reviewed LJS keys, validates the complete resulting config, creates a
mode-600 backup, and atomically replaces the file. It never prints secrets.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile
from datetime import datetime, timezone

from .config import load


EXPECTED_DEVICE = "serv-unit-001"
EXPECTED_LOCATION = "ljs-louisville-001"
BOARD_ZONE = {
    "x_min_pct": 0.0,
    "y_min_pct": 0.0,
    "x_max_pct": 0.72,
    "y_max_pct": 0.25,
}
ENTRY_ZONE = {
    "x_min_pct": 0.52,
    "y_min_pct": 0.26,
    "x_max_pct": 1.0,
    "y_max_pct": 1.0,
}
LEGACY_ENTRY_ZONE = {**ENTRY_ZONE, "y_min_pct": 0.28}
SETTLE_QUIET_SECONDS = 3.5
SETTLE_MAX_SECONDS = 4.0
APP_OCR_MIN = 0.80
GROUP_OCR_MIN = 0.80
HIGH_OCR_RESCUE_MIN = 0.98
PROVIDER_SELECTED_MIN_LENGTH = 4
DIAGNOSTIC_VALUES = {
    # One shared 300-case cap covers confirmed, missed and plate-free outcomes.
    # Raw plate text and images remain local to the Pi.
    "save_miss_plates": True,
    "save_confirmed_plates": True,
    "save_unidentified_vehicle_frames": True,
    "miss_plate_max_cases": 300,
}

BASE_ROUTE_VALUES = {
    "enabled": True,
    "profile_reconciliation_enabled": True,
    # 1 Sep field evidence: a real 48-point/56.8s lane pickup had one entry
    # center at y=0.313 and its next at y=0.2698. The old 0.28 edge reset the
    # two-point streak before the long board dwell. Moving only this edge to
    # 0.26 preserves a gap above the board zone ending at y=0.25.
    "entry_zone": ENTRY_ZONE,
    "min_trajectory_points": 8,
    "min_trajectory_duration_seconds": 2.0,
    "zone_confirm_points": 2,
    "min_leftward_progress": 0.08,
    "min_upward_progress": 0.03,
    "terminal_exit_zone": {
        "x_min_pct": 0.72,
        "y_min_pct": 0.0,
        "x_max_pct": 1.0,
        "y_max_pct": 0.28,
    },
    "decision_timeout_seconds": 300.0,
    # 31 Aug field sequence: one Caravan's 27-point entry track was rejected
    # and persisted 21 seconds before its 47-point qualified continuation
    # arrived. Keep rejected evidence local long enough for completed native
    # trajectory reconciliation; it still cannot create a Visit.
    "rejection_grace_seconds": 60.0,
    "unmatched_dwell_cache_seconds": 30.0,
    "terminal_tombstone_seconds": 300.0,
}
BOARD_DWELL_FALLBACK_VALUES = {
    # 31 Aug field evidence: one real lane truck produced 42 trajectory
    # points over 29.8s and remained at the board, but the provider track
    # began too close to the zone boundary to clear the 0.08 x-progress gate.
    # Short top-right fragments measured 3-8 points over 0.2-1.4s.
    "board_dwell_fallback_min_points": 20,
    "board_dwell_fallback_min_board_points": 8,
    "board_dwell_fallback_min_seconds": 8.0,
}
PROGRESSIVE_BOARD_RESCUE_VALUES = {
    # 1 Sep operator audit: real lane vehicles produced these completed
    # provider shapes after their front/side observations were fragmented:
    #   * 1 entry + 16 board samples / 3.2s,
    #   * 1 entry + 25 board samples / 16.6s,
    #   * 4 board-only samples / 0.6s with 0.20 leftward progress.
    # The two actual drive-offs terminated upper-right and are rejected before
    # this OR path.  Progress and a consecutive terminal board streak remain
    # mandatory, so this is not a generic four-point traffic count.
    "progressive_board_rescue_min_points": 4,
    "progressive_board_rescue_min_board_points": 2,
    "progressive_board_rescue_min_seconds": 0.5,
}
ROUTE_VALUES = {
    **BASE_ROUTE_VALUES,
    **BOARD_DWELL_FALLBACK_VALUES,
    **PROGRESSIVE_BOARD_RESCUE_VALUES,
}
LEGACY_REJECTION_GRACE_SECONDS = 10.0
PRIOR_AUTHORITATIVE_ROUTE_VALUES = (
    BASE_ROUTE_VALUES,
    {**BASE_ROUTE_VALUES, "entry_zone": LEGACY_ENTRY_ZONE},
    {
        **BASE_ROUTE_VALUES,
        "rejection_grace_seconds": LEGACY_REJECTION_GRACE_SECONDS,
    },
    {
        **BASE_ROUTE_VALUES,
        "entry_zone": LEGACY_ENTRY_ZONE,
        "rejection_grace_seconds": LEGACY_REJECTION_GRACE_SECONDS,
    },
)


def apply_phase(raw: dict, phase: str) -> list[str]:
    """Mutate one parsed config and return only the paths that changed."""
    if raw.get("device_id") != EXPECTED_DEVICE:
        raise ValueError("refusing config for an unexpected device_id")
    if raw.get("location_id") != EXPECTED_LOCATION:
        raise ValueError("refusing config for an unexpected location_id")
    if phase not in {"shadow", "authoritative"}:
        raise ValueError("phase must be shadow or authoritative")

    if phase == "authoritative":
        presence = raw.get("presence")
        route = (
            presence.get("route_qualification")
            if isinstance(presence, dict) else None
        )
        alpr = raw.get("alpr")
        thresholds = (
            alpr.get("thresholds") if isinstance(alpr, dict) else None
        )
        detector_thresholds = raw.get("thresholds")
        common_ready = (
            isinstance(route, dict)
            and presence.get("board_zone") == BOARD_ZONE
            and isinstance(thresholds, dict)
            and isinstance(detector_thresholds, dict)
            and thresholds.get("identity_settle_quiet_seconds")
            == SETTLE_QUIET_SECONDS
            and thresholds.get("identity_settle_max_seconds")
            == SETTLE_MAX_SECONDS
            and thresholds.get("app_ocr_min") in (0.85, APP_OCR_MIN)
            and thresholds.get("provider_high_ocr_rescue_ocr_min")
            in (0.99, HIGH_OCR_RESCUE_MIN)
            and detector_thresholds.get("min_group_ocr_confidence")
            in (0.85, GROUP_OCR_MIN)
        )
        shadow_ready = (
            common_ready
            and route.get("shadow_only") is True
            and all(route.get(key) == value
                    for key, value in ROUTE_VALUES.items())
        )
        # LJS was already explicitly promoted before the guarded board-dwell
        # fallback, longer rejection grace, and 1 Sep entry-edge calibration.
        # Permit only those exact reviewed baselines to migrate in place; an
        # unreviewed config still cannot bypass the shadow phase.
        prior_authoritative_ready = (
            common_ready
            and route.get("shadow_only") is False
            and any(
                all(route.get(key) == value for key, value in values.items())
                for values in PRIOR_AUTHORITATIVE_ROUTE_VALUES
            )
            and all(route.get(key) in (None, value)
                    for key, value in BOARD_DWELL_FALLBACK_VALUES.items())
            and all(route.get(key) in (None, value)
                    for key, value in PROGRESSIVE_BOARD_RESCUE_VALUES.items())
        )
        if not (shadow_ready or prior_authoritative_ready):
            raise ValueError(
                "authoritative phase requires the reviewed shadow phase "
                "or the exact previously-authoritative LJS baseline"
            )

    changes: list[str] = []

    def assign(container: dict, key: str, value, path: str) -> None:
        if container.get(key) != value:
            container[key] = value
            changes.append(path)

    presence = raw.setdefault("presence", {})
    assign(
        presence,
        "board_zone",
        BOARD_ZONE,
        "presence.board_zone",
    )
    route = presence.setdefault("route_qualification", {})
    for key, value in ROUTE_VALUES.items():
        assign(route, key, value, f"presence.route_qualification.{key}")
    assign(
        route,
        "shadow_only",
        phase == "shadow",
        "presence.route_qualification.shadow_only",
    )

    thresholds = raw.setdefault("alpr", {}).setdefault("thresholds", {})
    # 25 Sep human-reviewed four-character plate passed normal gates but
    # lacked a second provider report. Retain encounter/geometry safeguards.
    assign(
        thresholds,
        "provider_selected_single_min_length",
        PROVIDER_SELECTED_MIN_LENGTH,
        "alpr.thresholds.provider_selected_single_min_length",
    )
    assign(
        thresholds,
        "identity_settle_quiet_seconds",
        SETTLE_QUIET_SECONDS,
        "alpr.thresholds.identity_settle_quiet_seconds",
    )
    assign(
        thresholds,
        "identity_settle_max_seconds",
        SETTLE_MAX_SECONDS,
        "alpr.thresholds.identity_settle_max_seconds",
    )
    assign(
        thresholds,
        "app_ocr_min",
        APP_OCR_MIN,
        "alpr.thresholds.app_ocr_min",
    )
    assign(
        thresholds,
        "provider_high_ocr_rescue_ocr_min",
        HIGH_OCR_RESCUE_MIN,
        "alpr.thresholds.provider_high_ocr_rescue_ocr_min",
    )
    detector_thresholds = raw.setdefault("thresholds", {})
    assign(
        detector_thresholds,
        "min_group_ocr_confidence",
        GROUP_OCR_MIN,
        "thresholds.min_group_ocr_confidence",
    )
    diagnostics = raw.setdefault("diagnostics", {})
    for key, value in DIAGNOSTIC_VALUES.items():
        assign(diagnostics, key, value, f"diagnostics.{key}")
    return changes


def _write_validated(path: Path, raw: dict) -> Path:
    stat = path.stat()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.before-route-{stamp}")
    shutil.copy2(path, backup)
    os.chmod(backup, 0o600)

    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(raw, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, stat.st_mode & 0o777)
        os.chown(temp, stat.st_uid, stat.st_gid)
        load(temp)
        os.replace(temp, path)
    except Exception:
        temp.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)
        raise
    return backup


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("shadow", "authoritative"), required=True)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/home/serv/plate_reader/config.json"),
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    raw = json.loads(args.config.read_text(encoding="utf-8"))
    changes = apply_phase(raw, args.phase)
    if not changes:
        print(f"LJS route config already matches phase={args.phase}")
        return 0
    print(f"LJS route config phase={args.phase}; changing:")
    for path in changes:
        print(f"  {path}")
    if args.check:
        print("check only; no file changed")
        return 0

    backup = _write_validated(args.config, raw)
    print(f"validated and installed; backup={backup}")
    print("restart required: sudo systemctl restart plate-reader")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
