#!/usr/bin/env python3
"""Unprivileged identity-handover fixtures: no AWS, sudo, network or real state."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/install_ljs_identity_cutover.sh'
SOURCE = SCRIPT.read_text()

def embedded(name):
    block = re.search(r"<<'" + name + r"'\n(.*?)\n" + name, SOURCE, re.S).group(1)
    namespace = {'__name__': 'unprivileged_cutover_test'}
    exec(compile(block, str(SCRIPT), 'exec'), namespace)
    return namespace

validate = embedded('PY_PREFLIGHT')['validate_preflight']
archive_state = embedded('PY_STATE')['archive_state']
BASE = '4ad0ab71e0a145daf3a13d80305217e1d43e59b5'
SHA = 'a' * 40
HELPER = b'reviewed helper fixture'
HELPER_SHA = hashlib.sha256(HELPER).hexdigest()

class IdentityCutoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        for name in ('etc/serv', 'etc/systemd/system', 'var/lib/serv-replacement',
                     'usr/local/sbin', 'home/serv/plate_reader', 'home/serv/serv-pi',
                     'home/serv/stream', 'run', 'inputs'):
            (self.root / name).mkdir(parents=True, mode=0o755, exist_ok=True)
        # Parent mkdir modes otherwise vary with the invoking shell's umask.
        for folder in self.root.rglob('*'):
            if folder.is_dir(): folder.chmod(0o755)
        self.write('etc/serv/approved-deploy-sha', SHA)
        self.write_json('etc/serv/replacement-bench.json', dict(phase='bench_only', reviewed_sha=BASE))
        self.write_json('etc/serv/cellular-patch.json', dict(application_baseline_sha=BASE, installed_helper_sha256=HELPER_SHA))
        self.write('usr/local/sbin/serv-cellular-supervisor', HELPER, 0o755)
        self.write('home/serv/plate_reader/.deployed_sha', BASE, 0o640)
        self.write('home/serv/plate_reader/.update_channel', 'codex/ljs-replacement-recovery')
        self.active = dict(device_id='serv-unit-bench', location_id='bench-servcamog',
            api_base='https://sb13iibr7f.execute-api.us-east-1.amazonaws.com',
            alpr={'engine': 'pr_stream'}, api_key='b' * 64)
        self.new = dict(self.active, device_id='serv-unit-001', location_id='ljs-louisville-001', api_key='c' * 64)
        self.write_json('home/serv/plate_reader/config.json', self.active)
        self.write_json('inputs/production.json', self.new)
        self.write('etc/hostname', 'servCamOG\n', 0o644)
        self.write('etc/hosts', '127.0.1.1 servCamOG\n', 0o644)

    def tearDown(self): self.tmp.cleanup()

    def write(self, name, data, mode=0o600):
        path = self.root / name
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        path.chmod(mode)
        return path

    def write_json(self, name, value): return self.write(name, json.dumps(value))

    def check(self):
        validate(self.root, SHA, BASE, HELPER_SHA, '/inputs/production.json', root_uid=os.getuid())

    def rejected(self):
        with self.assertRaises((ValueError, OSError)): self.check()

    def test_valid_exact_identity(self): self.check()

    def test_approval_mismatch(self):
        self.write('etc/serv/approved-deploy-sha', 'f' * 40)
        self.rejected()

    def test_world_readable_approval(self):
        (self.root / 'etc/serv/approved-deploy-sha').chmod(0o644)
        self.rejected()

    def test_missing_bench_guard(self):
        (self.root / 'etc/serv/replacement-bench.json').unlink()
        self.rejected()

    def test_wrong_bench_baseline(self):
        self.write_json('etc/serv/replacement-bench.json', dict(phase='bench_only', reviewed_sha='d' * 40))
        self.rejected()

    def test_wrong_bench_phase(self):
        self.write_json('etc/serv/replacement-bench.json', dict(phase='production', reviewed_sha=BASE))
        self.rejected()

    def test_existing_private_handover_rejected(self):
        self.write_json('etc/serv/identity-cutover.json', {})
        self.rejected()

    def test_existing_public_proof_rejected(self):
        self.write_json('etc/serv/identity-cutover-ready.json', {})
        self.rejected()

    def test_existing_runtime_hold_rejected(self):
        self.write('run/serv-ljs-shipping-hold', 'held')
        self.rejected()

    def test_manual_override_rejected(self):
        self.write('home/serv/plate_reader/.manual_override', 'operator hold')
        self.rejected()

    def test_patch_marker_mismatch(self):
        self.write_json('etc/serv/cellular-patch.json', dict(application_baseline_sha=BASE, installed_helper_sha256='0' * 64))
        self.rejected()

    def test_changed_running_helper_rejected(self):
        self.write('usr/local/sbin/serv-cellular-supervisor', 'changed', 0o755)
        self.rejected()

    def test_wrong_app_baseline_rejected(self):
        self.write('home/serv/plate_reader/.deployed_sha', 'f' * 40, 0o640)
        self.rejected()

    def test_wrong_channel_rejected(self):
        self.write('home/serv/plate_reader/.update_channel', 'main')
        self.rejected()

    def test_active_production_rejected(self):
        self.write_json('home/serv/plate_reader/config.json', self.new)
        self.rejected()

    def test_wrong_new_device_rejected(self):
        self.new['device_id'] = 'serv-unit-003'
        self.write_json('inputs/production.json', self.new)
        self.rejected()

    def test_wrong_new_location_rejected(self):
        self.new['location_id'] = 'not-ljs'
        self.write_json('inputs/production.json', self.new)
        self.rejected()

    def test_wrong_engine_rejected(self):
        self.new['alpr'] = {'engine': 'ultimatealpr'}
        self.write_json('inputs/production.json', self.new)
        self.rejected()

    def test_new_endpoint_rejected(self):
        self.new['api_base'] = 'https://example.com'
        self.write_json('inputs/production.json', self.new)
        self.rejected()

    def test_bench_key_reuse_rejected(self):
        self.new['api_key'] = self.active['api_key']
        self.write_json('inputs/production.json', self.new)
        self.rejected()

    def test_malformed_key_rejected(self):
        self.new['api_key'] = 'not-a-valid-key'
        self.write_json('inputs/production.json', self.new)
        self.rejected()

    def test_readable_credential_rejected(self):
        (self.root / 'inputs/production.json').chmod(0o644)
        self.rejected()

    def test_key_symlink_rejected(self):
        candidate = self.root / 'inputs/production.json'
        candidate.unlink()
        candidate.symlink_to(self.root / 'home/serv/plate_reader/config.json')
        self.rejected()

    def test_bad_hostname_rejected(self):
        self.write('etc/hostname', 'other-pi\n', 0o644)
        self.rejected()

    def test_state_symlink_rejected(self):
        (self.root / 'home/serv/plate_reader/queue.db').symlink_to(self.root / 'etc/hosts')
        self.rejected()

    def test_state_fifo_rejected(self):
        os.mkfifo(self.root / 'home/serv/plate_reader/special')
        self.rejected()

    def test_unsafe_privileged_parent_rejected(self):
        (self.root / 'etc/systemd/system').chmod(0o777)
        self.rejected()

    def test_extra_dropin_rejected(self):
        self.write('etc/systemd/system/mediamtx.service.d/arbitrary.conf', '[Service]\n', 0o644)
        self.rejected()

    def test_expected_dropins_accepted(self):
        self.write('etc/systemd/system/plate-reader.service.d/20-replacement-hold.conf', '[Service]\n', 0o644)
        self.write('etc/systemd/system/serv-capture-hours.service.d/20-image-pin.conf', '[Service]\n', 0o644)
        self.check()

    def test_wrong_root_owner_rejected(self):
        with self.assertRaises(ValueError):
            validate(self.root, SHA, BASE, HELPER_SHA, '/inputs/production.json', root_uid=os.getuid() + 1)

    def test_complete_state_preserved_and_new_state_clean(self):
        expected = {}
        for name in ('queue.db', 'queue.db-wal', 'queue.db-shm', 'bench_audit_cases/case.jpg', 'bench_audit_cases/case.json'):
            expected[name] = ('private fixture ' + name).encode()
            self.write('home/serv/plate_reader/' + name, expected[name])
        old_config = (self.root / 'home/serv/plate_reader/config.json').read_bytes()
        backup = self.root / 'var/lib/serv-replacement/identity-fixture'
        backup.mkdir(mode=0o700)
        archive_state(self.root, backup, uid=os.getuid(), gid=os.getgid(), root_uid=os.getuid(), root_gid=os.getgid())
        fresh = self.root / 'home/serv/plate_reader'
        self.assertEqual({p.name for p in fresh.iterdir()}, {'.deployed_sha', '.update_channel'})
        self.assertEqual((fresh / '.deployed_sha').read_text(), BASE)
        self.assertEqual((fresh / '.deployed_sha').stat().st_mode & 0o777, 0o640)
        self.assertEqual((fresh / '.update_channel').stat().st_mode & 0o777, 0o600)
        for name, content in expected.items():
            self.assertEqual((backup / 'bench-state' / name).read_bytes(), content)
        self.assertEqual((backup / 'bench-state/config.json').read_bytes(), old_config)

    def test_repeated_archive_fails_without_changing_source(self):
        backup = self.root / 'var/lib/serv-replacement/identity-fixture'
        (backup / 'bench-state').mkdir(mode=0o700, parents=True)
        before = (self.root / 'home/serv/plate_reader/config.json').read_bytes()
        with self.assertRaises(ValueError):
            archive_state(self.root, backup, uid=os.getuid(), gid=os.getgid(), root_uid=os.getuid(), root_gid=os.getgid())
        self.assertEqual((self.root / 'home/serv/plate_reader/config.json').read_bytes(), before)

    def test_help_and_shell_syntax(self):
        subprocess.run(['bash', '-n', str(SCRIPT)], check=True)
        result = subprocess.run(['bash', str(SCRIPT), '--help'], text=True, capture_output=True, check=True)
        self.assertIn('ALREADY human-approved', result.stdout)
        self.assertIn('.deployed_sha stays unchanged', result.stdout)

    def test_missing_arguments_fails_before_any_action(self):
        result = subprocess.run(['bash', str(SCRIPT)], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--reviewed-sha', result.stderr)

    def test_boundaries_and_stop_before_state_move(self):
        self.assertLess(SOURCE.index('validate_preflight(Path'), SOURCE.index('mktemp -d'))
        self.assertLess(SOURCE.index('cmp -s "${BASH_SOURCE[0]}"'), SOURCE.index('trap failed EXIT'))
        self.assertLess(SOURCE.index("<<'PY_HOLD'"), SOURCE.index('docker update --restart=no stream >/dev/null\n'))
        self.assertLess(SOURCE.index('systemctl stop plate-reader.service mediamtx.service\n'), SOURCE.index("<<'PY_STATE'"))
        self.assertLess(SOURCE.index("<<'PY_STATE'"), SOURCE.index('hostnamectl set-hostname'))
        self.assertLess(SOURCE.index('systemctl enable plate-reader.service'), SOURCE.index('identity-cutover-ready.json\', os.O_WRONLY'))
        for forbidden in ('systemctl restart cellular', 'systemctl restart NetworkManager', 'tailscale up',
                          'tailscale logout', 'tailscaled.state', 'ssh_host_', 'aws ', 'rm -',
                          '> /etc/serv/approved-deploy-sha', '> /home/serv/plate_reader/.deployed_sha'):
            self.assertNotIn(forbidden, SOURCE)
        self.assertIn('gitc archive --format=tar "$SHA"', SOURCE)
        self.assertIn('config_sha256=', SOURCE)
        self.assertIn('api_key_sha256=', SOURCE)
        self.assertIn("queue_isolated=True, shipping_hold=True", SOURCE)
        self.assertIn('tailscale_dns_name=tailnet.get', SOURCE)
        self.assertIn('for competing in plate_reader.service pr-stream.service stream.service ultimatealpr.service', SOURCE)

if __name__ == '__main__': unittest.main(verbosity=2)
