import importlib.util
import json
import os
import re
import sys
import types
import unittest
from decimal import Decimal
from pathlib import Path


AWS_ROOT = Path(__file__).resolve().parents[1]


class FakeExpression:
    def __init__(self, value):
        self.value = value

    def __and__(self, other):
        return FakeExpression(("and", self.value, other.value))


class FakeKey:
    def __init__(self, name):
        self.name = name

    def eq(self, value):
        return FakeExpression(("eq", self.name, value))

    def between(self, start, end):
        return FakeExpression(("between", self.name, start, end))


class FakeAttr:
    def __init__(self, name):
        self.name = name

    def eq(self, value):
        return FakeExpression(("attr_eq", self.name, value))

    def not_exists(self):
        return FakeExpression(("not_exists", self.name))

    def __or__(self, other):
        return FakeExpression(("or", self, other))

    def __and__(self, other):
        return FakeExpression(("and", self, other))


class FakeTypeDeserializer:
    def deserialize(self, value):
        if "S" in value:
            return value["S"]
        if "N" in value:
            number = value["N"]
            return float(number) if "." in number else int(number)
        if "BOOL" in value:
            return value["BOOL"]
        if "NULL" in value:
            return None
        raise ValueError(f"unsupported DynamoDB value: {value}")


class ConditionalCheckFailedException(Exception):
    """Mirrors the real botocore exception name/shape closely enough that
    `except dynamodb.meta.client.exceptions.ConditionalCheckFailedException`
    — the exact idiom aggregate_visits.py already uses — resolves under the
    fake without the calling code needing to know it's under test."""


class GoneException(Exception):
    """Mirrors apigatewaymanagementapi's stale-connection exception."""


class _FakeExceptions:
    ConditionalCheckFailedException = ConditionalCheckFailedException


class _FakeClient:
    def __init__(self):
        self.exceptions = _FakeExceptions()


class _FakeMeta:
    def __init__(self):
        self.client = _FakeClient()


class FakeTable:
    def __init__(self):
        self.items = []
        self.updates = []
        self.query_results = []
        self.scan_result = {"Items": []}
        # Numeric ADD-accumulator state, keyed by the sorted Key tuple, so
        # ReturnValues="UPDATED_NEW" can report a real running total the way
        # DynamoDB does — separate from `updates`, which stays the raw call
        # log every existing test already asserts against.
        self._records = {}

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        condition = kwargs.get("ConditionExpression")
        if condition:
            # Only the one idiom this codebase actually uses:
            # attribute_not_exists(<sort_key_attr>), meaning "insert only if
            # no item already exists at this item's own primary key". Fake
            # tables aren't schema-aware, so uniqueness is approximated as
            # (pk, <that attribute>) — true for every table this harness
            # loads (DailyVehicleSeen: pk + vehicle_hash).
            match = re.match(r"attribute_not_exists\((\w+)\)", condition)
            if match:
                attr = match.group(1)
                pk_val = item.get("pk")
                attr_val = item.get(attr)
                for existing in self.items:
                    if (
                        existing.get(attr) == attr_val
                        and existing.get("pk") == pk_val
                    ):
                        raise ConditionalCheckFailedException()
        self.items.append(item)
        return {}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        expr = kwargs.get("UpdateExpression", "")
        values = kwargs.get("ExpressionAttributeValues", {})
        if expr.strip().startswith("ADD"):
            key_tuple = tuple(sorted(kwargs.get("Key", {}).items()))
            record = self._records.setdefault(key_tuple, {})
            for clause in expr.strip()[len("ADD"):].split(","):
                attr, placeholder = clause.split()
                record[attr] = record.get(attr, 0) + values[placeholder]
            if kwargs.get("ReturnValues") == "UPDATED_NEW":
                return {"Attributes": dict(record)}
        return {}

    def query(self, **kwargs):
        if self.query_results:
            return self.query_results.pop(0)
        return {"Items": list(self.items)}

    def scan(self, **kwargs):
        return self.scan_result


