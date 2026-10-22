"""Pi-only synthetic AWS test, restricted to the Ivar's sandbox identity.

Exercises the installed PR payload parser, unchanged normalization/hash, real
durable Sender, HTTP authentication, retry identity and customer API. This is
NOT an optical test and never inserts a synthetic visit into the real site.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import time
import uuid


def smoke(identity_path, report_path):
    from fieldkit.host import private_json
    identity = private_json(identity_path)
    if (identity.get('device_id'), identity.get('location_id')) != (
            'serv-ivars-001-sandbox', 'ivars-001-sandbox'):
        raise ValueError('Cloud smoke is sandbox-only')
    if identity.get('api_base') != 'https://sb13iibr7f.execute-api.us-east-1.amazonaws.com':
        raise ValueError('Unexpected cloud endpoint')
    import requests
    from plate_reader.config import load
    from plate_reader.normalizer import normalize
    from plate_reader.hasher import hash_plate
    from plate_reader.stream_engine import parse_webhook_payload
    from plate_reader import sender as sender_mod
    with tempfile.TemporaryDirectory(prefix='serv-cloud-smoke-') as td:
        root = Path(td)
        cfg_raw = private_json('/home/serv/plate_reader/config.json')
        cfg_raw.update({k: identity[k] for k in ('device_id', 'location_id', 'api_key', 'api_base')})
        cfg_raw['backend_writes_enabled'] = True
        cfg_path = root / 'config.json'; cfg_path.write_text(json.dumps(cfg_raw)); cfg_path.chmod(0o600)
        cfg = load(cfg_path)
        original_queue_path = sender_mod.QUEUE_DB_PATH
        sender_mod.QUEUE_DB_PATH = root / 'queue.db'
        sender = sender_mod.Sender(cfg)
        sender_mod.QUEUE_DB_PATH = original_queue_path
        candidate = 'IV' + secrets.token_hex(5).upper()
        stamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        event = {'data': {'timestamp': stamp, 'results': [{'plate': candidate.lower(),
            'score': 0.999, 'dscore': 0.99,
            'box': {'xmin': 100, 'ymin': 100, 'xmax': 250, 'ymax': 150},
            'vehicle': {'type': 'Car', 'score': 0.99},
            'model_make': [{'make': 'Sandbox', 'model': 'Synthetic', 'score': 0.99}],
            'color': [{'color': 'white', 'score': 0.99}]}]}}
        reads = parse_webhook_payload(event)
        assert len(reads) == 1
        digest = hash_plate(normalize(reads[0].text))
        visit_id = 'sandbox-' + str(uuid.uuid4())
        payload = dict(licensePlate=digest, visitId=visit_id, encounterId=visit_id,
            detectedAt=stamp, vehicleDetected=True, plateDetection='recognized',
            make=reads[0].make, model=reads[0].model, color=reads[0].color,
            vehicleType='Car', makeConfidence=0.99, modelConfidence=0.99,
            colorConfidence=0.99, detectionConfidence=0.99, ocrConfidence=0.999,
            frameCount=1, detectionOutcome='confirmed')
        assert candidate not in json.dumps(payload) and reads[0].text not in json.dumps(payload)
        sender.send_visit(payload)
        with sqlite3.connect(sender._db_path) as db:
            saved, idem = db.execute('SELECT payload,idempotency_key FROM queue').fetchone()
        start = time.monotonic()
        sender._drain_one()
        first_ms = round((time.monotonic() - start) * 1000)
        assert sender.queue_size() == 0, 'Sandbox request did not drain successfully'
        duplicate = sender._session.post(cfg.api_base + '/visit', json=json.loads(saved),
            headers={'Idempotency-Key': idem}, timeout=20)
        assert duplicate.status_code == 200 and duplicate.json().get('duplicate') is True
        wrong = requests.post(cfg.api_base + '/heartbeat', json={}, headers={
            'x-device-id': cfg.device_id, 'x-api-key': '0' * 64}, timeout=15)
        assert wrong.status_code in (401, 403), 'Invalid credential was not rejected'
        forbidden = sender._session.get(cfg.api_base + '/customers', params={
            'location_id': 'ivars-001'}, timeout=15)
        assert forbidden.status_code == 403, 'Sandbox key could read another location'
        found = None
        for _ in range(10):
            response = sender._session.get(cfg.api_base + '/customers', params={
                'location_id': cfg.location_id, 'search': digest}, timeout=15)
            response.raise_for_status()
            found = next((c for c in response.json().get('customers', []) if c.get('vehicle_hash') == digest), None)
            if found:
                break
            time.sleep(1)
        assert found and found.get('visits') == 1, 'Customer missing or duplicate changed visit count'
        assert found.get('make') == 'Sandbox' and found.get('model') == 'Synthetic'
        assert found.get('color') == 'white'
        report = dict(synthetic=True, scope=cfg.location_id, at=stamp, vehicle_hash=digest,
            visit_id=visit_id, parser_hash_sender_cloud=True, customer_verified=True,
            mmc_verified=True, duplicate_idempotent=True, invalid_key_rejected=True,
            cross_location_read_rejected=True, first_request_ms=first_ms,
            raw_text_exported=False, queue_drained=True,
            limitations=['Synthetic input bypasses camera OCR, detector voting and the running app.',
                         'Does not establish field optics, day/night recall or physical route calibration.'])
        report_path.write_text(json.dumps(report, indent=2) + '\n'); report_path.chmod(0o600)
        print(json.dumps({k: v for k, v in report.items() if k not in ('vehicle_hash', 'visit_id')}, indent=2))


if __name__ == '__main__':
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--identity', type=Path, required=True)
    p.add_argument('--report', type=Path, required=True)
    a = p.parse_args()
    smoke(a.identity, a.report)
