#!/usr/bin/env python3
"""Capture every Plate Recognizer Stream webhook to JSONL for threshold tuning.

Records the full result object per read so the analysis can slice on any
field afterwards without re-running the 18-minute footage.
"""
import json
import time
import urllib.parse
from email.parser import BytesParser
from email.policy import default as _default
from http.server import BaseHTTPRequestHandler, HTTPServer

OUT = "/tmp/reads.jsonl"
T0 = time.time()
count = 0


def extract(body: bytes, ctype: str):
    ctype = (ctype or "").lower()
    if ctype.startswith("application/json"):
        return json.loads(body.decode("utf-8", "replace"))
    if ctype.startswith("application/x-www-form-urlencoded"):
        fields = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
        vals = fields.get("json")
        return json.loads(vals[0]) if vals else None
    if ctype.startswith("multipart/"):
        msg = BytesParser(policy=_default).parsebytes(
            b"Content-Type: " + ctype.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
        )
        for part in msg.iter_parts():
            if "name=\"json\"" in part.get("content-disposition", ""):
                return json.loads(part.get_payload(decode=True))
    return None


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        global count
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        try:
            payload = extract(body, self.headers.get("Content-Type", ""))
            data = (payload or {}).get("data", {})
            elapsed = time.time() - T0
            with open(OUT, "a") as fh:
                for r in data.get("results", []):
                    fh.write(json.dumps({
                        "elapsed_s": round(elapsed, 2),
                        "position_sec": r.get("position_sec"),
                        "timestamp": data.get("timestamp"),
                        "plate": r.get("plate"),
                        "score": r.get("score"),
                        "dscore": r.get("dscore"),
                        "region": (r.get("region") or {}).get("code"),
                        "region_score": (r.get("region") or {}).get("score"),
                        "vehicle_type": (r.get("vehicle") or {}).get("type"),
                        "vehicle_score": (r.get("vehicle") or {}).get("score"),
                        "candidates": r.get("candidates"),
                        "box": r.get("box"),
                    }) + "\n")
                    count += 1
            if count % 25 == 0:
                print(f"[{elapsed:7.1f}s] reads={count}", flush=True)
        except Exception as exc:                       # never drop the 200
            print("parse error:", exc, flush=True)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


open(OUT, "w").close()
print(f"capturing to {OUT}", flush=True)
HTTPServer(("127.0.0.1", 8129), Handler).serve_forever()
