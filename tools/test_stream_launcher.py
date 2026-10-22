#!/usr/bin/env python3
"""Exercise launcher safety with a fake Docker binary; never starts Docker."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def run_launcher(self, secret="TOKEN=test-token\nLICENSE_KEY=test-license\n",
                     mode=0o600, exists=False, **extra):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            credentials = root / "credentials.env"
            credentials.write_text(secret)
            credentials.chmod(mode)
            (root / "config.ini").write_text("[cameras]\n")
            docker = root / "docker"
            docker.write_text("#!/bin/sh\n"
                              'printf "%s\\n" "$*" >> "$DOCKER_CALLS"\n'
                              'if [ "$1 $2" = "container inspect" ]; then exit "$CONTAINER_EXISTS"; fi\n'
                              "exit 0\n")
            docker.chmod(0o700)
            calls = root / "calls"
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"],
                       ENV_FILE=str(credentials), WORKDIR=str(root),
                       DOCKER_CALLS=str(calls), CONTAINER_EXISTS="0" if exists else "1",
                       STREAM_IMAGE="platerecognizer/alpr-stream@sha256:" + "a" * 64,
                       **extra)
            p = subprocess.run(["bash", str(ROOT / "stream/run_stream.sh")],
                               env=env, text=True, capture_output=True)
            return p, calls.read_text() if calls.exists() else ""

    def test_no_pull_and_no_force_removal(self):
        p, calls = self.run_launcher()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("image inspect", calls)
        self.assertIn("run --detach", calls)
        self.assertNotIn("pull ", calls)
        self.assertNotIn("rm ", calls)
        self.assertNotIn("test-token", calls + p.stdout + p.stderr)

    def test_existing_container_preserved(self):
        p, calls = self.run_launcher(exists=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("run ", calls)
        self.assertNotIn("rm ", calls)

    def test_public_credentials_rejected(self):
        p, calls = self.run_launcher(mode=0o644)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(calls, "")

    def test_shell_code_is_never_sourced(self):
        p, calls = self.run_launcher(secret="TOKEN=x\nLICENSE_KEY=y\necho EXECUTED\n")
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("EXECUTED", p.stdout)
        self.assertEqual(calls, "")

    def test_duplicate_field_rejected(self):
        p, calls = self.run_launcher(secret="TOKEN=x\nTOKEN=z\nLICENSE_KEY=y\n")
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(calls, "")

    def test_manual_pull_is_explicit(self):
        p, calls = self.run_launcher(SKIP_PULL="0")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("pull platerecognizer/alpr-stream@sha256:", calls)

    def test_infrastructure_cannot_be_mislabeled_as_ota_applied(self):
        source = (ROOT / "scripts/update.sh").read_text()
        manual_gate = source.split("# Did dependencies change?", 1)[1].split("sync_code", 1)[0]
        for path in ("scripts/cellular-connect.sh", "scripts/serv-cellular-supervisor",
                     "systemd/cellular-connect.service", "scripts/network-priority.sh",
                     "scripts/serv-capture-hours", "stream/run_stream.sh"):
            self.assertIn(path, manual_gate)


if __name__ == "__main__":
    unittest.main()
