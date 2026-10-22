#!/usr/bin/env python3
"""Local synthetic smoke test, explicitly NOT evidence of camera recognition."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import socket
import sqlite3
import time
import urllib.request

if socket.gethostname().lower() != 'servtest':
    raise SystemExit('Expected home bench Pi')
cfg = json.loads(Path('/home/serv/plate_reader/config.json').read_text())
if cfg.get('backend_writes_enabled') is not False or cfg.get('device_id') != 'serv-unit-bench-reolink':
    raise SystemExit('Synthetic test requires isolated bench identity and disabled backend')
candidate = 'BENCH' + secrets.token_hex(3).upper()
digest = hashlib.sha256(candidate.encode()).hexdigest()
stamp = datetime.now(timezone.utc).isoformat()
payload = {'hook': {'event': 'recognition', 'id': 'synthetic-bench-test'},
           'data': {'camera_id': 'synthetic-bench-test', 'timestamp': stamp,
                    'results': [{'plate': candidate.lower(), 'dscore': 0.99, 'score': 0.999,
                      'box': {'xmin': 2400, 'ymin': 1500, 'xmax': 2600, 'ymax': 1570},
                      'vehicle': {'type': 'Car', 'score': 0.99,
                                  'box': {'xmin': 1900, 'ymin': 1000, 'xmax': 2800, 'ymax': 1800}},
                      'model_make': [{'make': 'Bench', 'model': 'Synthetic', 'score': 0.99}],
                      'color': [{'color': 'white', 'score': 0.99}]}]}}


def post():
    request = urllib.request.Request('http://127.0.0.1:8129/', data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200


def matches():
    with sqlite3.connect('file:/home/serv/plate_reader/queue.dry-run.db?mode=ro', uri=True) as conn:
        return [json.loads(row[0]) for row in conn.execute("SELECT payload FROM queue WHERE endpoint='/visit'")
                if json.loads(row[0]).get('licensePlate') == digest]


post()
for _ in range(20):
    time.sleep(0.5)
    if matches():
        break
assert len(matches()) == 1, 'Expected one correctly hashed local Visit'
post()
time.sleep(5)
assert len(matches()) == 1, 'Duplicate webhook produced extra Visit'
result = {'synthetic': True, 'hash_verified': True, 'duplicate_suppressed': True,
          'timestamp': stamp, 'expected_hash': digest,
          'mmc_preserved': matches()[0].get('make') == 'Bench'}
path = Path('/home/serv/reolink-bench/synthetic-smoke.json')
path.write_text(json.dumps(result, indent=2) + '\n')
path.chmod(0o600)
result.pop('expected_hash')
print(json.dumps(result))
