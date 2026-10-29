#!/usr/bin/env python3
"""Manual exact-release updater for Ivar's 001 only; never a fleet timer.

A human installs this helper and runs --approve. Staging or pushing a commit
never creates approval. No cellular, camera, identity or queue migration.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import time
import urllib.request

BASE = 'c15ae99253f8a26810ca44c9ea9b09d96e9a4d54'
CHANNEL = 'codex/ivars-001-fieldkit'
ROOT = Path('/var/lib/serv-ivars-updates')
REPO = ROOT / 'repository.git'
APP = Path('/opt/serv-reolink/plate_reader')
CONFIG = Path('/home/serv/plate_reader/config.json')
PROFILE = Path('/etc/serv-field/profiles/production.json')
UNIT = Path('/etc/serv-field/unit.json')
CHANNEL_FILE = CONFIG.parent / '.update_channel'
SHA_FILE = CONFIG.parent / '.deployed_sha'
APPROVAL = ROOT / 'approval.json'
STATE = ROOT / 'last-update.json'
SERVICE = 'serv-reolink-app.service'
SCOPE = {'plate_reader/config.py', 'plate_reader/stream_engine.py',
         'plate_reader/ivars_route.py', 'plate_reader/route_audit.py'}


def run(args, timeout=45):
    result = subprocess.run(args, capture_output=True, timeout=timeout,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'})
    if result.returncode:
        raise ValueError('Command failed (private output suppressed): ' + args[0])
    return result.stdout


def git(*args):
    return run(['git', '-c', 'core.hooksPath=/dev/null', '--git-dir=' + str(REPO), *args])


def digest(data):
    return hashlib.sha256(data).hexdigest()


def trusted(path, directory=False):
    s = path.lstat()
    if (s.st_uid != 0 or s.st_mode & 0o022 or
            not (stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode))):
        raise ValueError('Expected root-owned, non-writable regular object: ' + str(path))


def atomic(path, data, mode=0o600, uid=0, gid=0):
    fd, name = tempfile.mkstemp(prefix='.route-update-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data); out.flush(); os.fsync(out.fileno())
            os.fchmod(out.fileno(), mode); os.fchown(out.fileno(), uid, gid)
        os.replace(name, path)
        d = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(d)
        finally: os.close(d)
    finally:
        Path(name).unlink(missing_ok=True)


def encode(value):
    return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()


def patch_config(blob, mode):
    if mode not in ('active', 'shadow'):
        raise ValueError('Choose active or shadow')
    cfg = json.loads(blob)
    if (cfg.get('device_id') != 'serv-ivars-001' or cfg.get('location_id') != 'ivars-001'
            or cfg.get('capture', {}).get('width') != 2304
            or cfg.get('capture', {}).get('height') != 1296
            or cfg.get('backend_writes_enabled', True) is not True):
        raise ValueError('Expected the existing Ivar\'s production profile')
    presence = cfg['presence']
    presence['board_zone'] = dict(x_min_pct=.05, y_min_pct=.65, x_max_pct=.39, y_max_pct=1.0)
    presence['route_qualification'].update(
        enabled=True, policy='ivars_window_v1', shadow_only=mode == 'shadow',
        profile_reconciliation_enabled=True, terminal_exit_zone=None,
        entry_zone=dict(x_min_pct=.40, y_min_pct=.55, x_max_pct=.95, y_max_pct=.74))
    return encode(cfg)


def identity():
    if os.geteuid() != 0 or socket.gethostname().lower() != 'servtest':
        raise ValueError('Only root on the adopted Ivar\'s Pi may run this helper')
    trusted(ROOT, True); trusted(REPO, True); trusted(UNIT)
    unit = json.loads(UNIT.read_bytes())
    if (unit.get('device_id') != 'serv-ivars-001' or unit.get('location_id') != 'ivars-001'
            or unit.get('machine_fingerprint') != digest(Path('/etc/machine-id').read_bytes().strip())):
        raise ValueError('Physical unit identity mismatch')
    return unit


def modules(sha):
    result = {}
    for line in git('ls-tree', '-r', sha, '--', 'plate_reader').decode().splitlines():
        metadata, name = line.split('\t')
        if metadata.split()[0] != '100644' or not re.fullmatch(r'plate_reader/[A-Za-z0-9_]+\.py', name):
            raise ValueError('Unexpected application tree member')
        data = git('show', sha + ':' + name)
        compile(data, name, 'exec')
        result[Path(name).name] = data
    if not result:
        raise ValueError('Empty application package')
    return result


def release(sha, require_channel=True):
    if not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError('Full 40-character commit required')
    if git('rev-parse', 'refs/remotes/origin/' + CHANNEL).decode().strip() != sha:
        raise ValueError('Release must equal the locally fetched channel head')
    if require_channel:
        trusted(CHANNEL_FILE)
        if CHANNEL_FILE.read_text().strip() != CHANNEL:
            raise ValueError('Unexpected update channel')
    changed = set(git('diff', '--name-only', BASE, sha, '--', 'plate_reader').decode().splitlines())
    if changed != SCOPE:
        raise ValueError('This one-time updater only permits the reviewed four route modules')
    return modules(sha)


def baseline():
    expected = modules(BASE)
    actual = {}
    trusted(APP, True)
    for p in APP.rglob('*'):
        trusted(p, p.is_dir())
    for p in APP.glob('*.py'):
        trusted(p)
        if p.name.startswith('._') and p.read_bytes()[:8] == bytes.fromhex('0005160700020000'):
            continue
        actual[p.name] = p.read_bytes()
    if actual != expected:
        raise ValueError('Installed application differs from the verified baseline')
    a, b = CONFIG.read_bytes(), PROFILE.read_bytes()
    for p in (CONFIG, PROFILE):
        if p.is_symlink() or not stat.S_ISREG(p.lstat().st_mode) or p.stat().st_mode & 0o077:
            raise ValueError('Private regular profiles required')
    trusted(PROFILE)
    if a != b:
        raise ValueError('Active and saved production profiles differ; review drift first')
    return {'config': digest(a), 'profile': digest(b), 'unit': digest(UNIT.read_bytes())}


def validate_config(package, blob):
    # Test proposed parser as the application user, never as root. Secrets stay
    # in private temporary files on the Pi and never enter command output.
    with tempfile.TemporaryDirectory(prefix='ivars-route-check-') as temp:
        folder = Path(temp); folder.chmod(0o755)
        target = folder / 'plate_reader'; target.mkdir(mode=0o755); target.chmod(0o755)
        for name, data in package.items():
            (target / name).write_bytes(data); (target / name).chmod(0o644)
        c = folder / 'config.json'; c.write_bytes(blob); c.chmod(0o600)
        user = pwd.getpwnam('serv'); os.chown(c, user.pw_uid, user.pw_gid)
        script = ('import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
                  'import plate_reader.log as log; log._configured=True; '
                  'from plate_reader.config import load; load(Path(sys.argv[1])/"config.json")')
        run(['runuser', '-u', 'serv', '--', '/usr/bin/python3', '-I', '-B', '-c', script, temp])


def install(sha):
    identity(); release(sha, require_channel=False); baseline()
    source = git('show', sha + ':fieldkit/update_routes.py')
    if Path(__file__).read_bytes() != source:
        raise ValueError('Bootstrap helper differs from the exact Git object')
    launcher = Path('/usr/local/sbin/serv-update')
    if launcher.exists() or launcher.is_symlink():
        raise ValueError('Refusing to overwrite an existing updater')
    helper = Path('/usr/local/libexec/serv-ivars-update.py')
    helper.parent.mkdir(mode=0o755, exist_ok=True)
    atomic(helper, source, 0o644)
    atomic(CHANNEL_FILE, (CHANNEL + '\n').encode(), 0o644)
    atomic(launcher, b'#!/bin/sh\nexec /usr/bin/python3 -I /usr/local/libexec/serv-ivars-update.py "$@"\n', 0o755)
    print('Ivar\'s updater installed. No approval created; application unchanged.')


def approve(sha, mode):
    identity(); package = release(sha); before = baseline()
    validate_config(package, patch_config(CONFIG.read_bytes(), mode))
    atomic(APPROVAL, encode({'sha': sha, 'mode': mode, 'before': before,
                            'approved_at': datetime.now(timezone.utc).isoformat()}))
    print('Approved exact Ivar\'s route release ' + sha + ' in ' + mode + ' mode. Not applied.')


def healthy():
    try:
        run(['systemctl', 'is-active', '--quiet', SERVICE])
        with urllib.request.urlopen('http://127.0.0.1:8129/', timeout=3) as response:
            return json.load(response) == {'ok': True, 'engine': 'pr_stream'}
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def snapshot(backup):
    backup.mkdir(mode=0o700)
    shutil.copytree(APP, backup / 'plate_reader', symlinks=False)
    saved = {}
    for label, p in [('config', CONFIG), ('production', PROFILE), ('unit', UNIT), ('sha', SHA_FILE)]:
        if p.is_symlink(): raise ValueError('Unexpected symlink in deployment state')
        if p.exists():
            s = p.stat(); (backup / label).write_bytes(p.read_bytes())
            saved[label] = {'path': str(p), 'uid': s.st_uid, 'gid': s.st_gid, 'mode': s.st_mode & 0o777}
        else:
            saved[label] = {'path': str(p), 'absent': True}
    (backup / 'files.json').write_bytes(encode(saved))


def restore(backup):
    # App must be stopped by caller. Restore only code/config metadata; never
    # rewind the queue or duplicate cloud traffic already emitted.
    previous = APP.with_name('.plate_reader.failed-' + str(time.time_ns()))
    if APP.exists(): os.replace(APP, previous)
    shutil.copytree(backup / 'plate_reader', APP)
    for label, item in json.loads((backup / 'files.json').read_bytes()).items():
        p = Path(item['path'])
        if item.get('absent'): p.unlink(missing_ok=True)
        else: atomic(p, (backup / label).read_bytes(), item['mode'], item['uid'], item['gid'])
    if previous.exists(): shutil.rmtree(previous)


def apply(lane_empty):
    if not lane_empty:
        raise ValueError('Restart loses in-memory tracks: verify an empty lane and use --lane-empty')
    identity(); trusted(APPROVAL)
    approval = json.loads(APPROVAL.read_bytes()); sha = approval['sha']; mode = approval['mode']
    package = release(sha)
    if baseline() != approval['before']:
        raise ValueError('Application/configuration changed since human approval')
    blob = patch_config(CONFIG.read_bytes(), mode); validate_config(package, blob)
    run(['systemctl', 'is-active', '--quiet', SERVICE])
    backup = ROOT / ('backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    snapshot(backup)
    staging = APP.with_name('.plate_reader.route-' + sha)
    staging.mkdir(mode=0o755); staging.chmod(0o755)
    for name, data in package.items():
        (staging / name).write_bytes(data); (staging / name).chmod(0o644)
    previous = APP.with_name('.plate_reader.pre-route-' + sha)
    if previous.exists(): raise ValueError('Previous update directory exists')
    state = {'sha': sha, 'mode': mode, 'backup': str(backup), 'status': 'prepared',
             'started_at': datetime.now(timezone.utc).isoformat()}
    atomic(STATE, encode(state))
    # Same lock as field mode/hours reconciliation; do not race profile swaps.
    run(['systemctl', 'stop', SERVICE])
    try:
        if baseline() != approval['before']:
            raise ValueError('Configuration drift during shutdown')
        os.replace(APP, previous); os.replace(staging, APP)
        for label, p in [('config', CONFIG), ('production', PROFILE)]:
            s = p.stat(); atomic(p, blob, s.st_mode & 0o777, s.st_uid, s.st_gid)
        unit = json.loads(UNIT.read_bytes())
        unit.update(release=sha, previous_release=unit.get('release'),
                    route_mode=mode, route_update_backup=str(backup), route_activation_at=state['started_at'])
        atomic(UNIT, encode(unit), 0o644)
        atomic(SHA_FILE, (sha + '\n').encode(), 0o644)
        run(['systemctl', 'start', SERVICE])
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if healthy(): break
            time.sleep(2)
        else: raise ValueError('Application listener health check failed')
        state['status'] = 'applied'; state['completed_at'] = datetime.now(timezone.utc).isoformat()
        atomic(STATE, encode(state)); APPROVAL.unlink()
        shutil.rmtree(previous)
    except Exception:
        run(['systemctl', 'stop', SERVICE]); restore(backup); run(['systemctl', 'start', SERVICE])
        state['status'] = 'rolled_back_after_failure'; atomic(STATE, encode(state))
        APPROVAL.unlink(missing_ok=True)
        raise
    print(json.dumps(state))


def rollback(lane_empty):
    if not lane_empty: raise ValueError('Rollback requires an empty lane')
    identity(); trusted(STATE)
    state = json.loads(STATE.read_bytes()); backup = Path(state['backup'])
    if backup.parent != ROOT or not backup.name.startswith('backup-'):
        raise ValueError('Invalid backup path')
    trusted(backup, True)
    if state['status'] not in ('applied', 'prepared'):
        raise ValueError('No applied or interrupted route update to roll back')
    run(['systemctl', 'stop', SERVICE]); restore(backup); run(['systemctl', 'start', SERVICE])
    state['status'] = 'rolled_back'; atomic(STATE, encode(state)); APPROVAL.unlink(missing_ok=True)
    print('Prior code and profiles restored. Queue untouched. Verify camera, app and AWS health.')


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    group = p.add_mutually_exclusive_group()
    group.add_argument('--install', metavar='SHA')
    group.add_argument('--approve', metavar='SHA')
    group.add_argument('--check', metavar='SHA')
    group.add_argument('--rollback', action='store_true')
    p.add_argument('--mode', choices=['active', 'shadow'])
    p.add_argument('--lane-empty', action='store_true')
    a = p.parse_args()
    identity()
    with (ROOT / 'update.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with Path('/run/serv-field/control.lock').open('a') as control:
            fcntl.flock(control, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if a.install: install(a.install)
            elif a.approve:
                if not a.mode: p.error('--approve requires --mode active or shadow')
                approve(a.approve, a.mode)
            elif a.check:
                package = release(a.check, require_channel=False); baseline()
                for mode in ('active', 'shadow'): validate_config(package, patch_config(CONFIG.read_bytes(), mode))
                print('Exact release, baseline and both proposed profiles verified; nothing applied.')
            elif a.rollback: rollback(a.lane_empty)
            else: apply(a.lane_empty)


if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # Configuration and subprocess exception details can contain credentials.
        print('Update refused or failed; inspect private state. Category: ' + type(error).__name__)
        raise SystemExit(1)
