"""Aggregate daily recognition diagnostics and vehicle-level drive-offs."""

import os

import boto3
from boto3.dynamodb.conditions import Key

from location_time import local_date_utc_range, local_yesterday


dynamodb = boto3.resource("dynamodb")
events_table = dynamodb.Table(os.environ.get("EVENTS_TABLE", "DetectionEvents"))
stats_table = dynamodb.Table(os.environ.get("STATS_TABLE", "LocationStats"))
devices_table = dynamodb.Table(os.environ.get("DEVICES_TABLE", "Devices"))

OUTCOME_FIELDS = {
    "no_plate_detected": "failed_no_plate",
    "low_confidence": "failed_low_confidence",
    "vote_disagreement": "failed_vote_disagreement",
    "group_confidence_floor": "failed_group_floor",
    "below_threshold": "failed_below_threshold",
    "unreadable": "failed_unreadable",
    "drive_off": "drive_offs",
    "unknown": "failed_unknown",
}

def handler(event, context):
    locations = _active_location_ids()
    results = {}
    periods = {}

    for location_id in locations:
        local_day = local_yesterday(location_id)
        day_start, day_end = local_date_utc_range(location_id, local_day)
        period_key = f"DAY#{local_day}"
        periods[location_id] = period_key
        counts = {field: 0 for field in OUTCOME_FIELDS.values()}
        drive_off_had_plate = 0
        drive_off_split = 0

        response = events_table.query(
            KeyConditionExpression=(
                Key("location_id").eq(location_id)
                & Key("timestamp").between(day_start, day_end)
            )
        )
        items = response.get("Items", [])
        while "LastEvaluatedKey" in response:
            response = events_table.query(
                KeyConditionExpression=(
                    Key("location_id").eq(location_id)
                    & Key("timestamp").between(day_start, day_end)
                ),
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            items.extend(response.get("Items", []))

        for item in items:
            outcome = item.get("outcome", "unknown")
            field = OUTCOME_FIELDS.get(outcome, "failed_unknown")
            counts[field] += 1
            if outcome == "drive_off":
                if item.get("had_plate_candidate"):
                    drive_off_had_plate += 1
                if item.get("split_from_color"):
                    drive_off_split += 1

        # DetectionEvents are diagnostics, not vehicle records. A single car
        # can open multiple read episodes and can later confirm, so deriving
        # traffic from episode_id double-counts physical vehicles. Keep the
        # legacy field pinned to zero while old dashboard clients migrate.
        estimated_additional_vehicles = 0

        stats_table.update_item(
            Key={"location_id": location_id, "period": period_key},
            UpdateExpression=(
                "SET failed_no_plate = :fnp, "
                "failed_low_confidence = :flc, "
                "failed_vote_disagreement = :fvd, "
                "failed_group_floor = :fgf, "
                "failed_below_threshold = :fbt, "
                "failed_unreadable = :fur, "
                "drive_offs = :do, "
                "failed_unknown = :fu, "
                "drive_offs_had_plate_candidate = :dohp, "
                "drive_offs_split_from_color = :dosc, "
                "estimated_additional_vehicles = :eav"
            ),
            ExpressionAttributeValues={
                ":fnp": counts["failed_no_plate"],
                ":flc": counts["failed_low_confidence"],
                ":fvd": counts["failed_vote_disagreement"],
                ":fgf": counts["failed_group_floor"],
                ":fbt": counts["failed_below_threshold"],
                ":fur": counts["failed_unreadable"],
                ":do": counts["drive_offs"],
                ":fu": counts["failed_unknown"],
                ":dohp": drive_off_had_plate,
                ":dosc": drive_off_split,
                ":eav": estimated_additional_vehicles,
            },
        )
        results[location_id] = {
            **counts,
            "estimated_additional_vehicles": estimated_additional_vehicles,
        }

    return {"ok": True, "periods": periods, "locations": results}


def _active_location_ids() -> list[str]:
    seen = set()
    response = devices_table.scan(
        ProjectionExpression="location_id, #s",
        ExpressionAttributeNames={"#s": "status"},
    )
    for item in response.get("Items", []):
        if item.get("status") == "active" and item.get("location_id"):
            seen.add(item["location_id"])
    while "LastEvaluatedKey" in response:
        response = devices_table.scan(
            ProjectionExpression="location_id, #s",
            ExpressionAttributeNames={"#s": "status"},
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        for item in response.get("Items", []):
            if item.get("status") == "active" and item.get("location_id"):
                seen.add(item["location_id"])
    return list(seen)
