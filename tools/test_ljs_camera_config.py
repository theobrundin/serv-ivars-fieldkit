#!/usr/bin/env python3
"""Focused tests for the LJS exposure/shutter canary utility."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from plate_reader.ljs_camera_config import corrected_text, install


BASE = """paths:
  cam:
    source: rpiCamera
    rpiCameraWidth: 1456
    rpiCameraHeight: 1088
    rpiCameraFPS: 5
    rpiCameraCodec: auto
"""


class CameraConfigTests(unittest.TestCase):
    def test_checked_in_template_with_source_comment_is_accepted(self):
        template = (
            pathlib.Path(__file__).resolve().parents[1]
            / "stream"
            / "mediamtx.yml"
        ).read_text()
        corrected, changes = corrected_text(template, "short")
        self.assertEqual(changes, [])
        self.assertEqual(corrected, template)

    def test_short_mode_is_inserted_without_touching_fps(self):
        corrected, changes = corrected_text(BASE, "short")
        self.assertEqual(
            changes,
            [("rpiCameraExposure", "<absent/normal>", "short")],
        )
        self.assertIn("    rpiCameraFPS: 5\n", corrected)
        self.assertIn("    rpiCameraExposure: short\n", corrected)
        self.assertIn("    rpiCameraCodec: auto\n", corrected)

    def test_normal_is_an_explicit_rollback(self):
        short, _ = corrected_text(BASE, "short")
        normal, changes = corrected_text(short, "normal")
        self.assertEqual(
            changes,
            [("rpiCameraExposure", "short", "normal")],
        )
        self.assertIn("rpiCameraExposure: normal", normal)
        self.assertEqual(corrected_text(normal, "normal")[1], [])

    def test_motion_adds_one_300_second_shutter_without_touching_fps(self):
        short, _ = corrected_text(BASE, "short")
        motion, changes = corrected_text(short, "motion")
        self.assertEqual(
            changes,
            [("rpiCameraShutter", "<absent/0>", "3333")],
        )
        self.assertIn("rpiCameraExposure: short", motion)
        self.assertIn("rpiCameraShutter: 3333", motion)
        self.assertIn("rpiCameraFPS: 5", motion)
        self.assertEqual(corrected_text(motion, "motion")[1], [])

    def test_short_rolls_motion_shutter_back_to_auto(self):
        motion, _ = corrected_text(BASE, "motion")
        short, changes = corrected_text(motion, "short")
        self.assertEqual(
            changes,
            [("rpiCameraShutter", "3333", "0")],
        )
        self.assertIn("rpiCameraShutter: 0", short)

    def test_twilight_uses_low_light_one_60_second_shutter(self):
        short, _ = corrected_text(BASE, "short")
        twilight, changes = corrected_text(short, "twilight")
        self.assertEqual(
            changes,
            [("rpiCameraShutter", "<absent/0>", "16667")],
        )
        self.assertIn("rpiCameraExposure: short", twilight)
        self.assertIn("rpiCameraShutter: 16667", twilight)
        self.assertIn("rpiCameraFPS: 5", twilight)
        self.assertEqual(corrected_text(twilight, "twilight")[1], [])

    def test_twilight_can_rollback_or_change_to_motion(self):
        twilight, _ = corrected_text(BASE, "twilight")
        short, rollback = corrected_text(twilight, "short")
        self.assertEqual(rollback, [("rpiCameraShutter", "16667", "0")])
        self.assertIn("rpiCameraShutter: 0", short)
        motion, switch = corrected_text(twilight, "motion")
        self.assertEqual(switch, [("rpiCameraShutter", "16667", "3333")])
        self.assertIn("rpiCameraShutter: 3333", motion)

    def test_refuses_an_unreviewed_capture_rate(self):
        with self.assertRaisesRegex(ValueError, "unexpected LJS camera FPS"):
            corrected_text(BASE.replace("FPS: 5", "FPS: 30"))
        with self.assertRaisesRegex(ValueError, "unexpected LJS camera FPS"):
            corrected_text(BASE.replace("FPS: 5", "FPS: 10"))

    def test_refuses_duplicate_exposure_settings(self):
        duplicate = BASE + "    rpiCameraExposure: normal\n" \
            "    rpiCameraExposure: short\n"
        with self.assertRaisesRegex(ValueError, "duplicate"):
            corrected_text(duplicate)

    def test_refuses_fixed_shutter(self):
        fixed = BASE + "    rpiCameraShutter: 100000\n"
        with self.assertRaisesRegex(ValueError, "unreviewed fixed rpiCameraShutter"):
            corrected_text(fixed)

    def test_refuses_fixed_gain(self):
        fixed = BASE + "    rpiCameraGain: 8\n"
        with self.assertRaisesRegex(ValueError, "fixed rpiCameraGain"):
            corrected_text(fixed)

    def test_accepts_explicit_auto_shutter_and_gain(self):
        automatic = BASE + "    rpiCameraShutter: 0\n" \
            "    rpiCameraGain: 0\n"
        corrected, changes = corrected_text(automatic)
        self.assertEqual(len(changes), 1)
        self.assertIn("rpiCameraShutter: 0", corrected)
        self.assertIn("rpiCameraGain: 0", corrected)

    def test_atomic_install_keeps_a_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "mediamtx.yml"
            path.write_text(BASE)
            corrected, _ = corrected_text(BASE)
            backup = install(path, corrected)
            self.assertEqual(backup.read_text(), BASE)
            self.assertEqual(path.read_text(), corrected)


if __name__ == "__main__":
    unittest.main()
