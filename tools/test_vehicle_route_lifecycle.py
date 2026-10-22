#!/usr/bin/env python3
"""Focused regressions for LJS vehicle-profile and route lifecycle failures.

These cases model the field failures audited on 26 Aug 2026:

* one vehicle split into a near/front plateless profile and a far/rear keyed
  profile while route filtering is still in shadow mode;
* a later, partial departure trajectory downgrading an already-qualified car;
* the same car reappearing after its terminal unidentified Visit was drained;
* top-right cross traffic leaking into traffic totals; and
* a completed dwell hook racing ahead of the recognition hook it belongs to.

The module exposes ``run_checks`` so ``tools/test_stream_engine.py`` can keep
its lightweight, dependency-free runner.  It is also directly executable.
No raw production plate is used here; candidate strings are synthetic.
"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from plate_reader.engines import RawPlateRead
from plate_reader.stream_engine import (
    StreamVehicle,
    StreamWebhookEngine,
    route_diagnostic_classification,
)


ENTRY = (814, 300, 1450, 1080)
MID = (500, 180, 1150, 700)
BOARD = (109, 6, 795, 438)
BOARD_DEPARTURE = (100, 0, 850, 350)
UPPER_RIGHT = (1080, 0, 1450, 300)
RACE_BOARD = (200, 0, 1000, 500)


class _RouteCfg:
    class stream:
        webhook_host = "127.0.0.1"
        webhook_port = 8491
        vehicle_quiet_seconds = 12.0
        unidentified_resolution_seconds = 90.0

    class capture:
        width = 1456
        height = 1088

    class presence:
        camera_position = "behind_cars"
        encounter_handoff_seconds = 5.0
        board_zone = (0.0, 0.0, 0.72, 0.25)

        class route_qualification:
            enabled = True
            shadow_only = False
            profile_reconciliation_enabled = True
            entry_zone = (0.52, 0.26, 1.0, 1.0)
            decision_timeout_seconds = 120.0
            terminal_tombstone_seconds = 300.0
            rejection_grace_seconds = 10.0

        @staticmethod
        def resolved_board_zone():
            return _RouteCfg.presence.board_zone


class _ShadowCfg(_RouteCfg):
    class presence(_RouteCfg.presence):
        class route_qualification(_RouteCfg.presence.route_qualification):
            shadow_only = True


class _SettleCfg(_RouteCfg):
    class alpr:
        class thresholds:
            identity_settle_quiet_seconds = 0.25
            identity_settle_max_seconds = 0.75


class _FieldSettleCfg(_RouteCfg):
    class alpr:
        class thresholds:
            identity_settle_quiet_seconds = 3.5
            identity_settle_max_seconds = 4.0


class _FieldRouteCfg(_RouteCfg):
    class presence(_RouteCfg.presence):
        class route_qualification(_RouteCfg.presence.route_qualification):
            min_trajectory_points = 8
            min_trajectory_duration_seconds = 2.0
            zone_confirm_points = 2
            min_leftward_progress = 0.08
            min_upward_progress = 0.03
            board_dwell_fallback_min_points = 20
            board_dwell_fallback_min_board_points = 8
            board_dwell_fallback_min_seconds = 8.0
            progressive_board_rescue_min_points = 4
            progressive_board_rescue_min_board_points = 2
            progressive_board_rescue_min_seconds = 0.5
            terminal_exit_zone = (0.72, 0.0, 1.0, 0.28)


class _FieldLongGraceCfg(_FieldRouteCfg):
    class presence(_FieldRouteCfg.presence):
        class route_qualification(
            _FieldRouteCfg.presence.route_qualification
        ):
            rejection_grace_seconds = 60.0


def _vehicle(box, *, confidence=0.9, make=None, model=None, color=None,
             vehicle_type="SUV"):
    return StreamVehicle(
        vehicle_bbox=box,
        vehicle_confidence=confidence,
        make=make,
        model=model,
        color=color,
        vehicle_type=vehicle_type,
    )


def _box_dict(box):
    return dict(zip(("xmin", "ymin", "xmax", "ymax"), box))


def _read(text, encounter_id):
    return RawPlateRead(
        text=text,
        detection_confidence=0.9,
        ocr_confidence=0.99,
        bbox=(300, 200, 420, 250),
        vehicle_bbox=BOARD,
        encounter_id=encounter_id,
    )


def _dwell_payload(
    boxes,
    *,
    plate=None,
    start="2026-08-26T17:48:18Z",
    end="2026-08-26T17:48:25Z",
    positions=None,
):
    if positions is None:
        positions = (
            [0.0]
            if len(boxes) == 1
            else [
                6.0 * index / (len(boxes) - 1)
                for index in range(len(boxes))
            ]
        )
    result = {
        "dwell_time": 7.0,
        "first_seen": start,
        "last_seen": end,
        "trajectory": [
            {
                "position_sec": float(position),
                "vehicle": {
                    "type": "SUV",
                    "dscore": 0.9,
                    "box": _box_dict(box),
                },
            }
            for position, box in zip(positions, boxes)
        ],
    }
    if plate is not None:
        result["plate"] = plate
    return {
        "hook": {"event": "dwell_time", "id": "camera-1"},
        "data": {"results": [result]},
    }


def run_checks(check):
    """Run focused lifecycle assertions through the suite's ``check`` API."""

    print("\n=== 26 Aug vehicle route/profile lifecycle regressions ===")

    # Photo #2: Stream opened a front/side plateless profile, then a keyed
    # rear profile after the ordinary 5-second handoff bound.  The native
    # trajectory is strong correlation evidence even while route rejection
    # remains shadow-only.  Reconciliation must not be disabled merely because
    # counting authority has not yet been promoted.
    shadow = StreamWebhookEngine(_ShadowCfg())
    front_id = shadow.observe_vehicle(
        _vehicle(ENTRY, make="Subaru", model="Impreza", color="black"),
        now=1000.0,
        observed_at="2026-08-26T17:48:18Z",
    )
    rear_id = shadow.observe_vehicle(
        _vehicle(BOARD, make="Ford", model="Escape", color="silver"),
        now=1006.0,
        observed_at="2026-08-26T17:48:24Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="TEST123",
    )
    check("field split begins as two sparse profiles", front_id == rear_id, False)
    rear_profile = next(
        enc for enc in shadow._encounters
        if enc["encounter_id"] == rear_id
    )
    check(
        "shadow plated-board evidence never grants authoritative route status",
        (
            shadow.route_status(rear_id),
            rear_profile.get("route_shadow_status"),
            rear_profile.get("route_shadow_reason"),
        ),
        ("pending", "pending", "plate_at_board_pending_trajectory"),
    )
    shadow.mark_encounter_identified(rear_id)
    shadow.submit_dwell_payload(
        _dwell_payload([ENTRY, MID, BOARD], plate="test123")
    )
    check(
        "shadow count mode still reconciles the one exact rear + one front profile",
        shadow.canonical_encounter_id(front_id),
        rear_id,
    )
    reconciled_profile = next(
        enc for enc in shadow._encounters
        if enc["encounter_id"] == rear_id
    )
    check(
        "reconciliation preserves the newer board geometry",
        reconciled_profile["vehicle"].vehicle_bbox,
        BOARD,
    )
    check(
        "shadow reconciliation does not silently promote route authority",
        (shadow.route_status(front_id), shadow.route_status(rear_id)),
        ("pending", "pending"),
    )
    check(
        "reconciled identified profile cannot later emit a second anonymous Visit",
        shadow.drain_vehicles(now=1400.0),
        [],
    )

    # Defense in depth for mixed-version/stale state during a rollout: even a
    # plate-free fragment incorrectly carrying an old live-qualified marker
    # must fail closed unless a completed provider route owns it.
    stale = StreamWebhookEngine(_RouteCfg())
    stale_id = stale.observe_vehicle(
        _vehicle(BOARD),
        now=1410.0,
        observed_at="2026-08-26T17:55:00Z",
    )
    stale_profile = next(
        enc for enc in stale._encounters
        if enc["encounter_id"] == stale_id
    )
    stale._qualify_route_locked(
        stale_profile,
        "legacy_live_route_marker",
        "2026-08-26T17:55:00Z",
    )
    check(
        "anonymous fragment cannot emit from a stale live-qualified marker",
        stale.drain_vehicles(now=1510.0),
        [],
    )
    check(
        "stale anonymous live qualification is explicitly rejected",
        stale.route_metadata(stale_id),
        None,
    )

    # Adjacent-car negative control: a track that overlaps two plateless
    # candidates is ambiguous.  Exact plate correlation owns the rear profile,
    # but must not guess which of two front profiles to alias into it.
    ambiguous = StreamWebhookEngine(_ShadowCfg())
    first_front = ambiguous.observe_vehicle(
        _vehicle(ENTRY, make="Ford", color="white"),
        now=1100.0,
        observed_at="2026-08-26T17:48:18Z",
    )
    second_box = (430, 180, 750, 510)
    second_front = ambiguous.observe_vehicle(
        _vehicle(second_box, make="Honda", color="blue"),
        now=1100.2,
        observed_at="2026-08-26T17:48:19Z",
    )
    keyed_rear = ambiguous.observe_vehicle(
        _vehicle(BOARD, make="Jeep", color="gray"),
        now=1106.0,
        observed_at="2026-08-26T17:48:24Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="SAFE456",
    )
    ambiguous.mark_encounter_identified(keyed_rear)
    ambiguous.submit_dwell_payload(
        _dwell_payload([ENTRY, second_box, BOARD], plate="safe456")
    )
    check(
        "two trajectory-overlapping front candidates fail closed",
        (
            ambiguous.canonical_encounter_id(first_front),
            ambiguous.canonical_encounter_id(second_front),
        ),
        (first_front, second_front),
    )

    # Different OCR variants may share geometry only during the engine's
    # explicit sub-second settlement stage. The 12-second vehicle quiet window
    # is far too broad: a second car can occupy the same board box by then.
    settle_base = time.monotonic()
    settled = StreamWebhookEngine(_SettleCfg())
    weak_id = settled.observe_vehicle(
        _vehicle(BOARD),
        now=settle_base,
        observed_at="2026-08-26T20:00:00Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="WEAKA11",
    )
    settled.submit([_read("WEAKA11", weak_id)], now=settle_base)
    strong_id = settled.observe_vehicle(
        _vehicle(BOARD),
        now=settle_base + 3.0,
        observed_at="2026-08-26T20:00:03Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="STRONGB",
    )
    check(
        "different candidate after settlement opens a separate profile",
        strong_id == weak_id,
        False,
    )
    strong_meta = settled.route_metadata(strong_id)
    check(
        "recent plated predecessor quarantines board-only qualification",
        (
            strong_meta and strong_meta["route_status"],
            strong_meta and strong_meta["route_reason"],
        ),
        ("pending", "recent_plate_candidate_conflict"),
    )
    settled.submit_dwell_payload(_dwell_payload(
        [ENTRY, MID, BOARD],
        plate="STRONGB",
        start="2026-08-26T20:00:03Z",
        end="2026-08-26T20:00:10Z",
    ))
    check(
        "separate follower qualifies with its own keyed native trajectory",
        settled.route_status(strong_id),
        "qualified",
    )

    settling = StreamWebhookEngine(_SettleCfg())
    first_variant = settling.observe_vehicle(
        _vehicle(BOARD),
        now=settle_base,
        observed_at="2026-08-26T20:10:00Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="SHORT11",
    )
    settling.submit([_read("SHORT11", first_variant)], now=settle_base)
    second_variant = settling.observe_vehicle(
        _vehicle(BOARD),
        now=settle_base + 0.2,
        observed_at="2026-08-26T20:10:00.200Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="FULL111",
    )
    check(
        "different candidates inside active settlement share one profile",
        second_variant,
        first_variant,
    )

    # The 27 Aug CR-V variants were 1.605s apart in provider time but 3.115s
    # apart at the local release boundary. Settlement uses local receipt time,
    # so model that end to end and prove only the fuller candidate escapes.
    field_settling = StreamWebhookEngine(_FieldSettleCfg())
    field_first = field_settling.observe_vehicle(
        _vehicle(BOARD),
        now=settle_base,
        observed_at="2026-08-26T20:11:00Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="ABC12",
    )
    field_settling.submit(
        [_read("ABC12", field_first)], now=settle_base
    )
    check(
        "measured first CR-V variant cannot release before the second receipt",
        field_settling.infer(None, now=settle_base + 3.0),
        [],
    )
    field_second = field_settling.observe_vehicle(
        _vehicle(BOARD),
        now=settle_base + 3.115,
        observed_at="2026-08-26T20:11:01.605Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="ABC123",
    )
    field_settling.submit(
        [_read("ABC123", field_second)], now=settle_base + 3.115
    )
    check(
        "field candidates 3.115s apart remain one provisional profile",
        field_second,
        field_first,
    )
    check(
        "field settlement remains bounded before its four-second maximum",
        field_settling.infer(None, now=settle_base + 3.999),
        [],
    )
    settled_reads = field_settling.infer(
        None, now=settle_base + 4.001
    )
    check(
        "one fuller CR-V identity leaves settlement for hashing",
        [(read.text, read.encounter_id) for read in settled_reads],
        [("ABC123", field_first)],
    )

    provisional = StreamWebhookEngine(_SettleCfg())
    provisional_id = provisional.observe_vehicle(
        _vehicle(MID),
        now=settle_base,
        observed_at="2026-08-26T20:15:00Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="SAME111",
    )
    provisional_repeat = provisional.observe_vehicle(
        _vehicle(MID),
        now=settle_base + 2.0,
        observed_at="2026-08-26T20:15:02Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="SAME111",
    )
    check(
        "same provisional key continues one overlapping sub-quiet episode",
        provisional_repeat,
        provisional_id,
    )
    provisional_after_quiet = provisional.observe_vehicle(
        _vehicle(MID),
        now=settle_base + 20.0,
        observed_at="2026-08-26T20:15:20Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="SAME111",
    )
    check(
        "same provisional key after quiet cannot inherit the old profile",
        provisional_after_quiet == provisional_id,
        False,
    )

    # An unkeyed completed dwell may beat its first plate webhook by a fraction
    # of a second. Native board endpoint timestamp + geometry owns that race;
    # the 90-second anonymous hold prevents an already-emitted duplicate.
    completion_race = StreamWebhookEngine(_RouteCfg())
    race_id = completion_race.observe_vehicle(
        _vehicle(ENTRY),
        now=settle_base,
        observed_at="2026-08-26T20:20:00Z",
    )
    completion_race.observe_vehicle(
        _vehicle(MID),
        now=settle_base + 3.0,
        observed_at="2026-08-26T20:20:03Z",
    )
    completion_race.observe_vehicle(
        _vehicle(RACE_BOARD),
        now=settle_base + 6.0,
        observed_at="2026-08-26T20:20:06Z",
    )
    completion_race.submit_dwell_payload(_dwell_payload(
        [ENTRY, MID, RACE_BOARD],
        start="2026-08-26T20:20:00Z",
        end="2026-08-26T20:20:07Z",
    ))
    check(
        "completed unkeyed route does not emit before late plate resolution",
        completion_race.drain_vehicles(now=settle_base + 6.05),
        [],
    )
    late_plate_id = completion_race.observe_vehicle(
        _vehicle(RACE_BOARD),
        now=settle_base + 6.1,
        observed_at="2026-08-26T20:20:06.100Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="LATE100",
    )
    check(
        "first board plate at +100ms owns completed native endpoint",
        late_plate_id,
        race_id,
    )
    completion_race.mark_encounter_identified(late_plate_id, "LATE100")
    check(
        "late-identified completed route never emits prior anonymous vehicle",
        completion_race.drain_vehicles(now=settle_base + 200.0),
        [],
    )

    # Route qualification is cumulative.  Stream can post a valid full dwell
    # and then a short departure fragment for the same provider vehicle.  The
    # fragment never gets to erase proof that entry -> board already happened.
    monotonic = StreamWebhookEngine(_RouteCfg())
    mono_id = monotonic.observe_vehicle(
        _vehicle(ENTRY, make="Ford", color="white"),
        now=1200.0,
        observed_at="2026-08-26T17:48:18Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="MONO789",
    )
    monotonic.observe_vehicle(
        _vehicle(BOARD, make="Ford", color="white"),
        now=1203.0,
        observed_at="2026-08-26T17:48:22Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="MONO789",
    )
    monotonic.submit_dwell_payload(
        _dwell_payload([ENTRY, MID, BOARD], plate="mono789")
    )
    check("complete entry-to-board track qualifies", monotonic.route_status(mono_id),
          "qualified")
    monotonic.submit_dwell_payload(_dwell_payload(
        [BOARD, UPPER_RIGHT],
        plate="mono789",
        start="2026-08-26T17:48:22Z",
        end="2026-08-26T17:48:25Z",
    ))
    mono_meta = monotonic.route_metadata(mono_id)
    check(
        "later departure fragment cannot downgrade a qualified profile",
        mono_meta and mono_meta["route_status"],
        "qualified",
    )
    check(
        "shorter fragment cannot erase stronger trajectory evidence",
        bool(mono_meta and mono_meta["trajectory_points"] >= 3),
        True,
    )

    # Photos #6/#7: after one qualified, weak-plate terminal outcome, keep only
    # a lightweight physical-profile tombstone.  A later overlapping departure
    # is the same car even if Stream's descriptive MMC flips.  It must be
    # swallowed under the original ID, not opened as a second traffic outcome.
    base = time.monotonic()
    terminal = StreamWebhookEngine(_RouteCfg())
    terminal_id = terminal.observe_vehicle(
        _vehicle(ENTRY, make="Ford", model="Passport", color="white"),
        now=base,
        observed_at="2026-08-26T18:05:33Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="WEAK123",
        plate_detection_confidence=0.661,
        plate_ocr_confidence=0.795,
    )
    terminal.observe_vehicle(
        _vehicle(BOARD, make="Ford", model="Passport", color="white"),
        now=base + 6.0,
        observed_at="2026-08-26T18:05:39Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="WEAK123",
        plate_detection_confidence=0.661,
        plate_ocr_confidence=0.795,
    )
    terminal.submit_dwell_payload(_dwell_payload(
        [ENTRY, MID, BOARD],
        plate="weak123",
        start="2026-08-26T18:05:33Z",
        end="2026-08-26T18:05:40Z",
    ))
    check(
        "completed route stays live through identity resolution window",
        terminal.drain_vehicles(now=base + 10.0),
        [],
    )
    first_terminal = terminal.drain_vehicles(now=base + 100.0)
    check(
        "qualified weak-plate vehicle has exactly one terminal outcome",
        (
            len(first_terminal),
            first_terminal[0].encounter_id if first_terminal else None,
        ),
        (1, terminal_id),
    )
    check(
        "authoritative anonymous Visit keeps its terminal owner guard",
        (
            terminal.terminal_visit_emitted(terminal_id),
            terminal.terminal_visit_owner(terminal_id),
        ),
        (True, terminal_id),
    )
    tomb = terminal._terminal_tombstones[0]
    check(
        "terminal tombstone retains no image bytes or plate candidate keys",
        (
            tomb.get("candidate_keys"),
            tomb.get("confirmed_candidate_key"),
            tomb["vehicle"].diagnostic_image_jpeg,
            tomb["vehicle"].diagnostic_image_bbox,
        ),
        (set(), None, None, None),
    )
    departing_id = terminal.observe_vehicle(
        _vehicle(
            BOARD_DEPARTURE,
            make="Chevrolet",
            model="Tahoe",
            color="black",
        ),
        now=base + 138.0,
        observed_at="2026-08-26T18:06:21Z",
    )
    check(
        "terminal tombstone absorbs the same overlapping departure despite MMC flip",
        departing_id,
        terminal_id,
    )
    continuing_departure_id = terminal.observe_vehicle(
        _vehicle(BOARD_DEPARTURE, make="GMC", color="blue"),
        now=base + 144.0,
        observed_at="2026-08-26T18:06:27Z",
    )
    check(
        "claimed tombstone owns one continuous departure episode",
        continuing_departure_id,
        terminal_id,
    )
    later_board_id = terminal.observe_vehicle(
        _vehicle(BOARD_DEPARTURE, make="Toyota", color="white"),
        now=base + 151.0,
        observed_at="2026-08-26T18:06:34Z",
    )
    check(
        "departure claim expires after one quiet window",
        later_board_id == terminal_id,
        False,
    )
    check(
        "absorbed departure never creates a second Visit",
        terminal.drain_vehicles(now=base + 500.0),
        [],
    )
    next_car = terminal.observe_vehicle(
        _vehicle(ENTRY, make="Toyota", model="Camry", color="white"),
        now=base + 50.0,
        observed_at="2026-08-26T18:06:23Z",
    )
    check(
        "adjacent next car entering the lane is not absorbed by the tombstone",
        next_car == terminal_id,
        False,
    )

    # Photos #8/#9: a top-right-only path never reaches the entry zone.  With
    # route authority enabled it is diagnostic evidence only, never traffic.
    rejected = StreamWebhookEngine(_RouteCfg())
    rejected_id = rejected.observe_vehicle(
        _vehicle(UPPER_RIGHT, make="Toyota", color="red"),
        now=base,
        observed_at="2026-08-26T18:09:24Z",
    )
    rejected.submit_dwell_payload(_dwell_payload(
        [UPPER_RIGHT, (1120, 0, 1450, 220)],
        start="2026-08-26T18:09:24Z",
        end="2026-08-26T18:09:25Z",
    ))
    check("upper-right-only track is rejected", rejected.route_status(rejected_id),
          "rejected")
    check(
        "authoritative rejection emits no vehicle Visit",
        rejected.drain_vehicles(now=base + 400.0),
        [],
    )

    # 27 Aug false positive: a partial upper-right GMC appeared to cross both
    # broad zones in six samples over one second. Completed route proof must be
    # stable, directional, and evaluated through the terminal sample.
    short_route = StreamWebhookEngine(_FieldRouteCfg())
    short_id = short_route.observe_vehicle(
        _vehicle(ENTRY),
        now=base,
        observed_at="2026-08-27T19:34:50.000Z",
    )
    short_payload = _dwell_payload(
        [ENTRY, ENTRY, MID, MID, BOARD, UPPER_RIGHT],
        start="2026-08-27T19:34:50.000Z",
        end="2026-08-27T19:34:51.000Z",
        positions=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
    )
    short_payload["data"]["results"][0]["dwell_time"] = 1.0
    short_route.submit_dwell_payload(short_payload)
    short_meta = short_route.route_metadata(short_id)
    check(
        "six-point upper-right exit is rejected by terminal direction",
        (short_meta["route_status"], short_meta["route_reason"]),
        ("rejected", "terminal_upper_right_exit"),
    )
    check(
        "short zone-crossing fragment never becomes traffic",
        short_route.drain_vehicles(now=base + 400.0),
        [],
    )

    seven_point = StreamWebhookEngine(_FieldRouteCfg())
    seven_track = seven_point._parse_dwell_tracks(_dwell_payload(
        [ENTRY, ENTRY, MID, MID, BOARD, BOARD, BOARD],
        positions=[0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0],
    ))[0]
    check(
        "seven directional points qualify through progressive-board OR",
        seven_point._classify_trajectory(seven_track),
        ("qualified", "entry_then_progressive_board_trajectory"),
    )

    fast_route = StreamWebhookEngine(_FieldRouteCfg())
    fast_payload = _dwell_payload(
        [ENTRY, ENTRY, MID, MID, BOARD, BOARD, BOARD, BOARD],
        positions=[0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.999],
    )
    fast_payload["data"]["results"][0]["dwell_time"] = 1.999
    fast_track = fast_route._parse_dwell_tracks(fast_payload)[0]
    check(
        "displayed two-second route tolerates timestamp rounding",
        fast_route._classify_trajectory(fast_track),
        ("qualified", "entry_then_board_trajectory"),
    )

    # 1 Sep images #9/#10 were real lane vehicles whose native tracks began
    # only as the rears reached the board.  Preserve the exact field boxes;
    # no production plate text is part of this regression.
    board_only_route = StreamWebhookEngine(_FieldRouteCfg())
    board_only_track = board_only_route._parse_dwell_tracks(_dwell_payload(
        [
            (60, 0, 1103, 529),
            (79, 0, 1044, 487),
            (43, 1, 858, 489),
            (6, 9, 585, 433),
        ],
        positions=[0.0, 0.2, 0.4, 0.6],
    ))[0]
    check(
        "four-point board-only field track qualifies by progress",
        board_only_route._classify_trajectory(board_only_track),
        ("qualified", "board_only_progressive_trajectory"),
    )

    one_entry_route = StreamWebhookEngine(_FieldRouteCfg())
    one_entry_track = one_entry_route._parse_dwell_tracks(_dwell_payload(
        [
            (211, 0, 1345, 607),
            (136, 8, 1336, 546),
            (55, 0, 1098, 446),
            (48, 0, 1013, 397),
        ],
        positions=[0.0, 0.2, 0.4, 0.8],
    ))[0]
    check(
        "one-entry two-board field track qualifies by progress",
        one_entry_route._classify_trajectory(one_entry_track),
        ("qualified", "entry_then_progressive_board_trajectory"),
    )

    entry_only_route = StreamWebhookEngine(_FieldRouteCfg())
    entry_only_track = entry_only_route._parse_dwell_tracks(_dwell_payload(
        [
            (369, 0, 1442, 761),
            (460, 0, 1446, 729),
            (508, 154, 922, 625),
        ],
        positions=[0.0, 0.2, 0.4],
    ))[0]
    check(
        "three-point entry fragment still waits for its plated continuation",
        entry_only_route._classify_trajectory(entry_only_track),
        ("rejected", "trajectory_insufficient_points"),
    )

    valid_route = StreamWebhookEngine(_FieldRouteCfg())
    valid_id = valid_route.observe_vehicle(
        _vehicle(ENTRY),
        now=base,
        observed_at="2026-08-27T20:00:00.000Z",
    )
    valid_payload = _dwell_payload(
        [ENTRY, ENTRY, MID, MID, BOARD, BOARD, BOARD, BOARD],
        start="2026-08-27T20:00:00.000Z",
        end="2026-08-27T20:00:02.000Z",
        positions=[0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0],
    )
    valid_payload["data"]["results"][0]["dwell_time"] = 2.0
    valid_route.submit_dwell_payload(valid_payload)
    valid_route.drain_vehicles(now=time.monotonic() + 31.0)
    valid_meta = valid_route.route_metadata(valid_id)
    check(
        "stable eight-point two-second lane path qualifies",
        (valid_meta["route_status"], valid_meta["route_reason"]),
        ("qualified", "entry_then_board_trajectory"),
    )
    check(
        "route trigger is first confirmed-board sample, not completion",
        valid_meta["route_qualified_at"],
        "2026-08-27T20:00:01.000Z",
    )

    # 1 Sep field false negative: this long pickup track entered at a slight
    # angle. Its first two real centers are y=0.313 and y=0.2698. The old
    # y>=0.28 entry edge admitted only the first and reset the required streak.
    angled_entry = StreamWebhookEngine(_FieldRouteCfg())
    angled_boxes = [
        (106, 9, 1411, 672),
        (123, 0, 1428, 587),
        (102, 0, 1455, 501),
        (111, 1, 1426, 447),
        (111, 2, 1287, 412),
        (57, 0, 1160, 376),
        (39, 0, 1089, 359),
        (58, 0, 966, 343),
    ] + [(76, 1, 821, 294)] * 12
    angled_track = angled_entry._parse_dwell_tracks(_dwell_payload(
        angled_boxes,
        positions=[0.0, 0.2] + [float(i) for i in range(1, 19)],
    ))[0]
    check(
        "slightly angled 1 Sep pickup clears calibrated entry edge",
        angled_entry._classify_trajectory(angled_track),
        ("qualified", "entry_then_sustained_board_dwell"),
    )

    terminal_exit = StreamWebhookEngine(_FieldRouteCfg())
    terminal_track = terminal_exit._parse_dwell_tracks(_dwell_payload(
        [ENTRY, ENTRY, MID, MID, BOARD, BOARD,
         UPPER_RIGHT, UPPER_RIGHT, UPPER_RIGHT],
        positions=[0.0, 0.4, 0.8, 1.2, 1.6, 2.0, 2.4, 2.8, 3.2],
    ))[0]
    check(
        "full track ending upper-right rejects despite touching board",
        terminal_exit._classify_trajectory(terminal_track),
        ("rejected", "terminal_upper_right_exit"),
    )

    plated_driveoff = StreamWebhookEngine(_FieldRouteCfg())
    plated_driveoff_id = plated_driveoff.observe_vehicle(
        _vehicle(BOARD),
        now=base + 1.6,
        observed_at="2026-08-27T20:05:01.600Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="DRIVE123",
    )
    plated_driveoff.mark_encounter_identified(
        plated_driveoff_id, "DRIVE123"
    )
    check(
        "plate at board remains provisional before terminal route evidence",
        (
            plated_driveoff.route_status(plated_driveoff_id),
            plated_driveoff.route_metadata(plated_driveoff_id)["route_reason"],
        ),
        ("pending", "plate_at_board_pending_trajectory"),
    )
    plated_driveoff.submit_dwell_payload(_dwell_payload(
        [ENTRY, ENTRY, MID, MID, BOARD, BOARD,
         UPPER_RIGHT, UPPER_RIGHT, UPPER_RIGHT],
        plate="drive123",
        start="2026-08-27T20:05:00.000Z",
        end="2026-08-27T20:05:03.200Z",
        positions=[0.0, 0.4, 0.8, 1.2, 1.6, 2.0, 2.4, 2.8, 3.2],
    ))
    plated_driveoff_meta = plated_driveoff.route_metadata(
        plated_driveoff_id
    )
    check(
        "plated upper-right exit revokes all Visit authority",
        (
            plated_driveoff_meta["route_status"],
            plated_driveoff_meta["route_reason"],
            plated_driveoff_meta["route_qualified_at"],
        ),
        ("rejected", "terminal_upper_right_exit", None),
    )
    check(
        "route diagnostic taxonomy separates drive-offs from weak evidence",
        (
            route_diagnostic_classification("terminal_upper_right_exit"),
            route_diagnostic_classification("left_before_board_trigger"),
            route_diagnostic_classification("insufficient_leftward_progress"),
            route_diagnostic_classification("trajectory_insufficient_points"),
        ),
        (
            "drive_off",
            "route_unresolved",
            "route_unresolved",
            "route_unresolved",
        ),
    )

    # 31 Aug images #7/#8: Stream ended one 27-point entry-side Caravan
    # trajectory, then supplied the same car's qualified rear trajectory 21
    # seconds after the rejection.  The old 10-second grace persisted a false
    # drive-off diagnostic before native trajectory reconciliation could join
    # the two physical fragments.
    split_base = time.monotonic()
    split_route = StreamWebhookEngine(_FieldLongGraceCfg())
    entry_fragment_id = split_route.observe_vehicle(
        _vehicle(
            ENTRY,
            make="Dodge",
            model="Caravan",
            color="silver",
            vehicle_type="Van",
        ),
        now=split_base,
        observed_at="2026-08-31T23:26:02.000Z",
    )
    split_route.submit_dwell_payload(_dwell_payload(
        [ENTRY, ENTRY, ENTRY, MID, MID, MID, MID, MID],
        start="2026-08-31T23:26:02.000Z",
        end="2026-08-31T23:26:31.000Z",
        positions=[0.0, 4.0, 8.0, 12.0, 16.0, 20.0, 24.0, 29.0],
    ))
    check(
        "split entry trajectory is rejected without becoming traffic",
        split_route.route_status(entry_fragment_id),
        "rejected",
    )
    check(
        "long rejection grace retains split evidence past old 10s boundary",
        (
            split_route.drain_vehicles(now=split_base + 11.0),
            split_route.drain_route_diagnostics(),
        ),
        ([], []),
    )
    rear_fragment_id = split_route.observe_vehicle(
        _vehicle(
            BOARD,
            make="Dodge",
            model="Caravan",
            color="silver",
            vehicle_type="Van",
        ),
        now=split_base + 22.0,
        observed_at="2026-08-31T23:26:47.000Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="CARAVAN1",
    )
    split_route.mark_encounter_identified(rear_fragment_id, "CARAVAN1")
    split_route.submit_dwell_payload(_dwell_payload(
        [ENTRY, ENTRY, MID, MID, BOARD, BOARD, BOARD, BOARD],
        plate="caravan1",
        start="2026-08-31T23:26:32.000Z",
        end="2026-08-31T23:27:02.000Z",
        # The later recognition frame is owned by the first board sample at
        # 23:26:47; exact sample-time ownership is what makes the merge safe.
        positions=[0.0, 4.0, 8.0, 12.0, 15.0, 18.0, 22.0, 30.0],
    ))
    check(
        "qualified continuation absorbs rejected predecessor under one owner",
        (
            split_route.canonical_encounter_id(entry_fragment_id)
            == split_route.canonical_encounter_id(rear_fragment_id),
            split_route.route_status(rear_fragment_id),
            split_route.drain_route_diagnostics(),
        ),
        (True, "qualified", []),
    )
    check(
        "identified upper-right exit never drains as traffic",
        plated_driveoff.drain_vehicles(now=time.monotonic() + 11.0),
        [],
    )
    plated_route_diagnostics = plated_driveoff.drain_route_diagnostics()
    check(
        "identified upper-right exit drains once as a local diagnostic",
        (
            len(plated_route_diagnostics),
            plated_route_diagnostics[0].traffic_classification,
            plated_route_diagnostics[0].route_reason,
            plated_route_diagnostics[0].saw_plate,
        ),
        (1, "drive_off", "terminal_upper_right_exit", True),
    )
    plated_driveoff.drain_vehicles(now=time.monotonic() + 30.0)
    check(
        "route diagnostics are idempotent across repeated drains",
        plated_driveoff.drain_route_diagnostics(),
        [],
    )
    retry_diagnostics = plated_driveoff.drain_route_diagnostics(
        now=time.monotonic() + 31.0
    )
    check(
        "unacknowledged route evidence retries after persistence backoff",
        len(retry_diagnostics),
        1,
    )
    plated_driveoff.ack_route_diagnostic(
        plated_route_diagnostics[0].encounter_id
    )
    check(
        "durably acknowledged route evidence leaves the retry queue",
        plated_driveoff.drain_route_diagnostics(
            now=time.monotonic() + 62.0
        ),
        [],
    )

    rejected_miss = StreamWebhookEngine(_FieldRouteCfg())
    rejected_miss_id = rejected_miss.observe_vehicle(
        _vehicle(BOARD),
        now=base + 20.0,
        observed_at="2026-08-27T20:05:21.600Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="WEAK123",
    )
    rejected_miss.submit_dwell_payload(_dwell_payload(
        [ENTRY, ENTRY, MID, MID, BOARD, BOARD,
         UPPER_RIGHT, UPPER_RIGHT, UPPER_RIGHT],
        plate="weak123",
        start="2026-08-27T20:05:20.000Z",
        end="2026-08-27T20:05:23.200Z",
        positions=[0.0, 0.4, 0.8, 1.2, 1.6, 2.0, 2.4, 2.8, 3.2],
    ))
    check(
        "rejected weak plate waits for Detector's one miss case",
        (
            rejected_miss.drain_vehicles(now=time.monotonic() + 11.0),
            rejected_miss.drain_route_diagnostics(),
        ),
        ([], []),
    )
    rejected_miss.note_detection_event(
        rejected_miss_id, "episode-for-existing-case"
    )
    check(
        "linked miss case then becomes the sole route diagnostic",
        rejected_miss.drain_vehicles(now=time.monotonic() + 12.0),
        [],
    )
    linked_diagnostics = rejected_miss.drain_route_diagnostics()
    check(
        "route diagnostic carries Detector episode ownership",
        (
            len(linked_diagnostics),
            linked_diagnostics[0].diagnostic_episode_id,
            linked_diagnostics[0].traffic_classification,
        ),
        (1, "episode-for-existing-case", "drive_off"),
    )

    stale_qualified = StreamWebhookEngine(_RouteCfg())
    stale_id = stale_qualified.observe_vehicle(
        _vehicle(BOARD),
        now=base + 40.0,
        observed_at="2026-08-27T20:06:00.000Z",
    )
    with stale_qualified._lock:
        stale = stale_qualified._encounter_locked(stale_id)
        stale["route_status"] = "qualified"
        stale["route_reason"] = "legacy_board_observation"
        stale["route_complete"] = False
    check(
        "qualified profile without completed route never becomes traffic",
        stale_qualified.drain_vehicles(now=base + 131.0),
        [],
    )
    stale_diagnostics = stale_qualified.drain_route_diagnostics()
    check(
        "mixed-version incomplete route is retained as unresolved evidence",
        (
            len(stale_diagnostics),
            stale_diagnostics[0].route_reason,
            stale_diagnostics[0].traffic_classification,
        ),
        (1, "profile_without_completed_route", "route_unresolved"),
    )

    geometry = StreamWebhookEngine(_RouteCfg())
    geometry_now = time.monotonic() - 10.0
    geometry_front = geometry.observe_vehicle(
        _vehicle(ENTRY),
        now=geometry_now,
        observed_at="2026-08-27T20:06:00.000Z",
    )
    geometry_rear = geometry.observe_vehicle(
        _vehicle(BOARD),
        now=geometry_now + 6.0,
        observed_at="2026-08-27T20:06:06.000Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="GEO123",
    )
    geometry.mark_encounter_identified(geometry_rear, "GEO123")
    geometry.submit_dwell_payload(_dwell_payload(
        [ENTRY, MID, BOARD],
        plate="geo123",
        start="2026-08-27T20:06:00.000Z",
        end="2026-08-27T20:06:07.000Z",
    ))
    check(
        "keyed rear owns the reconciled front fragment",
        geometry.canonical_encounter_id(geometry_front),
        geometry_rear,
    )
    geometry_profile = next(
        enc for enc in geometry._encounters
        if enc["encounter_id"] == geometry_rear
    )
    check(
        "canonical geometry stays at the newer board observation",
        geometry_profile["vehicle"].vehicle_bbox,
        BOARD,
    )
    variant_id = geometry.observe_vehicle(
        _vehicle(BOARD),
        now=time.monotonic() + 1.0,
        observed_at="2026-08-27T20:06:08.000Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="OTHER999",
    )
    variant_meta = geometry.route_metadata(variant_id)
    check(
        "new overlapping OCR variant is quarantined after reconciliation",
        (
            variant_id == geometry_rear,
            variant_meta["route_status"],
            variant_meta["route_reason"],
        ),
        (False, "pending", "recent_plate_candidate_conflict"),
    )

    near_entry = (700, 280, 844, 372)
    near_board = (686, 200, 830, 300)
    wrong_way = StreamWebhookEngine(_FieldRouteCfg())
    wrong_track = wrong_way._parse_dwell_tracks(_dwell_payload(
        [near_entry, near_entry, MID, MID,
         near_board, near_board, near_board, near_board],
        positions=[0.0, 0.4, 0.8, 1.2, 1.6, 2.0, 2.4, 2.8],
    ))[0]
    check(
        "zone touches without leftward progress reject",
        wrong_way._classify_trajectory(wrong_track),
        ("rejected", "insufficient_leftward_progress"),
    )

    # 31 Aug live false rejection: a real truck spent about 30 seconds at the
    # menu board across 42 native trajectory points. Its provider track began
    # close to the entry/board boundary, so the ordered zones were valid but
    # the x delta narrowly missed 0.08. Sustained board occupancy is stronger
    # evidence than that small delta; short touches must remain rejected.
    board_dwell_boxes = (
        [near_entry, near_entry, MID, MID]
        + [near_board] * 20
    )
    board_dwell = StreamWebhookEngine(_FieldRouteCfg())
    board_dwell_track = board_dwell._parse_dwell_tracks(_dwell_payload(
        board_dwell_boxes,
        start="2026-08-31T21:37:09.000Z",
        end="2026-08-31T21:37:32.000Z",
        positions=[float(index) for index in range(24)],
    ))[0]
    check(
        "sustained menu-board dwell rescues boundary-started lane vehicle",
        board_dwell._classify_trajectory(board_dwell_track),
        ("qualified", "entry_then_sustained_board_dwell"),
    )

    short_board_touch = StreamWebhookEngine(_FieldRouteCfg())
    short_board_track = short_board_touch._parse_dwell_tracks(_dwell_payload(
        [near_entry, near_entry, MID, MID] + [near_board] * 20,
        positions=[0.25 * index for index in range(24)],
    ))[0]
    check(
        "many board points without eight seconds do not use dwell fallback",
        short_board_touch._classify_trajectory(short_board_track),
        ("rejected", "insufficient_leftward_progress"),
    )

    sparse_board = StreamWebhookEngine(_FieldRouteCfg())
    sparse_board_track = sparse_board._parse_dwell_tracks(_dwell_payload(
        [near_entry, near_entry] + [MID] * 16 + [near_board, near_board],
        positions=[float(index) for index in range(19)] + [27.0],
    ))[0]
    check(
        "two far-apart board samples are not sustained board occupancy",
        sparse_board._classify_trajectory(sparse_board_track),
        ("rejected", "insufficient_leftward_progress"),
    )

    missing_time = StreamWebhookEngine(_FieldRouteCfg())
    missing_payload = _dwell_payload(
        [ENTRY, ENTRY, MID, MID, BOARD, BOARD, BOARD, BOARD]
    )
    missing_result = missing_payload["data"]["results"][0]
    missing_result.pop("first_seen", None)
    missing_result.pop("last_seen", None)
    for sample in missing_result["trajectory"]:
        sample.pop("position_sec", None)
    missing_track = missing_time._parse_dwell_tracks(missing_payload)[0]
    check(
        "trajectory without sample timing fails closed",
        missing_time._classify_trajectory(missing_track),
        ("rejected", "trajectory_missing_timing"),
    )

    # Webhook ordering is not guaranteed.  A terminal dwell can arrive a few
    # milliseconds before its recognition webhook.  Cache the unmatched track
    # briefly and retry association when the keyed profile appears; otherwise
    # a real drive-through vehicle stays pending and eventually disappears.
    raced = StreamWebhookEngine(_RouteCfg())
    raced.submit_dwell_payload(_dwell_payload(
        [ENTRY, MID, BOARD],
        plate="race321",
        start="2026-08-26T18:12:00Z",
        end="2026-08-26T18:12:07Z",
    ))
    raced_id = raced.observe_vehicle(
        _vehicle(BOARD, make="Honda", color="blue"),
        now=base,
        observed_at="2026-08-26T18:12:06Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="RACE321",
    )
    check(
        "dwell-before-recognition race is retried against the arriving profile",
        raced.route_status(raced_id),
        "qualified",
    )

    partial_race = StreamWebhookEngine(_RouteCfg())
    partial_front = partial_race.observe_vehicle(
        _vehicle(ENTRY),
        now=base + 450.0,
        observed_at="2026-08-26T18:50:00Z",
    )
    partial_race.submit_dwell_payload(_dwell_payload(
        [ENTRY, MID, BOARD],
        start="2026-08-26T18:50:00Z",
        end="2026-08-26T18:50:07Z",
    ))
    check(
        "dwell after only the first sparse hook waits for its split profile",
        (
            partial_race.route_status(partial_front),
            len(partial_race._pending_dwell_tracks),
        ),
        ("pending", 1),
    )
    partial_board = partial_race.observe_vehicle(
        _vehicle(BOARD),
        now=base + 456.0,
        observed_at="2026-08-26T18:50:06Z",
    )
    check(
        "later board hook completes the cached unkeyed profile",
        (
            partial_race.canonical_encounter_id(partial_board),
            partial_race.route_status(partial_front),
            len(partial_race._pending_dwell_tracks),
        ),
        (partial_front, "qualified", 0),
    )

    # A plate-free car may also be split into one entry profile and one board
    # profile. Equal overlap is not generic ambiguity when the two profile
    # windows are non-concurrent and map strictly forward along one qualified
    # native trajectory.
    unkeyed = StreamWebhookEngine(_RouteCfg())
    unkeyed_front = unkeyed.observe_vehicle(
        _vehicle(ENTRY, make="Toyota", color="white"),
        now=base + 500.0,
        observed_at="2026-08-26T19:00:00Z",
    )
    unkeyed_board = unkeyed.observe_vehicle(
        _vehicle(BOARD, make="Honda", color="black"),
        now=base + 506.0,
        observed_at="2026-08-26T19:00:06Z",
    )
    check("unkeyed sparse route begins as split profiles",
          unkeyed_front == unkeyed_board, False)
    unkeyed.submit_dwell_payload(_dwell_payload(
        [ENTRY, MID, BOARD],
        start="2026-08-26T19:00:00Z",
        end="2026-08-26T19:00:07Z",
    ))
    check(
        "one ordered unkeyed trajectory reconciles its sequential profiles",
        unkeyed.canonical_encounter_id(unkeyed_board),
        unkeyed_front,
    )
    unkeyed_meta = unkeyed.route_metadata(unkeyed_front)
    check(
        "unkeyed route trigger is first board sample, not dwell end",
        unkeyed_meta and unkeyed_meta["route_qualified_at"],
        "2026-08-26T19:00:06.000Z",
    )
    unkeyed_terminal = unkeyed.drain_vehicles(now=base + 510.0)
    check("reconciled unkeyed route emits one vehicle",
          len(unkeyed_terminal), 1)
    check("terminal cleanup prunes obsolete encounter aliases",
          len(unkeyed._encounter_aliases), 0)

    # Two queue occupants can overlap one provider path geometrically. Their
    # concurrently open timestamp windows make the association ambiguous, so
    # neither an unkeyed track nor a repeated OCR candidate may collapse them.
    concurrent = StreamWebhookEngine(_RouteCfg())
    concurrent_entry = concurrent.observe_vehicle(
        _vehicle(ENTRY),
        now=base + 600.0,
        observed_at="2026-08-26T19:10:00Z",
    )
    concurrent.observe_vehicle(
        _vehicle(ENTRY),
        now=base + 600.2,
        observed_at="2026-08-26T19:10:03Z",
    )
    concurrent_board = concurrent.observe_vehicle(
        _vehicle(BOARD),
        now=base + 600.3,
        observed_at="2026-08-26T19:10:02Z",
    )
    concurrent.submit_dwell_payload(_dwell_payload(
        [ENTRY, MID, BOARD],
        start="2026-08-26T19:10:00Z",
        end="2026-08-26T19:10:05Z",
    ))
    check(
        "concurrent unkeyed queue profiles fail closed",
        concurrent.canonical_encounter_id(concurrent_board) == concurrent_entry,
        False,
    )

    duplicate_key = StreamWebhookEngine(_RouteCfg())
    duplicate_entry = duplicate_key.observe_vehicle(
        _vehicle(ENTRY),
        now=base + 700.0,
        observed_at="2026-08-26T19:20:00Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="DUP123",
        source_batch_id="duplicate-provider-frame",
    )
    duplicate_key.observe_vehicle(
        _vehicle(ENTRY),
        now=base + 700.2,
        observed_at="2026-08-26T19:20:03Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="DUP123",
        source_batch_id="duplicate-provider-frame",
    )
    duplicate_board = duplicate_key.observe_vehicle(
        _vehicle(BOARD),
        now=base + 700.3,
        observed_at="2026-08-26T19:20:02Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="DUP123",
    )
    duplicate_key.submit_dwell_payload(_dwell_payload(
        [ENTRY, MID, BOARD],
        plate="dup123",
        start="2026-08-26T19:20:00Z",
        end="2026-08-26T19:20:05Z",
    ))
    check(
        "same OCR candidate cannot merge concurrent spatial profiles",
        duplicate_key.canonical_encounter_id(duplicate_board)
        == duplicate_entry,
        False,
    )

    # A completed anonymous car must not own the next car's board box merely
    # because this fixed camera makes both boxes overlap. A fresh route-entered
    # profile wins, and the new car's own dwell then reconciles its split.
    ownership = StreamWebhookEngine(_RouteCfg())
    old_entry = ownership.observe_vehicle(
        _vehicle(ENTRY),
        now=base + 800.0,
        observed_at="2026-08-26T19:30:00Z",
    )
    ownership.observe_vehicle(
        _vehicle(BOARD),
        now=base + 806.0,
        observed_at="2026-08-26T19:30:06Z",
    )
    ownership.submit_dwell_payload(_dwell_payload(
        [ENTRY, MID, BOARD],
        start="2026-08-26T19:30:00Z",
        end="2026-08-26T19:30:07Z",
    ))
    check("old anonymous route reaches one terminal Visit",
          len(ownership.drain_vehicles(now=base + 810.0)), 1)
    new_entry = ownership.observe_vehicle(
        _vehicle(ENTRY),
        now=base + 830.0,
        observed_at="2026-08-26T19:30:30Z",
    )
    new_board = ownership.observe_vehicle(
        _vehicle(BOARD_DEPARTURE),
        now=base + 836.0,
        observed_at="2026-08-26T19:30:36Z",
    )
    check("old tombstone cannot steal a fresh car's board observation",
          new_board == old_entry, False)
    ownership.submit_dwell_payload(_dwell_payload(
        [ENTRY, MID, BOARD_DEPARTURE],
        start="2026-08-26T19:30:30Z",
        end="2026-08-26T19:30:37Z",
    ))
    check(
        "fresh car fragments reconcile under their own encounter",
        ownership.canonical_encounter_id(new_board),
        new_entry,
    )
    check("fresh car still emits exactly one Visit",
          len(ownership.drain_vehicles(now=base + 840.0)), 1)

    shadow_terminal = StreamWebhookEngine(_ShadowCfg())
    shadow_terminal_id = shadow_terminal.observe_vehicle(
        _vehicle(ENTRY),
        now=base + 900.0,
        observed_at="2026-08-26T19:40:00Z",
    )
    shadow_terminal.observe_vehicle(
        _vehicle(BOARD),
        now=base + 906.0,
        observed_at="2026-08-26T19:40:06Z",
    )
    shadow_terminal.submit_dwell_payload(_dwell_payload(
        [ENTRY, MID, BOARD],
        start="2026-08-26T19:40:00Z",
        end="2026-08-26T19:40:07Z",
    ))
    shadow_terminal.drain_vehicles(now=base + 1000.0)
    check("shadow route classification creates no suppressing tombstone",
          len(shadow_terminal._terminal_tombstones), 0)
    check(
        "terminal shadow Visit survives live profile cleanup",
        (
            shadow_terminal.terminal_visit_emitted(shadow_terminal_id),
            shadow_terminal.terminal_visit_owner(shadow_terminal_id),
            shadow_terminal._encounter_locked(shadow_terminal_id),
        ),
        (True, shadow_terminal_id, None),
    )

    shadow_pending = StreamWebhookEngine(_ShadowCfg())
    shadow_pending_id = shadow_pending.observe_vehicle(
        _vehicle(ENTRY),
        now=base + 1100.0,
        observed_at="2026-08-26T19:50:00Z",
    )
    shadow_legacy_visit = shadow_pending.drain_vehicles(now=base + 1191.0)
    check(
        "shadow classifier preserves the legacy 90s anonymous Visit timing",
        (
            len(shadow_legacy_visit),
            shadow_legacy_visit[0].encounter_id,
            shadow_legacy_visit[0].route_status,
        ),
        (1, shadow_pending_id, "pending"),
    )
    shadow_timeout = shadow_pending.drain_vehicles(now=base + 1221.0)
    shadow_timeout_diagnostics = shadow_pending.drain_route_diagnostics()
    check(
        "shadow timeout adds only a local tag, never a second Visit",
        (
            len(shadow_timeout),
            len(shadow_timeout_diagnostics),
            shadow_timeout_diagnostics[0].traffic_classification,
        ),
        (0, 1, "route_unresolved"),
    )
    check(
        "pending shadow Visit remains terminal after timeout cleanup",
        shadow_pending.terminal_visit_emitted(shadow_pending_id),
        True,
    )

    stale_qualified = StreamWebhookEngine(_RouteCfg())
    stale_id = stale_qualified.observe_vehicle(
        _vehicle(BOARD),
        now=base + 1230.0,
        observed_at="2026-08-26T19:52:10Z",
    )
    with stale_qualified._lock:
        stale_profile = stale_qualified._encounter_locked(stale_id)
        stale_profile["route_status"] = "qualified"
        stale_profile["route_complete"] = False
    check(
        "incomplete stale qualification is exposed as pending",
        stale_qualified.route_status(stale_id),
        "pending",
    )
    stale_qualified.mark_encounter_identified(stale_id, "STALE1")
    stale_qualified.drain_vehicles(now=base + 1351.0)
    stale_diagnostics = stale_qualified.drain_route_diagnostics()
    check(
        "identified incomplete qualification becomes unresolved evidence",
        (
            len(stale_diagnostics),
            stale_diagnostics[0].route_reason,
            stale_diagnostics[0].traffic_classification,
        ),
        (1, "profile_without_completed_route", "route_unresolved"),
    )

    shadow_late = StreamWebhookEngine(_ShadowCfg())
    shadow_late_id = shadow_late.observe_vehicle(
        _vehicle(ENTRY),
        now=base + 1400.0,
        observed_at="2026-08-26T19:55:00Z",
    )
    check(
        "shadow audit profile emits one legacy Visit",
        len(shadow_late.drain_vehicles(now=base + 1491.0)),
        1,
    )
    late_meta = shadow_late.mark_encounter_identified(
        shadow_late_id, "LATE12"
    )
    check(
        "retained shadow profile remembers its terminal Visit",
        (
            shadow_late.terminal_visit_emitted(shadow_late_id),
            late_meta["terminal_visit_emitted"],
        ),
        (True, True),
    )
    post_terminal_id = shadow_late.observe_vehicle(
        _vehicle(UPPER_RIGHT),
        now=base + 1492.0,
        observed_at="2026-08-26T19:56:32Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="OTHER9",
    )
    check(
        "post-terminal fragment begins as a separate live profile",
        post_terminal_id == shadow_late_id,
        False,
    )
    with shadow_late._lock:
        old_terminal = shadow_late._encounter_locked(shadow_late_id)
        new_primary = shadow_late._encounter_locked(post_terminal_id)
        shadow_late._merge_encounters_locked(new_primary, old_terminal)
    check(
        "post-terminal merge preserves the ID already sent in the Visit",
        (
            shadow_late.terminal_visit_owner(shadow_late_id),
            shadow_late.terminal_visit_owner(post_terminal_id),
        ),
        (shadow_late_id, shadow_late_id),
    )

    # Audit image ownership is independent of the encounter's latest geometry.
    audit_first = _vehicle(ENTRY, confidence=0.95)
    audit_first = StreamVehicle(
        **{
            **audit_first.__dict__,
            "diagnostic_image_jpeg": b"entry-frame",
            "diagnostic_image_bbox": ENTRY,
            "diagnostic_image_observed_at": "2026-08-26T20:00:00Z",
        }
    )
    audit_later = _vehicle(BOARD, confidence=0.70)
    audit_later = StreamVehicle(
        **{
            **audit_later.__dict__,
            "diagnostic_image_jpeg": b"board-frame",
            "diagnostic_image_bbox": BOARD,
            "diagnostic_image_observed_at": "2026-08-26T20:00:10Z",
        }
    )
    audit_merged = StreamWebhookEngine._merge_vehicle(
        audit_first, audit_later
    )
    check(
        "retained audit JPEG advances to the later board frame",
        (
            audit_merged.diagnostic_image_jpeg,
            audit_merged.diagnostic_image_bbox,
            audit_merged.vehicle_bbox,
        ),
        (b"board-frame", BOARD, BOARD),
    )

    audit_reconcile = StreamWebhookEngine(_RouteCfg())
    front_id = audit_reconcile.observe_vehicle(
        audit_first,
        now=base + 1300.0,
        observed_at="2026-08-26T20:00:00Z",
    )
    rear_id = audit_reconcile.observe_vehicle(
        audit_later,
        now=base + 1306.0,
        observed_at="2026-08-26T20:00:10Z",
        saw_plate=True,
        plated_attempt=True,
        candidate_key="LATE123",
    )
    with audit_reconcile._lock:
        rear = audit_reconcile._encounter_locked(rear_id)
        front = audit_reconcile._encounter_locked(front_id)
        audit_reconcile._merge_encounters_locked(rear, front)
        reconciled_vehicle = rear["vehicle"]
    check(
        "front/rear reconciliation cannot rewind the later board JPEG",
        (
            reconciled_vehicle.diagnostic_image_jpeg,
            reconciled_vehicle.diagnostic_image_bbox,
            reconciled_vehicle.diagnostic_image_observed_at,
        ),
        (b"board-frame", BOARD, "2026-08-26T20:00:10Z"),
    )

    audit_out_of_order = StreamWebhookEngine(_RouteCfg())
    audit_out_of_order.observe_vehicle(
        audit_later,
        now=base + 1400.0,
        observed_at="2026-08-26T20:00:10Z",
    )
    delayed_front = StreamVehicle(
        **{
            **audit_first.__dict__,
            "vehicle_bbox": BOARD,
            "diagnostic_image_bbox": BOARD,
            "diagnostic_image_observed_at": "2026-08-26T20:00:05Z",
        }
    )
    delayed_id = audit_out_of_order.observe_vehicle(
        delayed_front,
        now=base + 1401.0,
        observed_at="2026-08-26T20:00:05Z",
    )
    delayed_meta = audit_out_of_order.route_metadata(delayed_id)
    with audit_out_of_order._lock:
        delayed_vehicle = audit_out_of_order._encounter_locked(
            delayed_id
        )["vehicle"]
    check(
        "out-of-order webhook cannot rewind retained audit evidence",
        (
            delayed_meta["last_seen_at"],
            delayed_vehicle.diagnostic_image_jpeg,
            delayed_vehicle.diagnostic_image_observed_at,
        ),
        (
            "2026-08-26T20:00:10Z",
            b"board-frame",
            "2026-08-26T20:00:10Z",
        ),
    )


if __name__ == "__main__":
    passed = 0
    failed = 0

    def _check(label, got, want):
        global passed, failed
        if got == want:
            passed += 1
            print(f"PASS  {label}  (got={got!r})")
        else:
            failed += 1
            print(f"FAIL  {label}  (got={got!r} want={want!r})")

    run_checks(_check)
    print("\n" + "-" * 60)
    print(f"{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
