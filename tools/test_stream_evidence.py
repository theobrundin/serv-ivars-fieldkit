#!/usr/bin/env python3
"""Focused regressions for Stream temporal evidence and webhook retries."""
from __future__ import annotations

import copy
import pathlib
import sys
import time
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from plate_reader.config import (  # noqa: E402
    Alpr, AlprThresholds, Capture, Classifiers, Dedup, DetectionZone,
    Diagnostics, DwellZone, EngineAdvanced, NormBox, Thresholds, VehicleOnly,
)
from plate_reader.detector import Detector, ConfirmedIdentification  # noqa: E402
from plate_reader.engines import RawPlateRead  # noqa: E402
from plate_reader.stream_engine import (  # noqa: E402
    StreamWebhookEngine, parse_webhook_payload,
)


def _result(plate: str, vehicle_box: tuple[int, int, int, int]) -> dict:
    vx1, vy1, vx2, vy2 = vehicle_box
    return {
        "plate": plate,
        "score": 0.96,
        "dscore": 0.91,
        "box": {"xmin": vx1 + 30, "ymin": vy1 + 50,
                "xmax": vx1 + 100, "ymax": vy1 + 75},
        "vehicle": {
            "type": "Sedan",
            "score": 0.92,
            "box": {"xmin": vx1, "ymin": vy1,
                    "xmax": vx2, "ymax": vy2},
        },
    }


def _payload(timestamp: str, results: list[dict]) -> dict:
    return {
        "hook": {"event": "recognition", "id": "camera-1"},
        "data": {
            "camera_id": "camera-1",
            "timestamp": timestamp,
            "results": results,
        },
    }


class _StreamCfg:
    class stream:
        webhook_host = "127.0.0.1"
        webhook_port = 0
        vehicle_quiet_seconds = 12.0
        unidentified_resolution_seconds = 90.0

    class thresholds:
        min_detection_confidence = 0.70
        min_ocr_confidence = 0.85
        min_normalized_length = 3
        max_normalized_length = 12
        vote_min_frames = 2
        high_ocr_single_frame = 0.99

    class alpr:
        engine = "pr_stream"

        class thresholds:
            identity_settle_quiet_seconds = 0.0
            identity_settle_max_seconds = 0.0
            consensus_rescue_min_frames = 0
            consensus_rescue_detect_min = 0.0
            consensus_rescue_ocr_min = 0.0


@dataclass
class _DetectorCfg:
    thresholds: object
    detection_zone: object
    alpr: object
    dwell_zone: object
    dedup: object
    diagnostics: object
    capture: object
    vehicle_only: object


def _detector_cfg() -> _DetectorCfg:
    return _DetectorCfg(
        thresholds=Thresholds(
            min_detection_confidence=0.70,
            min_ocr_confidence=0.85,
            min_group_confidence=0.70,
            min_group_detection_confidence=0.70,
            min_group_ocr_confidence=0.85,
            vote_window_seconds=5.0,
            vote_min_frames=2,
            high_ocr_single_frame=0.0,
            min_normalized_length=3,
            max_normalized_length=12,
        ),
        detection_zone=DetectionZone(False, 0.0, 1.0, 0.0, 1.0),
        alpr=Alpr(
            engine="pr_stream", roi=NormBox(),
            thresholds=AlprThresholds(), classifiers=Classifiers(),
            engine_advanced=EngineAdvanced(),
        ),
        dwell_zone=DwellZone(),
        dedup=Dedup(cooldown_seconds=0.0),
        diagnostics=Diagnostics(),
        capture=Capture(width=1000, height=1000),
        vehicle_only=VehicleOnly(),
    )


class _Frame:
    shape = (1000, 1000, 3)


class _Once:
    name = "pr_stream"

    def __init__(self, reads):
        self.reads = reads

    def infer(self, _frame):
        reads, self.reads = self.reads, []
        return reads


def _read(observation: str, captured_at: str) -> RawPlateRead:
    return RawPlateRead(
        text="ABC123", detection_confidence=0.91, ocr_confidence=0.96,
        bbox=(500, 600, 600, 640), vehicle_bbox=(200, 300, 800, 900),
        encounter_id="encounter-1", captured_at_iso=captured_at,
        source_observation_id=observation,
    )


def test_provider_timestamp_and_observation_identity() -> None:
    event = _payload(
        "2026-08-28T15:01:02.123456Z",
        [_result("ABC123", (100, 100, 400, 400))],
    )
    first = parse_webhook_payload(event)[0]
    retry = parse_webhook_payload(copy.deepcopy(event))[0]
    later_event = copy.deepcopy(event)
    later_event["data"]["timestamp"] = "2026-08-28T15:01:03.123456Z"
    later = parse_webhook_payload(later_event)[0]
    same_source_alternate = copy.deepcopy(event)
    same_source_alternate["data"]["results"][0]["plate"] = "ABC12B"
    alternate = parse_webhook_payload(same_source_alternate)[0]

    assert first.captured_at_iso == "2026-08-28T15:01:02.123456Z"
    assert first.source_observation_id == retry.source_observation_id
    assert first.source_observation_id == alternate.source_observation_id
    assert first.source_observation_id != later.source_observation_id


