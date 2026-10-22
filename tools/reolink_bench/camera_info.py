#!/usr/bin/env python3
"""Read camera model/encoding over port 9000 without enabling HTTP or changing settings.

Optional dependency: reolink-aio==0.21.17 installed in the private camera-lib
directory beside the on-Pi credential file. Never prints serials or credentials.
"""
import asyncio
import json
import logging
import os
from pathlib import Path
import socket
import stat
import sys

ROOT = Path('/home/serv/reolink-bench')


async def main():
    if socket.gethostname().lower() != 'servtest':
        raise SystemExit('Home bench only')
    secret = ROOT / 'camera-credentials.json'
    st = secret.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid():
        raise SystemExit('Camera credential file must be private and owned')
    sys.path.insert(0, str(ROOT / 'camera-lib'))
    from reolink_aio.api import Host
    credentials = json.loads(secret.read_text())
    host = Host('192.168.50.2', credentials['username'], credentials['password'],
                bc_only=True, bc_port=9000, timeout=5)
    try:
        # Direct Baichuan reads avoid the general Host discovery routine,
        # which can negotiate/enlarge the enabled HTTP feature set.
        await host.baichuan.login()
        await host.baichuan.get_info()
        await host.baichuan.GetEnc(0)
        result = {'model': host.model, 'hardware': host.hardware_version,
                  'firmware': host.sw_version, 'settings_changed': False,
                  'streams': {}}
        for stream in ('main', 'sub'):
            enc = host._enc_settings.get(0, {}).get(stream + 'Stream', {})
            result['streams'][stream] = {
                k: enc.get(k) for k in ('width', 'height', 'frameRate', 'bitRate', 'videoEncType')}
            result['streams'][stream]['available_frame_rates'] = host.frame_rate_list(0, stream)
        path = ROOT / 'camera-info.json'
        os.umask(0o077)
        path.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
    finally:
        await host.logout()


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(asyncio.wait_for(main(), 35))
    except Exception as exc:
        raise SystemExit('Camera read failed: ' + type(exc).__name__)
