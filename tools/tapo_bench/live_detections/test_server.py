"""Synthetic-only tests; never post fixtures to the production listener."""
import copy
import http.client
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('live_detections', Path(__file__).with_name('server.py'))
viewer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(viewer)
from plate_reader.hasher import hash_plate

NOW = 1792609200.0


def result(plate='TEST123', x=100, ocr=.99):
    return {'plate': {'score': .9, 'box': {'xmin': x+30, 'ymin': 150, 'xmax': x+80, 'ymax': 170},
                      'props': {'plate': [{'value': plate, 'score': ocr}]}},
            'vehicle': {'score': .95, 'type': 'Sedan',
                        'box': {'xmin': x, 'ymin': 50, 'xmax': x+200, 'ymax': 220},
                        'props': {'make_model': [{'make': 'Example', 'model': 'Sedan', 'score': .8}],
                                  'color': [{'value': 'Silver', 'score': .7}]}}}


def payload(*results, stamp=NOW):
    return {'hook': {'event': 'recognition'},
            'data': {'camera_id': 'tapo-bench', 'timestamp': viewer.utc(stamp),
                     'results': list(results or [result()])}}


def visit(plate='TEST123', stamp=NOW):
    return {'licensePlate': hash_plate(plate), 'detectedAt': viewer.utc(stamp),
            'detectionOutcome': 'confirmed'}


class EventTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.events = viewer.Events(clock=lambda: self.now, limit=2, ttl=30)

    def test_candidate_is_not_confirmation_even_at_high_scores(self):
        self.events.accept(payload())
        row = self.events.snapshot()['rows'][0]
        self.assertFalse(row['confirmed'])
        self.assertEqual(row['candidates'][0]['plate'], 'TEST123')
        self.assertNotIn('_hash', json.dumps(row))
        self.assertNotIn(hash_plate('TEST123'), json.dumps(row))

    def test_confirmation_needs_exact_timestamp_and_hash(self):
        self.events.accept(payload())
        for wrong in [visit(stamp=NOW-1), visit('OTHER99'), {**visit(), 'detectionOutcome': 'insufficient_confidence'}]:
            self.assertFalse(self.events.snapshot([wrong])['rows'][0]['confirmed'])
        self.assertTrue(self.events.snapshot([visit()])['rows'][0]['confirmed'])

    def test_ambiguous_same_plate_same_time_not_confirmed(self):
        self.events.accept(payload(result(), result(x=500)))
        self.assertFalse(any(r['confirmed'] for r in self.events.snapshot([visit()])['rows']))

    def test_later_ambiguity_or_missing_queue_cannot_keep_confirmation(self):
        self.events.accept(payload())
        self.assertTrue(self.events.snapshot([visit()])['rows'][0]['confirmed'])
        self.assertFalse(self.events.snapshot()['rows'][0]['confirmed'])
        self.events.accept(payload(result(x=500)))
        self.assertFalse(any(r['confirmed'] for r in self.events.snapshot([visit()])['rows']))

    def test_same_vehicle_alternative_plate_one_reading(self):
        self.assertEqual(self.events.accept(payload(result(), result('OTHER99'))), 1)
        self.assertEqual(len(self.events.snapshot()['rows'][0]['candidates']), 2)

    def test_two_vehicle_boxes_remain_separate(self):
        self.assertEqual(self.events.accept(payload(result(), result('OTHER99', x=500))), 2)
        self.assertEqual(len(self.events.snapshot()['rows']), 2)

    def test_exact_retry_dedup_and_later_read_is_new(self):
        p = payload()
        self.events.accept(p)
        self.assertEqual(self.events.accept(copy.deepcopy(p)), 0)
        self.now += 1
        self.assertEqual(self.events.accept(payload(stamp=self.now)), 1)

    def test_plate_free_vehicle_and_bad_entries(self):
        vehicle = result(); vehicle.pop('plate')
        bad = result(); bad['plate']['props'] = 'bad'
        self.events.accept(payload(vehicle, bad, {'plate': None}))
        rows = self.events.snapshot()['rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['candidates'], [])

    def test_ttl_and_capacity_bound_memory(self):
        for i in range(12):
            self.events.accept(payload(result(f'TEST{i:03}'), stamp=NOW+i/10))
        self.assertEqual(len(self.events.rows), 2)
        self.assertLessEqual(len(self.events.seen), 10)
        self.now += 31
        self.assertEqual(self.events.snapshot()['rows'], [])
        self.assertEqual(len(self.events.seen), 0)

    def test_dwell_and_wrong_camera_do_not_add_readings(self):
        dwell = payload(); dwell['hook']['event'] = 'dwell'
        self.events.accept(dwell)
        other = payload(); other['data']['camera_id'] = 'production'
        self.events.accept(other)
        self.assertEqual(self.events.snapshot()['rows'], [])

    def test_bad_clock_cannot_match_confirmation(self):
        self.events.accept(payload(stamp=NOW-86400))
        row = self.events.snapshot([visit(stamp=NOW-86400)])['rows'][0]
        self.assertFalse(row['providerTimeValid'])
        self.assertFalse(row['confirmed'])
        self.assertEqual(row['observedAt'], viewer.utc(NOW))

    def test_read_only_queue_excludes_prior_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'queue.db'
            with sqlite3.connect(path) as conn:
                conn.execute('CREATE TABLE queue(id INTEGER, endpoint TEXT, payload TEXT, enqueued_at REAL)')
                for i, stamp in enumerate([NOW-1, NOW+1]):
                    conn.execute('INSERT INTO queue VALUES (?, ?, ?, ?)',
                                 (i, '/visit', json.dumps(visit(stamp=stamp)), stamp))
            self.assertEqual(len(viewer.queue_visits(path, NOW)), 1)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.app = viewer.App({'mode':'pi', 'hook_token':'hook-secret', 'transport_token':'transport-secret', 'hosts':[]})
        self.server = viewer.Server(('127.0.0.1', 0), viewer.Handler)
        self.port = self.server.server_port
        self.app.config['hosts'] = [f'127.0.0.1:{self.port}']
        self.server.app = self.app
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        conn.request(method, path, body, headers or {})
        response = conn.getresponse(); data = response.read()
        result = (response.status, dict(response.getheaders()), data)
        conn.close(); return result

    def test_auth_host_no_image_and_urlencoded_live_format(self):
        self.assertEqual(self.request('GET', '/snapshot')[0], 401)
        self.assertEqual(self.request('GET', '/snapshot', headers={'Host':'evil.test'})[0], 403)
        self.assertEqual(self.request('POST', '/hook/wrong', '{}')[0], 404)
        self.assertEqual(self.request('POST', '/hook/hook-secret', 'image', {'Content-Type':'multipart/form-data'})[0], 415)
        body = urlencode({'json':json.dumps(payload(stamp=viewer.time.time()))})
        self.assertEqual(self.request('POST', '/hook/hook-secret', body, {'Content-Type':'application/x-www-form-urlencoded'})[0], 204)
        code, headers, body = self.request('GET', '/snapshot', headers={'Authorization':'Bearer transport-secret'})
        self.assertEqual(code, 200); self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(len(json.loads(body)['rows']), 1)
        self.assertNotIn(b'upload', body)

    def test_bad_payload_returns_400_and_server_survives(self):
        self.assertEqual(self.request('POST', '/hook/hook-secret', '[]', {'Content-Type':'application/json'})[0], 400)
        self.assertEqual(self.request('GET', '/snapshot', headers={'Authorization':'Bearer transport-secret'})[0], 200)

    def test_relay_session_cookie_origin_and_failed_upstream(self):
        self.app.config.update(mode='relay', viewer_token='viewer-secret',
                               upstream='http://127.0.0.1:1', connection_label='Test')
        body = json.dumps({'token':'viewer-secret'})
        self.assertEqual(self.request('POST', '/api/session', body)[0], 403)
        code, headers, _ = self.request('POST', '/api/session', body,
                                       {'Origin':f'http://127.0.0.1:{self.port}'})
        self.assertEqual(code, 204)
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertEqual(self.request('GET', '/api/state')[0], 401)
        self.assertEqual(self.request('GET', '/api/state', headers={'Cookie':'serv_bench=viewer-secret'})[0], 503)
        self.assertEqual(self.request('GET', '/../../server.py')[0], 404)
        self.assertEqual(self.request('GET', '/')[0], 200)


if __name__ == '__main__':
    unittest.main()
