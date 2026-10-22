"""Human-approved incremental operator update for the already adopted Ivar's Pi.

Records the user's explicit open-camera-Wi-Fi preference. No recognition
restart, production activation, Wi-Fi reconfiguration or cellular reset.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time

BASE = '768983ae4177ddc0bcc555c74f215655893ecd6d'


def run(args):
    p = subprocess.run(args, text=True, capture_output=True, timeout=30)
    if p.returncode:
        raise RuntimeError('Operator update preflight failed; private command output suppressed')
    return p.stdout.strip()


def atomic(path, data, mode):
    fd, name = tempfile.mkstemp(prefix='.operator-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno()); os.fchmod(f.fileno(), mode)
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def update(approved_sha, allow_open_camera_wifi):
    if not allow_open_camera_wifi:
        raise ValueError('Explicit --allow-open-camera-wifi choice required')
    if os.geteuid() != 0 or socket.gethostname().lower() != 'servtest':
        raise ValueError('This update is restricted to the adopted servTest Pi')
    if not re.fullmatch('[0-9a-f]{40}', approved_sha):
        raise ValueError('Human-approved full commit required')
    source = Path(__file__).resolve().parents[1]
    git = ['git', '-c', 'safe.directory=' + str(source), '-C', str(source)]
    if run(git + ['rev-parse', 'HEAD']) != approved_sha:
        raise ValueError('Checkout does not equal the approved commit')
    run(git + ['diff', '--no-ext-diff', '--no-textconv', '--exit-code', approved_sha, '--'])
    if run(git + ['hash-object', str(Path(__file__).resolve())]) != run(git + ['rev-parse', approved_sha + ':fieldkit/update_operator.py']):
        raise ValueError('Updater differs from the approved object')
    manifest = Path('/etc/serv-field/unit.json')
    if manifest.is_symlink() or manifest.stat().st_uid != 0 or manifest.stat().st_mode & 0o022:
        raise ValueError('Unsafe unit manifest')
    previous = manifest.read_bytes(); unit = json.loads(previous)
    if (unit.get('release') != BASE or unit.get('device_id') != 'serv-ivars-001'
            or unit.get('location_id') != 'ivars-001'
            or unit.get('machine_fingerprint') != hashlib.sha256(Path('/etc/machine-id').read_bytes().strip()).hexdigest()):
        raise ValueError('Unexpected installed release or physical unit')
    if '100.77.200.119' not in json.loads(run(['tailscale', 'status', '--json'])).get('TailscaleIPs', []):
        raise ValueError('Unexpected Tailscale identity')
    active = json.loads(Path('/home/serv/plate_reader/config.json').read_text())
    if active.get('backend_writes_enabled') is not False:
        raise ValueError('This operator update is restricted to the commissioning sandbox')
    root = Path('/opt/serv-field')
    for tree in (root, Path('/opt/serv-reolink')):
        if any(p.is_symlink() or p.stat().st_uid != 0 or p.stat().st_mode & 0o022
               for p in [tree, *tree.rglob('*')]):
            raise ValueError('Unsafe installed code ownership or permissions')
    # The approved update may not change the recognition application at all.
    run(git + ['diff', '--no-ext-diff', '--no-textconv', '--exit-code', BASE, approved_sha, '--', 'plate_reader'])
    for folder, destination in [('plate_reader', Path('/opt/serv-reolink')), ('fieldkit', root)]:
        for name in run(git + ['ls-tree', '-r', '--name-only', BASE, '--', folder]).splitlines():
            if name.endswith('.py'):
                expected = subprocess.check_output(git + ['show', BASE + ':' + name])
                if (destination / name).read_bytes() != expected:
                    raise ValueError('Installed source has drifted: ' + name)
    files = {}
    for name in run(git + ['ls-tree', '-r', '--name-only', approved_sha, '--', 'fieldkit']).splitlines():
        if name.endswith('.py'):
            data = subprocess.check_output(git + ['show', approved_sha + ':' + name])
            compile(data, name, 'exec')
            files[name] = data
    # Wait only for this short root-control critical section; no service stops.
    with Path('/run/serv-field/control.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        backup = Path('/var/backups') / ('serv-ivars-operator-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))
        backup.mkdir(mode=0o700)
        shutil.copytree(root / 'fieldkit', backup / 'fieldkit')
        (backup / 'unit.json').write_bytes(previous)
        try:
            for name, data in files.items():
                dest = root / name; dest.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                dest.parent.chmod(0o755)
                atomic(dest, data, 0o644)
            unit.update(release=approved_sha, allow_open_camera_wifi=True,
                        open_camera_wifi_authorized_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                        operator_update_backup=str(backup))
            atomic(manifest, json.dumps(unit, indent=2).encode() + b'\n', 0o644)
        except Exception:
            for name in files:
                saved = backup / name
                if saved.is_file(): atomic(root / name, saved.read_bytes(), 0o644)
                else: (root / name).unlink(missing_ok=True)
            atomic(manifest, previous, 0o644)
            raise
    print(json.dumps({'installed': approved_sha, 'open_camera_wifi_authorized': True,
                      'camera_wifi_changed': False, 'recognition_restarted': False,
                      'production_activated': False, 'backup': str(backup)}))


if __name__ == '__main__':
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--approved-sha', required=True)
    p.add_argument('--allow-open-camera-wifi', action='store_true')
    a = p.parse_args()
    update(a.approved_sha, a.allow_open_camera_wifi)