def test_exact_retry_is_not_new_evidence() -> None:
    event = _payload(
        "2026-08-28T15:02:00Z",
        [_result("ABC123", (100, 100, 400, 400))],
    )
    engine = StreamWebhookEngine(_StreamCfg())
    assert engine.submit_recognition_payload(event, now=100.0) is True
    assert engine.submit_recognition_payload(copy.deepcopy(event), now=101.0) is False
    assert len(engine.infer(None, now=101.0)) == 1
    assert len(engine.drain_vehicle_observations()) == 1


def test_default_clock_domains_remain_separate() -> None:
    observed_at = "2026-08-28T15:02:30.123456Z"
    event = _payload(
        observed_at, [_result("ABC123", (100, 100, 400, 400))],
    )
    engine = StreamWebhookEngine(_StreamCfg())
    before = time.monotonic()
    engine.submit_recognition_payload(event)
    after = time.monotonic()
    encounter = engine._encounters[0]
    assert before <= encounter["first_seen"] <= after
    assert before <= encounter["last_seen"] <= after
    assert encounter["first_seen_at"] == observed_at
    assert encounter["last_seen_at"] == observed_at
    assert engine.infer(None)[0].captured_at_iso == observed_at


def test_multi_plate_result_shares_one_vehicle_encounter() -> None:
    same_box = (100, 100, 500, 500)
    event = _payload(
        "2026-08-28T15:03:00Z",
        [_result("ABC123", same_box), _result("DEALER", same_box)],
    )
    engine = StreamWebhookEngine(_StreamCfg())
    engine.submit_recognition_payload(event, now=200.0)
    reads = engine.infer(None, now=200.0)
    assert len(reads) == 1  # settlement chooses one identity candidate
    assert len({read.encounter_id for read in reads}) == 1
    assert len(engine._encounters) == 1
    assert len(engine.drain_vehicle_observations()) == 1

    # Stream can put a plateless vehicle row before a plate row for that same
    # exact associated box. Group first, then observe once: first-result order
    # must not erase the later plate or its stronger MMC evidence.
    plate_later = _result("REAL123", same_box)
    plate_later["vehicle"]["score"] = 0.97
    plate_later["model_make"] = [
        {"make": "Toyota", "model": "Camry", "score": 0.91}
    ]
    plate_later["color"] = [{"color": "blue", "score": 0.93}]
    plateless_first = {
        "vehicle": {
            "type": "Sedan", "score": 0.40,
            "box": {"xmin": same_box[0], "ymin": same_box[1],
                    "xmax": same_box[2], "ymax": same_box[3]},
        },
        "model_make": [
            {"make": "Honda", "model": "Civic", "score": 0.20}
        ],
        "color": [{"color": "grey", "score": 0.25}],
    }
    mixed = StreamWebhookEngine(_StreamCfg())
    mixed.submit_recognition_payload(_payload(
        "2026-08-28T15:03:00.500Z",
        [plateless_first, plate_later],
    ), now=200.5)
    mixed_observations = mixed.drain_vehicle_observations()
    mixed_encounter = mixed._encounters[0]
    assert len(mixed._encounters) == 1
    assert len(mixed_observations) == 1
    assert mixed_observations[0].saw_plate is True
    assert mixed_observations[0].plate_detection_confidence == 0.91
    assert mixed_observations[0].plate_ocr_confidence == 0.96
    assert (
        mixed_encounter["vehicle"].make,
        mixed_encounter["vehicle"].model,
        mixed_encounter["vehicle"].color,
    ) == ("Toyota", "Camry", "blue")
    assert "REAL123" in mixed_encounter["candidate_keys"]

    distinct = _payload(
        "2026-08-28T15:03:01Z",
        [_result("CAR111", (50, 50, 300, 300)),
         _result("CAR222", (600, 500, 900, 900))],
    )
    other = StreamWebhookEngine(_StreamCfg())
    other.submit_recognition_payload(distinct, now=201.0)
    observations = other.drain_vehicle_observations()
    assert len(observations) == 2
    assert len({vehicle.encounter_id for vehicle in observations}) == 2


