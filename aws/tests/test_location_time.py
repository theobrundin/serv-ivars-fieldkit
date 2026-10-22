import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambdas"))
import location_time  # noqa: E402


class LocationTimeTests(unittest.TestCase):
    def test_eastern_evening_does_not_roll_the_restaurant_day(self):
        day, hour = location_time.local_day_hour(
            "2026-08-19T00:19:39.761Z", "ljs-louisville-001"
        )
        self.assertEqual(day, "2026-08-18")
        self.assertEqual(hour, "2026-08-18T20")

    def test_lunch_hour_is_four_hours_behind_utc_in_august(self):
        day, hour = location_time.local_day_hour(
            "2026-08-18T16:00:00Z", "ljs-louisville-001"
        )
        self.assertEqual(day, "2026-08-18")
        self.assertEqual(hour, "2026-08-18T12")

    def test_qdoba_uses_puerto_rico(self):
        self.assertEqual(
            location_time.timezone_for("qdoba-pr-001"), "America/Puerto_Rico"
        )

    def test_env_override_and_default(self):
        old = os.environ.get("LOCATION_TIMEZONES")
        os.environ["LOCATION_TIMEZONES"] = "loc-x=America/Chicago"
        try:
            self.assertEqual(location_time.timezone_for("loc-x"), "America/Chicago")
            self.assertEqual(location_time.timezone_for("unknown"), "America/New_York")
        finally:
            if old is None:
                os.environ.pop("LOCATION_TIMEZONES", None)
            else:
                os.environ["LOCATION_TIMEZONES"] = old

    def test_local_date_utc_range_covers_eastern_day(self):
        start, end = location_time.local_date_utc_range(
            "ljs-louisville-001", "2026-08-18"
        )
        self.assertEqual(start, "2026-08-18T04:00:00.000Z")
        self.assertEqual(end, "2026-08-19T03:59:59.999Z")

    def test_local_yesterday_uses_location_clock(self):
        now = datetime(2026, 8, 19, 2, 0, tzinfo=timezone.utc)  # 10pm EDT Aug 18
        self.assertEqual(
            location_time.local_yesterday("ljs-louisville-001", now=now),
            "2026-08-17",
        )


if __name__ == "__main__":
    unittest.main()
