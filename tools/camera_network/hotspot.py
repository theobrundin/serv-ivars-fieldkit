#!/usr/bin/env python3
"""Timed, reversible hotspot cutover on the verified home-bench host."""
import argparse
import json
from pathlib import Path
import subprocess
import time
from cellular import guard

WIFI_UUID = '4a5eeba2-2232-348b-af81-fe30caadc1db'
TIMER = 'serv-camera-hotspot-rollback'


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=35).stdout


def ready():
    status = json.loads(Path('/run/serv-bench-cellular/status.json').read_text())
    if status['stage'] != 'connected' or time.time() - status['updated_at'] > 90:
        raise RuntimeError('Fresh healthy cellular status required')
    if json.loads(run('tailscale', 'status', '--json'))['BackendState'] != 'Running':
        raise RuntimeError('Connected Tailscale management required')
    run('systemctl', 'is-active', '--quiet', 'serv-camera-isolation', 'serv-camera-dhcp')
    run('nft', 'list', 'table', 'inet', 'serv_camera_isolation')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'commit', 'rollback'))
    args = parser.parse_args()
    guard()
    if args.action == 'rollback':
        run('nmcli', 'connection', 'modify', 'serv-camera', 'connection.autoconnect', 'no')
        run('nmcli', '-w', '25', 'connection', 'up', 'uuid', WIFI_UUID)
        print('Home Wi-Fi restored; hotspot autoconnect disabled')
        return
    ready()
    if args.action == 'start':
        run('systemd-run', '--unit=' + TIMER, '--on-active=300s',
            '/usr/bin/python3', str(Path(__file__).resolve()), 'rollback')
        run('nmcli', '-w', '25', 'connection', 'up', 'serv-camera')
        print('Hotspot active; automatic Wi-Fi rollback in five minutes unless committed')
    else:
        addresses = json.loads(run('ip', '-j', '-4', 'addr', 'show', 'wlan0'))
        if not any(a['local'] == '10.42.0.1' for link in addresses for a in link['addr_info']):
            raise RuntimeError('Hotspot address missing; refusing commit')
        run('nmcli', 'connection', 'modify', 'serv-camera', 'connection.autoconnect', 'yes')
        run('systemctl', 'stop', TIMER + '.timer')
        print('Hotspot autoconnect enabled; rollback timer cancelled')


if __name__ == '__main__':
    main()
