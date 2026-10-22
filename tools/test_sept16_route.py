#!/usr/bin/env python3
"""Geometry-only regressions from the operator-reviewed 16 Sep route audit.

No plate text, identity, image, credential, or production timestamp is stored.
The two box sequences and relative timing reproduce the field failures.
"""

from __future__ import annotations

import pathlib
import json
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from plate_reader.stream_engine import StreamDwellTrack, StreamVehicle, StreamWebhookEngine
from tools.test_vehicle_route_lifecycle import _FieldRouteCfg


FRAGMENTED_ARRIVAL = (
    (592, 0, 1427, 966), (402, 0, 1423, 945), (330, 4, 1433, 979),
    (229, 2, 1429, 803), (167, 0, 1436, 713), (121, 0, 1426, 590),
    (165, 2, 1434, 504), (0, 516, 517, 862), (139, 0, 1444, 432),
)
FRAGMENTED_TIMES = (
    0.0, 0.191695, 0.399640, 0.601834, 0.806523, 1.006002,
    1.196022, 1.405181, 1.405181,
)
FAST_ARRIVAL = (
    (898, 56, 1455, 753), (640, 0, 1434, 1060), (474, 9, 1434, 1012),
    (337, 0, 1416, 817), (198, 0, 1440, 675), (190, 0, 1424, 549),
    (178, 10, 1447, 446), (202, 10, 1336, 366), (267, 0, 1207, 322),
)
FAST_TIMES = (
    0.0, 0.417335, 0.614492, 0.812358, 1.013332, 1.217053,
    1.414402, 1.604976, 1.813372,
)
UPPER_RIGHT = (1100, 0, 1450, 300)


def track(boxes, times):
    observed = tuple(
        datetime.fromtimestamp(946684800 + t, timezone.utc).isoformat()
        for t in times
    )
    return StreamDwellTrack(
        first_seen_at=observed[0] if observed else None,
        last_seen_at=observed[-1] if observed else None,
        dwell_seconds=times[-1] - times[0] if times else 0,
        plate_key=None,
        samples=tuple(StreamVehicle(vehicle_confidence=0.5, vehicle_bbox=b) for b in boxes),
        sample_observed_at=observed,
    )


class Sept16RouteTests(unittest.TestCase):
    def setUp(self):
        self.engine = StreamWebhookEngine(_FieldRouteCfg())

    def test_fast_arrival_uses_confirmed_entry_not_last_entry(self):
        result = self.engine._classify_trajectory(track(FAST_ARRIVAL, FAST_TIMES))
        self.assertEqual(result, (
            "qualified", "confirmed_entry_then_progressive_board_trajectory",
        ))

    def test_coincident_disjoint_fragment_does_not_break_board_streak(self):
        native = track(FRAGMENTED_ARRIVAL, FRAGMENTED_TIMES)
        samples_before = native.samples
        status, reason, board_index = self.engine._trajectory_route_evidence(native)
        self.assertEqual((status, reason, board_index), (
            "qualified", "confirmed_entry_then_progressive_board_trajectory", 6,
        ))
        self.assertIs(native.samples, samples_before)
        self.assertEqual(len(native.samples), 9)
        self.assertEqual(native.sample_observed_at[-1], native.sample_observed_at[-2])

    def test_distinct_timestamp_disjoint_fragment_is_not_erased(self):
        times = (*FRAGMENTED_TIMES[:-2], 1.30, FRAGMENTED_TIMES[-1])
        self.assertEqual(self.engine._classify_trajectory(track(FRAGMENTED_ARRIVAL, times))[0],
                         "rejected")

    def test_either_coincident_terminal_exit_vetoes_qualification(self):
        for ending in ((UPPER_RIGHT, FRAGMENTED_ARRIVAL[-1]),
                       (FRAGMENTED_ARRIVAL[-1], UPPER_RIGHT)):
            with self.subTest(ending=ending):
                self.assertEqual(self.engine._classify_trajectory(track(
                    (*FRAGMENTED_ARRIVAL[:-2], *ending), FRAGMENTED_TIMES,
                )), ("rejected", "terminal_upper_right_exit"))

    def test_two_possible_overlapping_continuations_remain_unresolved(self):
        boxes = (*FRAGMENTED_ARRIVAL[:-2], (140, 0, 1440, 450), FRAGMENTED_ARRIVAL[-1])
        self.assertEqual(self.engine._classify_trajectory(track(boxes, FRAGMENTED_TIMES)),
                         ("rejected", "trajectory_insufficient_duration"))

    def test_low_overlap_alternative_does_not_get_dropped(self):
        # Even weak competing overlap is not the measured fully disjoint case.
        boxes = (*FRAGMENTED_ARRIVAL[:-2], (0, 480, 517, 862), FRAGMENTED_ARRIVAL[-1])
        self.assertEqual(self.engine._classify_trajectory(track(boxes, FRAGMENTED_TIMES)),
                         ("rejected", "trajectory_insufficient_duration"))

    def test_ambiguous_initial_timestamp_cannot_choose_convenient_anchor(self):
        boxes = (FAST_ARRIVAL[0], (0, 300, 300, 700), *FAST_ARRIVAL[1:])
        times = (FAST_TIMES[0], *FAST_TIMES)
        self.assertEqual(self.engine._classify_trajectory(track(boxes, times)),
                         ("rejected", "trajectory_insufficient_duration"))

    def test_long_ordinary_proof_survives_internal_coincident_ambiguity(self):
        fixtures = json.loads((pathlib.Path(__file__).parent / "fixtures" /
                               "sept16_route_geometry.json").read_text())
        for fixture in fixtures:
            with self.subTest(case=fixture["case"]):
                native = track(tuple(tuple(b) for b in fixture["boxes"]),
                               tuple(fixture["relative_seconds"]))
                self.assertEqual(self.engine._classify_trajectory(native),
                                 ("qualified", fixture["reason"]))

    def test_identical_duplicates_are_not_independent_route_samples(self):
        # Three real instants repeated three times cannot meet the four-point OR.
        boxes = tuple(b for b in (FAST_ARRIVAL[0], FAST_ARRIVAL[-2], FAST_ARRIVAL[-1]) for _ in range(3))
        times = tuple(t for t in (0.0, 0.6, 1.2) for _ in range(3))
        self.assertEqual(self.engine._classify_trajectory(track(boxes, times))[0], "rejected")

    def test_three_point_shadow_or_edge_fragment_stays_excluded(self):
        self.assertEqual(self.engine._classify_trajectory(track(
            ((1000, 300, 1450, 1000), (850, 300, 1450, 850), (800, 280, 1450, 780)),
            (0.0, 0.2, 0.4),
        ))[0], "rejected")

    def test_without_terminal_board_streak_early_arrival_is_not_counted(self):
        self.assertEqual(self.engine._classify_trajectory(track(
            FAST_ARRIVAL[:7], FAST_TIMES[:7],
        ))[0], "rejected")

    def test_static_board_box_cannot_manufacture_route(self):
        self.assertEqual(self.engine._classify_trajectory(track(
            (FAST_ARRIVAL[-1],) * 9, FAST_TIMES,
        ))[0], "rejected")

    def test_normal_duration_floor_is_unchanged(self):
        self.assertEqual(self.engine._route_min_trajectory_duration_seconds, 2.0)
        self.assertEqual(self.engine._route_min_trajectory_points, 8)

    def test_empty_track_is_unresolved(self):
        self.assertEqual(self.engine._classify_trajectory(track((), ())),
                         ("rejected", "trajectory_insufficient_points"))


if __name__ == "__main__":
    unittest.main()
