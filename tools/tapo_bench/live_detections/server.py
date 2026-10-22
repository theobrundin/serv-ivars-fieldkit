#!/usr/bin/env python3
"""Temporary, explicitly authorized home-test viewer. Never a fleet service.

Pi: a second loopback PR webhook, bounded memory, read-only queue matching.
Mac: authenticated LAN UI through an independently owned SSH metadata tunnel.
No image endpoint, disk event history, request logging, or production writes.
"""
import argparse
from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = Path(__file__).resolve().parent
MAX_BODY = 512 * 1024
MAX_ROWS = 200
TTL = 1800


def epoch(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def label(value, length=80):
    return str(value or '')[:length]


def score(value):
    try:
        result = float(value)
        return max(0, min(1, result)) if math.isfinite(result) else 0
    except (ValueError, TypeError):
        return 0


class Events:
    def __init__(self, clock=time.time, limit=MAX_ROWS, ttl=TTL):
        self.clock, self.limit, self.ttl = clock, limit, ttl
        self.started = clock()
        self.rows = OrderedDict()
        self.seen = OrderedDict()
        self.lock = threading.Lock()
        self.webhooks = 0
        self.last_webhook = None
        self.last_format = None
        self.total = 0

    def prune(self):
        cutoff = self.clock() - self.ttl
        for key in list(self.rows):
            if self.rows[key]['receivedEpoch'] < cutoff:
                del self.rows[key]
        while len(self.rows) > self.limit:
            self.rows.popitem(last=False)
        for key in list(self.seen):
            if self.seen[key] < cutoff:
                del self.seen[key]
        while len(self.seen) > self.limit * 5:
            self.seen.popitem(last=False)

    def accept(self, payload, content_type='application/json'):
        from plate_reader.stream_engine import (
            _hook_event, _payload_camera, _payload_timestamp, _plate_fields,
            _results, _vehicle_fields,
        )
        from plate_reader.normalizer import normalize
        from plate_reader.hasher import hash_plate

        if not isinstance(payload, dict):
            raise ValueError('Invalid webhook')
        if _payload_camera(payload) != 'tapo-bench':
            return 0
        now = self.clock()
        with self.lock:
            self.prune()
            self.webhooks += 1
            self.last_webhook = now
            self.last_format = content_type.split(';')[0]
            if _hook_event(payload) != 'recognition':
                return 0
            # Fingerprint only in Pi memory; exact HTTP retries add no readings.
            fingerprint = hashlib.sha256(json.dumps(
                payload, sort_keys=True, separators=(',', ':')
            ).encode()).hexdigest()
            if fingerprint in self.seen:
                return 0
            self.seen[fingerprint] = now
            captured = epoch(_payload_timestamp(payload))
            # Never present an inconsistent camera/provider clock as a live time.
            valid_time = captured is not None and now - TTL <= captured <= now + 5
            observed = captured if valid_time else now
            groups = OrderedDict()
            for index, result in enumerate(_results(payload)):
                try:
                    raw, det, ocr, _ = _plate_fields(result)
                    box, confidence, kind, make, model, color, *_ = _vehicle_fields(result)
                    if not raw and box is None and not kind:
                        continue
                    key = tuple(box) if box and any(box) else ('result', index)
                    normalized = normalize(raw) if raw else None
                    candidate = None if not raw else {
                        'plate': label(raw, 32).upper(), 'ocr': score(ocr),
                        'detection': score(det),
                        '_hash': hash_plate(normalized) if normalized else None,
                    }
                    if key not in groups:
                        groups[key] = {
                            'id': secrets.token_hex(8), 'observedAt': utc(observed),
                            'receivedAt': utc(now), 'receivedEpoch': now,
                            '_captured': captured if valid_time else None,
                            'providerTimeValid': valid_time,
                            'make': label(make), 'model': label(model),
                            'color': label(color), 'vehicleType': label(kind),
                            'vehicleConfidence': score(confidence), 'candidates': [],
                            'confirmed': False,
                        }
                    if candidate and not any(
                        c['plate'] == candidate['plate'] for c in groups[key]['candidates']
                    ):
                        groups[key]['candidates'].append(candidate)
                except (TypeError, ValueError, AttributeError, KeyError):
                    continue
            for row in groups.values():
                row['candidates'] = sorted(row['candidates'], key=lambda c: c['ocr'], reverse=True)[:6]
                self.rows[row['id']] = row
            self.total += len(groups)
            self.prune()
            return len(groups)

    def snapshot(self, visits=()):
        # A score alone can NEVER mark a result confirmed. Require exactly one
        # candidate row matching an actual new queue visit's hash AND timestamp.
        # Ambiguous or later reads remain candidates, even if they look similar.
        with self.lock:
            self.prune()
            for row in self.rows.values():
                row['confirmed'] = False
                row.pop('confirmedPlate', None)
            for visit in visits:
                if visit.get('detectionOutcome') != 'confirmed':
                    continue
                stamp = epoch(visit.get('detectedAt'))
                digest = visit.get('licensePlate')
                if stamp is None or not isinstance(digest, str) or len(digest) != 64:
                    continue
                matches = [
                    (row, c) for row in self.rows.values()
                    if row['_captured'] is not None and abs(row['_captured'] - stamp) < .01
                    for c in row['candidates'] if c['_hash'] == digest
                ]
                if len(matches) == 1:
                    row, candidate = matches[0]
                    row['confirmed'] = True
                    row['confirmedPlate'] = candidate['plate']
            rows = []
            for row in reversed(self.rows.values()):
                public = {k: v for k, v in row.items()
                          if not k.startswith('_') and k not in ('candidates', 'receivedEpoch')}
                public['candidates'] = [{k: v for k, v in c.items() if not k.startswith('_')}
                                        for c in row['candidates']]
                rows.append(public)
            return {
                'sessionStarted': utc(self.started), 'serverTime': utc(self.clock()),
                'rows': rows, 'totalReadings': self.total,
                'lastWebhook': utc(self.last_webhook) if self.last_webhook else None,
                'webhooksReceived': self.webhooks, 'lastWebhookFormat': self.last_format,
                'retentionSeconds': self.ttl,
            }


def queue_visits(path, since):
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=1) as conn:
        rows = conn.execute(
            "SELECT payload FROM queue WHERE endpoint='/visit' AND enqueued_at >= ? "
            'ORDER BY id DESC LIMIT 1000', (since,)
        ).fetchall()
    out = []
    for (payload,) in rows:
        try:
            value = json.loads(payload)
            if isinstance(value, dict):
                out.append(value)
        except (ValueError, TypeError):
            pass
    return out


