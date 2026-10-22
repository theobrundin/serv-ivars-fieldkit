#!/usr/bin/env python3
"""Exercise the exact nft policy in disposable Linux network namespaces.

Requires root, iproute2, nftables, ping and curl. Does not alter host routes.
"""
import json
import os
from pathlib import Path
import subprocess
import time


def run(*args, ok=True, timeout=10):
    return subprocess.run(args, check=ok, capture_output=True, text=True, timeout=timeout)


def main():
    assert os.geteuid() == 0
    base = f"serv-isolation-{os.getpid()}"
    names = {k: base + '-' + k for k in ('pi', 'eth', 'wifi', 'wan')}
    created = []
    servers = []
    def ns(which, *args, **kwargs):
        return run('ip', 'netns', 'exec', names[which], *args, **kwargs)
    try:
        for name in names.values():
            run('ip', 'netns', 'add', name)
            created.append(name)
            run('ip', '-n', name, 'link', 'set', 'lo', 'up')
        for key, iface, prefix, v6 in (
                ('eth', 'eth0', '192.168.50', 'fd42:50'),
                ('wifi', 'wlan0', '10.42.0', 'fd42:42'),
                ('wan', 'wwan0', '198.18.0', 'fd42:99')):
            ns('pi', 'ip', 'link', 'add', iface, 'type', 'veth', 'peer', 'name', 'peer')
            ns('pi', 'ip', 'link', 'set', 'peer', 'netns', names[key])
            for owner, device, number in (('pi', iface, 1), (key, 'peer', 2)):
                ns(owner, 'ip', 'addr', 'add', f'{prefix}.{number}/24', 'dev', device)
                ns(owner, 'ip', '-6', 'addr', 'add', f'{v6}::{number}/64', 'dev', device, 'nodad')
                ns(owner, 'ip', 'link', 'set', device, 'up')
            ns(key, 'ip', 'route', 'add', 'default', 'via', f'{prefix}.1')
            ns(key, 'ip', '-6', 'route', 'add', 'default', 'via', f'{v6}::1')
        ns('pi', 'sysctl', '-qw', 'net.ipv4.ip_forward=1', 'net.ipv6.conf.all.forwarding=1')
        for key in ('eth', 'wifi', 'wan'):
            servers.append(subprocess.Popen(['ip', 'netns', 'exec', names[key],
                'python3', '-m', 'http.server', '554', '--bind', '0.0.0.0', '--directory', '/var/empty'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(0.4)
        # Establish the WAN path before installing the firewall. These are
        # synthetic packets to another namespace, never real internet traffic.
        for key in ('eth', 'wifi'):
            ns(key, 'ping', '-c', '2', '-W', '2', '198.18.0.2')
            ns(key, 'ping', '-6', '-c', '2', '-W', '2', 'fd42:99::2')
        policy = str(Path(__file__).with_name('camera-guard.nft'))
        ns('pi', 'nft', '-f', policy)
        ns('pi', 'nft', '-f', policy)  # Atomic reload must not duplicate rules.
        for key, address in (('eth', '192.168.50.2'), ('wifi', '10.42.0.2')):
            # Pi-to-camera TCP remains usable, including replies.
            ns('pi', 'curl', '-sS', '--max-time', '3', '-o', '/dev/null', f'http://{address}:554/')
            assert ns(key, 'ping', '-c', '1', '-W', '1', '198.18.0.2', ok=False).returncode
            assert ns(key, 'ping', '-6', '-c', '1', '-W', '1', 'fd42:99::2', ok=False).returncode
            assert ns(key, 'curl', '-sS', '--max-time', '1', '-o', '/dev/null',
                      'http://198.18.0.2:554/', ok=False).returncode
        # Even a future routing mistake must not send camera addresses to WAN.
        ns('pi', 'ip', 'route', 'replace', '10.42.0.2/32', 'via', '198.18.0.2', 'dev', 'wwan0')
        assert ns('pi', 'ping', '-c', '1', '-W', '1', '10.42.0.2', ok=False).returncode
        report = json.loads(ns('pi', 'nft', '-j', 'list', 'counters', 'table', 'inet', 'serv_camera_isolation').stdout)
        counters = {x['counter']['name']: x['counter']['packets'] for x in report['nftables'] if 'counter' in x}
        assert counters['camera_forward_blocked'] >= 6
        assert counters['camera_wrong_route_blocked'] >= 1
        print(json.dumps({'local_camera_tcp': 'passed', 'ipv4_ipv6_wan_block': 'passed',
                          'wrong_route_block': 'passed', 'counters': counters}))
    finally:
        for p in servers:
            p.terminate()
            p.wait(timeout=5)
        for name in reversed(created):
            run('ip', 'netns', 'del', name, ok=False)


if __name__ == '__main__':
    main()
