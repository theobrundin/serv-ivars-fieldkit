#!/usr/bin/env python3
"""Check the real DHCP config offers a local lease without router/DNS options."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=15).stdout


def main():
    assert os.geteuid() == 0
    names = [f'serv-dhcp-{os.getpid()}-{x}' for x in ('server', 'client')]
    made = []
    daemon = None
    with tempfile.TemporaryDirectory(prefix='serv-dhcp-test-') as tmp:
        root = Path(tmp)
        try:
            for name in names:
                run('ip', 'netns', 'add', name)
                made.append(name)
                run('ip', '-n', name, 'link', 'set', 'lo', 'up')
            def ns(n, *args):
                return run('ip', 'netns', 'exec', names[n], *args)
            ns(0, 'ip', 'link', 'add', 'wlan0', 'type', 'veth', 'peer', 'name', 'client')
            ns(0, 'ip', 'link', 'set', 'client', 'netns', names[1])
            ns(0, 'ip', 'addr', 'add', '10.42.0.1/24', 'dev', 'wlan0')
            ns(0, 'ip', 'link', 'set', 'wlan0', 'up')
            ns(1, 'ip', 'link', 'set', 'client', 'up')
            cfg = Path(__file__).with_name('camera-dnsmasq.conf').read_text()
            cfg = cfg.replace('/run/serv-camera-dhcp/dnsmasq.pid', str(root / 'pid'))
            cfg = cfg.replace('/var/lib/serv-bench-network/camera.leases', str(root / 'leases'))
            cfg = cfg.replace('/etc/serv-bench-network/camera-hosts', str(root / 'hosts'))
            (root / 'hosts').touch()
            (root / 'config').write_text(cfg)
            script = root / 'bound'
            script.write_text('#!/bin/sh\nif [ "$1" = bound ]; then\n'
                              'printf "%s\\n" "$ip" "$router" "$dns" > ' + str(root / 'result') + '\nfi\n')
            script.chmod(0o700)
            daemon = subprocess.Popen(['ip', 'netns', 'exec', names[0], 'dnsmasq',
                '--no-daemon', '--conf-file=' + str(root / 'config')],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.4)
            ns(1, 'busybox', 'udhcpc', '-n', '-q', '-t', '3', '-T', '2', '-i', 'client', '-s', str(script))
            address, router, dns = (root / 'result').read_text().splitlines()
            assert address.startswith('10.42.0.') and 20 <= int(address.split('.')[-1]) <= 80
            assert not router and not dns
            print(json.dumps({'lease': address, 'router_advertised': False, 'dns_advertised': False}))
        finally:
            if daemon:
                daemon.terminate()
                daemon.wait(timeout=5)
            for name in reversed(made):
                subprocess.run(['ip', 'netns', 'del', name], check=False, capture_output=True)


if __name__ == '__main__':
    main()