def camera_status():
    try:
        try:
            response = urllib.request.urlopen('http://127.0.0.1:8001/status/', timeout=2)
        except urllib.error.HTTPError as exc:
            response = exc  # PR returns its status JSON even for some non-200 states.
        with response:
            data = json.loads(response.read(MAX_BODY))
        camera = data.get('cameras', {}).get('tapo-bench', {})
        app = subprocess.run(['systemctl', 'is-active', 'serv-reolink-app.service'],
                             capture_output=True, timeout=2, text=True)
        fps = float(camera.get('received_fps') or 0)
        health = float(camera.get('health') or 0)
        checked = epoch(camera.get('last_checked'))
        fresh = checked is not None and -5 <= time.time() - checked < 15
        return {'online': camera.get('status') == 'running' and fresh and fps > 0 and health > 0,
                'fps': fps, 'health': health,
                'appRunning': app.stdout.strip() == 'active', 'checkedAt': utc(time.time())}
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return {'online': False, 'appRunning': False, 'checkedAt': utc(time.time())}


class App:
    def __init__(self, config):
        self.config = config
        self.events = Events()
        self.health = {'online': False, 'appRunning': False}
        self.visits = []
        self.queue_ok = False
        self.cache = None
        self.cache_at = 0
        self.lock = threading.Lock()
        self.stopped = threading.Event()

    def refresh(self):
        while not self.stopped.is_set():
            health = camera_status()
            try:
                visits = queue_visits(self.config['queue'], self.events.started)
                queue_ok = True
            except (sqlite3.Error, OSError):
                visits, queue_ok = [], False
            with self.lock:
                self.health, self.visits, self.queue_ok = health, visits, queue_ok
            self.stopped.wait(2)

    def snapshot(self):
        if self.config['mode'] == 'pi':
            with self.lock:
                visits, health, queue_ok = self.visits, dict(self.health), self.queue_ok
            out = self.events.snapshot(visits)
            out.update(camera=health, confirmationAvailable=queue_ok)
            return out
        # At most one fetch/second shared by phone + laptop. No stale fallback.
        with self.lock:
            if self.cache is not None and time.monotonic() - self.cache_at < 1:
                return self.cache
            req = urllib.request.Request(self.config['upstream'] + '/snapshot', headers={
                'Authorization': 'Bearer ' + self.config['transport_token']})
            with urllib.request.urlopen(req, timeout=3) as response:
                body = response.read(MAX_BODY + 1)
                if len(body) > MAX_BODY:
                    raise ValueError('Snapshot limit')
                out = json.loads(body)
            if not isinstance(out, dict) or not isinstance(out.get('rows'), list):
                raise ValueError('Invalid snapshot')
            out['connection'] = self.config['connection_label']
            self.cache, self.cache_at = out, time.monotonic()
            return out


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def handle_error(self, request, client_address):
        pass  # Avoid traceback/request data in logs; clients see a failed refresh.


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    server_version = 'ServBench'
    sys_version = ''

    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    @property
    def app(self):
        return self.server.app

    def reply(self, code, body=b'', mime='application/json', cookie=None):
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                         "base-uri 'none'; form-action 'none'")
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(body)

    def host_ok(self):
        return self.headers.get('Host', '') in self.app.config['hosts']

    def authorized(self):
        config = self.app.config
        if config['mode'] == 'pi':
            value = self.headers.get('Authorization', '')
            return secrets.compare_digest(value, 'Bearer ' + config['transport_token'])
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            value = cookie['serv_bench'].value if 'serv_bench' in cookie else ''
            return secrets.compare_digest(value, config['viewer_token'])
        except Exception:
            return False

    def do_GET(self):
        if not self.host_ok():
            return self.reply(403)
        config = self.app.config
        if config['mode'] == 'relay' and self.path in ('/', '/app.js', '/style.css'):
            names = {'/': ('index.html', 'text/html; charset=utf-8'),
                     '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                     '/style.css': ('style.css', 'text/css; charset=utf-8')}
            name, mime = names[self.path]
            return self.reply(200, (HERE / name).read_bytes(), mime)
        route = '/snapshot' if config['mode'] == 'pi' else '/api/state'
        if self.path != route:
            return self.reply(404)
        if not self.authorized():
            return self.reply(401)
        try:
            return self.reply(200, json.dumps(self.app.snapshot(), allow_nan=False).encode())
        except (OSError, ValueError, TypeError):
            return self.reply(503, b'{"error":"Pi connection unavailable"}')

    def do_POST(self):
        if not self.host_ok():
            return self.reply(403)
        config = self.app.config
        try:
            size = int(self.headers.get('Content-Length', '-1'))
        except ValueError:
            return self.reply(400)
        if size < 0 or size > MAX_BODY or self.headers.get('Transfer-Encoding'):
            return self.reply(413)
        if config['mode'] == 'pi':
            if not secrets.compare_digest(self.path, '/hook/' + config['hook_token']):
                return self.reply(404)
            content_type = self.headers.get('Content-Type', '').lower()
            # Explicitly reject image/multipart uploads: this feed is text only.
            if content_type.split(';')[0] not in ('application/json', 'application/x-www-form-urlencoded'):
                return self.reply(415)
            from plate_reader.stream_engine import extract_json_field
            try:
                payload = extract_json_field(self.rfile.read(size), content_type)
                self.app.events.accept(payload, content_type)
            except (ValueError, TypeError, AttributeError):
                return self.reply(400)
            return self.reply(204)
        if self.path != '/api/session':
            return self.reply(404)
        if self.headers.get('Origin') != 'http://' + self.headers.get('Host', ''):
            return self.reply(403)
        try:
            data = json.loads(self.rfile.read(size))
            token = data.get('token') if isinstance(data, dict) else None
            if not isinstance(token, str) or not secrets.compare_digest(token, config['viewer_token']):
                return self.reply(401)
        except (ValueError, TypeError):
            return self.reply(400)
        self.reply(204, cookie='serv_bench=' + token + '; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    path = Path(args.config)
    if path.stat().st_mode & 0o077:
        parser.error('Private configuration must have mode 0600')
    config = json.loads(path.read_text())
    if config['mode'] not in ('pi', 'relay'):
        parser.error('Invalid mode')
    bind = ipaddress.ip_address(config['bind'])
    if config['mode'] == 'pi' and not bind.is_loopback:
        parser.error('Webhook listener must bind loopback')
    if config['mode'] == 'relay' and not (bind.is_private and not bind.is_unspecified):
        parser.error('Viewer must bind one specific private address')
    if config['mode'] == 'pi':
        sys.path.insert(0, config['app_root'])
    else:
        upstream = urllib.parse.urlsplit(config['upstream'])
        if upstream.scheme != 'http' or upstream.hostname != '127.0.0.1':
            parser.error('Relay upstream must be the loopback SSH tunnel')
    app = App(config)
    server = Server((config['bind'], config['port']), Handler)
    server.app = app
    if config['mode'] == 'pi':
        threading.Thread(target=app.refresh, daemon=True).start()
    print('Temporary bench viewer ready (' + config['mode'] + ')', flush=True)
    try:
        server.serve_forever()
    finally:
        app.stopped.set()
        server.server_close()


if __name__ == '__main__':
    main()
