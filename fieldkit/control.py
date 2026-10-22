"""Root operator controls for one reviewed, commissioned Ivar's unit."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import time

from fieldkit import host
from fieldkit.cellular import guard

ROOT = Path('/etc/serv-field')


def command(args, timeout=45):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError('Command failed: ' + args[0] + ' (private output suppressed)')
    return p.stdout.strip()


def atomic(path, data, uid=0, gid=0):
    temp = path.with_name(path.name + '.new')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.chown(temp, uid, gid)
        os.replace(temp, path)
        dfd = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(dfd)
        finally: os.close(dfd)
    finally:
        temp.unlink(missing_ok=True)


def capture_desired():
    return command(['runuser', '-u', 'serv', '--', 'env', 'PYTHONPATH=/opt/serv-reolink',
        'python3', '-c', 'from plate_reader.config import capture_desired_state; print(capture_desired_state())'])


def reconcile():
    desired = capture_desired()
    if desired not in ('on', 'off', 'unmanaged'):
        raise ValueError('Invalid capture schedule')
    active = host.run(['systemctl', 'is-active', '--quiet', host.STREAM])[0] == 0
    # The IP camera needs no MediaMTX or Pi sensor ownership.
    marker = Path('/run/serv-field/capture-allowed')
    if desired in ('on', 'unmanaged'):
        marker.touch(mode=0o600)
        if not active:
            command(['systemctl', 'start', host.STREAM])
    elif desired == 'off':
        marker.unlink(missing_ok=True)
        if active:
            command(['systemctl', 'stop', host.STREAM])


def production_checks(config):
    state = host.status()
    # main() has verified this root-owned manifest and its physical unit.
    unit = host.read_json(ROOT / 'unit.json')
    evidence = host.read_json(ROOT / 'commissioning.json') if (ROOT / 'commissioning.json').exists() else {}
    checks = {
        'own_identity': config.get('device_id') == 'serv-ivars-001' and config.get('location_id') == 'ivars-001',
        'hours_configured': bool(config.get('hours')),
        'camera_wifi_policy_accepted': state['camera_wifi_secured'] or open_wifi_authorized(unit),
        'camera_local': state['camera_local_route'],
        'tailscale_running': state['tailscale']['running'],
        'clock_synchronized': command(['timedatectl', 'show', '-p', 'NTPSynchronized', '--value']) == 'yes',
        'day_view_measured': evidence.get('day_view_passed') is True,
        'night_view_measured': evidence.get('night_view_passed') is True,
        'lane_counting_measured': evidence.get('lane_counting_passed') is True,
        'evidence_for_this_site': evidence.get('location_id') == 'ivars-001' and evidence.get('device_id') == 'serv-ivars-001',
    }
    import plate_reader.log as logging_module
    logging_module._configured = True
    from plate_reader.config import parse_hours
    hours = parse_hours(config.get('hours'))
    checks['recognizer_ready_for_open_window'] = bool(state['camera']['healthy']) or not hours.capture_should_run()
    # No raw data in this request. Test the exact production credential.
    import requests
    try:
        response = requests.post(config['api_base'] + '/heartbeat', json={}, headers={
            'x-device-id': config['device_id'], 'x-api-key': config['api_key']}, timeout=12)
        checks['aws_authentication'] = 200 <= response.status_code < 300
    except requests.RequestException:
        checks['aws_authentication'] = False
    return checks


def open_wifi_authorized(unit):
    """An explicit per-unit operator choice; never describe open Wi-Fi as secured."""
    return (unit.get('allow_open_camera_wifi') is True
            and unit.get('device_id') == 'serv-ivars-001'
            and unit.get('location_id') == 'ivars-001')


def mode(name, lane_empty):
    if not lane_empty:
        raise ValueError('Changing mode restarts the app. Confirm an empty lane with --lane-empty.')
    target = ROOT / 'profiles' / (name + '.json')
    new = host.private_json(target)
    # Validate with the actual installed parser before stopping anything.
    sys.path.insert(0, '/opt/serv-reolink')
    import plate_reader.log as logging_module
    # The process is root but must not rotate the application's log as root.
    logging_module._configured = True
    from plate_reader.config import load
    load(target)
    if name == 'production':
        checks = production_checks(new)
        if not all(checks.values()):
            print(json.dumps({'production_checks': checks}, indent=2))
            raise ValueError('Production prerequisites are incomplete; configuration unchanged')
    previous = host.CONFIG.read_bytes()
    current = json.loads(previous)
    if current == new:
        reconcile(); print('Mode already selected.'); return
    account = pwd.getpwnam('serv')
    command(['systemctl', 'stop', host.APP])
    try:
        atomic(host.CONFIG, json.dumps(new, indent=2).encode() + b'\n', account.pw_uid, account.pw_gid)
        command(['systemctl', 'start', host.APP])
        reconcile()
        command(['systemctl', 'is-active', '--quiet', host.APP])
    except Exception:
        atomic(host.CONFIG, previous, account.pw_uid, account.pw_gid)
        command(['systemctl', 'start', host.APP]); reconcile()
        raise
    print(json.dumps({'mode': name, 'device': new['device_id'], 'location': new['location_id'],
                      'bench_records_replayed': False}))


def network(preference):
    # Never switch wlan0 away from its camera AP.
    if preference == 'wifi':
        interfaces = {r.get('dev') for r in host.routes() if r.get('dst') == 'default'}
        candidates = sorted(i for i in interfaces if i and i != 'wlan0' and (Path('/sys/class/net') / i / 'wireless').exists())
        if not candidates:
            raise ValueError('No Wi-Fi internet adapter is connected. wlan0 belongs to the camera; attach/configure a second Wi-Fi adapter first.')
        interface = candidates[0]
    else:
        interface = 'wwan0'
    code = command(['curl', '--interface', interface, '--max-time', '12', '-sS', '-o', '/dev/null',
                    '-w', '%{http_code}', 'https://example.com/'], timeout=15)
    if code != '200':
        raise ValueError('Requested uplink failed its bound HTTPS test; policy unchanged')
    previous = (ROOT / 'network.json').read_bytes()
    atomic(ROOT / 'network.json', json.dumps({'preferred': preference}).encode())
    try:
        command(['systemctl', 'restart', 'serv-bench-cellular.service'])
    except Exception:
        atomic(ROOT / 'network.json', previous)
        command(['systemctl', 'restart', 'serv-bench-cellular.service'])
        raise
    print(json.dumps({'preferred': preference, 'fallback_retained': True,
                      'preview_cellular_consent_still_required': True,
                      'note': 'Preference selects an uplink; Tailscale can use fallback routes. Run status after reconnection.'}))


def main():
    os.umask(0o077); guard()
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    sub.add_parser('doctor'); sub.add_parser('reconcile')
    m = sub.add_parser('mode'); m.add_argument('mode', choices=('sandbox', 'production')); m.add_argument('--lane-empty', action='store_true')
    n = sub.add_parser('network'); n.add_argument('preference', choices=('cellular', 'wifi'))
    a = p.parse_args()
    Path('/run/serv-field').mkdir(mode=0o700, exist_ok=True)
    with open('/run/serv-field/control.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if a.action == 'reconcile': reconcile()
        elif a.action == 'mode': mode(a.mode, a.lane_empty)
        elif a.action == 'network': network(a.preference)
        else:
            profile = ROOT / 'profiles/production.json'
            checks = production_checks(host.private_json(profile)) if profile.exists() else {'production_profile_prepared': False}
            print(json.dumps({'status': host.status(), 'production_checks': checks,
                              'ready_to_activate': all(checks.values())}, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as e:
        print(str(e) if isinstance(e, (ValueError, RuntimeError)) else type(e).__name__, file=sys.stderr)
        sys.exit(1)
