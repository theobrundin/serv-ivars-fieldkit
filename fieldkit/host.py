"""Read-only status, aggregate scanning evidence, and bounded SSH preview.

No raw OCR, API keys, authenticated URLs or images enter status / metrics.
Preview is an explicit operator action; its MPEG-TS bytes go only to stdout.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote

CONFIG = Path('/home/serv/plate_reader/config.json')
UNIT = Path('/etc/serv-field/unit.json')
CAMERA = '10.42.0.36'
APP = 'serv-reolink-app.service'
STREAM = 'serv-tapo-stream.service'


def run(args, timeout=5):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip()


def read_json(path):
    return json.loads(Path(path).read_text())


def private_json(path):
    p = Path(path); s = p.lstat()
    if p.is_symlink() or not p.is_file() or s.st_mode & 0o077:
        raise ValueError('Private file permissions required')
    return read_json(p)


def routes():
    return json.loads(run(['ip', '-j', '-4', 'route', 'show', 'table', 'all'])[1])


def metered_possible(route_list):
    # Tailscale can roam onto a cellular fallback. A preferred Wi-Fi default
    # alone is NOT proof that the overlay will never use cellular.
    return any(str(r.get('dev', '')).startswith('wwan') for r in route_list)


def preview_permitted(route_list, allow_cellular):
    if metered_possible(route_list) and not allow_cellular:
        raise ValueError('Cellular is available to Tailscale. Preview requires explicit --allow-cellular, or a verified Wi-Fi-only session with cellular routes withdrawn.')


def camera_status():
    try:
        try:
            response = urllib.request.urlopen('http://127.0.0.1:8001/status/', timeout=4)
        except urllib.error.HTTPError as e:
            response = e
        with response:
            data = json.load(response)
        c = data.get('cameras', {}).get('tapo-bench', {})
        out = {k: c.get(k) for k in ('status', 'health', 'received_fps', 'buffered_frames', 'last_checked')}
        age = time.time() - datetime.fromisoformat(c['last_checked']).timestamp()
        out['fresh'] = -5 <= age <= 30
        out['healthy'] = out['fresh'] and c.get('status') == 'running' and c.get('health', 0) > 0 and c.get('received_fps', 0) > 0
        out['version'] = data.get('version')
        return out
    except (OSError, ValueError, KeyError, TypeError):
        return {'healthy': False, 'fresh': False}


def queue_counts(path):
    if not path.exists():
        return {'pending': 0, 'dead_letter': 0}
    with sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True, timeout=2) as db:
        return {'pending': db.execute('SELECT count(*) FROM queue').fetchone()[0],
                'dead_letter': db.execute('SELECT count(*) FROM dead_letter').fetchone()[0],
                'by_endpoint': dict(db.execute('SELECT endpoint,count(*) FROM queue GROUP BY endpoint'))}


def status():
    cfg = private_json(CONFIG)
    rr = routes()
    _, ts = run(['tailscale', 'status', '--json'])
    ts = json.loads(ts or '{}')
    _, route = run(['ip', '-j', '-4', 'route', 'get', '1.1.1.1'])
    effective = json.loads(route or '[{}]')[0].get('dev')
    security = run(['nmcli', '-g', '802-11-wireless-security.key-mgmt', 'connection', 'show', 'serv-camera'])[1]
    _, camera_route = run(['ip', '-j', '-4', 'route', 'get', CAMERA])
    cr = json.loads(camera_route or '[{}]')[0]
    cfg_mode = 'production' if cfg.get('backend_writes_enabled', True) else 'sandbox'
    unit = read_json(UNIT) if UNIT.exists() else {}
    out = {'at': datetime.now(timezone.utc).isoformat(), 'host': socket.gethostname(),
        'mode': cfg_mode, 'installed_release': unit.get('release'),
        'device': cfg.get('device_id'), 'location': cfg.get('location_id'), 'hours': cfg.get('hours'),
        'internet_interface': effective, 'cellular_possible_for_preview': metered_possible(rr),
        'tailscale': {'running': ts.get('BackendState') == 'Running',
                      'addresses': ts.get('TailscaleIPs', []), 'key_expiry': ts.get('Self', {}).get('KeyExpiry')},
        'camera_local_route': cr.get('dev') == 'wlan0' and not cr.get('gateway'),
        'camera_wifi_secured': security in ('wpa-psk', 'sae'), 'camera': camera_status(),
        'services': {s: run(['systemctl', 'is-active', s])[1] for s in
                     (APP, STREAM, 'serv-bench-cellular.service', 'serv-camera-isolation.service')},
        'queue': queue_counts(CONFIG.parent / ('queue.db' if cfg_mode == 'production' else 'queue.dry-run.db'))}
    out['temperature_c'] = int(Path('/sys/class/thermal/thermal_zone0/temp').read_text()) / 1000
    out['throttled'] = run(['vcgencmd', 'get_throttled'])[1]
    return out


def metrics(minutes):
    """Audit outcomes are evidence, not a delivered/unique-customer counter."""
    cutoff = time.time() - minutes * 60
    counts = {}; decisions = {}; newest = None; cases = 0
    for p in (CONFIG.parent / 'audit_cases').glob('*.json'):
        if p.stat().st_mtime < cutoff:
            continue
        try:
            data = read_json(p)
        except (OSError, ValueError):
            continue
        # Only fixed outcome labels; never print arbitrary provider fields.
        outcome = data.get('outcome')
        if outcome not in ('confirmed', 'insufficient_confidence', 'no_visible_plate',
                           'drive_off', 'non_drive_through', 'route_unresolved'):
            outcome = 'other'
        counts[outcome] = counts.get(outcome, 0) + 1; cases += 1
        if data.get('trafficDecision') == 'visit_queued':
            decisions['visit_queued'] = decisions.get('visit_queued', 0) + 1
        newest = max(newest or 0, p.stat().st_mtime)
    return {'window_minutes': minutes, 'recently_written_audit_cases': cases,
            'outcomes': counts, 'decisions': decisions, 'latest_case_written_at': newest,
            'limitation': 'Bounded local audit files; not unique vehicles, delivered visits, or a measured recognition rate.'}


def preview(seconds, allow_cellular):
    if not 1 <= seconds <= 300:
        raise ValueError('Preview duration must be 1–300 seconds')
    peer = os.environ.get('SSH_CONNECTION', '').split()
    if not peer or ipaddress.ip_address(peer[0]) not in ipaddress.ip_network('100.64.0.0/10'):
        raise ValueError('Preview requires authenticated SSH over Tailscale')
    preview_permitted(routes(), allow_cellular)
    cr = json.loads(run(['ip', '-j', '-4', 'route', 'get', CAMERA])[1])[0]
    if cr.get('dev') != 'wlan0' or cr.get('gateway'):
        raise ValueError('Camera route is not the isolated local Wi-Fi link')
    # One preview per unit. PR remains the production recognizer throughout.
    lock = open(CONFIG.parent / '.field-preview.lock', 'a')
    os.chmod(lock.name, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    c = private_json('/home/serv/tapo-bench/camera-credentials.json')
    url = 'rtsp://{}:{}@{}:554/stream1'.format(quote(c['username'], safe=''), quote(c['password'], safe=''), CAMERA)
    proc = subprocess.Popen(['ffmpeg', '-nostdin', '-v', 'error', '-rtsp_transport', 'tcp',
        '-timeout', '5000000', '-i', url, '-map', '0:v:0', '-an', '-c:v', 'copy',
        '-t', str(seconds), '-mpegts_flags', 'resend_headers', '-flush_packets', '1',
        '-muxdelay', '0', '-f', 'mpegts', 'pipe:1'], stdout=sys.stdout.buffer,
        stderr=subprocess.DEVNULL)
    def stop(*_):
        raise KeyboardInterrupt
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, stop)
    try:
        deadline = time.monotonic() + seconds + 10
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                break
            # Re-check fallback eligibility during a Wi-Fi-only preview.
            preview_permitted(routes(), allow_cellular)
            time.sleep(1)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
        lock.close()


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    sub.add_parser('status')
    m = sub.add_parser('metrics'); m.add_argument('--minutes', type=int, default=15)
    v = sub.add_parser('view'); v.add_argument('--seconds', type=int, default=120)
    v.add_argument('--allow-cellular', action='store_true')
    a = p.parse_args()
    if a.action == 'view':
        preview(a.seconds, a.allow_cellular)
    elif a.action == 'metrics':
        if not 1 <= a.minutes <= 1440:
            p.error('minutes must be 1–1440')
        print(json.dumps(metrics(a.minutes), indent=2))
    else:
        print(json.dumps(status(), indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print('Field command failed: ' + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__), file=sys.stderr)
        sys.exit(1)
