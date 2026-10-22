#!/usr/bin/env python3
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "bench_identity", Path(__file__).with_name("prepare_replacement_bench_identity.py"))
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


class IdentityTests(unittest.TestCase):
    def test_prepare_does_not_touch_aws(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(subject, "aws") as aws:
            subject.prepare(Path(folder))
            aws.assert_not_called()
            key = Path(folder, "bench-api-key.txt")
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(key.read_text().strip()), 64)

    def test_creation_never_uses_production_identity(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(subject, "aws", return_value={}) as aws:
            subject.prepare(Path(folder), True)
            create = aws.call_args_list[1].args
            self.assertEqual(create[0], "put-item")
            self.assertEqual(create[1]["Item"]["location_id"]["S"], "bench-servcamog")
            self.assertEqual(create[1]["ConditionExpression"], "attribute_not_exists(device_id)")
            self.assertNotIn("serv-unit-001", str(aws.call_args_list))

    def test_conflict_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(subject, "aws", return_value={"Item": {"device_id": {"S": subject.DEVICE}}}) as aws:
            with self.assertRaises(ValueError):
                subject.prepare(Path(folder), True)
            self.assertEqual(aws.call_count, 1)

    def test_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "bench-api-key.txt").symlink_to("missing")
            with self.assertRaises(ValueError):
                subject.prepare(Path(folder))

    def test_public_directory_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            os.chmod(folder, 0o755)
            with self.assertRaises(ValueError):
                subject.prepare(Path(folder))


if __name__ == "__main__":
    unittest.main()
