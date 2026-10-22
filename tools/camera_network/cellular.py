#!/usr/bin/env python3
"""Home-bench entry point for the unchanged, bounded QMI supervisor.

The production helper is installed beside this file from the reviewed local
source. This entry point does not invoke or approve a production updater.
"""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import sys


def load_core(path=None):
    path = path or Path(__file__).with_name("serv-cellular-supervisor")
    loader = importlib.machinery.SourceFileLoader("bench_cellular_core", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def configuration(core):
    cfg = core.Config(runtime="/run/serv-bench-cellular", dns_mode="resolved",
                      preferred_metric=1600, probation_metric=42760,
                      check_url="https://example.com/",
                      secondary_url="https://controlplane.tailscale.com/")
    cfg.validate()
    return cfg


def guard():
    if socket.gethostname().lower() != "servtest" or os.geteuid() != 0:
        raise RuntimeError("This installer is restricted to the home bench")
    cfg = json.loads(Path("/home/serv/plate_reader/config.json").read_text())
    if cfg.get("device_id") != "serv-unit-bench-reolink" or cfg.get("backend_writes_enabled") is not False:
        raise RuntimeError("Isolated bench identity and disabled backend required")


def main():
    guard()
    core = load_core()
    cfg = configuration(core)
    runtime = Path(cfg.runtime)
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    if runtime.is_symlink() or runtime.stat().st_uid != 0 or runtime.stat().st_mode & 0o077:
        raise RuntimeError("Unsafe cellular runtime directory")
    Path(cfg.qmi_lock).parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    supervisor = core.Supervisor(cfg)
    # A restored owned session still needs its per-link resolver settings
    # restored after a service/resolved restart.
    supervisor.configuration_ready = False
    with core.Lock(str(runtime / "supervisor.lock")):
        for unit in ("ModemManager.service", "cellular-connect.service"):
            if supervisor.command(["systemctl", "is-active", "--quiet", unit], required=False).code == 0:
                raise RuntimeError("Another cellular manager is active")
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: setattr(supervisor, "stopping", True))
        supervisor.run()


if __name__ == "__main__":
    main()
