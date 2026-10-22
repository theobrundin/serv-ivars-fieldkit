"""Reviewed field entry point retaining the existing exact QMI session owner.

Installed beside the existing supervisor and shutdown helper. No modem client
IDs, packet handles or Tailscale identity are copied or invented.
"""
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys


def load_core(path=None):
    loader = importlib.machinery.SourceFileLoader('field_cellular_core', str(path or Path(__file__).with_name('serv-cellular-supervisor')))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec); sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def guard():
    if os.geteuid() != 0:
        raise RuntimeError('Administrator required')
    path = Path('/etc/serv-field/unit.json')
    if path.is_symlink() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
        raise RuntimeError('Unsafe unit manifest')
    unit = json.loads(path.read_text())
    fingerprint = hashlib.sha256(Path('/etc/machine-id').read_bytes().strip()).hexdigest()
    if unit.get('machine_fingerprint') != fingerprint or unit.get('device_id') != 'serv-ivars-001':
        raise RuntimeError('Wrong physical unit')


def configuration(core):
    policy = json.loads(Path('/etc/serv-field/network.json').read_text())
    if policy['preferred'] not in ('cellular', 'wifi'):
        raise RuntimeError('Unknown network policy')
    cfg = core.Config(runtime='/run/serv-bench-cellular', dns_mode='resolved',
        preferred_metric=50 if policy['preferred'] == 'cellular' else 1600,
        probation_metric=42760, check_url='https://example.com/',
        secondary_url='https://controlplane.tailscale.com/')
    cfg.validate()
    return cfg


def main():
    guard(); core = load_core(); cfg = configuration(core)
    runtime = Path(cfg.runtime); runtime.mkdir(mode=0o700, exist_ok=True)
    if runtime.is_symlink() or runtime.stat().st_uid != 0 or runtime.stat().st_mode & 0o077:
        raise RuntimeError('Unsafe cellular runtime')
    Path(cfg.qmi_lock).parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    supervisor = core.Supervisor(cfg); supervisor.configuration_ready = False
    with core.Lock(str(runtime / 'supervisor.lock')):
        for unit in ('ModemManager.service', 'cellular-connect.service'):
            if supervisor.command(['systemctl', 'is-active', '--quiet', unit], required=False).code == 0:
                raise RuntimeError('Another cellular manager is active')
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: setattr(supervisor, 'stopping', True))
        supervisor.run()


if __name__ == '__main__':
    main()
