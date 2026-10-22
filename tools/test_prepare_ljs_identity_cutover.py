#!/usr/bin/env python3
"""No network: existing-record CAS, staging permissions and secret boundaries."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("cutover", Path(__file__).with_name("prepare_ljs_identity_cutover.py"))
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)
OLD = "a" * 64
NEW = "b" * 64
SHA = "c" * 40


class CutoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.item = {
            "device_id": {"S": subject.DEVICE}, "location_id": {"S": subject.LOCATION},
            "status": {"S": "active"}, "api_key": {"S": OLD}, "created_at": {"S": "2026-06-13T00:00:00Z"},
            "type": {"S": "device"},
        }
        self.calls = []
        self.aws_patch = patch.object(subject, "aws", self.fake_aws)
        self.aws_patch.start()
        self.addCleanup(self.aws_patch.stop)
        self.key_patch = patch.object(subject.secrets, "token_hex", return_value=NEW)
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)

    def fake_aws(self, operation, payload):
        self.calls.append((operation, copy.deepcopy(payload)))
        if operation == "get-item":
            return {"Item": copy.deepcopy(self.item)}
        self.assertEqual(operation, "update-item")
        self.assertEqual(self.item["api_key"], payload["ExpressionAttributeValues"][":old"])
        self.item["api_key"] = copy.deepcopy(payload["ExpressionAttributeValues"][":new"])
        return {}

    def stage(self):
        return subject.stage(self.directory, SHA)

    def held_host(self):
        self.config = self.directory / "production-config.json"
        data = (json.dumps({"device_id": subject.DEVICE, "location_id": subject.LOCATION,
                           "api_base": subject.API_BASE, "api_key": NEW}) + "\n").encode()
        subject.create_private(self.config, data)
        self.proof = self.directory / "host-ready.json"
        proof = {"device_id": subject.DEVICE, "location_id": subject.LOCATION,
                 "hostname": subject.HOSTNAME, "reviewed_sha": SHA,
                 "config_sha256": subject.digest(data), "api_key_sha256": subject.digest(NEW),
                 "queue_isolated": True, "shipping_hold": True,
                 "phase": "host_prepared_cloud_pending"}
        subject.create_private(self.proof, json.dumps(proof).encode())
        return proof

    def modify_proof(self, key, value):
        proof = subject.read_json(self.proof)
        proof[key] = value
        self.proof.write_text(json.dumps(proof))

    def test_stage_only_reads_minimal_existing_record(self):
        result = self.stage()
        self.assertFalse(result["aws_changed"])
        self.assertEqual([x[0] for x in self.calls], ["get-item"])
        payload = self.calls[0][1]
        self.assertTrue(payload["ConsistentRead"])
        self.assertEqual(payload["Key"], {"device_id": {"S": subject.DEVICE}})
        self.assertEqual(set(payload["ExpressionAttributeNames"].values()), set(subject._FIELDS))
        self.assertEqual((self.directory / "old-api-key.txt").read_text().strip(), OLD)
        for name in ("old-api-key.txt", "new-api-key.txt", "cutover.json"):
            self.assertEqual(stat.S_IMODE((self.directory / name).stat().st_mode), 0o600)
        self.assertNotIn(OLD, (self.directory / "cutover.json").read_text())
        self.assertNotIn(NEW, (self.directory / "cutover.json").read_text())

    def test_stage_idempotent_and_no_key_regeneration(self):
        self.stage()
        with patch.object(subject.secrets, "token_hex", side_effect=AssertionError("do not regenerate")):
            result = self.stage()
        self.assertTrue(result["reused_stage"])
        self.assertTrue(all(call[0] == "get-item" for call in self.calls))

    def test_rotation_only_conditional_key_update_and_retry_idempotent(self):
        self.stage()
        self.held_host()
        original = copy.deepcopy(self.item)
        result = subject.rotate(self.directory, self.config, self.proof)
        self.assertEqual(result["changed_projected_attributes"], ["api_key"])
        writes = [p for op, p in self.calls if op == "update-item"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["UpdateExpression"], "SET #api_key = :new")
        for token in ("attribute_exists(#device_id)", "#location_id = :location", "#status = :active", "#api_key = :old"):
            self.assertIn(token, writes[0]["ConditionExpression"])
        self.assertEqual(writes[0]["ReturnValues"], "NONE")
        original["api_key"] = {"S": NEW}
        self.assertEqual(self.item, original)
        self.assertTrue(subject.rotate(self.directory, self.config, self.proof)["already_rotated"])
        self.assertEqual(sum(op == "update-item" for op, _ in self.calls), 1)
        self.assertEqual(self.stage()["credential_state"], "new")

    def test_wrong_record_or_missing_record_fails_closed(self):
        for field, value in (("device_id", "other"), ("location_id", "other"), ("status", "inactive")):
            with self.subTest(field=field):
                original = self.item[field]
                self.item[field] = {"S": value}
                with self.assertRaises(subject.CutoverError):
                    self.stage()
                self.item[field] = original
        with patch.object(subject, "aws", return_value={}):
            with self.assertRaises(subject.CutoverError):
                self.stage()

    def test_conflicting_cloud_key_never_overwritten(self):
        self.stage()
        self.held_host()
        self.item["api_key"] = {"S": "d" * 64}
        with self.assertRaises(subject.CutoverError):
            subject.rotate(self.directory, self.config, self.proof)
        self.assertFalse(any(op == "update-item" for op, _ in self.calls))

    def test_malformed_cloud_credential_fails_without_traceback_data(self):
        self.item["api_key"] = OLD
        with self.assertRaises(subject.CutoverError):
            self.stage()

    def test_host_proof_every_required_field_enforced(self):
        self.stage()
        proof = self.held_host()
        for field in proof:
            with self.subTest(field=field):
                self.modify_proof(field, "wrong")
                with self.assertRaises(subject.CutoverError):
                    subject.rotate(self.directory, self.config, self.proof)
                self.modify_proof(field, proof[field])
        self.assertFalse(any(op == "update-item" for op, _ in self.calls))

    def test_numeric_boolean_proof_rejected(self):
        self.stage()
        self.held_host()
        self.modify_proof("shipping_hold", 1)
        with self.assertRaises(subject.CutoverError):
            subject.rotate(self.directory, self.config, self.proof)

    def test_wrong_config_key_rejected_even_with_matching_proof_hash(self):
        self.stage()
        self.held_host()
        self.config.write_text(self.config.read_text().replace(NEW, OLD))
        self.modify_proof("config_sha256", subject.digest(self.config.read_bytes()))
        with self.assertRaises(subject.CutoverError):
            subject.rotate(self.directory, self.config, self.proof)

    def test_metadata_change_requires_review(self):
        self.stage()
        self.item["created_at"] = {"S": "changed"}
        with self.assertRaises(subject.CutoverError):
            subject.verify(self.directory)

    def test_uncertain_success_reconciled_read_only(self):
        self.stage()
        self.held_host()
        def uncertain(operation, payload):
            result = self.fake_aws(operation, payload)
            if operation == "update-item":
                raise subject.CutoverError("request timed out")
            return result
        with patch.object(subject, "aws", uncertain):
            self.assertTrue(subject.rotate(self.directory, self.config, self.proof)["response_reconciled"])
        self.assertEqual(sum(op == "update-item" for op, _ in self.calls), 1)

    def test_failed_cas_does_not_overwrite_or_report_success(self):
        self.stage()
        self.held_host()
        def failed(operation, payload):
            if operation == "update-item":
                raise subject.CutoverError("failed")
            return self.fake_aws(operation, payload)
        with patch.object(subject, "aws", failed):
            with self.assertRaises(subject.CutoverError):
                subject.rotate(self.directory, self.config, self.proof)
        self.assertEqual(self.item["api_key"]["S"], OLD)

    def test_public_directory_and_key_rejected(self):
        os.chmod(self.directory, 0o755)
        with self.assertRaises(subject.CutoverError):
            self.stage()
        os.chmod(self.directory, 0o700)
        self.stage()
        os.chmod(self.directory / "new-api-key.txt", 0o644)
        with self.assertRaises(subject.CutoverError):
            subject.verify(self.directory)

    def test_symlink_file_and_directory_rejected(self):
        (self.directory / "new-api-key.txt").symlink_to("missing")
        with self.assertRaises(subject.CutoverError):
            self.stage()
        link = self.directory / "link"
        link.symlink_to(self.directory)
        with self.assertRaises(subject.CutoverError):
            subject.private_directory(link)

    def test_partial_stage_never_regenerates(self):
        subject.create_private(self.directory / "old-api-key.txt", OLD.encode())
        with patch.object(subject.secrets, "token_hex", side_effect=AssertionError("do not regenerate")):
            with self.assertRaises(subject.CutoverError):
                self.stage()

    def test_stage_release_cannot_be_changed(self):
        self.stage()
        with self.assertRaises(subject.CutoverError):
            subject.stage(self.directory, "d" * 40)

    def test_cli_does_not_emit_secrets_on_success_or_error(self):
        for fail in (False, True):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                if fail:
                    with patch.object(subject, "stage", side_effect=ValueError(OLD + NEW)):
                        rc = subject.main(["stage", "--directory", str(self.directory), "--reviewed-sha", SHA])
                else:
                    rc = subject.main(["stage", "--directory", str(self.directory), "--reviewed-sha", SHA])
            self.assertEqual(rc, int(fail))
            self.assertNotIn(OLD, stdout.getvalue() + stderr.getvalue())
            self.assertNotIn(NEW, stdout.getvalue() + stderr.getvalue())


class AwsTransportTests(unittest.TestCase):
    def test_credentials_only_in_private_temporary_payload_not_argv(self):
        paths = []
        def run(argv, **kwargs):
            self.assertNotIn(OLD, str(argv))
            self.assertNotIn(NEW, str(argv))
            self.assertEqual(argv[argv.index("--region") + 1], "us-east-1")
            filename = Path(argv[argv.index("--cli-input-json") + 1][7:])
            self.assertEqual(stat.S_IMODE(filename.stat().st_mode), 0o600)
            self.assertEqual(json.loads(filename.read_text()), {"old": OLD, "new": NEW})
            paths.append(filename)
            self.assertTrue(kwargs["capture_output"])
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        with patch.object(subject.subprocess, "run", run):
            subject.aws("update-item", {"old": OLD, "new": NEW})
        self.assertFalse(paths[0].exists())

    def test_provider_failure_and_timeout_do_not_expose_payload(self):
        for outcome in (subprocess.CompletedProcess([], 1, OLD, NEW),
                        subprocess.TimeoutExpired(["aws"], 45, output=OLD, stderr=NEW)):
            with self.subTest(outcome=type(outcome).__name__):
                with patch.object(subject.subprocess, "run", side_effect=outcome if isinstance(outcome, Exception) else None,
                                  return_value=outcome):
                    with self.assertRaises(subject.CutoverError) as raised:
                        subject.aws("update-item", {"old": OLD, "new": NEW})
                self.assertNotIn(OLD, str(raised.exception))
                self.assertNotIn(NEW, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
