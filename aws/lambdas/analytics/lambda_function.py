import json
import os
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

from location_time import timezone_for


dynamodb = boto3.resource("dynamodb")
stats = dynamodb.Table(os.environ.get("STATS_TABLE", "LocationStats"))
customers = dynamodb.Table(os.environ.get("CUSTOMERS_TABLE", "Customers"))


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            n = float(o)
            return int(n) if n.is_integer() else n
        return super().default(o)


def safe_int(v):
    return int(v) if v is not None else 0


def safe_float(v):
    return float(v) if v is not None else 0.0


def safe_div(a, b):
    return round(float(a) / float(b), 4) if b else 0


def build_identification_section(day_rows):
    """Compute diagnostics and terminal vehicle-encounter KPIs.

    Visits are the vehicle ledger. DetectionEvents are calibration evidence,
    never additional cars: one physical vehicle can create several failed-read
    episodes before it confirms or before its one anonymous Visit is emitted.

    The product KPI intentionally excludes ``no_visible_plate`` encounters.
    They are real cars in traffic totals, but they are not vehicles the system
    had enough plate evidence to identify. The denominator is therefore:

        recognized + insufficient_confidence
    """
    roundtrip_sum = sum(safe_int(r.get("roundtrip_sum_ms")) for r in day_rows)
    roundtrip_count = sum(safe_int(r.get("roundtrip_count")) for r in day_rows)
    avg_roundtrip_ms = round(roundtrip_sum / roundtrip_count) if roundtrip_count else None

    latency_buckets = {
        "under_200ms": sum(safe_int(r.get("lat_under_200")) for r in day_rows),
        "200_500ms": sum(safe_int(r.get("lat_200_500")) for r in day_rows),
        "500_1000ms": sum(safe_int(r.get("lat_500_1000")) for r in day_rows),
        "1000_2000ms": sum(safe_int(r.get("lat_1000_2000")) for r in day_rows),
        "over_2000ms": sum(safe_int(r.get("lat_over_2000")) for r in day_rows),
    }

    det_sum = sum(safe_float(r.get("det_conf_sum")) for r in day_rows)
    det_count = sum(safe_int(r.get("det_conf_count")) for r in day_rows)
    avg_det_conf = round(det_sum / det_count, 4) if det_count else None

    ocr_sum = sum(safe_float(r.get("ocr_conf_sum")) for r in day_rows)
    ocr_count = sum(safe_int(r.get("ocr_conf_count")) for r in day_rows)
    avg_ocr_conf = round(ocr_sum / ocr_count, 4) if ocr_count else None

    # Frame/group-level diagnostics. A group-floor rejection is an attempted
    # identification, but it is not proof that a distinct vehicle drove off.
    total_vehicles = sum(safe_int(r.get("visits")) for r in day_rows)
    # Legacy daily rows predate recognized_visits and all of their Visits
    # required a plate hash. Preserve their meaning while using the explicit
    # counter for every vehicle-centric row written after this deployment.
    confirmed = sum(
        safe_int(r.get("recognized_visits"))
        if "recognized_visits" in r
        else safe_int(r.get("visits"))
        for r in day_rows
    )
    unidentified = sum(
        safe_int(r.get("unidentified_visits")) for r in day_rows
    )
    insufficient_confidence = sum(
        safe_int(r.get("insufficient_confidence_visits")) for r in day_rows
    )
    no_visible_plate = sum(
        safe_int(r.get("no_visible_plate_visits")) for r in day_rows
    )
    no_plate = sum(safe_int(r.get("failed_no_plate")) for r in day_rows)
    low_conf = sum(safe_int(r.get("failed_low_confidence")) for r in day_rows)
    vote_disag = sum(safe_int(r.get("failed_vote_disagreement")) for r in day_rows)
    group_floor = sum(safe_int(r.get("failed_group_floor")) for r in day_rows)
    below_threshold = sum(
        safe_int(r.get("failed_below_threshold")) for r in day_rows
    )
    unreadable = sum(safe_int(r.get("failed_unreadable")) for r in day_rows)

    total_attempts = (
        confirmed + no_plate + low_conf + vote_disag + group_floor
        + below_threshold + unreadable
    )
    success_rate = round(confirmed / total_attempts, 4) if total_attempts else None

    # Vehicle-level misses remain separate so repeated frame/group failures do
    # not inflate the physical vehicle count.
    drive_offs = sum(safe_int(r.get("drive_offs")) for r in day_rows)
    drive_offs_had_plate = sum(
        safe_int(r.get("drive_offs_had_plate_candidate")) for r in day_rows
    )
    drive_offs_split = sum(
        safe_int(r.get("drive_offs_split_from_color")) for r in day_rows
    )
    identifiable_vehicles = confirmed + insufficient_confidence
    recognition_rate = (
        round(confirmed / identifiable_vehicles, 4)
        if identifiable_vehicles else None
    )
    missed_identifiable_rate = (
        round(insufficient_confidence / identifiable_vehicles, 4)
        if identifiable_vehicles else None
    )
    unidentified_rate = (
        round(unidentified / total_vehicles, 4) if total_vehicles else None
    )

    # Compatibility only. Releases before the encounter ledger treated each
    # distinct DetectionEvent episode as an extra car. That double-counted an
    # anonymous Visit and any later confirmation from the same physical car.
    # The writers now hold this at zero; keep the response keys temporarily so
    # older dashboards degrade to visit-only traffic instead of crashing.
    estimated_additional_vehicles = sum(
        safe_int(r.get("estimated_additional_vehicles")) for r in day_rows
    )
    # These legacy aliases now intentionally resolve to the encounter-ledger
    # values.  Keeping the historical formulas here would let an older UI show
    # a third, contradictory percentage even after the source counters were
    # corrected.
    total_vehicles_including_estimate = total_vehicles
    recognition_rate_including_estimate = recognition_rate

    return {
        "avg_roundtrip_ms": avg_roundtrip_ms,
        "latency_buckets": latency_buckets,
        "avg_detection_confidence": avg_det_conf,
        "avg_ocr_confidence": avg_ocr_conf,
        "success_rate": success_rate,
        "confirmed_visits": confirmed,
        "recognized_vehicles": confirmed,
        "unidentified_vehicles": unidentified,
        "cars_through": total_vehicles,
        "identifiable_vehicles": identifiable_vehicles,
        "identified_vehicles": confirmed,
        "missed_identifiable_vehicles": insufficient_confidence,
        "identification_rate": recognition_rate,
        "missed_identifiable_rate": missed_identifiable_rate,
        "vehicle_recognition_rate": recognition_rate,
        "unidentified_vehicle_rate": unidentified_rate,
        "insufficient_confidence_vehicles": insufficient_confidence,
        "no_visible_plate_vehicles": no_visible_plate,
        "failed_no_plate": no_plate,
        "failed_low_confidence": low_conf,
        "failed_vote_disagreement": vote_disag,
        "failed_group_confidence_floor": group_floor,
        "failed_below_threshold": below_threshold,
        "failed_unreadable": unreadable,
        "total_attempts": total_attempts,
        "drive_offs": drive_offs,
        "drive_offs_had_plate_candidate": drive_offs_had_plate,
        "drive_offs_split_from_color": drive_offs_split,
        "total_vehicles": total_vehicles,
        # Legacy diagnostic retained for existing consumers. New unidentified
        # vehicles are represented directly in Visits and the fields above.
        "drive_off_rate": unidentified_rate,
        "estimated_additional_vehicles": estimated_additional_vehicles,
        "total_vehicles_including_estimate": total_vehicles_including_estimate,
        "recognition_rate_including_estimate": recognition_rate_including_estimate,
    }


