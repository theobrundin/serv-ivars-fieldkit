#!/usr/bin/env python3
"""Focused tests for the site-local route rollout utility."""

from copy import deepcopy
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from plate_reader.ljs_route_config import apply_phase


def base():
    return {
        "device_id": "serv-unit-001",
        "location_id": "ljs-louisville-001",
        "api_key": "preserve-me",
        "unrelated": {"keep": True},
        "alpr": {"thresholds": {"app_detect_min": 0.70}},
        "presence": {"route_qualification": {"enabled": True}},
    }


def main() -> int:
    raw = base()
    changes = apply_phase(raw, "shadow")
    route = raw["presence"]["route_qualification"]
    assert route["shadow_only"] is True
    assert route["profile_reconciliation_enabled"] is True
    assert route["entry_zone"]["y_min_pct"] == 0.26
    assert route["min_trajectory_points"] == 8
    assert route["terminal_exit_zone"]["x_min_pct"] == 0.72
    assert route["decision_timeout_seconds"] == 300.0
    assert route["rejection_grace_seconds"] == 60.0
    assert route["board_dwell_fallback_min_points"] == 20
    assert route["board_dwell_fallback_min_board_points"] == 8
    assert route["board_dwell_fallback_min_seconds"] == 8.0
    assert route["progressive_board_rescue_min_points"] == 4
    assert route["progressive_board_rescue_min_board_points"] == 2
    assert route["progressive_board_rescue_min_seconds"] == 0.5
    assert raw["alpr"]["thresholds"]["identity_settle_quiet_seconds"] == 3.5
    assert raw["alpr"]["thresholds"]["app_ocr_min"] == 0.80
    assert raw["alpr"]["thresholds"][
        "provider_high_ocr_rescue_ocr_min"
    ] == 0.98
    assert raw["thresholds"]["min_group_ocr_confidence"] == 0.80
    assert raw["diagnostics"]["save_miss_plates"] is True
    assert raw["diagnostics"]["save_confirmed_plates"] is True
    assert raw["diagnostics"]["save_unidentified_vehicle_frames"] is True
    assert raw["diagnostics"]["miss_plate_max_cases"] == 300
    assert raw["api_key"] == "preserve-me"
    assert raw["unrelated"] == {"keep": True}
    assert changes

    before = deepcopy(raw)
    assert apply_phase(raw, "shadow") == []
    assert raw == before

    authoritative = apply_phase(raw, "authoritative")
    assert authoritative == ["presence.route_qualification.shadow_only"]
    assert route["shadow_only"] is False
    assert route["decision_timeout_seconds"] == 300.0

    migrated = deepcopy(raw)
    migrated_route = migrated["presence"]["route_qualification"]
    migrated_route["shadow_only"] = False
    migrated_route["rejection_grace_seconds"] = 10.0
    migrated_route.pop("board_dwell_fallback_min_points")
    migrated_route.pop("board_dwell_fallback_min_board_points")
    migrated_route.pop("board_dwell_fallback_min_seconds")
    migration_changes = apply_phase(migrated, "authoritative")
    assert migration_changes == [
        "presence.route_qualification.rejection_grace_seconds",
        "presence.route_qualification.board_dwell_fallback_min_points",
        "presence.route_qualification.board_dwell_fallback_min_board_points",
        "presence.route_qualification.board_dwell_fallback_min_seconds",
    ]
    assert migrated_route["shadow_only"] is False

    field_migrated = deepcopy(raw)
    field_route = field_migrated["presence"]["route_qualification"]
    field_route["shadow_only"] = False
    for key in (
        "progressive_board_rescue_min_points",
        "progressive_board_rescue_min_board_points",
        "progressive_board_rescue_min_seconds",
    ):
        field_route.pop(key)
    field_migrated["alpr"]["thresholds"]["app_ocr_min"] = 0.85
    field_migrated["alpr"]["thresholds"][
        "provider_high_ocr_rescue_ocr_min"
    ] = 0.99
    field_migrated["thresholds"]["min_group_ocr_confidence"] = 0.85
    field_changes = apply_phase(field_migrated, "authoritative")
    assert field_changes == [
        "presence.route_qualification.progressive_board_rescue_min_points",
        "presence.route_qualification.progressive_board_rescue_min_board_points",
        "presence.route_qualification.progressive_board_rescue_min_seconds",
        "alpr.thresholds.app_ocr_min",
        "alpr.thresholds.provider_high_ocr_rescue_ocr_min",
        "thresholds.min_group_ocr_confidence",
    ]

    entry_migrated = deepcopy(raw)
    entry_migrated_route = entry_migrated["presence"]["route_qualification"]
    entry_migrated_route["entry_zone"]["y_min_pct"] = 0.28
    assert apply_phase(entry_migrated, "authoritative") == [
        "presence.route_qualification.entry_zone",
    ]
    assert entry_migrated_route["entry_zone"]["y_min_pct"] == 0.26

    direct = base()
    try:
        apply_phase(direct, "authoritative")
    except ValueError:
        pass
    else:
        raise AssertionError("authoritative phase bypassed shadow staging")

    wrong = base()
    wrong["location_id"] = "another-site"
    try:
        apply_phase(wrong, "shadow")
    except ValueError:
        pass
    else:
        raise AssertionError("wrong site was accepted")
    print("7 passed, 0 failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
