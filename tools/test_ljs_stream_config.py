#!/usr/bin/env python3
"""Focused checks for the site-local Stream config correction utility."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plate_reader.ljs_stream_config import corrected_text, install  # noqa: E402


GOOD = """timezone = America/New_York
[cameras]
  [[camera-1]]
    active = yes
    url = rtsp://127.0.0.1:8554/cam
    sample = 1
    max_prediction_delay = 3
    memory_decay = 300
    max_dwell_delay = 15
    plates_per_vehicle = 2
    detection_mode = vehicle

[webhooks]
  [[serv-local]]
    url = http://127.0.0.1:8129/
"""


class StreamConfigCorrectionTests(unittest.TestCase):
    def test_changes_only_reviewed_values(self) -> None:
        corrected, changes = corrected_text(GOOD)
        self.assertEqual(
            changes,
            [
                ("max_prediction_delay", "3", "12"),
                ("memory_decay", "300", "90"),
                ("max_dwell_delay", "15", "30"),
                ("plates_per_vehicle", "2", "1"),
                ("report_static", "(missing)", "no"),
            ],
        )
        self.assertIn("    max_prediction_delay = 12\n", corrected)
        self.assertIn("    memory_decay = 90\n", corrected)
        self.assertIn("    max_dwell_delay = 30\n", corrected)
        self.assertIn("    plates_per_vehicle = 1\n", corrected)
        self.assertIn("    report_static = no\n", corrected)
        self.assertIn("    sample = 1\n", corrected)
        self.assertIn("    detection_mode = vehicle\n", corrected)

    def test_is_idempotent_and_preserves_inline_comment(self) -> None:
        first, _ = corrected_text(
            GOOD.replace("memory_decay = 300", "memory_decay = 300  # old")
        )
        second, changes = corrected_text(first)
        self.assertEqual(changes, [])
        self.assertIn("memory_decay = 90  # old", second)

    def test_updates_existing_report_static_without_duplication(self) -> None:
        original = GOOD.replace(
            "    detection_mode = vehicle\n",
            "    report_static = yes\n    detection_mode = vehicle\n",
        )
        corrected, changes = corrected_text(original)
        self.assertIn(("report_static", "yes", "no"), changes)
        self.assertEqual(corrected.count("report_static = no"), 1)

    def test_refuses_unexpected_source(self) -> None:
        with self.assertRaisesRegex(ValueError, "unexpected.*url"):
            corrected_text(GOOD.replace("127.0.0.1", "192.0.2.1", 1))

    def test_refuses_missing_or_duplicate_target(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing required"):
            corrected_text(GOOD.replace("    max_prediction_delay = 3\n", ""))
        with self.assertRaisesRegex(ValueError, "duplicate 'memory_decay'"):
            corrected_text(
                GOOD.replace(
                    "    memory_decay = 300\n",
                    "    memory_decay = 300\n    memory_decay = 4\n",
                )
            )

    def test_install_preserves_mode_and_creates_private_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.ini"
            path.write_text(GOOD, encoding="utf-8")
            path.chmod(0o640)
            corrected, _ = corrected_text(GOOD)
            backup = install(path, corrected)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(backup.read_text(encoding="utf-8"), GOOD)
            self.assertEqual(path.read_text(encoding="utf-8"), corrected)


if __name__ == "__main__":
    unittest.main()
