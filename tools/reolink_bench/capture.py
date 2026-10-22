#!/usr/bin/env python3
"""Bounded, explicit diagnostic capture; all images/video stay private on Pi."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import time
from urllib.parse import quote

if socket.gethostname().lower() != 'servtest':
    raise SystemExit('Expected home bench Pi')
parser = argparse.ArgumentParser()
parser.add_argument('--seconds', type=int, default=0, help='0: one JPEG; 1-30: main-stream video clip')
args = parser.parse_args()
if not 0 <= args.seconds <= 30:
    parser.error('duration must be 0-30 seconds')
root = Path('/home/serv/reolink-bench')
secret = root / 'camera-credentials.json'
s = secret.lstat()
if not stat.S_ISREG(s.st_mode) or s.st_mode & 0o077 or s.st_uid != os.getuid():
    raise SystemExit('Camera credential file must be private and owned')
credentials = json.loads(secret.read_text())
url = 'rtsp://{}:{}@192.168.50.2:554/Preview_01_main'.format(
    quote(credentials['username'], safe=''), quote(credentials['password'], safe=''))
directory = root / 'captures'
directory.mkdir(mode=0o700, exist_ok=True)
os.umask(0o077)
suffix = '.mkv' if args.seconds else '.jpg'
output = directory / (time.strftime('%Y%m%dT%H%M%S', time.gmtime()) + f'-{time.time_ns()}' + suffix)
command = ['ffmpeg', '-nostdin', '-v', 'error', '-rtsp_transport', 'tcp',
           '-timeout', '5000000', '-allowed_media_types', 'video',
           '-threads', '2', '-i', url, '-map', '0:v:0', '-an']
if args.seconds:
    command += ['-t', str(args.seconds), '-c:v', 'copy', '-fs', '100000000']
else:
    command += ['-frames:v', '1', '-q:v', '3']
command += ['-n', str(output)]
# Suppress vendor stderr: it can print the authenticated URL. Report only
# exit status. A separate process group bounds a stalled decoder's lifetime.
proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        start_new_session=True)
try:
    result = proc.wait(timeout=args.seconds + 15)
except subprocess.TimeoutExpired:
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
    result = -1
if result or not output.exists():
    output.unlink(missing_ok=True)
    raise SystemExit('Capture failed or timed out; authenticated decoder output suppressed')
files = sorted(directory.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
for old in files[5:]:
    if old.is_file() and old.suffix in ('.mkv', '.jpg'):
        old.unlink()
print(json.dumps({'saved_on_pi': str(output), 'bytes': output.stat().st_size,
                  'video_seconds_limit': args.seconds, 'retention_files': 5}))
