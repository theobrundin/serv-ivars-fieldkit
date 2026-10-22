#!/usr/bin/env python3
"""Restart only the bench recognizer after a sustained, reachable-camera stall."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.error
import urllib.request


def advance(state, *, healthy, camera_ready, service_active, now):
    state = dict(state)
    state.setdefault('first_seen', now)
    if not service_active:
        # Respect intentional stops and systemd's exhausted restart limit.
        state.pop('bad_since', None)
        state.pop('good_since', None)
        return state, False
    if healthy:
        state['seen_healthy'] = True
        state.setdefault('good_since', now)
        if now - state['good_since'] >= 15:
            state.pop('bad_since', None)
        return state, False
    state.pop('good_since', None)
    state.setdefault('bad_since', now)
    if not state.get('seen_healthy') and now - state['first_seen'] < 90:
        return state, False
    restart = (camera_ready and now - state['bad_since'] >= 30
               and now - state.get('last_restart', -1000) >= 180)
    if restart:
        state['last_restart'] = now
        state.pop('bad_since', None)
    return state, restart


def camera_healthy(data, now):
    camera = data.get('cameras', {}).get('reolink-bench', {})
    try:
        stamp = datetime.fromisoformat(camera['last_checked'])
        age = (now - stamp).total_seconds()
        return (0 <= age <= 25 and camera.get('status') == 'running'
                and camera.get('health', 0) > 0 and camera.get('received_fps', 0) > 0)
    except (KeyError, TypeError, ValueError):
        return False


def main():
    if socket.gethostname().lower() != 'servtest' or os.getuid() != 0:
        raise SystemExit('Home bench administrator only')
    path = Path('/run/serv-reolink-watchdog/state.json')
    state = json.loads(path.read_text()) if path.exists() else {}
    active = subprocess.run(['systemctl', 'is-active', '--quiet', 'serv-reolink-stream.service'],
                            timeout=3).returncode == 0
    healthy = False
    if active:
        try:
            try:
                response = urllib.request.urlopen('http://127.0.0.1:8001/status/', timeout=3)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                healthy = camera_healthy(json.load(response), datetime.now(timezone.utc))
        except (OSError, ValueError):
            pass
    ready = False
    if active and not healthy:
        try:
            with socket.create_connection(('192.168.50.2', 554), timeout=1):
                ready = True
        except OSError:
            pass
    state, restart = advance(state, healthy=healthy, camera_ready=ready,
                             service_active=active, now=time.monotonic())
    path.parent.mkdir(mode=0o700, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(state) + '\n')
    temp.chmod(0o600)
    temp.replace(path)
    if restart:
        print('Camera reachable but PR unhealthy for 30s; requesting bounded PR-only restart', flush=True)
        subprocess.run(['systemctl', '--no-block', 'try-restart', 'serv-reolink-stream.service'],
                       check=True, timeout=4)


if __name__ == '__main__':
    main()
