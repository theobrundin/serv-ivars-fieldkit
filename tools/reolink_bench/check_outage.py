#!/usr/bin/env python3
"""Bounded home-bench fault injection with an independent rollback timer.

Camera mode stalls RTSP packets; internet mode blocks WAN while retaining the
verified Wi-Fi management subnet. Neither is a physical power-cycle test.
"""
import argparse
from datetime import datetime, timezone
import json
import ipaddress
import os
from pathlib import Path
import socket
import subprocess
import time
from status import snapshot

parser = argparse.ArgumentParser()
parser.add_argument('mode', choices=('camera', 'internet'))
args = parser.parse_args()
if socket.gethostname().lower() != 'servtest' or os.getuid() != 0:
    raise SystemExit('Administrator on home bench host servTest required')
cfg = json.loads(Path('/home/serv/plate_reader/config.json').read_text())
if cfg.get('backend_writes_enabled') is not False:
    raise SystemExit('Fault injection requires bench delivery disabled')
if subprocess.run(['nft', 'list', 'table', 'inet', 'serv_reolink_fault'],
                  capture_output=True).returncode == 0:
    raise SystemExit('Existing fault table; resolve it before another test')
baseline = snapshot()
if baseline.get('source') != 'live-camera' or baseline.get('camera', {}).get('status') != 'running':
    raise SystemExit('Live camera must be running before this test')
if args.mode == 'camera':
    rule = 'ip daddr 192.168.50.2 tcp dport 554 drop'
else:
    # SSH/mDNS may use link-local IPv6 even when management has an IPv4
    # address. Preserve connected IPv6 LAN prefixes as well as IPv4 LAN.
    allowed_v6 = {'fe80::/10', 'ff00::/8'}
    routes = json.loads(subprocess.check_output(['ip', '-j', '-6', 'route', 'show', 'dev', 'wlan0']))
    for route in routes:
        if route.get('dst') != 'default' and not route.get('gateway'):
            allowed_v6.add(str(ipaddress.IPv6Network(route['dst'], strict=False)))
    lan = ', '.join(sorted(allowed_v6))
    rule = ('oifname "wlan0" ip daddr != 192.168.39.0/24 drop; '
            f'oifname "wlan0" ip6 daddr != {{ {lan} }} drop')
rules = ('table inet serv_reolink_fault {\n chain output {\n '
         'type filter hook output priority -20; policy accept;\n ' + rule + ';\n }\n}\n')
subprocess.run(['nft', '--check', '-f', '-'], input=rules, text=True, check=True)
timer = 'serv-reolink-restore-' + str(int(time.time()))
subprocess.run(['systemd-run', '--unit', timer, '--on-active=120',
                '/usr/sbin/nft', 'delete', 'table', 'inet', 'serv_reolink_fault'],
               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
report = {'kind': args.mode, 'started_at': datetime.now(timezone.utc).isoformat(),
          'baseline': baseline, 'samples': [], 'physical_test': False}
applied = False
blocked_at = time.monotonic()
try:
    subprocess.run(['nft', '-f', '-'], input=rules, text=True, check=True)
    applied = True
    if args.mode == 'internet':
        try:
            with socket.create_connection(('1.1.1.1', 443), timeout=3):
                report['wan_connection_blocked'] = False
        except OSError:
            report['wan_connection_blocked'] = True
    until = blocked_at + (55 if args.mode == 'camera' else 35)
    while time.monotonic() < until:
        report['samples'].append({'phase': 'blocked', **snapshot()})
        time.sleep(5)
finally:
    if applied:
        subprocess.run(['nft', 'delete', 'table', 'inet', 'serv_reolink_fault'], check=True)
    subprocess.run(['systemctl', 'stop', timer + '.timer'], check=True)
restored = time.monotonic()
report['blocked_seconds'] = round(restored - blocked_at, 2)
report['restored_at'] = datetime.now(timezone.utc).isoformat()
healthy_updates = []
while time.monotonic() - restored < 100:
    value = snapshot()
    report['samples'].append({'phase': 'restored', **value})
    cam = value.get('camera', {})
    checked = cam.get('last_checked')
    try:
        fresh = datetime.fromisoformat(checked) > datetime.fromisoformat(report['restored_at'])
    except (TypeError, ValueError):
        fresh = False
    if (cam.get('status') == 'running' and (cam.get('received_fps') or 0) >= 15
            and (cam.get('health') or 0) >= 50 and fresh):
        if not healthy_updates or healthy_updates[-1][0] != checked:
            healthy_updates.append((checked, time.monotonic() - restored))
        if len(healthy_updates) >= 3:
            report['reported_recovery_seconds'] = round(healthy_updates[0][1], 2)
            report['stable_recovery_verified_seconds'] = round(time.monotonic() - restored, 2)
            break
    else:
        healthy_updates.clear()
    time.sleep(5)
report['final'] = snapshot()
os.umask(0o077)
path = Path('/home/serv/reolink-bench') / (args.mode + '-outage.json')
path.write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({'test': args.mode, 'physical_test': False,
                  'reported_recovery_seconds': report.get('reported_recovery_seconds'),
                  'before_visits': baseline.get('local_queue_counts', {}).get('/visit', 0),
                  'after_visits': report['final'].get('local_queue_counts', {}).get('/visit', 0),
                  'report': str(path)}))
