#!/usr/bin/env python3
"""Synthetic route-proof provenance regressions; no field IDs or plate data.

Route authority remains monotonic. Its audit track must explain the selected
status/reason, rather than whichever unrelated fragment has the most points.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plate_reader.stream_engine import (  # noqa: E402
    StreamDwellTrack, StreamVehicle, StreamWebhookEngine, _dwell_fingerprint,
)


def engine(shadow=False):
    return StreamWebhookEngine(SimpleNamespace(
        capture=SimpleNamespace(width=1000, height=1000),
        presence=SimpleNamespace(
            board_zone=(0.0, 0.0, 0.72, 0.25),
            route_qualification=SimpleNamespace(
                enabled=True, shadow_only=shadow,
                entry_zone=(0.52, 0.26, 1.0, 1.0),
                terminal_exit_zone=(0.72, 0.0, 1.0, 0.28),
                min_trajectory_points=4,
                min_trajectory_duration_seconds=0.5,
                zone_confirm_points=2,
                min_leftward_progress=0.08,
                min_upward_progress=0.03,
            ),
        ),
    ))


def track(*, qualified, points, offset=0):
    entry = (700, 500, 900, 800)
    board = (300, 20, 500, 220)
    exit_box = (800, 20, 980, 220)
    boxes = [entry, entry] + [board] * (points - 2)
    if not qualified:
        boxes[-1] = exit_box
    start = datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=offset)
    times = tuple((start + timedelta(seconds=i * 0.2)).isoformat()
                  for i in range(points))
    return StreamDwellTrack(
        first_seen_at=times[0], last_seen_at=times[-1],
        dwell_seconds=(points - 1) * 0.2, plate_key=None,
        samples=tuple(StreamVehicle(box, 0.8) for box in boxes),
        sample_observed_at=times,
    )


def profile(name):
    return {
        "encounter_id": name,
        "vehicle": StreamVehicle((700, 500, 900, 800), 0.8),
        "first_seen": 1.0, "last_seen": 1.0,
        "first_seen_at": "2020-01-01T00:00:00+00:00",
        "last_seen_at": "2020-01-01T00:00:00+00:00",
        "route_status": "pending", "route_shadow_status": "pending",
    }


class RouteAuditProvenanceTests(unittest.TestCase):
    def apply(self, e, p, t):
        status, reason = e._classify_trajectory(t)
        e._apply_track_evidence_locked(p, t, status, reason, 20.0)

    def assert_proof(self, e, p, t):
        status, reason = e._classify_trajectory(t)
        prefix = "route_shadow" if e._route_shadow_only else "route"
        self.assertEqual(p[prefix + "_status"], status)
        self.assertEqual(p[prefix + "_reason"], reason)
        self.assertEqual(p["trajectory_points"], len(t.samples))
        self.assertEqual(p["dwell_seconds"], t.dwell_seconds)
        self.assertEqual(p["route_track_fingerprint"], _dwell_fingerprint(t))
        self.assertEqual(p["route_trajectory_bboxes"],
                         tuple(sample.vehicle_bbox for sample in t.samples))
        self.assertEqual(p["route_trajectory_timestamps"], t.sample_observed_at)

    def test_shorter_qualification_replaces_long_rejected_proof(self):
        for shadow in (False, True):
            with self.subTest(shadow=shadow):
                e, p = engine(shadow), profile("synthetic-primary")
                rejected = track(qualified=False, points=40)
                qualified = track(qualified=True, points=26, offset=20)
                self.apply(e, p, rejected)
                self.apply(e, p, qualified)
                self.assert_proof(e, p, qualified)

    def test_later_long_rejection_cannot_overwrite_qualified_proof(self):
        for shadow in (False, True):
            with self.subTest(shadow=shadow):
                e, p = engine(shadow), profile("synthetic-primary")
                qualified = track(qualified=True, points=8)
                self.apply(e, p, qualified)
                self.apply(e, p, track(qualified=False, points=40, offset=20))
                self.assert_proof(e, p, qualified)

    def test_new_shorter_rejection_retains_its_own_track(self):
        for shadow in (False, True):
            with self.subTest(shadow=shadow):
                e, p = engine(shadow), profile("synthetic-primary")
                self.apply(e, p, track(qualified=False, points=40))
                latest = track(qualified=False, points=5, offset=20)
                self.apply(e, p, latest)
                self.assert_proof(e, p, latest)

    def test_new_qualified_decision_retains_its_actual_track(self):
        for shadow in (False, True):
            with self.subTest(shadow=shadow):
                e, p = engine(shadow), profile("synthetic-primary")
                self.apply(e, p, track(qualified=True, points=26))
                latest = track(qualified=True, points=8, offset=20)
                self.apply(e, p, latest)
                self.assert_proof(e, p, latest)

    def test_merge_selects_proof_from_decision_owner_not_larger_track(self):
        for shadow in (False, True):
            for primary_qualified in (False, True):
                with self.subTest(shadow=shadow, primary_qualified=primary_qualified):
                    e = engine(shadow)
                    p, other = profile("synthetic-primary"), profile("synthetic-other")
                    qualified = track(qualified=True, points=8)
                    rejected = track(qualified=False, points=40, offset=20)
                    self.apply(e, p, qualified if primary_qualified else rejected)
                    self.apply(e, other, rejected if primary_qualified else qualified)
                    e._encounters = [p, other]
                    e._merge_encounters_locked(p, other)
                    self.assert_proof(e, p, qualified)
                    self.assertEqual(len(e._encounters), 1)

    def test_merge_same_status_keeps_primary_decision_and_proof(self):
        for shadow in (False, True):
            for qualified in (False, True):
                with self.subTest(shadow=shadow, qualified=qualified):
                    e = engine(shadow)
                    p, other = profile("synthetic-primary"), profile("synthetic-other")
                    original = track(qualified=qualified, points=8)
                    self.apply(e, p, original)
                    self.apply(e, other, track(qualified=qualified, points=40, offset=20))
                    e._encounters = [p, other]
                    e._merge_encounters_locked(p, other)
                    self.assert_proof(e, p, original)

    def test_shadow_proof_does_not_grant_traffic_authority(self):
        e, p = engine(True), profile("synthetic-primary")
        qualified = track(qualified=True, points=8)
        self.apply(e, p, qualified)
        self.assert_proof(e, p, qualified)
        self.assertEqual(p["route_status"], "pending")
        self.assertNotIn("route_qualified_at", p)
        self.assertFalse(p.get("route_complete", False))


if __name__ == "__main__":
    unittest.main()
