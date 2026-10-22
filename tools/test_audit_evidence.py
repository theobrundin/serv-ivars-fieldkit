#!/usr/bin/env python3
"""Synthetic evidence-only regressions; never reads production files."""
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import json
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plate_reader.audit_evidence import AuditFrameCache, evidence_timing
from plate_reader.miss_diagnostics import tag_existing_route_case
from tools.test_route_audit_provenance import engine, profile, track

T0 = "2020-01-01T00:00:00Z"
T1 = "2020-01-01T00:00:01Z"
T2 = "2020-01-01T00:00:02Z"
BOX = (10, 10, 100, 100)


class AuditEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.cache = AuditFrameCache()

    def match(self, **kw):
        args = dict(camera="camera-1", timestamps=(T1,), boxes=(BOX,), after=T0, now=20)
        args.update(kw)
        return self.cache.latest_on_track(**args)

    def test_exact_timestamp_and_camera_owns_frame(self):
        self.cache.add("camera-1", T1, b"original", 10)
        frame, box = self.match()
        self.assertEqual((frame.jpeg, box, frame.observed_at), (b"original", BOX, T1))

    def test_other_camera_or_missing_camera_cannot_supply_frame(self):
        self.cache.add("camera-2", T1, b"other", 10)
        self.assertIsNone(self.match())
        self.assertIsNone(self.match(camera=None))

    def test_later_unrelated_frame_is_not_terminal_evidence(self):
        self.cache.add("camera-1", T2, b"next-car", 10)
        self.assertIsNone(self.match())

    def test_conflicting_boxes_at_same_time_are_not_unique(self):
        self.cache.add("camera-1", T1, b"original", 10)
        self.assertIsNone(self.match(timestamps=(T1, T1), boxes=(BOX, (300, 10, 400, 200))))

    def test_conflicting_images_at_same_timestamp_are_not_used(self):
        self.cache.add("camera-1", T1, b"one", 10)
        self.cache.add("camera-1", T1, b"two", 11)
        self.assertIsNone(self.match())

    def test_old_frame_does_not_rewind_existing_case(self):
        self.cache.add("camera-1", T1, b"original", 10)
        self.assertIsNone(self.match(after=T2))

    def test_rounded_timestamp_only_small_tolerance(self):
        self.cache.add("camera-1", "2020-01-01T00:00:01.020Z", b"original", 10)
        self.assertIsNotNone(self.match())
        self.assertIsNone(self.match(timestamps=("2020-01-01T00:00:01.100Z",)))

    def test_expiration_and_byte_cap(self):
        self.cache.MAX_BYTES = 6
        self.cache.add("camera-1", T0, b"aaaa", 10)
        self.cache.add("camera-1", T1, b"bbbb", 11)
        self.assertEqual(len(self.cache.frames), 1)
        self.cache.add("camera-1", T2, b"toolarge", 12)
        self.assertEqual(len(self.cache.frames), 1)
        self.assertIsNone(self.match(now=132))

    def test_frame_count_cap(self):
        self.cache.MAX_FRAMES = 1
        self.cache.add("camera-1", T0, b"a", 10)
        self.cache.add("camera-1", T1, b"b", 11)
        self.assertEqual(len(self.cache.frames), 1)

    def test_naive_or_missing_timestamp_not_assumed_utc(self):
        for value in (None, "bad", "2020-01-01T00:00:01"):
            self.cache.add("camera-1", value, b"original", 10)
        self.assertEqual(self.cache.frames, [])

    def test_freshness_labels_do_not_claim_plate_visibility(self):
        self.assertEqual(evidence_timing(T0, T2),
                         {"imageToTrackEndSeconds": 2.0, "imageTiming": "early_frame"})
        self.assertEqual(evidence_timing(T1, T2)["imageTiming"], "near_track_end")
        self.assertEqual(evidence_timing(T2, T0)["imageTiming"], "after_track_end")
        self.assertEqual(evidence_timing(None, T2)["imageTiming"], "unknown")

    def test_engine_uses_later_frame_without_extra_detection(self):
        e, p = engine(), profile("synthetic")
        native = replace(track(qualified=True, points=8), camera_id="camera-1")
        p["vehicle"] = replace(p["vehicle"], diagnostic_image_jpeg=b"early",
                               diagnostic_image_observed_at=native.first_seen_at)
        e._audit_frames.add("camera-1", native.last_seen_at, b"later", 10)
        e._encounters = [p]
        with patch.object(e, "_match_dwell_locked", return_value=[p]), \
             patch.object(e, "_awaiting_track_fragment", return_value=False):
            self.assertTrue(e._process_dwell_track_locked(native, 20, cache_unmatched=False))
        self.assertEqual(len(e._encounters), 1)
        self.assertEqual(p["vehicle"].diagnostic_image_jpeg, b"later")
        self.assertEqual(p["vehicle"].diagnostic_image_bbox, native.samples[-1].vehicle_bbox)
        self.assertEqual(p["route_status"], "qualified")
        self.assertEqual(e._identity_stages, {})
        e.close()
        self.assertEqual(e._audit_frames.frames, [])

    def test_engine_cache_requires_full_decoded_frame(self):
        for shape, expected in (((1000, 1000, 3), 1), ((200, 300, 3), 0), ((), 0)):
            e = engine()
            e.submit_recognition_payload(
                {"hook": {"id": "camera-1"}, "data": {"timestamp": T1, "results": []}},
                jpeg=b"opaque-original", frame=SimpleNamespace(shape=shape), now=10,
            )
            self.assertEqual(len(e._audit_frames.frames), expected)

    def test_retag_never_retimes_an_unchanged_jpeg(self):
        for actual, source, expected in ((T0, "provider_webhook", T0),
                                         (None, None, None),
                                         (T0, None, None)):
            with self.subTest(source=source, actual=actual), tempfile.TemporaryDirectory() as folder:
                jpg = Path(folder) / "synthetic.jpg"
                sidecar = jpg.with_suffix(".json")
                jpg.write_bytes(b"unchanged-early-frame")
                sidecar.write_text(json.dumps({
                    "encounterId": "synthetic", "image": jpg.name,
                    "detectedAt": T0, "diagnosticImageObservedAt": actual,
                    "imageTimestampSource": source,
                }))
                tag_existing_route_case(
                    directory=folder, encounter_id="synthetic", episode_id=None,
                    traffic_classification="route_unresolved", route_status="rejected",
                    route_reason="left_before_board_trigger", route_track_fingerprint=None,
                    trajectory_points=2, dwell_seconds=2, plate_candidate=True,
                    diagnostic_image_observed_at=T2, route_trajectory_timestamps=(T0, T2),
                )
                meta = json.loads(sidecar.read_text())
                self.assertEqual(jpg.read_bytes(), b"unchanged-early-frame")
                self.assertEqual(meta["diagnosticImageObservedAt"], expected)
                self.assertEqual(meta["latestAvailableFrameObservedAt"], T2)
                self.assertEqual(meta["imageTiming"], "early_frame" if expected else "unknown")


if __name__ == "__main__":
    unittest.main()
