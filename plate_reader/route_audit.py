"""Bounded, plate-free native trajectory evidence for site calibration.

Kept on the Pi; no network sender. The allowlist must never include plate_key,
candidate strings, appearance descriptors, hashes or JPEG payloads.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time


def retain_track(directory, track, *, status, reason, association,
                 max_cases=200, max_age_seconds=86400):
    def timestamp(value):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
        except (AttributeError, ValueError, TypeError):
            return None
    rows = [[list(sample.vehicle_bbox) if sample.vehicle_bbox else None,
             timestamp(track.sample_observed_at[i]) if i < len(track.sample_observed_at) else None]
            for i, sample in enumerate(track.samples[:2048])]
    evidence = dict(first_seen=timestamp(track.first_seen_at), last_seen=timestamp(track.last_seen_at),
                    samples=rows, truncated=len(track.samples)>2048)
    # Content address of geometric evidence only, unrelated to vehicle identity.
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    data = dict(schema=1, received_at=datetime.now(timezone.utc).isoformat(),
                status=status, reason=reason, association=association, **evidence)
    blob = json.dumps(data, separators=(",", ":")).encode()
    if len(blob)>512*1024:
        return False
    folder=Path(directory);folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary=tempfile.mkstemp(prefix=".route-",suffix=".tmp",dir=folder)
    try:
        with os.fdopen(fd,"wb") as out:out.write(blob)
        os.replace(temporary,folder/f"route-{digest}.json")
    finally:
        if os.path.exists(temporary):os.unlink(temporary)
    cutoff=time.time()-max_age_seconds
    files=sorted(folder.glob("route-*.json"),key=lambda p:p.stat().st_mtime,reverse=True)
    for i,p in enumerate(files):
        if i>=max_cases or p.stat().st_mtime<cutoff:p.unlink(missing_ok=True)
    return True
