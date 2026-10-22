"""Location-local calendar buckets for LocationStats.

Visit and detection timestamps stay UTC ISO on the wire and in Dynamo.
DAY# and HOUR# keys are the restaurant's local date/hour so a dashboard
`from`/`to` in store time matches the day the store actually ran.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_TIMEZONE = "America/New_York"

LOCATION_TIMEZONES = {
    "ljs-louisville-001": "America/New_York",
    "ljs-louisville-002": "America/New_York",
    "ljs-lexington-001": "America/New_York",
    "qdoba-pr-001": "America/Puerto_Rico",
}


def timezone_for(location_id: str) -> str:
    mapping = dict(LOCATION_TIMEZONES)
    for part in os.environ.get("LOCATION_TIMEZONES", "").split(","):
        if "=" not in part:
            continue
        loc, tz_name = part.split("=", 1)
        loc, tz_name = loc.strip(), tz_name.strip()
        if loc and tz_name:
            mapping[loc] = tz_name
    return mapping.get(location_id) or DEFAULT_TIMEZONE


def _zone(location_id: str) -> ZoneInfo:
    name = timezone_for(location_id)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        print(f"unknown timezone {name!r} for {location_id}; falling back to UTC")
        return ZoneInfo("UTC")


def parse_utc(timestamp: str | None) -> datetime:
    text = (timestamp or "").strip()
    if not text:
        return datetime.now(timezone.utc)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_local(timestamp: str | None, location_id: str) -> datetime:
    return parse_utc(timestamp).astimezone(_zone(location_id))


def local_day_hour(timestamp: str | None, location_id: str) -> tuple[str, str]:
    """Return (YYYY-MM-DD, YYYY-MM-DDTHH) in the location timezone."""
    local = to_local(timestamp, location_id)
    return local.strftime("%Y-%m-%d"), local.strftime("%Y-%m-%dT%H")


def local_yesterday(location_id: str, now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    local = current.astimezone(_zone(location_id))
    return (local.date() - timedelta(days=1)).isoformat()


def _iso_z(dt: datetime) -> str:
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def local_date_utc_range(location_id: str, local_date: str) -> tuple[str, str]:
    """Inclusive UTC ISO bounds covering one local calendar day."""
    year, month, day = (int(part) for part in local_date.split("-"))
    start = datetime(year, month, day, tzinfo=_zone(location_id))
    end = start + timedelta(days=1) - timedelta(milliseconds=1)
    return _iso_z(start), _iso_z(end)
