import copy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import plate_reader.log as logging_module
logging_module._configured = True
from plate_reader.config import ConfigError, WEEKDAYS, capture_desired_state, parse_hours
from fieldkit import install


SITE = Path(__file__).resolve().parents[1] / 'sites/ivars-001.json'


class WeeklyHoursTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads(SITE.read_text())['hours']
        self.hours = parse_hours(self.raw)

    def test_lakewood_every_minute_for_one_week(self):
        monday = datetime.fromisoformat('2026-10-19T00:00:00-07:00')
        for minute in range(7 * 1440):
            when = monday + timedelta(minutes=minute)
            local_minute = when.hour * 60 + when.minute
            closing = 22 * 60 if when.weekday() in (4, 5) else 21 * 60
            self.assertEqual(self.hours.is_open(when), 660 <= local_minute < closing, when)
            self.assertEqual(self.hours.capture_should_run(when), 645 <= local_minute < closing, when)

    def test_local_day_not_utc_day_selects_friday_close(self):
        # Already Saturday UTC, still Friday evening in Lakewood.
        self.assertTrue(self.hours.is_open(datetime.fromisoformat('2026-10-24T04:59:59+00:00')))
        self.assertFalse(self.hours.is_open(datetime.fromisoformat('2026-10-24T05:00:00+00:00')))
        # Sunday returns to the 9pm closing time.
        self.assertFalse(self.hours.is_open(datetime.fromisoformat('2026-10-26T04:00:00+00:00')))

    def test_daylight_saving_keeps_eleven_am_local(self):
        for date, utc_hour in [('2026-10-25', 18), ('2026-11-01', 19), ('2027-03-14', 18)]:
            opening = datetime.fromisoformat(f'{date}T{utc_hour}:00:00+00:00')
            self.assertFalse(self.hours.is_open(opening - timedelta(seconds=1)))
            self.assertTrue(self.hours.is_open(opening))
            self.assertTrue(self.hours.capture_should_run(opening - timedelta(minutes=15)))

    def test_overnight_belongs_to_opening_day_even_if_next_day_closed(self):
        weekly = {day: None for day in WEEKDAYS}
        weekly['fri'] = {'open': '22:00', 'close': '02:00'}
        hours = parse_hours(dict(timezone='America/Los_Angeles', weekly=weekly))
        for timestamp, expected in [('2026-10-23T01:00:00-07:00', False),
                                    ('2026-10-24T01:59:59-07:00', True),
                                    ('2026-10-24T02:00:00-07:00', False),
                                    ('2026-10-24T22:00:00-07:00', False)]:
            self.assertEqual(hours.is_open(datetime.fromisoformat(timestamp)), expected)

    def test_warmup_wraps_sunday_into_monday_without_counting_visits(self):
        weekly = {day: None for day in WEEKDAYS}
        weekly['mon'] = {'open': '00:05', 'close': '02:00'}
        hours = parse_hours(dict(timezone='America/Los_Angeles', weekly=weekly))
        when = datetime.fromisoformat('2026-10-25T23:50:00-07:00')
        self.assertTrue(hours.capture_should_run(when))
        self.assertFalse(hours.capture_should_run(when - timedelta(seconds=1)))
        self.assertFalse(hours.is_open(when))

    def test_all_closed_and_single_twenty_four_hour_day(self):
        weekly = {day: None for day in WEEKDAYS}
        hours = parse_hours(dict(timezone='UTC', weekly=weekly))
        self.assertFalse(hours.is_open(datetime.fromisoformat('2026-10-23T14:00:00+00:00')))
        weekly['fri'] = {'open': '11:00', 'close': '11:00'}
        hours = parse_hours(dict(timezone='UTC', weekly=weekly))
        self.assertTrue(hours.is_open(datetime.fromisoformat('2026-10-24T10:59:00+00:00')))
        self.assertFalse(hours.is_open(datetime.fromisoformat('2026-10-24T11:00:00+00:00')))

    def test_invalid_or_ambiguous_weekly_config_fails_closed(self):
        invalid = []
        raw = copy.deepcopy(self.raw); del raw['weekly']['sun']; invalid.append(raw)
        raw = copy.deepcopy(self.raw); raw['open'] = '11:00'; invalid.append(raw)
        raw = copy.deepcopy(self.raw); raw['weekly']['friday'] = raw['weekly'].pop('fri'); invalid.append(raw)
        raw = copy.deepcopy(self.raw); raw['weekly']['fri']['close'] = '25:00'; invalid.append(raw)
        raw = copy.deepcopy(self.raw); raw['weekly']['fri'] = False; invalid.append(raw)
        raw = copy.deepcopy(self.raw); raw['weekly'] = {}; invalid.append(raw)
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                parse_hours(raw)

    def test_actual_watchdog_uses_the_same_weekday_schedule(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            path.write_text(json.dumps({'alpr': {'engine': 'pr_stream'}, 'hours': self.raw}))
            for timestamp, expected in [('2026-10-23T10:44:59-07:00', 'off'),
                                        ('2026-10-23T10:45:00-07:00', 'on'),
                                        ('2026-10-23T21:30:00-07:00', 'on'),
                                        ('2026-10-25T21:30:00-07:00', 'off')]:
                self.assertEqual(capture_desired_state(path, datetime.fromisoformat(timestamp)), expected)


class AdoptionMigrationTests(unittest.TestCase):
    def root(self, contents):
        files = {}
        for name, data in contents.items():
            file = Mock()
            file.name = name
            file.is_symlink.return_value = False
            file.stat.return_value = SimpleNamespace(st_uid=0)
            file.read_bytes.return_value = data
            files[name] = file
        root = Mock()
        root.glob.return_value = list(files.values())
        root.__truediv__ = Mock(side_effect=files.__getitem__)
        return root

    def test_only_reviewed_old_config_can_upgrade(self):
        old, new = b'known-old-parser', b'approved-weekly-parser'
        approved = {'config.py': new, 'detector.py': b'unchanged'}
        with patch.object(install, 'BENCH_CONFIG_SHA256', hashlib.sha256(old).hexdigest()):
            self.assertEqual(install.application_updates(approved, self.root({**approved, 'config.py': old})),
                             {'config.py': new})
            for changed in ({**approved, 'config.py': b'unknown'}, {**approved, 'detector.py': b'changed'},
                            {**approved, 'extra.py': b'extra'}, {'config.py': new}):
                with self.assertRaises(ValueError): install.application_updates(approved, self.root(changed))
        self.assertEqual(install.application_updates(approved, self.root(approved)), {})

    def test_mac_metadata_is_accepted_only_with_known_name_and_real_header(self):
        approved = {'config.py': b'approved'}
        header = bytes.fromhex('0005160700020000')
        self.assertEqual(install.application_updates(approved, self.root({
            **approved, '._config.py': header + b'metadata'})), {})
        for extra in ({'._config.py': b'python or unknown data'}, {'._extra.py': header}):
            with self.assertRaises(ValueError):
                install.application_updates(approved, self.root({**approved, **extra}))


if __name__ == '__main__':
    unittest.main()
