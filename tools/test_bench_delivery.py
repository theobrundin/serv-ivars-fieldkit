"""Prove that bench mode cannot make HTTP requests or replay bench events."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plate_reader.config import ConfigError, load
from plate_reader import sender as sender_mod
from tools.reolink_bench.configure import bench_config, stream_config


class BenchDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "queue.db"
        self.patch = patch.object(sender_mod, "QUEUE_DB_PATH", self.path)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.cfg = SimpleNamespace(
            device_id="serv-unit-bench", api_key="0" * 64,
            location_id="bench-reolink", sw_version="test",
            api_base="https://must-never-be-contacted.invalid",
            backend_writes_enabled=False,
            behavior=SimpleNamespace(queue_max_size=3, queue_ttl_hours=24),
        )

    def test_every_sender_entry_point_stays_local(self):
        with patch("requests.sessions.Session.request", side_effect=AssertionError("HTTP attempted")) as request:
            sender = sender_mod.Sender(self.cfg)
            self.assertTrue(sender.auth_preflight())
            sender.send_visit({"licensePlate": "a" * 64, "encounterId": "test-vehicle"})
            sender.send_detection_event({"reason": "test"})
            sender.send_heartbeat({"test": True})
            self.assertFalse(sender._drain_one())
            self.assertEqual(sender._post("/visit", {}), ("disabled", None))
            self.assertEqual(sender.queue_size(), 3)
            request.assert_not_called()

    def test_enabling_delivery_cannot_replay_bench_queue(self):
        bench = sender_mod.Sender(self.cfg)
        bench.send_visit({"encounterId": "bench-only"})
        self.cfg.backend_writes_enabled = True
        live = sender_mod.Sender(self.cfg)
        self.assertEqual(live.queue_size(), 0)
        self.assertEqual(bench.queue_size(), 1)
        self.assertNotEqual(bench._db_path, live._db_path)
        self.assertEqual(bench._db_path.stat().st_mode & 0o777, 0o600)

    def test_bench_queue_remains_bounded_and_keeps_visit_priority(self):
        sender = sender_mod.Sender(self.cfg)
        for i in range(8):
            sender.send_visit({"encounterId": str(i)})
        sender.send_heartbeat({})
        self.assertEqual(sender.queue_size(), 3)
        with sqlite3.connect(sender._db_path) as connection:
            self.assertEqual(connection.execute("SELECT DISTINCT endpoint FROM queue").fetchall(), [("/visit",)])

    def test_configuration_requires_boolean_and_preserves_default(self):
        raw = json.loads((ROOT / "config.example.json").read_text())
        raw.update(device_id="serv-unit-bench", location_id="bench-reolink", api_key="0" * 64)
        path = Path(self.directory.name) / "config.json"
        raw.pop("backend_writes_enabled", None)
        path.write_text(json.dumps(raw))
        self.assertTrue(load(path).backend_writes_enabled)
        raw["backend_writes_enabled"] = False
        path.write_text(json.dumps(raw))
        self.assertFalse(load(path).backend_writes_enabled)
        raw["backend_writes_enabled"] = "false"
        path.write_text(json.dumps(raw))
        with self.assertRaises(ConfigError):
            load(path)

    def test_generated_home_config_keeps_gates_and_separates_identity(self):
        template = json.loads((ROOT / 'config.example.json').read_text())
        cfg = bench_config(template)
        path = Path(self.directory.name) / 'config.json'
        path.write_text(json.dumps(cfg))
        parsed = load(path)
        self.assertFalse(parsed.backend_writes_enabled)
        self.assertEqual(parsed.device_id, 'serv-unit-bench-reolink')
        self.assertEqual(cfg['api_base'], 'https://backend-disabled.invalid')
        self.assertTrue(parsed.presence.route_qualification.shadow_only)
        self.assertTrue(parsed.presence.shadow_only)
        self.assertFalse(parsed.detection_zone.enabled)
        self.assertEqual((parsed.capture.width, parsed.capture.height), (3840, 2160))
        for key, value in template['alpr']['thresholds'].items():
            if not key.startswith('//'):
                self.assertEqual(cfg['alpr']['thresholds'][key], value)
        self.assertNotIn('hours', cfg)

    def test_stream_credentials_are_encoded_and_outputs_stay_local(self):
        cfg = stream_config({'username': 'test@user', 'password': 'secret%:#\ntext'})
        self.assertIn('test%40user:secret%25%3A%23%0Atext@192.168.50.2', cfg)
        self.assertNotIn('secret%:#', cfg)
        self.assertIn('url = http://127.0.0.1:8129/', cfg)
        self.assertNotIn('csv_file', cfg)
        self.assertNotIn('jsonlines_file', cfg)
        self.assertIn('image_format =\n', cfg)


if __name__ == "__main__":
    unittest.main()
