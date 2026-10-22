#!/usr/bin/env python3
"""Release our exact QMI session at orderly shutdown, after its owner stops."""
from pathlib import Path
import subprocess
from cellular import configuration, guard, load_core


def main():
    guard()
    if subprocess.run(['systemctl', 'is-active', '--quiet', 'serv-bench-cellular']).returncode == 0:
        raise RuntimeError('Stop the cellular supervisor before shutdown cleanup')
    core = load_core()
    cfg = configuration(core)
    with core.Lock(str(Path(cfg.runtime) / 'supervisor.lock')):
        supervisor = core.Supervisor(cfg)
        fingerprint = supervisor.modem_fingerprint()
        if fingerprint is None:
            return
        supervisor.restore(fingerprint)
        if supervisor.state is None:
            return
        supervisor.current_settings = supervisor.get_settings()
        if not supervisor.cleanup():
            raise RuntimeError('Exact owned session cleanup incomplete')
        print('Owned cellular session released for orderly shutdown')


if __name__ == '__main__':
    main()
