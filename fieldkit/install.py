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
import subprocess
import time


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
    for name in ('sandbox', 'production'):
        p = profiles / (name + '.json')
        if p.is_symlink() or p.stat().st_mode & 0o077:
            raise ValueError('Profiles must be regular private files')
        data = json.loads(p.read_text())
        if name == 'production' and (data.get('device_id') != 'serv-ivars-001' or data.get('location_id') != 'ivars-001' or not data.get('hours')):
            raise ValueError('Production identity and confirmed hours required')
    # Adoption cannot silently update recognition behaviour. A separate app
    # release is needed if any installed Python source differs from this SHA.
    for p in (source / 'plate_reader').glob('*.py'):
        installed = Path('/opt/serv-reolink/plate_reader') / p.name
        if (not installed.exists() or installed.is_symlink() or installed.stat().st_uid != 0
                or installed.stat().st_mode & 0o022 or installed.read_bytes() != p.read_bytes()):
            raise ValueError('Application source differs: ' + p.name)
    if Path('/opt/serv-bench-network/serv-cellular-supervisor').read_bytes() != (source / 'scripts/serv-cellular-supervisor').read_bytes():
        raise ValueError('Cellular core differs; refusing an unreviewed ownership migration')
    backup = Path('/var/backups') / ('serv-ivars-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))
    backup.mkdir(mode=0o700)
    for name in ('/home/serv/plate_reader/config.json', '/home/serv/tapo-bench/stream/config.ini',
                 '/opt/serv-bench-network/cellular.py'):
        target = backup / name.lstrip('/'); target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(name, target)
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
        dest = conf / 'profiles' / (name + '.json'); shutil.copyfile(profiles / dest.name, dest); dest.chmod(0o600)
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
