#!/usr/bin/env python3
"""Print allow-listed local health only. Never export raw OCR, URLs or images."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request

ROOT = Path('/home/serv/reolink-bench')


def snapshot():
    out = {'time': datetime.now(timezone.utc).isoformat(), 'camera': {}}
    try:
        try:
            response = urllib.request.urlopen('http://127.0.0.1:8001/status/', timeout=4)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            data = json.load(response)
        out['pr_version'] = data.get('version')
        for key, value in data.get('cameras', {}).items():
            if key in ('reolink-bench', 'recorded-bench'):
                out['source'] = 'live-camera' if key == 'reolink-bench' else 'recorded-video'
                out['camera'] = {k: value.get(k) for k in
                    ('status', 'health', 'received_fps', 'buffered_frames', 'last_checked')}
    except Exception as exc:
        out['status_error'] = type(exc).__name__
    config = ROOT / 'stream/config.ini'
    if config.exists():
        match = re.search(r'^\s*sample\s*=\s*(\d+)\s*$', config.read_text(), re.M)
        if match:
            out['sample'] = int(match[1])
    for name in ('app', 'stream', 'dhcp'):
        result = subprocess.run(['systemctl', 'is-active', f'serv-reolink-{name}.service'],
                                capture_output=True, text=True, timeout=4)
        out[name + '_service'] = result.stdout.strip()
    out['temperature_c'] = int(Path('/sys/class/thermal/thermal_zone0/temp').read_text()) / 1000
    result = subprocess.run(['vcgencmd', 'get_throttled'], capture_output=True, text=True, timeout=4)
    out['throttled'] = result.stdout.strip().removeprefix('throttled=')
    mem = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    out['available_memory_mb'] = int(mem['MemAvailable'].split()[0]) / 1024
    out['swap_used_mb'] = (int(mem['SwapTotal'].split()[0]) - int(mem['SwapFree'].split()[0])) / 1024
    # The Pi kernel may disable memory cgroups (docker stats then shows 0B).
    # /proc RSS remains a useful measured value; never report that 0B as usage.
    try:
        import psutil
        procs = []
        for proc in psutil.process_iter(['pid', 'name', 'memory_info', 'cpu_times']):
            try:
                group = Path(f'/proc/{proc.pid}/cgroup').read_text()
                if 'docker-' in group or '/docker/' in group:
                    info = proc.info
                    procs.append({'pid': proc.pid, 'rss_mb': info['memory_info'].rss / 1048576,
                                  'cpu_seconds': sum(info['cpu_times'][:2])})
            except (OSError, psutil.Error):
                pass
        out['container_processes'] = procs
    except ImportError:
        pass
    queue = Path('/home/serv/plate_reader/queue.dry-run.db')
    if queue.exists():
        with sqlite3.connect(f'file:{queue}?mode=ro', uri=True) as conn:
            out['local_queue_counts'] = dict(conn.execute('SELECT endpoint, count(*) FROM queue GROUP BY endpoint'))
            out['backend_attempts'] = conn.execute('SELECT coalesce(sum(attempts),0) FROM queue').fetchone()[0]
            rows = conn.execute("SELECT payload FROM queue WHERE endpoint='/visit'").fetchall()
            visits = [json.loads(row[0]) for row in rows]
            out['visits_with_hash'] = sum(len(v.get('licensePlate', '')) == 64 for v in visits)
            out['visits_with_mmc'] = sum(bool(v.get('make') or v.get('model') or v.get('color')) for v in visits)
    return out


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--soak-minutes', type=float, default=0)
    args = parser.parse_args()
    if not 0 <= args.soak_minutes <= 60:
        parser.error('soak must be between zero and 60 minutes')
    if args.soak_minutes == 0:
        print(json.dumps(snapshot(), indent=2))
    else:
        path = ROOT / ('soak-' + time.strftime('%Y%m%dT%H%M%S', time.gmtime()) + '.jsonl')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        deadline = time.monotonic() + args.soak_minutes * 60
        with os.fdopen(fd, 'w') as out:
            while True:
                out.write(json.dumps(snapshot()) + '\n')
                out.flush()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(15, remaining))
        print('Completed bounded soak:', path)
