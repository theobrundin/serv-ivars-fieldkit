#!/usr/bin/env python3
"""Offline factory-config and credential-boundary regression tests."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plate_reader.config import load
from plate_reader.ljs_replacement_config import (
    API_BASE,
    BENCH_DEVICE,
    BENCH_LOCATION,
    build_camera_config,
    build_config,
    build_stream_config,
    main,
    read_key,
    write_validated,
)
from plate_reader.ljs_route_config import ROUTE_VALUES


FAKE_KEY = "a" * 64


class ReplacementConfigTests(unittest.TestCase):
    def setUp(self):
        self.template = json.loads((ROOT / "config.example.json").read_text())
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)

    def key_file(self, text=FAKE_KEY, mode=0o600):
        path = self.folder / "key"
        path.write_text(text)
        path.chmod(mode)
        return path

    def test_production_reproduces_field_profile(self):
        before = deepcopy(self.template)
        raw = build_config(self.template, FAKE_KEY, stage="production")
        self.assertEqual(self.template, before)
        self.assertEqual(raw["device_id"], "serv-unit-001")
        self.assertEqual(raw["location_id"], "ljs-louisville-001")
        self.assertEqual(raw["api_base"], API_BASE)
        self.assertEqual(raw["api_key"], FAKE_KEY)
        self.assertEqual(raw["alpr"]["engine"], "pr_stream")
        self.assertNotIn("license_token_file", raw["alpr"])
        self.assertFalse(raw["presence"]["route_qualification"]["shadow_only"])
        self.assertTrue(raw["presence"]["shadow_only"])
        for name, value in ROUTE_VALUES.items():
            self.assertEqual(raw["presence"]["route_qualification"][name], value)
        self.assertEqual(raw["hours"], {
            "timezone": "America/New_York", "open": "10:00", "close": "22:00", "warmup_minutes": 15,
        })
        self.assertEqual(raw["capture"], {"width": 1456, "height": 1088, "target_fps": 5})
        self.assertEqual(raw["camera"]["framerate"], 5)
        self.assertEqual(raw["detection_zone"], {
            "enabled": True, "x_min_pct": 0.0, "x_max_pct": 1.0, "y_min_pct": 0.0, "y_max_pct": 0.75,
        })

    def test_bench_is_separate_identity_without_schedule(self):
        raw = build_config(self.template, FAKE_KEY, stage="bench")
        self.assertEqual((raw["device_id"], raw["location_id"]), (BENCH_DEVICE, BENCH_LOCATION))
        self.assertNotIn("hours", raw)
        self.assertEqual(raw["diagnostics"]["miss_plate_dir"], "/home/serv/plate_reader/bench_audit_cases")
        self.assertFalse(raw["presence"]["route_qualification"]["shadow_only"])
        self.assertEqual(raw["api_key"], FAKE_KEY)

    def test_profiles_are_independent_and_deterministic(self):
        first = build_config(self.template, FAKE_KEY, stage="production")
        second = build_config(self.template, FAKE_KEY, stage="production")
        self.assertEqual(first, second)
        first["presence"]["route_qualification"]["entry_zone"]["y_min_pct"] = 0.9
        self.assertEqual(second["presence"]["route_qualification"]["entry_zone"]["y_min_pct"], 0.26)
        self.assertEqual(ROUTE_VALUES["entry_zone"]["y_min_pct"], 0.26)

    def test_effective_gates_validate_and_match(self):
        for stage in ("bench", "production"):
            path = self.folder / f"{stage}.json"
            write_validated(build_config(self.template, FAKE_KEY, stage=stage), path)
            cfg = load(path)
            self.assertEqual(cfg.alpr.thresholds.app_detect_min, 0.70)
            self.assertEqual(cfg.alpr.thresholds.app_ocr_min, 0.80)
            self.assertEqual(cfg.thresholds.min_group_detection_confidence, 0.70)
            self.assertEqual(cfg.thresholds.min_group_ocr_confidence, 0.80)
            self.assertEqual(cfg.thresholds.vote_min_frames, 2)
            self.assertEqual(cfg.alpr.thresholds.provider_high_ocr_rescue_ocr_min, 0.98)
            self.assertEqual(cfg.alpr.thresholds.identity_settle_quiet_seconds, 3.5)

    def test_no_secret_or_privacy_drift(self):
        raw = build_config(self.template, FAKE_KEY, stage="production")
        diag = raw["diagnostics"]
        self.assertFalse(diag["log_rejected_reads"])
        self.assertFalse(diag["save_presence_frames"])
        self.assertTrue(diag["save_miss_plates"])
        self.assertTrue(diag["save_confirmed_plates"])
        self.assertTrue(diag["save_unidentified_vehicle_frames"])
        self.assertEqual(diag["miss_plate_max_cases"], 300)
        self.assertFalse(raw["alpr"]["roi"]["enabled"])
        self.assertFalse(raw["alpr"]["engine_advanced"]["rectify_enabled"])

    def test_wrong_engine_endpoint_capture_or_stage_rejected(self):
        for block, key, value in (("alpr", "engine", "ultimatealpr"), ("capture", "target_fps", 10)):
            raw = deepcopy(self.template)
            raw[block][key] = value
            with self.assertRaises(ValueError):
                build_config(raw, FAKE_KEY, stage="bench")
        raw = deepcopy(self.template)
        raw["api_base"] = "https://unreviewed.invalid"
        with self.assertRaises(ValueError):
            build_config(raw, FAKE_KEY, stage="bench")
        with self.assertRaises(ValueError):
            build_config(self.template, FAKE_KEY, stage="somewhere")

    def test_key_permission_and_format(self):
        path = self.key_file(FAKE_KEY + "\n")
        self.assertEqual(read_key(path), FAKE_KEY)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            read_key(path)
        path.chmod(0o600)
        path.write_text("invalid-secret-key")
        with self.assertRaisesRegex(ValueError, "64 lowercase hexadecimal"):
            read_key(path)

    def test_key_symlink_and_fifo_rejected(self):
        source = self.key_file()
        link = self.folder / "link"
        link.symlink_to(source)
        with self.assertRaises(OSError):
            read_key(link)
        fifo = self.folder / "fifo"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(ValueError):
            read_key(fifo)

    def test_bench_key_read_checks_identity(self):
        path = self.key_file(json.dumps({"device_id": BENCH_DEVICE, "location_id": BENCH_LOCATION, "api_key": FAKE_KEY}))
        self.assertEqual(read_key(path, bench_config=True), FAKE_KEY)
        path.write_text(json.dumps({"device_id": "serv-unit-001", "location_id": "ljs-louisville-001", "api_key": FAKE_KEY}))
        with self.assertRaises(ValueError):
            read_key(path, bench_config=True)

    def test_output_private_and_never_overwrites(self):
        raw = build_config(self.template, FAKE_KEY, stage="production")
        target = self.folder / "config.json"
        write_validated(raw, target)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        before = target.read_bytes()
        with self.assertRaises(ValueError):
            write_validated(raw, target)
        self.assertEqual(target.read_bytes(), before)
        link = self.folder / "link"
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            write_validated(raw, link)
        self.assertEqual(list(self.folder.glob(".ljs-replacement-*")), [])

    def test_invalid_output_never_published_or_leaked(self):
        raw = build_config(self.template, FAKE_KEY, stage="production")
        target = self.folder / "config.json"
        with patch("plate_reader.ljs_replacement_config.load", side_effect=ValueError(FAKE_KEY)):
            with self.assertRaises(ValueError) as caught:
                write_validated(raw, target)
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        self.assertFalse(target.exists())
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_check_only_cli_and_private_logs(self):
        key = self.key_file()
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = main(["--example", str(ROOT / "config.example.json"), "--stage", "production", "--key-file", str(key), "--check"])
        self.assertEqual(result, 0)
        self.assertNotIn(FAKE_KEY, output.getvalue())
        self.assertIn("check only", output.getvalue())
        self.assertEqual(list(self.folder.iterdir()), [key])

    def test_production_cli_cannot_reuse_bench_key(self):
        output = io.StringIO()
        with redirect_stderr(output), self.assertRaises(SystemExit):
            main(["--example", str(ROOT / "config.example.json"), "--stage", "production", "--bench-key-config", "unused", "--check"])

    def test_stream_preserves_reviewed_field_settings(self):
        template = (ROOT / "stream/config.ini.template").read_text()
        result = build_stream_config(template)
        for setting in ("max_prediction_delay = 12", "memory_decay = 90", "max_dwell_delay = 30", "report_static = no", "sample = 1", "image = original"):
            self.assertIn(setting, result)
        # Older approved temporal settings may be migrated, not speculative 6/120.
        self.assertIn("memory_decay = 90", build_stream_config(template.replace("memory_decay = 90", "memory_decay = 30")))
        for before, after in (("image = original", "image = vehicle"), ("sample = 1", "sample = 2"), ("http://127.0.0.1:8129/", "https://remote.invalid/")):
            with self.assertRaises(ValueError):
                build_stream_config(template.replace(before, after))

    def test_camera_excludes_unvalidated_shutter_and_network_exposure(self):
        template = (ROOT / "stream/mediamtx.yml").read_text()
        result = build_camera_config(template)
        self.assertIn("rpiCameraFPS: 5", result)
        self.assertIn("rpiCameraExposure: short", result)
        result = build_camera_config(template + "\n    rpiCameraShutter: 16667\n")
        self.assertIn("rpiCameraShutter: 0", result)
        with self.assertRaises(ValueError):
            build_camera_config(template.replace("127.0.0.1:8554", "0.0.0.0:8554"))
        with self.assertRaises(ValueError):
            build_camera_config(template.replace("webrtc: no", "webrtc: yes"))
        with self.assertRaises(ValueError):
            build_camera_config(template.replace("moq: no", "moq: yes"))
        with self.assertRaises(ValueError):
            build_camera_config(template.replace("moq: no\n", ""))


if __name__ == "__main__":
    unittest.main()