def test_settled_batch_preserves_independent_votes() -> None:
    first_at = "2026-08-28T15:04:00.000Z"
    second_at = "2026-08-28T15:04:00.800Z"
    detector = Detector(_detector_cfg(), enable_vehicle_only_tracking=False)
    detector._engine = _Once([
        _read("provider-event-2", second_at),
        _read("provider-event-1", first_at),
    ])
    candidates, outcomes = detector.process_frame(_Frame())
    confirmed = [
        outcome for outcome in outcomes
        if isinstance(outcome, ConfirmedIdentification)
        and not outcome.suppressed
    ]
    # Raw drain order is deliberately newest-first; voting must still retain
    # the earliest provider timestamp as the identification's detected_at.
    assert [candidate.captured_at_iso for candidate in candidates] == [
        second_at, first_at,
    ]
    assert len(confirmed) == 1
    assert confirmed[0].frame_count == 2
    assert confirmed[0].detected_at_iso == first_at

    # Two boxes from the same provider observation are still one independent
    # source frame and must not satisfy a two-read vote by themselves.
    duplicate = Detector(_detector_cfg(), enable_vehicle_only_tracking=False)
    duplicate._engine = _Once([
        _read("same-provider-event", first_at),
        _read("same-provider-event", first_at),
    ])
    _, duplicate_outcomes = duplicate.process_frame(_Frame())
    assert not any(
        isinstance(outcome, ConfirmedIdentification)
        for outcome in duplicate_outcomes
    )


def test_memory_decay_reacquisition_is_evidence_not_an_extra_vehicle() -> None:
    """A post-decay reacquisition strengthens identity without adding traffic."""
    first_at = "2026-08-28T15:05:00.000Z"
    second_at = "2026-08-28T15:05:03.200Z"
    first_box = (100, 180, 500, 700)
    second_box = (145, 165, 545, 685)
    engine = StreamWebhookEngine(_StreamCfg())

    assert engine.submit_recognition_payload(
        _payload(first_at, [_result("ABC123", first_box)]), now=300.0
    ) is True
    first_reads = engine.infer(None, now=300.0)
    assert len(first_reads) == 1

    # A post-decay observation may report this car again once its view has
    # changed. The provider timestamp and box are both later/different, while
    # exact candidate continuity plus overlapping geometry retain one profile.
    assert engine.submit_recognition_payload(
        _payload(second_at, [_result("ABC123", second_box)]), now=303.2
    ) is True
    second_reads = engine.infer(None, now=303.2)
    assert len(second_reads) == 1
    reads = first_reads + second_reads
    assert [read.captured_at_iso for read in reads] == [first_at, second_at]
    assert [read.vehicle_bbox for read in reads] == [first_box, second_box]
    assert len({read.source_observation_id for read in reads}) == 2
    assert len({read.encounter_id for read in reads}) == 1
    assert len(engine._encounters) == 1
    encounter_id = reads[0].encounter_id

    # Deliver the provider observations in separate detector iterations. They
    # must remain two independent votes instead of being collapsed merely
    # because they belong to one physical encounter.
    detector = Detector(_detector_cfg(), enable_vehicle_only_tracking=False)
    detector._engine = _Once(first_reads)
    _, first_outcomes = detector.process_frame(_Frame())
    assert first_outcomes == []
    detector._engine = _Once(second_reads)
    _, second_outcomes = detector.process_frame(_Frame())
    confirmed = [
        outcome for outcome in second_outcomes
        if isinstance(outcome, ConfirmedIdentification)
        and not outcome.suppressed
    ]
    assert len(confirmed) == 1
    assert confirmed[0].frame_count == 2
    assert confirmed[0].encounter_id == encounter_id

    metadata = engine.mark_encounter_identified(
        encounter_id, confirmed[0].normalized
    )
    assert metadata is not None
    assert metadata["encounter_id"] == encounter_id
    assert {
        observation.encounter_id
        for observation in engine.drain_vehicle_observations()
    } == {encounter_id}

    # Once the identified-profile hold expires, the encounter is discarded;
    # it must never emerge from the anonymous terminal-vehicle drain.
    assert engine.drain_vehicles(now=604.0) == []
    assert engine._encounters == []


TESTS = (
    test_provider_timestamp_and_observation_identity,
    test_exact_retry_is_not_new_evidence,
    test_default_clock_domains_remain_separate,
    test_multi_plate_result_shares_one_vehicle_encounter,
    test_settled_batch_preserves_independent_votes,
    test_memory_decay_reacquisition_is_evidence_not_an_extra_vehicle,
)


def run_checks(check) -> None:
    """Run through the required Stream-suite check callback."""
    for test in TESTS:
        try:
            test()
        except AssertionError as exc:
            check(test.__name__, f"failed: {exc}", "pass")
        else:
            check(test.__name__, "pass", "pass")


if __name__ == "__main__":
    passed = failed = 0

    def _check(label, got, want):
        global passed, failed
        if got == want:
            passed += 1
            print(f"PASS {label}")
        else:
            failed += 1
            print(f"FAIL {label}: got={got!r}, want={want!r}")

    run_checks(_check)
    print(f"{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
