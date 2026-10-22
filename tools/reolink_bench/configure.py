#!/usr/bin/env python3
"""Generate a private, local-only home bench configuration on servTest."""
import json
import os
from pathlib import Path
import socket
import stat
from urllib.parse import quote


def bench_config(template):
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if not k.startswith('//')}
        return value
    cfg = clean(template)
    cfg.update(device_id='serv-unit-bench-reolink', location_id='bench-reolink-house',
               api_key='0' * 64, api_base='https://backend-disabled.invalid',
               backend_writes_enabled=False)
    cfg.pop('hours', None)
    cfg['alpr'].pop('license_token_file', None)
    cfg['alpr']['engine'] = 'pr_stream'
    cfg['capture'].update(width=3840, height=2160, target_fps=5)
    cfg['camera'].update(resolution_width=3840, resolution_height=2160, framerate=25)
    # Full-frame recognition for the home test. The LJS route remains shadow
    # evidence only; it must be calibrated for this camera before field use.
    cfg['detection_zone']['enabled'] = False
    cfg['presence']['shadow_only'] = True
    cfg['presence']['route_qualification']['shadow_only'] = True
    cfg['diagnostics'].update(miss_plate_max_cases=60, miss_plate_retain_hours=24,
                              log_rejected_reads=False, zone_preview_overlay=False)
    cfg['telemetry'].update(save_zone_preview_on_boot=False, include_tailscale=False,
                            include_cellular=False)
    return cfg


def stream_config(credentials):
    user = quote(credentials['username'], safe='')
    password = quote(credentials['password'], safe='')
    return f'''timezone = America/New_York
config_change_restart = no
[cameras]
  regions = us
  image_format =
  [[reolink-bench]]
    active = yes
    url = rtsp://{user}:{password}@192.168.50.2:554/Preview_01_main
    sample = 6
    max_prediction_delay = 12
    memory_decay = 90
    plates_per_vehicle = 1
    detection_mode = vehicle
    mmc = yes
    dwell_time = true
    trajectory = true
    max_dwell_delay = 30
    report_static = no
    max_reconnection_delay = 30
    webhook_targets = serv-local
[webhooks]
  [[serv-local]]
    url = http://127.0.0.1:8129/
    image = original
    request_timeout = 30
    caching = yes
'''


def private_create(path, content):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(content)


if __name__ == '__main__':
    if socket.gethostname().lower() != 'servtest' or os.getuid() == 0:
        raise SystemExit('Run as the serv user on the home bench host servTest')
    repo = Path(__file__).resolve().parents[2]
    cfg = bench_config(json.loads((repo / 'config.example.json').read_text()))
    from plate_reader.config import load as load_config
    root = Path('/home/serv/reolink-bench')
    secret = root / 'camera-credentials.json'
    mode = secret.lstat()
    if not stat.S_ISREG(mode.st_mode) or mode.st_mode & 0o077 or mode.st_uid != os.getuid():
        raise SystemExit('Camera credentials must be an owned regular private file')
    app_path = Path('/home/serv/plate_reader/config.json')
    stream_path = root / 'stream/config.ini'
    if app_path.exists() or stream_path.exists():
        raise SystemExit('Configuration already exists; refusing to overwrite')
    private_create(app_path, json.dumps(cfg, indent=2) + '\n')
    load_config(app_path)
    private_create(stream_path, stream_config(json.loads(secret.read_text())))
    print('Created validated local-only bench configuration; credentials omitted')