def _merge_count_maps(day_rows, attr):
    out = {}
    for row in day_rows:
        raw = row.get(attr) or {}
        if not isinstance(raw, dict):
            continue
        for key, value in raw.items():
            out[str(key)] = out.get(str(key), 0) + safe_int(value)
    return out


def _top_n_counts(counts, n):
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n])


def build_vehicle_mix_section(day_rows):
    """Appearance mix for the requested window. Additive; missing DAY# maps
    mean mix is empty until AggregateVisits has been live for that day."""
    visits = sum(safe_int(r.get("visits")) for r in day_rows)
    with_color = sum(safe_int(r.get("visits_with_color")) for r in day_rows)
    with_make = sum(safe_int(r.get("visits_with_make")) for r in day_rows)
    with_model = sum(safe_int(r.get("visits_with_model")) for r in day_rows)
    colors = _merge_count_maps(day_rows, "color_counts")
    makes = _top_n_counts(_merge_count_maps(day_rows, "make_counts"), 15)
    model_counts = _merge_count_maps(day_rows, "model_counts")
    top_models = []
    for key, count in sorted(
        model_counts.items(), key=lambda kv: (-kv[1], kv[0])
    )[:10]:
        if "|" not in key:
            continue
        make, model = key.split("|", 1)
        top_models.append({"make": make, "model": model, "visits": count})
    return {
        "colors": colors,
        "makes": makes,
        "top_models": top_models,
        "coverage": {
            "visits": visits,
            "with_color": with_color,
            "with_make": with_make,
            "with_model": with_model,
            "color_rate": safe_div(with_color, visits),
            "make_rate": safe_div(with_make, visits),
            "model_rate": safe_div(with_model, visits),
        },
    }


