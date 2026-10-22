#!/usr/bin/env python3
"""Install this explicitly authorized home-bench network, never a field unit.

Run from a root-owned staging directory containing these assets and the
unchanged scripts/serv-cellular-supervisor. Packages must already be installed.
Leaves the hotspot inactive and non-autoconnecting until a verified cutover.
"""
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import uuid

HERE = Path(__file__).resolve().parent
DEST = Path('/opt/serv-bench-network')
CONF = Path('/etc/serv-bench-network')
STATE = Path('/var/lib/serv-bench-network')


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=40).stdout


def main():
    os.umask(0o077)
    spec = importlib.util.spec_from_file_location('bench_cellular', HERE / 'cellular.py')
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)
    bench.guard()
    if HERE.stat().st_uid != 0 or HERE.stat().st_mode & 0o022:
        raise RuntimeError('Root-owned reviewed staging required')
    if not run('ip', '-4', 'route', 'show', 'default', 'dev', 'wlan0').strip():
        raise RuntimeError('Independent Wi-Fi rescue route required')
    for route in json.loads(run('ip', '-j', '-4', 'route', 'show', 'table', 'all')):
        dst = route.get('dst', 'default')
        if dst != 'default' and ipaddress.ip_network('10.42.0.0/24').overlaps(ipaddress.ip_network(dst, strict=False)):
            raise RuntimeError('Proposed hotspot subnet overlaps an existing route')
    for unit in ('ModemManager.service', 'cellular-connect.service', 'serv-bench-cellular.service'):
        if subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0:
            raise RuntimeError('Cellular manager already active; refusing first-install adoption')
    if (STATE / 'installed.json').exists():
        raise RuntimeError('Already installed; inspect before updating')
    run('systemctl', 'is-active', '--quiet', 'systemd-resolved')
    run('nft', '--check', '-f', str(HERE / 'camera-guard.nft'))
    for directory in (DEST, CONF, STATE):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ('cellular.py', 'serv-cellular-supervisor', 'hotspot.py', 'shutdown.py'):
        shutil.copyfile(HERE / name, DEST / name)
        (DEST / name).chmod(0o644)
    for name in ('camera-guard.nft', 'camera-dnsmasq.conf'):
        shutil.copyfile(HERE / name, CONF / name)
        (CONF / name).chmod(0o600)
    (CONF / 'camera-hosts').touch(mode=0o600)
    # Adopt only the exact client and packet handle created earlier in this
    # boot by our private connection test. Never infer or invent either ID.
    old_file = Path('/run/serv-sixfab-check/session.json')
    if old_file.stat().st_uid != 0 or old_file.stat().st_mode & 0o077:
        raise RuntimeError('Unsafe test ownership file')
    old = json.loads(old_file.read_text())
    assert old['stage'] == 'connected' and 1 <= old['cid'] <= 254
    assert 1 <= old['pdh'] <= 4294967295
    core = bench.load_core(DEST / 'serv-cellular-supervisor')
    supervisor = core.Supervisor(bench.configuration(core))
    Path(supervisor.cfg.qmi_lock).parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    actual = supervisor.qmi('--wds-get-current-settings', old['cid'])
    settings = core.Settings.parse(actual.out)
    if actual.code or str(ipaddress.ip_interface(old['address']).ip) != settings.address:
        raise RuntimeError('Existing owned cellular session does not match')
    Path(supervisor.cfg.runtime).mkdir(mode=0o700, exist_ok=True)
    supervisor.state = dict(version=1, fingerprint=supervisor.modem_fingerprint(), cid=old['cid'], pdh=old['pdh'])
    supervisor.persist()
    for name in ('serv-bench-cellular-shutdown', 'serv-bench-cellular', 'serv-camera-isolation', 'serv-camera-dhcp'):
        target = Path('/etc/systemd/system') / (name + '.service')
        shutil.copyfile(HERE / target.name, target)
        target.chmod(0o644)
        run('systemd-analyze', 'verify', str(target))
    drop = Path('/etc/systemd/system/NetworkManager.service.d')
    drop.mkdir(mode=0o755, exist_ok=True)
    (drop / 'serv-camera-isolation.conf').write_text('[Unit]\nRequires=serv-camera-isolation.service\nAfter=serv-camera-isolation.service\n')
    (drop / 'serv-camera-isolation.conf').chmod(0o644)
    # Static addressing and a DHCP-only service avoid NetworkManager's shared
    # mode, NAT, and an automatically advertised internet gateway.
    ident = str(uuid.uuid4())
    password = 'ServCamera-' + secrets.token_hex(6)
    keyfile = Path('/etc/NetworkManager/system-connections/serv-camera.nmconnection')
    if keyfile.exists():
        raise RuntimeError('Refusing an existing hotspot profile')
    keyfile.write_text(f'''[connection]
id=serv-camera
uuid={ident}
type=wifi
interface-name=wlan0
autoconnect=false
autoconnect-priority=100

[wifi]
band=bg
channel=6
mode=ap
ssid=SERV-Camera
powersave=2
ap-isolation=1

[wifi-security]
key-mgmt=wpa-psk
proto=rsn;
pairwise=ccmp;
group=ccmp;
psk={password}

[ipv4]
address1=10.42.0.1/24
method=manual
never-default=true
ignore-auto-dns=true

[ipv6]
method=disabled
''')
    keyfile.chmod(0o600)
    (CONF / 'hotspot.json').write_text(json.dumps(dict(uuid=ident, ssid='SERV-Camera', password=password)))
    run('nmcli', 'connection', 'load', str(keyfile))
    run('dnsmasq', '--test', '--conf-file=' + str(CONF / 'camera-dnsmasq.conf'))
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'serv-camera-isolation.service', 'serv-bench-cellular.service')
    run('systemctl', 'enable', '--now', 'serv-camera-dhcp.service')
    old_file.rename(old_file.with_suffix('.adopted.json'))
    (STATE / 'installed.json').write_text(json.dumps(dict(host='servTest', hotspot_active=False,
         scope='isolated_home_bench', backend_writes_enabled=False)))
    print('Bench cellular, camera isolation, and DHCP installed. Hotspot staged but inactive.')


if __name__ == '__main__':
    main()
