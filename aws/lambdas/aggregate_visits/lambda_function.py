import os
import time

import boto3
from boto3.dynamodb.types import TypeDeserializer

from location_time import local_day_hour


dynamodb = boto3.resource("dynamodb")
stats = dynamodb.Table(os.environ.get("STATS_TABLE", "LocationStats"))
seen = dynamodb.Table(os.environ.get("SEEN_TABLE", "DailyVehicleSeen"))
deser = TypeDeserializer()

BUCKETS = [
    ("b_0_1d", 86400),
    ("b_1_3d", 259200),
    ("b_3_7d", 604800),
    ("b_7_14d", 1209600),
    ("b_14_30d", 2592000),
    ("b_30d_plus", float("inf")),
]

# Canonical colour buckets for mix charts. Unknown labels become "other"
# so Dynamo map cardinality stays bounded.
_COLOR_CANON = {
    "white": "white",
    "black": "black",
    "gray": "gray",
    "grey": "gray",
    "silver": "silver",
    "red": "red",
    "blue": "blue",
    "green": "green",
    "yellow": "yellow",
    "gold": "gold",
    "brown": "brown",
    "orange": "orange",
    "beige": "beige",
    "maroon": "red",
    "navy": "blue",
}


def bucket_for(seconds):
    for name, ceiling in BUCKETS:
        if seconds <= ceiling:
            return name
    return "b_30d_plus"


def normalize_color(value):
    if not isinstance(value, str):
        return None
    key = value.strip().lower().replace("-", " ")
    if not key:
        return None
    if key in _COLOR_CANON:
        return _COLOR_CANON[key]
    parts = key.split()
    if parts and parts[-1] in _COLOR_CANON:
        return _COLOR_CANON[parts[-1]]
    return "other"


def normalize_make(value):
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    return key or None


def model_count_key(make, model):
    make_key = normalize_make(make)
    if not make_key or not isinstance(model, str):
        return None
    model_key = model.strip().lower()
    if not model_key:
        return None
    return f"{make_key}|{model_key}"


def bump(location_id, period, fields):
    expr = "ADD " + ", ".join(f"{key} :{key}" for key in fields)
    stats.update_item(
        Key={"location_id": location_id, "period": period},
        UpdateExpression=expr,
        ExpressionAttributeValues={f":{key}": value for key, value in fields.items()},
    )


def bump_mix(location_id, period, color=None, make=None, model_key=None):
    """Increment appearance histograms. Visit-level, not customer-level.

    Nested ADD fails if the parent map is missing, so create empty maps
    with if_not_exists before incrementing keys.
    """
    if not (color or make or model_key):
        return
    key = {"location_id": location_id, "period": period}
    stats.update_item(
        Key=key,
        UpdateExpression=(
            "SET color_counts = if_not_exists(color_counts, :empty), "
            "make_counts = if_not_exists(make_counts, :empty), "
            "model_counts = if_not_exists(model_counts, :empty)"
        ),
        ExpressionAttributeValues={":empty": {}},
    )
    names = {}
    values = {":one": 1}
    clauses = []
    if color:
        names["#ck"] = color
        clauses.append("color_counts.#ck :one")
        clauses.append("visits_with_color :one")
    if make:
        names["#mk"] = make
        clauses.append("make_counts.#mk :one")
        clauses.append("visits_with_make :one")
    if model_key:
        names["#md"] = model_key
        clauses.append("model_counts.#md :one")
        clauses.append("visits_with_model :one")
    kwargs = {
        "Key": key,
        "UpdateExpression": "ADD " + ", ".join(clauses),
        "ExpressionAttributeValues": values,
    }
    if names:
        kwargs["ExpressionAttributeNames"] = names
    stats.update_item(**kwargs)


def _plate_detection(image):
    value = image.get("plate_detection")
    if value in {"recognized", "insufficient_confidence", "no_visible_plate"}:
        return value
    # Visits written before the vehicle-centric schema all required a valid
    # plate hash, so absence of the new field means recognized legacy data.
    vehicle_hash = str(image.get("vehicle_hash", ""))
    return "no_visible_plate" if vehicle_hash.startswith("UNIDENTIFIED#") else "recognized"


def handler(event, context):
    for rec in event.get("Records", []):
        if rec.get("eventName") != "INSERT":
            continue
        image = {
            key: deser.deserialize(value)
            for key, value in rec["dynamodb"]["NewImage"].items()
        }
        location = image.get("location_id", "unknown")
        timestamp = str(image.get("timestamp", ""))
        day, hour = local_day_hour(timestamp, location)
        visit_number = int(image.get("visit_number", 1))
        is_repeat = bool(image.get("is_repeat", False))
        seconds_since_last = image.get("seconds_since_last_visit")
        plate_detection = _plate_detection(image)
        recognized = plate_detection == "recognized"

        day_fields = {
            "visits": 1,
            "repeat_visits": 1 if is_repeat else 0,
            "new_vehicle_visits": 0 if is_repeat else 1,
            "recognized_visits": 1 if recognized else 0,
            "unidentified_visits": 0 if recognized else 1,
            "insufficient_confidence_visits": (
                1 if plate_detection == "insufficient_confidence" else 0
            ),
            "no_visible_plate_visits": (
                1 if plate_detection == "no_visible_plate" else 0
            ),
        }
        if seconds_since_last is not None:
            day_fields[bucket_for(float(seconds_since_last))] = 1
            day_fields["interval_sum_s"] = int(float(seconds_since_last))
            day_fields["interval_count"] = 1
        if recognized and visit_number == 5:
            day_fields["vip_promotions"] = 1
        if image.get("prev_location_id") and image["prev_location_id"] != location:
            day_fields["cross_location_visits"] = 1

        try:
            seen.put_item(
                Item={
                    "pk": f"LOC#{location}#{day}",
                    "vehicle_hash": image["vehicle_hash"],
                    "expires_at": int(time.time()) + 3 * 86400,
                },
                ConditionExpression="attribute_not_exists(vehicle_hash)",
            )
            day_fields["unique_vehicles"] = 1
        except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
            pass

        bump(location, f"DAY#{day}", day_fields)
        bump_mix(
            location,
            f"DAY#{day}",
            color=normalize_color(image.get("color")),
            make=normalize_make(image.get("make")),
            model_key=model_count_key(image.get("make"), image.get("model")),
        )
        bump(
            location,
            f"HOUR#{hour}",
            {
                "visits": 1,
                "repeat_visits": 1 if is_repeat else 0,
                "recognized_visits": 1 if recognized else 0,
                "unidentified_visits": 0 if recognized else 1,
            },
        )
        bump(
            location,
            "TOTAL",
            {
                "visits": 1,
                "repeat_visits": 1 if is_repeat else 0,
                "recognized_visits": 1 if recognized else 0,
                "unidentified_visits": 0 if recognized else 1,
            },
        )
    return {"ok": True}
