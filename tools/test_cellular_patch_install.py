#!/usr/bin/env python3
"""Unprivileged preflight tests; never runs a real installer or remote command."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/install_cellular_patch.sh'
SOURCE = SCRIPT.read_text()
BLOCK = re.search(r"<<'PY_PREFLIGHT'\n(.*?)\nPY_PREFLIGHT", SOURCE, re.S).group(1)
namespace = {'__name__': 'patch_preflight_tests'}
exec(compile(BLOCK, str(SCRIPT), 'exec'), namespace)
validate = namespace['validate_preflight']
BASE = '4ad0ab71e0a145daf3a13d80305217e1d43e59b5'
PATCH = 'a' * 40


class CellularPatchPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        for path in ('etc/serv', 'var/lib/serv-replacement', 'home/serv/serv-pi',
                     'usr/local/sbin', 'run/serv-cellular'):
            # Match the privileged production directories explicitly; the Pi's
            # interactive umask may be 0002, unlike the developer Mac's 0022.
            (self.root / path).mkdir(mode=0o755, parents=True, exist_ok=True)
        self.write('etc/serv/approved-deploy-sha', PATCH)
        self.write('etc/serv/replacement-bench.json', json.dumps(dict(phase='bench_only', reviewed_sha=BASE)))
        self.config = dict(device_id='serv-unit-bench', location_id='bench-servcamog',
                           api_base='https://sb13iibr7f.execute-api.us-east-1.amazonaws.com',
                           alpr={'engine': 'pr_stream'}, api_key='not-printed')
        self.write('home/serv/plate_reader/config.json', json.dumps(self.config))
        self.write('home/serv/plate_reader/.deployed_sha', BASE, 0o640)
        self.write('home/serv/plate_reader/.update_channel', 'codex/ljs-replacement-recovery')
        self.write('usr/local/sbin/serv-cellular-supervisor', 'old helper', 0o755)
        self.write('usr/local/bin/cellular-connect.sh', 'old wrapper', 0o755)
        self.write('etc/systemd/system/cellular-connect.service', 'old unit', 0o644)
        self.write('run/serv-cellular/allocation-uncertain.json', '{"uncertain":true}')

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, text, mode=0o600):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)
        return path

    def check(self):
        validate(self.root, PATCH, BASE, root_uid=os.getuid())

    def test_valid_bench_and_uncertainty_untouched(self):
        state = self.root / 'run/serv-cellular/allocation-uncertain.json'
        before = state.read_bytes()
        self.check()
        self.assertEqual(before, state.read_bytes())

    def test_wrong_approval_rejected(self):
        self.write('etc/serv/approved-deploy-sha', 'b' * 40)
        with self.assertRaises(ValueError): self.check()

    def test_approval_permissions_rejected(self):
        (self.root / 'etc/serv/approved-deploy-sha').chmod(0o644)
        with self.assertRaises(ValueError): self.check()

    def test_missing_bench_marker_rejected(self):
        (self.root / 'etc/serv/replacement-bench.json').unlink()
        with self.assertRaises(OSError): self.check()

    def test_production_marker_rejected(self):
        self.write('etc/serv/replacement-bench.json', json.dumps(dict(phase='production', reviewed_sha=BASE)))
        with self.assertRaises(ValueError): self.check()

    def test_production_identity_rejected(self):
        self.config['device_id'] = 'serv-unit-001'
        self.config['location_id'] = 'ljs-louisville-001'
        self.write('home/serv/plate_reader/config.json', json.dumps(self.config))
        with self.assertRaises(ValueError): self.check()

    def test_wrong_engine_rejected(self):
        self.config['alpr']['engine'] = 'ultimatealpr'
        self.write('home/serv/plate_reader/config.json', json.dumps(self.config))
        with self.assertRaises(ValueError): self.check()

    def test_baseline_rejected(self):
        self.write('home/serv/plate_reader/.deployed_sha', 'b' * 40, 0o640)
        with self.assertRaises(ValueError): self.check()

    def test_baseline_marker_permissions_rejected(self):
        self.write('home/serv/plate_reader/.deployed_sha', BASE, 0o600)
        with self.assertRaises(ValueError): self.check()

    def test_channel_rejected(self):
        self.write('home/serv/plate_reader/.update_channel', 'main')
        with self.assertRaises(ValueError): self.check()

    def test_prior_patch_rejected(self):
        self.write('etc/serv/cellular-patch.json', '{}')
        with self.assertRaises(ValueError): self.check()

    def test_helper_symlink_rejected(self):
        path = self.root / 'usr/local/sbin/serv-cellular-supervisor'
        path.unlink()
        path.symlink_to(self.root / 'etc/serv/approved-deploy-sha')
        with self.assertRaises(ValueError): self.check()

    def test_runtime_state_symlink_rejected(self):
        (self.root / 'run/serv-cellular/evil.json').symlink_to(self.root / 'etc/serv/approved-deploy-sha')
        with self.assertRaises(ValueError): self.check()

    def test_world_writable_stage_parent_rejected(self):
        (self.root / 'var/lib/serv-replacement').chmod(0o777)
        with self.assertRaises(ValueError): self.check()

    def test_optional_environment_symlink_rejected(self):
        (self.root / 'etc/serv/cellular.env').symlink_to(self.root / 'etc/serv/approved-deploy-sha')
        with self.assertRaises(ValueError): self.check()

    def test_wrong_owner_rejected(self):
        with self.assertRaises(ValueError):
            validate(self.root, PATCH, BASE, root_uid=os.getuid() + 1)

    def test_help_and_syntax_safe_without_root(self):
        subprocess.run(['bash', '-n', str(SCRIPT)], check=True)
        result = subprocess.run(['bash', str(SCRIPT), '--help'], capture_output=True, text=True, check=True)
        self.assertIn('ALREADY', result.stdout)
        self.assertIn('.deployed_sha stays unchanged', result.stdout)

    def test_missing_sha_fails_without_installation(self):
        result = subprocess.run(['bash', str(SCRIPT)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--reviewed-sha', result.stderr)

    def test_installation_boundaries_and_order(self):
        self.assertLess(SOURCE.index('validate_preflight(Path'), SOURCE.index('mktemp -d'))
        self.assertLess(SOURCE.index('WiFi-bound TLS connectivity failed'), SOURCE.index('systemctl stop cellular-connect.service'))
        self.assertLess(SOURCE.index('installed helper differs from the supported baseline'), SOURCE.index('systemctl stop cellular-connect.service'))
        self.assertLess(SOURCE.index('trap failure_report EXIT'), SOURCE.index('systemctl stop cellular-connect.service'))
        self.assertLess(SOURCE.index('systemctl stop cellular-connect.service'), SOURCE.index('mv -f "$NEXT" "$TARGET"'))
        self.assertIn('gitc archive --format=tar "$SHA" scripts/serv-cellular-supervisor', SOURCE)
        self.assertIn('python3 "$STAGE/guard.py" extract', SOURCE)
        self.assertIn('gitc diff --quiet "$BASE" "$SHA" -- plate_reader', SOURCE)
        self.assertIn("curl -4 --noproxy '*' --proto '=https' --interface wlan0", SOURCE)
        self.assertIn('/run/serv-cellular/last-failure.json', SOURCE)
        self.assertNotIn('systemctl restart plate-reader', SOURCE)
        self.assertNotIn('rm ', SOURCE)
        self.assertNotIn('reboot\n', SOURCE)
        self.assertNotIn('> /etc/serv/approved-deploy-sha', SOURCE)
        self.assertNotIn('> /home/serv/plate_reader/.deployed_sha', SOURCE)


if __name__ == '__main__':
    unittest.main(verbosity=2)
