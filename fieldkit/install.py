"""Manual reviewed host install. A HUMAN must supply the approved exact SHA.

This is not the fleet updater and does not approve a fleet update. Leaves the
unit in its current sandbox mode. Production is a separate commissioning step.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import tempfile
import time

# The one allowed app migration is the shared weekday-hours parser. This is
# the config.py digest verified on the held bench before that change. Other
# recognition source must already equal the human-approved release exactly.
BENCH_CONFIG_SHA256 = '5d5a5dd0c057ea14eb5e9fef07af961e89e4c80f80ea5df60fbd271e7109d1d3'


def application_updates(approved, installed_root):
    if {p.name for p in installed_root.glob('*.py')} != set(approved):
        raise ValueError('Installed application module list differs')
    updates = {}
    for name, expected in approved.items():
        installed = installed_root / name
        if installed.is_symlink() or installed.stat().st_uid != 0:
            raise ValueError('Application source is not root-owned regular code: ' + name)
        actual = installed.read_bytes()
        if actual == expected:
            continue
        if name != 'config.py' or hashlib.sha256(actual).hexdigest() != BENCH_CONFIG_SHA256:
            raise ValueError('Application source differs: ' + name)
        updates[name] = expected
    return updates


def validate_profiles(approved, profile_bytes):
    # Parse the exact bytes with the proposed application, without installing
    # it or importing code from the mutable checkout into this root process.
    with tempfile.TemporaryDirectory(prefix='serv-ivars-validate-') as temp:
        root = Path(temp)
        package = root / 'plate_reader'; package.mkdir()
        for name, data in approved.items():
            (package / name).write_bytes(data)
        for name, data in profile_bytes.items():
            (root / (name + '.json')).write_bytes(data)
        script = ('import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); '
                  'import plate_reader.log as log; log._configured = True; '
                  'from plate_reader.config import load; '
                  '[load(Path(sys.argv[1]) / (name + ".json")) for name in ("sandbox", "production")]')
        command(['/usr/bin/python3', '-I', '-c', script, temp])


def command(args):
    p = subprocess.run(args, text=True, capture_output=True, timeout=60)
    if p.returncode:
        raise RuntimeError('Installation command failed: ' + args[0] + '; private output suppressed')
    return p.stdout.strip()


def install(approved_sha, profiles, preferred, lane_empty=False):
    if not lane_empty:
        raise ValueError('This installation restarts recognition; confirm --lane-empty')
    if os.geteuid() != 0 or socket.gethostname().lower() != 'servtest':
        raise ValueError('Only the reviewed servTest Pi may be adopted')
    if not re.fullmatch('[0-9a-f]{40}', approved_sha):
        raise ValueError('Human-approved full 40-character commit required')
    source = Path(__file__).resolve().parents[1]
    git = ['git', '-c', 'safe.directory=' + str(source), '-C', str(source)]
    if command(git + ['rev-parse', 'HEAD']) != approved_sha:
        raise ValueError('Checkout does not equal the approved commit')
    command(git + ['diff', '--no-ext-diff', '--no-textconv', '--exit-code', approved_sha, '--'])
    # Verify the executed installer itself is tracked at the approved object.
    if command(git + ['hash-object', str(Path(__file__).resolve())]) != command(git + ['rev-parse', approved_sha + ':fieldkit/install.py']):
        raise ValueError('Installer differs from approved object')
    if Path('/etc/serv-field/unit.json').exists():
        raise ValueError('Already adopted; use a separately reviewed incremental release')
    ts = json.loads(command(['tailscale', 'status', '--json']))
    if '100.77.200.119' not in ts.get('TailscaleIPs', []):
        raise ValueError('Unexpected Tailscale identity')
    active = json.loads(Path('/home/serv/plate_reader/config.json').read_text())
    if active.get('backend_writes_enabled') is not False or active.get('device_id') != 'serv-unit-bench-reolink':
        raise ValueError('Expected the held, local-only bench identity')
    queue = Path('/home/serv/plate_reader/queue.db')
    if queue.exists():
        with sqlite3.connect('file:' + str(queue) + '?mode=ro', uri=True) as db:
            if db.execute('SELECT count(*) FROM queue').fetchone()[0]:
                raise ValueError('Pre-existing production queue must be reviewed and archived under its old identity first')
    profile_bytes = {}
    for name in ('sandbox', 'production'):
        p = profiles / (name + '.json')
        if not stat.S_ISREG(p.lstat().st_mode) or p.stat().st_mode & 0o077:
            raise ValueError('Profiles must be regular private files')
        profile_bytes[name] = p.read_bytes()
        data = json.loads(profile_bytes[name])
        if name == 'production' and (data.get('device_id') != 'serv-ivars-001' or data.get('location_id') != 'ivars-001' or not data.get('hours')):
            raise ValueError('Production identity and confirmed hours required')
        if name == 'sandbox' and (data.get('backend_writes_enabled') is not False
                or data.get('device_id') != 'serv-unit-bench-reolink'):
            raise ValueError('Sandbox must retain the held bench identity and disabled delivery')
    modules = command(git + ['ls-tree', '--name-only', approved_sha + ':plate_reader']).splitlines()
    approved_app = {name: subprocess.check_output(git + ['show', approved_sha + ':plate_reader/' + name])
                    for name in modules if name.endswith('.py')}
    updates = application_updates(approved_app, Path('/opt/serv-reolink/plate_reader'))
    validate_profiles(approved_app, profile_bytes)
    application_tree = Path('/opt/serv-reolink')
    code_paths = [application_tree, *application_tree.rglob('*')]
    if any(p.is_symlink() or p.stat().st_uid != 0 for p in code_paths):
        raise ValueError('Application tree contains non-root-owned files or symbolic links; review before adoption')
    if Path('/opt/serv-bench-network/serv-cellular-supervisor').read_bytes() != (source / 'scripts/serv-cellular-supervisor').read_bytes():
        raise ValueError('Cellular core differs; refusing an unreviewed ownership migration')
    backup = Path('/var/backups') / ('serv-ivars-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))
    backup.mkdir(mode=0o700)
    for name in ('/home/serv/plate_reader/config.json', '/home/serv/tapo-bench/stream/config.ini',
                 '/opt/serv-bench-network/cellular.py', '/opt/serv-reolink/plate_reader/config.py'):
        target = backup / name.lstrip('/'); target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(name, target)
    # The operator has confirmed an empty lane. Stop before replacing the one
    # reviewed parser; recognition/gates/normalization/hashing are unchanged.
    command(['systemctl', 'stop', 'serv-reolink-app.service'])
    for name, data in updates.items():
        target = Path('/opt/serv-reolink/plate_reader') / name
        fd, temp_name = tempfile.mkstemp(prefix='.ivars-', dir=target.parent)
        with os.fdopen(fd, 'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno()); os.fchmod(f.fileno(), 0o644)
        os.replace(temp_name, target)
    # Verified source files from the original bench tar retained group-write
    # permission bits. Tighten the root-owned tree, including Python caches.
    for p in code_paths:
        p.chmod(0o755 if p.is_dir() else 0o644)
    # All executable assets are copied from the exact immutable Git object,
    # not untracked files which might be present in this checkout.
    root = Path('/opt/serv-field'); root.mkdir(mode=0o755); root.chmod(0o755)
    names = command(git + ['ls-tree', '-r', '--name-only', approved_sha, '--', 'fieldkit']).splitlines()
    for name in names:
        if name.endswith('.py'):
            dest = root / name; dest.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            dest.parent.chmod(0o755)
            data = subprocess.check_output(git + ['show', approved_sha + ':' + name])
            dest.write_bytes(data); dest.chmod(0o644)
    (root / 'fieldkit').chmod(0o755)
    conf = Path('/etc/serv-field'); conf.mkdir(mode=0o755); conf.chmod(0o755)
    (conf / 'profiles').mkdir(mode=0o700)
    for name in ('sandbox', 'production'):
        dest = conf / 'profiles' / (name + '.json'); dest.write_bytes(profile_bytes[name]); dest.chmod(0o600)
    unit = {'device_id': 'serv-ivars-001', 'location_id': 'ivars-001', 'release': approved_sha,
            'machine_fingerprint': hashlib.sha256(Path('/etc/machine-id').read_bytes().strip()).hexdigest(),
            'backup': str(backup)}
    (conf / 'unit.json').write_text(json.dumps(unit, indent=2)); (conf / 'unit.json').chmod(0o644)
    (conf / 'network.json').write_text(json.dumps({'preferred': preferred})); (conf / 'network.json').chmod(0o600)
    launcher = Path('/usr/local/sbin/serv-field')
    launcher.write_text('#!/bin/sh\ncd /opt/serv-field || exit 1\nexec env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin PYTHONPATH=/opt/serv-field:/opt/serv-reolink /usr/bin/python3 -s -m fieldkit.control "$@"\n'); launcher.chmod(0o755)
    shutil.copyfile(root / 'fieldkit/cellular.py', '/opt/serv-bench-network/cellular.py')
    Path('/opt/serv-bench-network/cellular.py').chmod(0o644)
    # Production needs AWS egress. Inbound access remains Tailscale-only.
    drop = Path('/etc/systemd/system/serv-reolink-app.service.d'); drop.mkdir(mode=0o755, exist_ok=True)
    (drop / 'field.conf').write_text('[Service]\nIPAddressDeny=\nIPAddressAllow=\n')
    drop = Path('/etc/systemd/system/serv-tapo-stream.service.d'); drop.mkdir(mode=0o755, exist_ok=True)
    (drop / 'field-hours.conf').write_text('[Unit]\nConditionPathExists=/run/serv-field/capture-allowed\n')
    for name in ('serv-field-hours.service', 'serv-field-hours.timer'):
        dest = Path('/etc/systemd/system') / name
        dest.write_bytes(subprocess.check_output(git + ['show', approved_sha + ':fieldkit/' + name])); dest.chmod(0o644)
    # Remove the temporary raw-text viewer from the PR fan-out.
    command(['systemctl', 'stop', 'serv-live-detections.service']) if host_unit_exists('serv-live-detections.service') else None
    ini = Path('/home/serv/tapo-bench/stream/config.ini')
    text = ini.read_text()
    text = re.sub(r'(?m)^(\s*webhook_targets\s*=).+$', r'\1 serv-local', text)
    ini.write_text(text); ini.chmod(0o600)
    # Retain the security baseline and add only camera DHCP-server ingress.
    # Never execute a mutable script from the serv-owned checkout as root.
    hardening = root / 'installation'
    for name in ('scripts/harden_pi.sh', 'systemd/serv-firewall.nft', 'systemd/serv-firewall.service'):
        dest = hardening / name; dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        dest.write_bytes(subprocess.check_output(git + ['show', approved_sha + ':' + name])); dest.chmod(0o600)
    command(['bash', str(hardening / 'scripts/harden_pi.sh'), '--key-only'])
    firewall = Path('/etc/serv/serv-firewall.nft')
    text = firewall.read_text().replace('        iifname "lo" accept',
        '        iifname "lo" accept\n        iifname "wlan0" udp sport 68 udp dport 67 accept')
    text = 'destroy table inet serv_filter\n' + text
    firewall.write_text(text); firewall.chmod(0o600)
    command(['nft', '--check', '-f', str(firewall)])
    # Replace the table in one nftables transaction; no unfiltered interval.
    command(['nft', '-f', str(firewall)])
    command(['systemctl', 'daemon-reload'])
    command(['systemd-analyze', 'verify', '/etc/systemd/system/serv-field-hours.service', '/etc/systemd/system/serv-field-hours.timer'])
    command(['systemctl', 'enable', '--now', 'serv-field-hours.timer'])
    command(['systemctl', 'start', 'serv-field-hours.service'])
    command(['systemctl', 'restart', 'serv-reolink-app.service'])
    command(['systemctl', 'restart', 'serv-tapo-stream.service'])
    command(['systemctl', 'restart', 'serv-bench-cellular.service'])
    print(json.dumps({'installed': approved_sha, 'mode': 'sandbox', 'backup': str(backup),
                      'production_activated': False, 'next': 'sudo serv-field doctor'}))


def host_unit_exists(unit):
    return subprocess.run(['systemctl', 'cat', unit], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


if __name__ == '__main__':
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--approved-sha', required=True, help='Exact reviewed SHA, explicitly supplied by the human operator')
    p.add_argument('--profiles', type=Path, required=True)
    p.add_argument('--preferred', choices=('cellular', 'wifi'), required=True)
    p.add_argument('--lane-empty', action='store_true')
    a = p.parse_args()
    install(a.approved_sha, a.profiles, a.preferred, a.lane_empty)