class FakeApiGatewayManagementApi:
    """Fake `boto3.client("apigatewaymanagementapi", ...)`."""

    def __init__(self):
        self.sent = []
        self.exceptions = types.SimpleNamespace(GoneException=GoneException)
        # Connection ids that should behave as already-disconnected.
        self.gone_connection_ids = set()

    def post_to_connection(self, **kwargs):
        if kwargs.get("ConnectionId") in self.gone_connection_ids:
            raise GoneException()
        self.sent.append(kwargs)
        return {}


class FakeDynamoDB:
    def __init__(self):
        self.tables = {}
        self.meta = _FakeMeta()
        self.apigw = FakeApiGatewayManagementApi()

    def Table(self, name):
        return self.tables.setdefault(name, FakeTable())


def load_lambda(name, relative_path):
    fake_db = FakeDynamoDB()
    boto3 = types.ModuleType("boto3")
    boto3.resource = lambda service: fake_db
    boto3.client = (
        lambda service, **kwargs: fake_db.apigw
        if service == "apigatewaymanagementapi" else None
    )
    conditions = types.ModuleType("boto3.dynamodb.conditions")
    conditions.Key = FakeKey
    conditions.Attr = FakeAttr
    dynamodb = types.ModuleType("boto3.dynamodb")
    dynamodb.conditions = conditions
    types_module = types.ModuleType("boto3.dynamodb.types")
    types_module.TypeDeserializer = FakeTypeDeserializer
    dynamodb.types = types_module

    previous = {
        key: sys.modules.get(key)
        for key in (
            "boto3",
            "boto3.dynamodb",
            "boto3.dynamodb.conditions",
            "boto3.dynamodb.types",
        )
    }
    sys.modules["boto3"] = boto3
    sys.modules["boto3.dynamodb"] = dynamodb
    sys.modules["boto3.dynamodb.conditions"] = conditions
    sys.modules["boto3.dynamodb.types"] = types_module
    lambdas_dir = str(AWS_ROOT / "lambdas")
    path_added = lambdas_dir not in sys.path
    if path_added:
        sys.path.insert(0, lambdas_dir)
    try:
        spec = importlib.util.spec_from_file_location(name, AWS_ROOT / relative_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if path_added:
            sys.path.remove(lambdas_dir)
        for key, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = old_module
    return module, fake_db


class InsertDetectionEventTests(unittest.TestCase):
    def setUp(self):
        self.module, self.db = load_lambda(
            "insert_detection_event_test",
            "lambdas/insert_detection_event/lambda_function.py",
        )
        self.table = self.db.tables["DetectionEvents"]

    @staticmethod
    def event(outcome, **body):
        body["outcome"] = outcome
        return {
            "requestContext": {"authorizer": {"lambda": {"locationId": "loc-1"}}},
            "body": json.dumps(body),
        }

    def test_group_floor_is_stored_as_diagnostic(self):
        result = self.module.handler(
            self.event(
                "group_confidence_floor",
                frameCount=4,
                durationSeconds=12,
                hadPlateCandidate=True,
            ),
            None,
        )

        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(self.table.items[0]["outcome"], "group_confidence_floor")
        self.assertNotIn("duration_seconds", self.table.items[0])
        self.assertNotIn("had_plate_candidate", self.table.items[0])

    def test_vehicle_only_remains_rejected(self):
        result = self.module.handler(self.event("vehicle_only"), None)

        self.assertEqual(result["statusCode"], 400)
        self.assertEqual(self.table.items, [])

    def test_missing_authorized_location_is_rejected(self):
        result = self.module.handler(
            {"requestContext": {"authorizer": {"lambda": {}}},
             "body": json.dumps({"outcome": "below_threshold"})},
            None,
        )

        self.assertEqual(result["statusCode"], 403)
        self.assertEqual(self.table.items, [])

    def test_tiered_miss_outcomes_are_preserved(self):
        for outcome in ("below_threshold", "unreadable"):
            result = self.module.handler(self.event(outcome), None)
            self.assertEqual(result["statusCode"], 200)
        self.assertEqual(
            [item["outcome"] for item in self.table.items],
            ["below_threshold", "unreadable"],
        )

    def test_drive_off_keeps_vehicle_evidence(self):
        result = self.module.handler(
            self.event("drive_off", durationSeconds=8, hadPlateCandidate=True),
            None,
        )

        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(self.table.items[0]["duration_seconds"], 8)
        self.assertTrue(self.table.items[0]["had_plate_candidate"])

    def test_paired_episode_telemetry_is_bounded_and_whitelisted(self):
        episode_id = "9f" * 16
        encounter_id = "8e" * 16
        result = self.module.handler(
            self.event(
                "below_threshold",
                bestJointDet=0.8086,
                bestJointOcr=0.701,
                bestJointClearedDet=True,
                framesClearingDet=1,
                framesClearingOcr=1,
                framesClearingBoth=0,
                readsInEpisode=2,
                framesAdmitted=0,
                maxBucketSize=0,
                episodeId=episode_id,
                encounterId=encounter_id,
                episodeStart="2026-08-10T16:45:00.000Z",
                episodeEnd="2026-08-10T16:45:23.000Z",
                rejectReasonCounts={
                    "failed_det_only": 1,
                    "failed_ocr_only": 1,
                    "failed_both": 0,
                    "attacker_controlled": 999,
                },
            ),
            None,
        )

        self.assertEqual(result["statusCode"], 200)
        item = self.table.items[0]
        self.assertEqual(
            item["best_joint_detection_confidence"], Decimal("0.8086")
        )
        self.assertEqual(item["best_joint_ocr_confidence"], Decimal("0.701"))
        self.assertEqual(item["episode_id"], episode_id)
        self.assertEqual(item["encounter_id"], encounter_id)
        self.assertEqual(item["reads_in_episode"], 2)
        self.assertEqual(
            item["reject_reason_counts"],
            {
                "failed_det_only": 1,
                "failed_ocr_only": 1,
                "failed_both": 0,
            },
        )

    def test_malformed_optional_telemetry_is_dropped_not_persisted(self):
        result = self.module.handler(
            self.event(
                "unreadable",
                bestJointDet=999,
                framesClearingDet=-1,
                episodeId="not-a-uuid",
                rejectReasonCounts={"failed_both": "not-an-int"},
            ),
            None,
        )

        self.assertEqual(result["statusCode"], 200)
        item = self.table.items[0]
        self.assertNotIn("best_joint_detection_confidence", item)
        self.assertNotIn("frames_clearing_detection", item)
        self.assertNotIn("episode_id", item)
        self.assertNotIn("reject_reason_counts", item)

    def test_non_object_body_is_rejected(self):
        result = self.module.handler(
            {
                "requestContext": {
                    "authorizer": {"lambda": {"locationId": "loc-1"}}
                },
                "body": "[]",
            },
            None,
        )

        self.assertEqual(result["statusCode"], 400)
        self.assertEqual(self.table.items, [])


class DetectionEventIsDiagnosticTests(unittest.TestCase):
    """DetectionEvents never change traffic or KPI counters."""

    RELATIVE_PATH = "lambdas/insert_detection_event/lambda_function.py"

    def _load(self, presence_disabled_locations="loc-1"):
        """Fresh module instance with PRESENCE_DISABLED_LOCATIONS set for
        just this test — the env var is read at module-exec time, so each
        variant needs its own load_lambda() call."""
        old = os.environ.get("PRESENCE_DISABLED_LOCATIONS")
        os.environ["PRESENCE_DISABLED_LOCATIONS"] = presence_disabled_locations

        def _restore():
            if old is None:
                os.environ.pop("PRESENCE_DISABLED_LOCATIONS", None)
            else:
                os.environ["PRESENCE_DISABLED_LOCATIONS"] = old

        self.addCleanup(_restore)
        return load_lambda("insert_detection_event_vcp_test", self.RELATIVE_PATH)

    @staticmethod
    def event(outcome, **body):
        body["outcome"] = outcome
        return {
            "requestContext": {"authorizer": {"lambda": {"locationId": "loc-1"}}},
            "body": json.dumps(body),
        }

    def test_detection_episode_is_diagnostic_only(self):
        module, db = self._load()
        episode_id = "a1" * 16

        result = module.handler(
            self.event("below_threshold", episodeId=episode_id), None
        )

        self.assertEqual(result["statusCode"], 200)
        self.assertNotIn("LocationStats", db.tables)
        self.assertEqual(db.apigw.sent, [])
        self.assertNotIn("DailyVehicleSeen", db.tables)

    def test_duplicate_episode_retry_does_not_double_count_or_double_push(self):
        module, db = self._load()
        episode_id = "b2" * 16
        request = self.event("below_threshold", episodeId=episode_id)

        module.handler(request, None)
        module.handler(request, None)

        self.assertEqual(len(db.tables["DetectionEvents"].items), 2)
        self.assertNotIn("LocationStats", db.tables)
        self.assertEqual(db.apigw.sent, [])

    def test_drive_off_never_touches_new_counter(self):
        module, db = self._load()

        result = module.handler(
            self.event("drive_off", episodeId="c3" * 16, durationSeconds=8),
            None,
        )

        self.assertEqual(result["statusCode"], 200)
        self.assertNotIn("LocationStats", db.tables)
        self.assertEqual(db.apigw.sent, [])
        self.assertNotIn("DailyVehicleSeen", db.tables)

    def test_presence_on_location_is_never_counted(self):
        # loc-1 (what `event()` authenticates as) is deliberately absent
        # from the allowlist here — this is the regression test for the
        # double-count finding: a presence-ON site's frame-level misses
        # must never touch the new counter.
        module, db = self._load(presence_disabled_locations="some-other-loc")

        result = module.handler(
            self.event("below_threshold", episodeId="d4" * 16), None
        )

        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(len(db.tables["DetectionEvents"].items), 1)
        self.assertNotIn("LocationStats", db.tables)
        self.assertEqual(db.apigw.sent, [])
        self.assertNotIn("DailyVehicleSeen", db.tables)

    def test_missing_or_invalid_episode_id_skips_counting_without_crashing(self):
        module, db = self._load()

        result = module.handler(self.event("below_threshold"), None)  # no episodeId

        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(len(db.tables["DetectionEvents"].items), 1)
        self.assertNotIn("LocationStats", db.tables)
        self.assertEqual(db.apigw.sent, [])

    def test_raw_write_has_no_counter_dependency(self):
        module, db = self._load()

        result = module.handler(
            self.event("below_threshold", episodeId="e5" * 16), None
        )

        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(len(db.tables["DetectionEvents"].items), 1)

    def test_real_time_and_nightly_hold_legacy_estimate_at_zero(self):
        module, db = self._load()

        episodes = ["f5" * 16, "f6" * 16, "f7" * 16]
        for episode_id in episodes:
            module.handler(self.event("below_threshold", episodeId=episode_id), None)
        module.handler(  # retry of the first episode
            self.event("below_threshold", episodeId=episodes[0]), None
        )

        self.assertNotIn("LocationStats", db.tables)

        old = os.environ.get("PRESENCE_DISABLED_LOCATIONS")
        os.environ["PRESENCE_DISABLED_LOCATIONS"] = "loc-1"
        try:
            nightly_module, nightly_db = load_lambda(
                "aggregate_detection_events_convergence_test",
                "lambdas/aggregate_detection_events/lambda_function.py",
            )
        finally:
            if old is None:
                os.environ.pop("PRESENCE_DISABLED_LOCATIONS", None)
            else:
                os.environ["PRESENCE_DISABLED_LOCATIONS"] = old

        nightly_db.tables["Devices"].scan_result = {
            "Items": [{"location_id": "loc-1", "status": "active"}]
        }
        # Feed the nightly job the exact raw log the real-time path produced.
        nightly_db.tables["DetectionEvents"].items = (
            db.tables["DetectionEvents"].items
        )

        nightly_module.handler({}, None)

        nightly_total = nightly_db.tables["LocationStats"].updates[0][
            "ExpressionAttributeValues"
        ][":eav"]
        self.assertEqual(nightly_total, 0)


class AggregateDetectionEventTests(unittest.TestCase):
    def test_group_floor_and_drive_off_are_separate_counters(self):
        module, db = load_lambda(
            "aggregate_detection_events_test",
            "lambdas/aggregate_detection_events/lambda_function.py",
        )
        db.tables["Devices"].scan_result = {
            "Items": [{"location_id": "loc-1", "status": "active"}]
        }
        db.tables["DetectionEvents"].items = [
            {"outcome": "group_confidence_floor"},
            {"outcome": "group_confidence_floor"},
            {"outcome": "drive_off", "had_plate_candidate": True},
            {"outcome": "below_threshold"},
            {"outcome": "unreadable"},
        ]

        module.handler({}, None)

        values = db.tables["LocationStats"].updates[0]["ExpressionAttributeValues"]
        self.assertEqual(values[":fgf"], 2)
        self.assertEqual(values[":do"], 1)
        self.assertEqual(values[":dohp"], 1)
        self.assertEqual(values[":fbt"], 1)
        self.assertEqual(values[":fur"], 1)

    def test_estimated_additional_vehicles_is_zero_for_all_locations(self):
        old = os.environ.get("PRESENCE_DISABLED_LOCATIONS")
        os.environ["PRESENCE_DISABLED_LOCATIONS"] = "loc-1"
        try:
            module, db = load_lambda(
                "aggregate_detection_events_vcp_test",
                "lambdas/aggregate_detection_events/lambda_function.py",
            )
        finally:
            if old is None:
                os.environ.pop("PRESENCE_DISABLED_LOCATIONS", None)
            else:
                os.environ["PRESENCE_DISABLED_LOCATIONS"] = old

        db.tables["Devices"].scan_result = {
            "Items": [
                {"location_id": "loc-1", "status": "active"},
                {"location_id": "loc-2", "status": "active"},
            ]
        }
        # loc-1: episode A appears twice (same vehicle, two frame-level
        # reads), episode B once, one drive_off (must be excluded), one row
        # with no episode_id at all (must not crash / must not count).
        shared_items = [
            {"outcome": "below_threshold", "episode_id": "a" * 32},
            {"outcome": "low_confidence", "episode_id": "a" * 32},
            {"outcome": "unreadable", "episode_id": "b" * 32},
            {"outcome": "drive_off", "episode_id": "c" * 32},
            {"outcome": "below_threshold"},
        ]

        def query_side_effect(**kwargs):
            loc = kwargs["KeyConditionExpression"].value[1][2]
            return {"Items": shared_items if loc == "loc-1" else []}

        db.tables["DetectionEvents"].query = query_side_effect

        module.handler({}, None)

        updates = {
            call["Key"]["location_id"]: call["ExpressionAttributeValues"][":eav"]
            for call in db.tables["LocationStats"].updates
        }
        self.assertEqual(updates["loc-1"], 0)
        self.assertEqual(updates["loc-2"], 0)


class AnalyticsTests(unittest.TestCase):
    def test_group_floor_affects_attempts_not_vehicle_count(self):
        module, _ = load_lambda(
            "analytics_test",
            "lambdas/analytics/lambda_function.py",
        )

        result = module.build_identification_section([{
            "visits": 4,
            "recognized_visits": 3,
            "unidentified_visits": 1,
            "insufficient_confidence_visits": 1,
            "failed_group_floor": 2,
            "failed_below_threshold": 2,
            "failed_unreadable": 1,
            "drive_offs": 7,
        }])

        self.assertEqual(result["failed_group_confidence_floor"], 2)
        self.assertEqual(result["total_attempts"], 8)
        self.assertEqual(result["success_rate"], 0.375)
        self.assertEqual(result["total_vehicles"], 4)
        self.assertEqual(result["drive_off_rate"], 0.25)
        self.assertEqual(result["recognized_vehicles"], 3)
        self.assertEqual(result["unidentified_vehicles"], 1)
        self.assertEqual(result["identifiable_vehicles"], 4)
        self.assertEqual(result["missed_identifiable_vehicles"], 1)
        self.assertEqual(result["identification_rate"], 0.75)
        self.assertEqual(result["missed_identifiable_rate"], 0.25)
        self.assertEqual(result["vehicle_recognition_rate"], 0.75)
        self.assertEqual(result["total_vehicles_including_estimate"], 4)
        self.assertEqual(result["recognition_rate_including_estimate"], 0.75)
        self.assertEqual(result["failed_below_threshold"], 2)
        self.assertEqual(result["failed_unreadable"], 1)


class AggregateVisitsTests(unittest.TestCase):
    def test_anonymous_visit_is_counted_once_but_not_as_recognized(self):
        module, db = load_lambda(
            "aggregate_visits_test",
            "lambdas/aggregate_visits/lambda_function.py",
        )
        module.handler(
            {
                "Records": [{
                    "eventName": "INSERT",
                    "dynamodb": {
                        "NewImage": {
                            "vehicle_hash": {"S": "UNIDENTIFIED#visit-1"},
                            "timestamp": {"S": "2026-08-06T00:00:00Z"},
                            "location_id": {"S": "loc-1"},
                            "plate_detection": {"S": "no_visible_plate"},
                            "visit_number": {"N": "1"},
                            "is_repeat": {"BOOL": False},
                        }
                    },
                }]
            },
            None,
        )

        daily = db.tables["LocationStats"].updates[0]
        self.assertEqual(daily["Key"]["period"], "DAY#2026-08-05")
        daily_vals = daily["ExpressionAttributeValues"]
        self.assertEqual(daily_vals[":visits"], 1)
        self.assertEqual(daily_vals[":recognized_visits"], 0)
        self.assertEqual(daily_vals[":unidentified_visits"], 1)
        self.assertEqual(daily_vals[":no_visible_plate_visits"], 1)
        hour = db.tables["LocationStats"].updates[1]
        self.assertEqual(hour["Key"]["period"], "HOUR#2026-08-05T20")
        self.assertEqual(len(db.tables["LocationStats"].updates), 3)

    def test_appearance_is_counted_on_anonymous_visits_too(self):
        module, db = load_lambda(
            "aggregate_visits_mmc_test",
            "lambdas/aggregate_visits/lambda_function.py",
        )
        self.assertEqual(module.normalize_color(" Grey "), "gray")
        self.assertEqual(module.normalize_color("dark blue"), "blue")
        self.assertEqual(module.normalize_color("chartreuse"), "other")
        self.assertEqual(
            module.model_count_key("Toyota", "Camry"), "toyota|camry"
        )
        module.handler(
            {
                "Records": [{
                    "eventName": "INSERT",
                    "dynamodb": {
                        "NewImage": {
                            "vehicle_hash": {"S": "UNIDENTIFIED#visit-2"},
                            "timestamp": {"S": "2026-08-18T16:00:00Z"},
                            "location_id": {"S": "loc-1"},
                            "plate_detection": {"S": "no_visible_plate"},
                            "visit_number": {"N": "1"},
                            "is_repeat": {"BOOL": False},
                            "color": {"S": "Green"},
                            "make": {"S": "Ford"},
                            "model": {"S": "F-150"},
                        }
                    },
                }]
            },
            None,
        )
        mix = db.tables["LocationStats"].updates[2]
        self.assertEqual(mix["Key"]["period"], "DAY#2026-08-18")
        hour = db.tables["LocationStats"].updates[3]
        self.assertEqual(hour["Key"]["period"], "HOUR#2026-08-18T12")
        self.assertIn("color_counts.#ck :one", mix["UpdateExpression"])
        self.assertEqual(mix["ExpressionAttributeNames"]["#ck"], "green")
        self.assertEqual(mix["ExpressionAttributeNames"]["#mk"], "ford")
        self.assertEqual(
            mix["ExpressionAttributeNames"]["#md"], "ford|f-150"
        )
        init = db.tables["LocationStats"].updates[1]
        self.assertIn("if_not_exists(color_counts", init["UpdateExpression"])


    def test_ljs_evening_utc_stays_on_the_eastern_calendar_day(self):
        module, db = load_lambda(
            "aggregate_visits_tz_test",
            "lambdas/aggregate_visits/lambda_function.py",
        )
        module.handler(
            {
                "Records": [{
                    "eventName": "INSERT",
                    "dynamodb": {
                        "NewImage": {
                            "vehicle_hash": {"S": "ab" * 32},
                            "timestamp": {"S": "2026-08-19T00:19:39.761Z"},
                            "location_id": {"S": "ljs-louisville-001"},
                            "plate_detection": {"S": "recognized"},
                            "visit_number": {"N": "1"},
                            "is_repeat": {"BOOL": False},
                        }
                    },
                }]
            },
            None,
        )
        periods = [call["Key"]["period"] for call in db.tables["LocationStats"].updates]
        self.assertIn("DAY#2026-08-18", periods)
        self.assertIn("HOUR#2026-08-18T20", periods)
        self.assertNotIn("DAY#2026-08-19", periods)


class VehicleMixAnalyticsTests(unittest.TestCase):
    def test_vehicle_mix_sums_maps_and_reports_coverage(self):
        module, _ = load_lambda(
            "analytics_mix_test",
            "lambdas/analytics/lambda_function.py",
        )
        result = module.build_vehicle_mix_section([
            {
                "visits": 3,
                "visits_with_color": 2,
                "visits_with_make": 2,
                "visits_with_model": 1,
                "color_counts": {"green": 2, "white": 0},
                "make_counts": {"ford": 1, "toyota": 1},
                "model_counts": {"ford|f-150": 1},
            },
            {
                "visits": 1,
                "visits_with_color": 1,
                "visits_with_make": 1,
                "visits_with_model": 1,
                "color_counts": {"green": 1},
                "make_counts": {"toyota": 1},
                "model_counts": {"toyota|camry": 1},
            },
        ])
        self.assertEqual(result["colors"]["green"], 3)
        self.assertEqual(result["makes"]["toyota"], 2)
        self.assertEqual(
            result["top_models"][0],
            {"make": "ford", "model": "f-150", "visits": 1},
        )
        self.assertEqual(result["coverage"]["visits"], 4)
        self.assertEqual(result["coverage"]["with_color"], 3)
        self.assertEqual(result["coverage"]["color_rate"], 0.75)

    def test_identification_section_unchanged_when_mix_absent(self):
        module, _ = load_lambda(
            "analytics_mix_compat_test",
            "lambdas/analytics/lambda_function.py",
        )
        result = module.build_identification_section([{
            "visits": 4,
            "recognized_visits": 3,
            "unidentified_visits": 1,
            "insufficient_confidence_visits": 1,
            "failed_group_floor": 2,
            "failed_below_threshold": 2,
            "failed_unreadable": 1,
            "drive_offs": 7,
        }])
        self.assertEqual(result["total_vehicles"], 4)
        self.assertEqual(result["recognized_vehicles"], 3)
        empty = module.build_vehicle_mix_section([{"visits": 4}])
        self.assertEqual(empty["colors"], {})
        self.assertEqual(empty["coverage"]["color_rate"], 0)


class GetCustomersTests(unittest.TestCase):
    def test_appearance_fields_pass_through(self):
        module, db = load_lambda(
            "get_customers_test",
            "lambdas/get_customers/lambda_function.py",
        )
        db.tables["Customers"].items.append({
            "vehicle_hash": "ab" * 32,
            "location_id": "loc-1",
            "visit_count": 3,
            "color": "green",
            "make": "Ford",
            "model": "F-150",
        })
        result = module.handler(
            {"queryStringParameters": {"location_id": "loc-1"}},
            None,
        )
        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        guest = body["customers"][0]
        self.assertEqual(guest["color"], "green")
        self.assertEqual(guest["make"], "Ford")
        self.assertEqual(guest["model"], "F-150")
        self.assertIsNone(body["customers"][0].get("licensePlate"))


if __name__ == "__main__":
    unittest.main()
