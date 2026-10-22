"""Paginated vehicle/customer profiles for the operator dashboard.

The authenticated device-key authorizer scopes ``location_id`` before this
handler runs. Vehicle appearance is descriptive only and never participates
in the profile identity, which remains the on-device plate hash.
"""

import base64
import json
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key


dynamodb = boto3.resource("dynamodb")
customers = dynamodb.Table(os.environ.get("CUSTOMERS_TABLE", "Customers"))
LOCATION_INDEX = os.environ.get("LOCATION_INDEX", "location-index")

DEFAULT_LIMIT = 25
MAX_LIMIT = 100


def tier_for(visit_count: int) -> str:
    """Mirror InsertData's customer recommendation thresholds."""
    if visit_count <= 1:
        return "new"
    if visit_count <= 4:
        return "returning"
    if visit_count <= 9:
        return "vip"
    return "super_regular"


def compute_at_risk(last_seen_iso, avg_days_between_visits, visit_count):
    """Compute cadence risk at read time so it can change without a visit."""
    if not last_seen_iso or visit_count < 2 or not avg_days_between_visits:
        return False
    try:
        last_seen = datetime.fromisoformat(last_seen_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    days_since = (
        datetime.now(timezone.utc) - last_seen
    ).total_seconds() / 86400
    return days_since > max(14, avg_days_between_visits * 2)


def handler(event, context):
    qs = event.get("queryStringParameters") or {}
    location_id = qs.get("location_id")
    if not location_id:
        return resp(400, {"error": "location_id query parameter is required"})

    try:
        limit = min(max(1, int(qs.get("limit", DEFAULT_LIMIT))), MAX_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT

    guest_type = qs.get("guest_type")
    status_filter = qs.get("status")
    loyalty_filter = qs.get("loyalty")
    search = (qs.get("search") or "").strip().lower()
    start_key = _decode_cursor(qs.get("cursor"))

    query_args = {
        "IndexName": LOCATION_INDEX,
        "KeyConditionExpression": Key("location_id").eq(location_id),
        "Limit": limit,
    }
    if start_key:
        query_args["ExclusiveStartKey"] = start_key

    filter_exprs = []
    if loyalty_filter == "member":
        filter_exprs.append(Attr("loyalty_member").eq(True))
    elif loyalty_filter == "non_member":
        filter_exprs.append(Attr("loyalty_member").eq(False))
    elif loyalty_filter == "unknown":
        filter_exprs.append(
            Attr("loyalty_member").not_exists()
            | Attr("loyalty_member").eq(None)
        )
    if filter_exprs:
        combined = filter_exprs[0]
        for expression in filter_exprs[1:]:
            combined = combined & expression
        query_args["FilterExpression"] = combined

    result = customers.query(**query_args)
    items = []
    for raw in result.get("Items", []):
        visit_count = int(raw.get("visit_count", 0))
        guest_tier = tier_for(visit_count)
        if guest_type and guest_tier != guest_type:
            continue

        vehicle_hash = raw.get("vehicle_hash", "")
        if search and not vehicle_hash.lower().startswith(search):
            continue

        avg_days_between = _num(raw.get("avg_days_between_visits"))
        at_risk = compute_at_risk(
            raw.get("last_seen"), avg_days_between, visit_count
        )
        if status_filter == "at_risk" and not at_risk:
            continue

        items.append(
            {
                "serv_id": _serv_id(vehicle_hash),
                "vehicle_hash": vehicle_hash,
                "guest_type": guest_tier,
                "visits": visit_count,
                "first_seen": raw.get("first_seen"),
                "last_seen": raw.get("last_seen"),
                "common_daypart": raw.get("common_daypart"),
                "avg_days_between_visits": avg_days_between,
                "avg_visits_per_month": _num(
                    raw.get("avg_visits_per_month")
                ),
                "at_risk": at_risk,
                # Highest-confidence MMC observed for this recognized vehicle.
                "color": raw.get("color"),
                "color_confidence": _num(raw.get("color_confidence")),
                "make": raw.get("make"),
                "make_confidence": _num(raw.get("make_confidence")),
                "model": raw.get("model"),
                "model_confidence": _num(raw.get("model_confidence")),
                "vehicle_type": raw.get("vehicle_type"),
                "loyalty": {
                    "member": _loyalty_member(raw),
                    "source": raw.get("loyalty_source"),
                    "last_synced": raw.get("loyalty_last_synced"),
                },
            }
        )

    return resp(
        200,
        {
            "location_id": location_id,
            "count": len(items),
            "customers": items,
            "cursor": _encode_cursor(result.get("LastEvaluatedKey")),
        },
    )


def _serv_id(vehicle_hash: str) -> str:
    if not vehicle_hash:
        return "Serv-000000"
    return "Serv-" + vehicle_hash[:6].upper()


def _loyalty_member(raw):
    if "loyalty_member" not in raw:
        return None
    value = raw.get("loyalty_member")
    return None if value is None else bool(value)


def _num(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return value


def _encode_cursor(last_key):
    if not last_key:
        return None
    return base64.urlsafe_b64encode(
        json.dumps(last_key, default=_dec_default).encode()
    ).decode()


def _decode_cursor(cursor):
    if not cursor:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception:
        return None


def _dec_default(value):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError


def resp(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, default=_dec_default),
    }