def handler(event, context):
    params = event.get("queryStringParameters") or {}
    location_id = params.get("location_id") or params.get("locationId")
    date_from = params.get("from")
    date_to = params.get("to")

    if not location_id:
        return _resp(400, {"error": "location_id is required"})
    if not date_from or not date_to:
        return _resp(400, {"error": "from and to (YYYY-MM-DD) are required"})

    day_rows = stats.query(
        KeyConditionExpression=(
            Key("location_id").eq(location_id)
            & Key("period").between(f"DAY#{date_from}", f"DAY#{date_to}~")
        )
    ).get("Items", [])

    hour_rows = stats.query(
        KeyConditionExpression=(
            Key("location_id").eq(location_id)
            & Key("period").between(f"HOUR#{date_from}", f"HOUR#{date_to}~")
        )
    ).get("Items", [])

    days = []
    totals = {
        "visits": 0,
        "repeat_visits": 0,
        "new_vehicle_visits": 0,
        "unique_vehicles": 0,
        "interval_sum_s": 0,
        "interval_count": 0,
        "cross_location_visits": 0,
        "vip_promotions": 0,
    }
    for row in sorted(day_rows, key=lambda x: x["period"]):
        day = {
            "date": row["period"].replace("DAY#", ""),
            "visits": safe_int(row.get("visits")),
            "repeat_visits": safe_int(row.get("repeat_visits")),
            "new_vehicle_visits": safe_int(row.get("new_vehicle_visits")),
            "unique_vehicles": safe_int(row.get("unique_vehicles")),
            "cross_location_visits": safe_int(row.get("cross_location_visits")),
        }
        day["repeat_rate"] = safe_div(day["repeat_visits"], day["visits"])
        days.append(day)
        for key in totals:
            totals[key] += safe_int(row.get(key))

    hour_heatmap = {hour: 0 for hour in range(24)}
    for row in hour_rows:
        try:
            hour = int(row["period"][-2:])
            hour_heatmap[hour] += safe_int(row.get("visits"))
        except (ValueError, KeyError):
            continue
    peak_hours = sorted(hour_heatmap.items(), key=lambda x: -x[1])[:3]

    histogram = {}
    for bucket in ["b_0_1d", "b_1_3d", "b_3_7d", "b_7_14d", "b_14_30d", "b_30d_plus"]:
        histogram[bucket] = sum(safe_int(r.get(bucket)) for r in day_rows)

    repeat_rate = safe_div(totals["repeat_visits"], totals["visits"])
    avg_return_interval_days = (
        safe_div(totals["interval_sum_s"], totals["interval_count"] * 86400)
        if totals["interval_count"]
        else 0
    )
    cross_location_share = safe_div(totals["cross_location_visits"], totals["visits"])

    return _resp(
        200,
        {
            "location_id": location_id,
            "timezone": timezone_for(location_id),
            "from": date_from,
            "to": date_to,
            "headline": {
                "total_visits": totals["visits"],
                "unique_vehicles": totals["unique_vehicles"],
                "repeat_visits": totals["repeat_visits"],
                "new_vehicle_visits": totals["new_vehicle_visits"],
                "repeat_rate": repeat_rate,
                "avg_return_interval_days": avg_return_interval_days,
                "cross_location_visits": totals["cross_location_visits"],
                "cross_location_share": cross_location_share,
                "vip_promotions": totals["vip_promotions"],
            },
            "daily": days,
            "hour_heatmap": hour_heatmap,
            "peak_hours": [{"hour": h, "visits": v} for h, v in peak_hours],
            "interval_histogram": histogram,
            "identification": build_identification_section(day_rows),
            "vehicle_mix": build_vehicle_mix_section(day_rows),
        },
    )


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,x-device-id,x-api-key",
            "Access-Control-Allow-Methods": "GET,OPTIONS",
        },
        "body": json.dumps(body, cls=DecimalEncoder),
    }
