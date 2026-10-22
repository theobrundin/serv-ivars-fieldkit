#!/usr/bin/env python3
"""Regression tests for confidence-scored Stream vehicle descriptions.

Plate Recognizer emits one score for a make/model *pair* and a separate
score for colour.  These fields enrich a vehicle profile; they are not
identity evidence.  The tests here deliberately use conflicting observations
to lock both properties down:

* the highest-confidence make/model pair and colour survive independently;
* an MMC agreement cannot manufacture a physical-vehicle match; and
* the confidence-selected values and scores reach the Visit payload.

Run: python3 -m unittest tools.test_mmc_confidence
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from types import SimpleNamespace


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from plate_reader.detector import (  # noqa: E402
    ConfirmedIdentification,
    Detector,
)
from plate_reader.main import App  # noqa: E402
from plate_reader.stream_engine import (  # noqa: E402
    StreamVehicle,
    StreamWebhookEngine,
    parse_vehicle_only,
    parse_webhook_payload,
)


class _StreamCfg:
    class stream:
        webhook_host = "127.0.0.1"
        webhook_port = 8499
        vehicle_quiet_seconds = 12.0
        unidentified_resolution_seconds = 90.0


class _CaptureSender:
    def __init__(self):
        self.visits = []

    def send_visit(self, payload):
        self.visits.append(payload)


class _CaptureCounters:
    def record_success(self, _inference_duration_ms):
        pass


class MmcPayloadParsingTests(unittest.TestCase):
    def test_plate_mode_selects_actual_highest_scored_candidates(self):
        payload = {"data": {"results": [{
            "plate": "LOCALONLY",
            "score": 0.99,
            "dscore": 0.91,
            "box": {"xmin": 1, "ymin": 2, "xmax": 3, "ymax": 4},
            "vehicle": {
                "type": "SUV",
                "score": 0.88,
                "box": {"xmin": 10, "ymin": 20, "xmax": 110, "ymax": 120},
            },
            # Deliberately out of score order.  Stream normally ranks these,
            # but selecting by the supplied score makes the contract robust.
            "model_make": [
                {"make": "Ford", "model": "Escape", "score": 0.31},
                {"make": "Honda", "model": "Passport", "score": 0.92},
            ],
            "color": [
                {"color": "black", "score": 0.40},
                {"color": "white", "score": 0.95},
            ],
        }]}}

        read = parse_webhook_payload(payload)[0]

        self.assertEqual((read.make, read.model), ("Honda", "Passport"))
        self.assertEqual(read.color, "white")
        self.assertEqual(read.make_confidence, 0.92)
        self.assertEqual(read.model_confidence, 0.92)
        self.assertEqual(read.color_confidence, 0.95)

    def test_malformed_high_score_cannot_erase_a_valid_candidate(self):
        payload = {"data": {"results": [{
            "plate": "LOCALONLY",
            "score": 0.99,
            "dscore": 0.91,
            "box": {"xmin": 1, "ymin": 2, "xmax": 3, "ymax": 4},
            "vehicle": {
                "type": "SUV",
                "score": 0.88,
                "box": {"xmin": 10, "ymin": 20, "xmax": 110, "ymax": 120},
            },
            "model_make": [
                {"make": "incomplete", "score": 1.0},
                {"make": "Honda", "model": "Passport", "score": 0.82},
            ],
            "color": [
                {"score": 1.0},
                {"color": "white", "score": 0.79},
            ],
        }]}}

        read = parse_webhook_payload(payload)[0]

        self.assertEqual((read.make, read.model), ("Honda", "Passport"))
        self.assertEqual(read.make_confidence, 0.82)
        self.assertEqual((read.color, read.color_confidence), ("white", 0.79))

    def test_vehicle_mode_maps_pair_and_color_scores(self):
        payload = {"data": {"results": [{
            "plate": {
                "type": "Plate",
                "score": 0.90,
                "box": {"xmin": 1, "ymin": 2, "xmax": 3, "ymax": 4},
                "props": {
                    "plate": [{"value": "LOCALONLY", "score": 0.99}],
                },
            },
            "vehicle": {
                "type": "SUV",
                "score": 0.88,
                "box": {"xmin": 10, "ymin": 20, "xmax": 110, "ymax": 120},
                "props": {
                    "make_model": [
                        {"make": "Ford", "model": "Escape", "score": 0.73},
                    ],
                    "color": [{"value": "silver", "score": 0.81}],
                },
            },
        }]}}

        read = parse_webhook_payload(payload)[0]

        self.assertEqual((read.make, read.model), ("Ford", "Escape"))
        self.assertEqual(read.color, "silver")
        self.assertEqual(read.make_confidence, 0.73)
        self.assertEqual(read.model_confidence, 0.73)
        self.assertEqual(read.color_confidence, 0.81)

    def test_vehicle_only_parser_uses_nested_props_without_double_count(self):
        base_vehicle = {
            "type": "SUV",
            "score": 0.88,
            "box": {"xmin": 10, "ymin": 20, "xmax": 110, "ymax": 120},
            "props": {
                "make_model": [
                    {"make": "Honda", "model": "Passport", "score": 0.73},
                ],
                "color": [{"value": "white", "score": 0.81}],
            },
        }
        no_plate = {"data": {"results": [{
            "plate": None,
            "vehicle": base_vehicle,
        }]}}
        with_plate = {"data": {"results": [{
            "plate": {
                "type": "Plate",
                "score": 0.90,
                "box": {"xmin": 1, "ymin": 2, "xmax": 3, "ymax": 4},
                "props": {
                    "plate": [{"value": "LOCALONLY", "score": 0.99}],
                },
            },
            "vehicle": base_vehicle,
        }]}}

        vehicle = parse_vehicle_only(no_plate)[0]

        self.assertEqual((vehicle.make, vehicle.model), ("Honda", "Passport"))
        self.assertEqual(
            (
                vehicle.make_confidence,
                vehicle.model_confidence,
                vehicle.color_confidence,
            ),
            (0.73, 0.73, 0.81),
        )
        self.assertEqual(parse_vehicle_only(with_plate), [])


class MmcEncounterTests(unittest.TestCase):
    def test_merge_keeps_best_pair_and_best_color_independently(self):
        earlier = StreamVehicle(
            vehicle_bbox=(0, 0, 100, 100),
            vehicle_confidence=0.80,
            make="Ford",
            model="Escape",
            color="white",
            make_confidence=0.31,
            model_confidence=0.31,
            color_confidence=0.95,
        )
        later = StreamVehicle(
            vehicle_bbox=(5, 5, 105, 105),
            vehicle_confidence=0.90,
            make="Honda",
            model="Passport",
            color="black",
            make_confidence=0.92,
            model_confidence=0.92,
            color_confidence=0.40,
        )

        merged = StreamWebhookEngine._merge_vehicle(earlier, later)

        self.assertEqual((merged.make, merged.model), ("Honda", "Passport"))
        self.assertEqual(
            (merged.make_confidence, merged.model_confidence),
            (0.92, 0.92),
        )
        self.assertEqual((merged.color, merged.color_confidence), ("white", 0.95))

    def test_pair_rank_uses_weaker_component_and_never_synthesizes(self):
        incumbent = StreamVehicle(
            vehicle_bbox=(0, 0, 100, 100),
            vehicle_confidence=0.90,
            make="Ford",
            model="Escape",
            make_confidence=0.95,
            model_confidence=0.30,
        )
        balanced = StreamVehicle(
            vehicle_bbox=(5, 5, 105, 105),
            vehicle_confidence=0.90,
            make="Honda",
            model="Passport",
            make_confidence=0.75,
            model_confidence=0.75,
        )

        merged = StreamWebhookEngine._merge_vehicle(incumbent, balanced)

        self.assertEqual((merged.make, merged.model), ("Honda", "Passport"))
        self.assertEqual(
            (merged.make_confidence, merged.model_confidence),
            (0.75, 0.75),
        )

    def test_partial_pair_cannot_replace_or_survive_encounter_profile(self):
        complete = StreamVehicle(
            vehicle_bbox=(0, 0, 100, 100),
            vehicle_confidence=0.90,
            make="Honda",
            model="Passport",
            make_confidence=0.80,
            model_confidence=0.80,
        )
        make_only = StreamVehicle(
            vehicle_bbox=(5, 5, 105, 105),
            vehicle_confidence=0.95,
            make="Toyota",
            model=None,
            make_confidence=0.99,
            model_confidence=0.99,
        )

        merged = StreamWebhookEngine._merge_vehicle(complete, make_only)
        self.assertEqual((merged.make, merged.model), ("Honda", "Passport"))

        cleaned = StreamWebhookEngine._merge_vehicle(
            make_only,
            StreamVehicle(
                vehicle_bbox=(10, 10, 110, 110),
                vehicle_confidence=0.90,
            ),
        )
        self.assertEqual((cleaned.make, cleaned.model), (None, None))
        self.assertEqual(
            (cleaned.make_confidence, cleaned.model_confidence),
            (None, None),
        )

        half_scored = StreamWebhookEngine._sanitize_mmc_pair(StreamVehicle(
            vehicle_bbox=(10, 10, 110, 110),
            vehicle_confidence=0.90,
            make="Honda",
            model="Passport",
            make_confidence=0.99,
        ))
        self.assertEqual(
            (half_scored.make, half_scored.model),
            ("Honda", "Passport"),
        )
        self.assertEqual(
            (half_scored.make_confidence, half_scored.model_confidence),
            (None, None),
        )

    def test_detector_bucket_requires_complete_pair_and_keeps_ties_stable(self):
        first = SimpleNamespace(
            make="Honda",
            model="Passport",
            make_confidence=0.90,
            model_confidence=0.90,
            color="white",
            color_confidence=0.90,
        )
        make_only = SimpleNamespace(
            make="Toyota",
            model=None,
            make_confidence=0.99,
            model_confidence=0.99,
            color=None,
            color_confidence=None,
        )
        tied = SimpleNamespace(
            make="Ford",
            model="Escape",
            make_confidence=0.90,
            model_confidence=0.90,
            color="black",
            color_confidence=0.90,
        )

        mmc = Detector._bucket_mmc(SimpleNamespace(
            candidates=[first, make_only, tied],
        ))

        self.assertEqual(mmc[:3], ("Honda", "Passport", "white"))
        self.assertEqual(mmc[3:], (0.90, 0.90, 0.90))

        half_scored = Detector._bucket_mmc(SimpleNamespace(candidates=[
            SimpleNamespace(
                make="Toyota",
                model="Camry",
                make_confidence=0.99,
                model_confidence=None,
                color=None,
                color_confidence=None,
            ),
        ]))
        self.assertEqual(half_scored[:2], ("Toyota", "Camry"))
        self.assertEqual(half_scored[3:5], (None, None))

    def test_matching_mmc_alone_is_not_physical_identity(self):
        engine = StreamWebhookEngine(_StreamCfg())
        incumbent = StreamVehicle(
            vehicle_bbox=(0, 0, 100, 100),
            vehicle_confidence=0.90,
            make="Honda",
            model="Passport",
            color="white",
            make_confidence=0.95,
            model_confidence=0.95,
            color_confidence=0.95,
        )
        distant = StreamVehicle(
            vehicle_bbox=(900, 700, 1000, 800),
            vehicle_confidence=0.90,
            make="Honda",
            model="Passport",
            color="white",
            make_confidence=0.95,
            model_confidence=0.95,
            color_confidence=0.95,
        )

        self.assertFalse(engine._same_vehicle(
            {"vehicle": incumbent, "identified": False}, distant
        ))


class MmcVisitTests(unittest.TestCase):
    def test_visit_uses_encounter_best_and_serializes_confidences(self):
        class _ProfileEngine:
            @staticmethod
            def mark_encounter_identified(
                encounter_id, confirmed_candidate_key=None
            ):
                return {
                    "encounter_id": encounter_id,
                    "first_seen_at": "2026-08-26T20:00:00.000Z",
                    "last_seen_at": "2026-08-26T20:00:08.000Z",
                    "make": "Honda",
                    "model": "Passport",
                    "color": "white",
                    "vehicle_type": "SUV",
                    "make_confidence": 0.92,
                    "model_confidence": 0.92,
                    "color_confidence": 0.95,
                }

        app = App()
        app.cfg = SimpleNamespace(hours=SimpleNamespace(is_open=lambda: True))
        app.sender = _CaptureSender()
        app.counters = _CaptureCounters()
        app.detector = SimpleNamespace(engine=_ProfileEngine())
        outcome = ConfirmedIdentification(
            normalized="LOCALONLY",
            detected_at_iso="2026-08-26T20:00:05.000Z",
            frame_count=2,
            detection_confidence=0.91,
            ocr_confidence=0.99,
            inference_duration_ms=10,
            # Lower-confidence evidence in the vote must not override the
            # encounter's stronger description.
            make="Ford",
            model="Escape",
            color="black",
            make_confidence=0.31,
            model_confidence=0.31,
            color_confidence=0.40,
            encounter_id="ab" * 16,
        )

        app._handle_outcome(outcome, {}, 300)

        payload = app.sender.visits[-1]
        self.assertEqual(
            (payload["make"], payload["model"], payload["color"]),
            ("Honda", "Passport", "white"),
        )
        self.assertEqual(
            (
                payload["makeConfidence"],
                payload["modelConfidence"],
                payload["colorConfidence"],
            ),
            (0.92, 0.92, 0.95),
        )


if __name__ == "__main__":
    unittest.main()
