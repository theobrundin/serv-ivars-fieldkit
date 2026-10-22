import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "recover_detection_dead_letters.py"


class DeadLetterRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "queue.db"
        with sqlite3.connect(self.db) as conn:
            conn.executescript("""
                CREATE TABLE queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    enqueued_at REAL NOT NULL,
                    endpoint TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at REAL,
                    last_status_code INTEGER,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    priority INTEGER NOT NULL DEFAULT 20,
                    idempotency_key TEXT
                );
                CREATE TABLE dead_letter (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_queue_id INTEGER,
                    enqueued_at REAL NOT NULL,
                    failed_at REAL NOT NULL,
                    endpoint TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    status_code INTEGER,
                    reason TEXT NOT NULL,
                    idempotency_key TEXT
                );
            """)
            rows = [
                ("below_threshold", None),
                ("unreadable", "unreadable-key"),
                ("group_confidence_floor", "already-key"),
                ("vehicle_only", "never-replay"),
            ]
            for index, (outcome, key) in enumerate(rows, 1):
                conn.execute(
                    "INSERT INTO dead_letter "
                    "(original_queue_id,enqueued_at,failed_at,endpoint,payload,"
                    "attempts,status_code,reason,idempotency_key) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        index, 1000 + index, 2000 + index, "/detection-event",
                        json.dumps({"outcome": outcome}), 1, 400,
                        "permanent_http_error", key,
                    ),
                )
            conn.execute(
                "INSERT INTO queue "
                "(enqueued_at,endpoint,payload,idempotency_key) VALUES (?,?,?,?)",
                (1003, "/detection-event", "{}", "already-key"),
            )
            conn.commit()

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, *args):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--db", str(self.db), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_dry_run_is_read_only_and_apply_is_idempotent(self):
        self.assertEqual(self.run_script(), [{
            "eligible": 3,
            "outcomes": {
                "below_threshold": 1,
                "group_confidence_floor": 1,
                "unreadable": 1,
            },
        }])
        with sqlite3.connect(self.db) as conn:
            self.assertNotIn(
                "recovered_at",
                {row[1] for row in conn.execute("PRAGMA table_info(dead_letter)")},
            )

        recovery_started = time.time()
        self.assertEqual(self.run_script("--apply")[1], {
            "already_queued": 1,
            "requeued": 2,
        })

        with sqlite3.connect(self.db) as conn:
            keys = {
                row[0] for row in conn.execute(
                    "SELECT idempotency_key FROM queue"
                )
            }
            self.assertIn("already-key", keys)
            self.assertIn("unreadable-key", keys)
            self.assertEqual(len(keys), 3)
            self.assertNotIn(None, keys)
            recovered_enqueue_times = [
                row[0] for row in conn.execute(
                    "SELECT enqueued_at FROM queue "
                    "WHERE idempotency_key != 'already-key'"
                )
            ]
            self.assertTrue(recovered_enqueue_times)
            self.assertTrue(all(
                timestamp >= recovery_started
                for timestamp in recovered_enqueue_times
            ))
            recovered = conn.execute(
                "SELECT COUNT(*) FROM dead_letter WHERE recovered_at IS NOT NULL"
            ).fetchone()[0]
            self.assertEqual(recovered, 3)
            vehicle_only = conn.execute(
                "SELECT recovered_at FROM dead_letter "
                "WHERE idempotency_key='never-replay'"
            ).fetchone()[0]
            self.assertIsNone(vehicle_only)

        self.assertEqual(self.run_script(), [{"eligible": 0, "outcomes": {}}])


if __name__ == "__main__":
    unittest.main()
