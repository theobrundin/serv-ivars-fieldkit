"""Regression tests for production failures found in the August 2026 audit.

No camera, SDK, network, or cloud credentials are required.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import stat
import sys
import tempfile
import time
import types
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The Codex test runtime deliberately has no third-party requests package.
# Supply only the import surface Sender needs; production installs requests.
try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    fake_requests = types.ModuleType("requests")

    class _Session:
        def __init__(self):
            self.headers = {}

    fake_requests.Session = _Session
    fake_requests.RequestException = Exception
    sys.modules["requests"] = fake_requests

from plate_reader.config import ConfigError, load  # noqa: E402
from plate_reader.config import Presence  # noqa: E402
from plate_reader.detector import (  # noqa: E402
    ConfirmedIdentification,
    DetectionResult,
    Detector,
)
from plate_reader.heartbeat import Heartbeat  # noqa: E402
from plate_reader.engines import RawPlateRead  # noqa: E402
from plate_reader.hasher import hash_plate  # noqa: E402
import plate_reader.sender as sender_mod  # noqa: E402
from plate_reader.presence import PresenceTracker  # noqa: E402
from plate_reader.telemetry import DetectionCounters  # noqa: E402


results = []


def check(name, got, want):
    ok = got == want
    results.append((name, ok, got, want))
    print(f"{'PASS' if ok else 'FAIL'}  {name} (got={got!r} want={want!r})")


# 1. Heartbeat must join cleanly; Thread._stop may not be shadowed.
class _HeartbeatSender:
    def send_heartbeat(self, payload):
        pass


class _Counters:
    def snapshot_and_reset(self):
        return {}


hb_cfg = SimpleNamespace(
    telemetry=SimpleNamespace(include_tailscale=False, include_cellular=False),
    behavior=SimpleNamespace(heartbeat_interval_seconds=999),
)
heartbeat = Heartbeat(hb_cfg, _HeartbeatSender(), _Counters())
heartbeat.start()
heartbeat.stop()
heartbeat.join(timeout=1)
check("heartbeat joins without corrupting Thread._stop", heartbeat.is_alive(), False)


# 2. A permanent 400 is dead-lettered and cannot block the following visit.
with tempfile.TemporaryDirectory(prefix="serv-sender-test-") as tmp_dir:
    sender_mod.QUEUE_DB_PATH = pathlib.Path(tmp_dir) / "queue.db"
    cfg = SimpleNamespace(
        device_id="dev", api_key="key", sw_version="test",
        api_base="https://invalid", location_id="loc",
        behavior=SimpleNamespace(queue_max_size=20, queue_ttl_hours=24),
    )
    sender = sender_mod.Sender(cfg)
    check(
        "queue database is owner-only",
        stat.S_IMODE(sender_mod.QUEUE_DB_PATH.stat().st_mode),
        0o600,
    )
    sender.send_visit({"seq": 1})
    sender.send_visit({"seq": 2})
    dispositions = iter([("permanent", 400), ("success", 200)])
    sender._post = lambda endpoint, payload, idem: next(dispositions)
    sender._drain_one()
    sender._drain_one()
    check("permanent 4xx does not head-of-line block", sender.queue_size(), 0)
    check("permanent 4xx is retained for forensics", sender.dead_letter_size(), 1)


# 3. A retryable first row is scheduled into the future, allowing row two.
with tempfile.TemporaryDirectory(prefix="serv-sender-retry-") as tmp_dir:
    sender_mod.QUEUE_DB_PATH = pathlib.Path(tmp_dir) / "queue.db"
    sender = sender_mod.Sender(cfg)
    sender.send_visit({"seq": 1})
    sender.send_visit({"seq": 2})

    def _post(endpoint, payload, idem):
        return ("retry", 503) if payload["seq"] == 1 else ("success", 200)

    sender._post = _post
    sender._drain_one()
    sender._drain_one()
    with sqlite3.connect(sender_mod.QUEUE_DB_PATH) as conn:
        queued = conn.execute(
            'SELECT json_extract(payload, "$.seq"), attempts '
            "FROM queue ORDER BY id"
        ).fetchall()
    check("backoff lets later ready work proceed", queued, [(1, 1)])


# 4. A low-priority heartbeat may never evict queued customer visits.
with tempfile.TemporaryDirectory(prefix="serv-sender-priority-") as tmp_dir:
    sender_mod.QUEUE_DB_PATH = pathlib.Path(tmp_dir) / "queue.db"
    priority_cfg = SimpleNamespace(
        device_id="dev", api_key="key", sw_version="test",
        api_base="https://invalid", location_id="loc",
        behavior=SimpleNamespace(queue_max_size=2, queue_ttl_hours=24),
    )
    sender = sender_mod.Sender(priority_cfg)
    sender.send_visit({"seq": 1})
    sender.send_visit({"seq": 2})
    sender.send_heartbeat({"heartbeat": True})
    with sqlite3.connect(sender_mod.QUEUE_DB_PATH) as conn:
        endpoints = conn.execute(
            "SELECT endpoint FROM queue ORDER BY id"
        ).fetchall()
    check("heartbeat cannot evict a full visit-only backlog",
          endpoints, [("/visit",), ("/visit",)])

    sender._session.post = lambda *args, **kwargs: SimpleNamespace(
        status_code=403, text="forbidden"
    )
    check("403 fails auth preflight instead of discarding future visits",
          sender.auth_preflight(), False)


# 5. Authoritative overrides must still pass safety validation; booleans strict.
example = json.loads((ROOT / "config.example.json").read_text())
example["device_id"] = "serv-unit-test"
example["api_key"] = "a" * 64
example["location_id"] = "test-location"
example["alpr"]["license_token_file"] = None

with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
    invalid = json.loads(json.dumps(example))
    invalid["presence"]["vote_min_frames"] = 1
    json.dump(invalid, handle)
    handle.flush()
    try:
        load(pathlib.Path(handle.name))
        rejected = False
    except ConfigError:
        rejected = True
check("one-frame override is rejected after merge", rejected, True)

with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
    invalid = json.loads(json.dumps(example))
    invalid["alpr"]["roi"]["enabled"] = "false"
    json.dump(invalid, handle)
    handle.flush()
    try:
        load(pathlib.Path(handle.name))
        rejected = False
    except ConfigError:
        rejected = True
check("quoted false cannot become true", rejected, True)


def _candidate(epoch: float) -> DetectionResult:
    return DetectionResult(
        raw_text="ABC123", normalized="ABC123",
        detection_confidence=0.99, ocr_confidence=0.99,
        bbox=(1, 1, 10, 10), inference_ms=5,
        captured_at_epoch=epoch, captured_at_iso="x", in_zone=True,
        in_dwell_zone=True, below_threshold=False,
    )


def _detector(min_zone_seconds=0.0):
    detector = Detector.__new__(Detector)
    detector.cfg = SimpleNamespace(
        thresholds=SimpleNamespace(
            vote_window_seconds=30.0, vote_min_frames=2,
            high_ocr_single_frame=0.0,
            group_detection_floor=0.9, group_ocr_floor=0.9,
            dwell_release_requires_absent_frames=3,
            dwell_suppress_seconds=60.0,
        ),
        dedup=SimpleNamespace(
            release_requires_absent_seconds=0.0,
            cooldown_seconds=0.0, near_match_max_distance=0,
        ),
        dwell_zone=SimpleNamespace(min_seconds_in_zone=min_zone_seconds),
        alpr=SimpleNamespace(
            classifiers=SimpleNamespace(vehicle_detect_enabled=False)
        ),
        diagnostics=SimpleNamespace(save_miss_plates=False),
    )
    detector._last_confirm_epoch = {}
    detector._last_confirm_encounter = {}
    detector._pending_miss = {}
    detector._last_miss_emit_epoch = {}
    detector._enable_vehicle_only_tracking = False
    detector._zone_entry_epoch = {}
    detector._vehicle_only_pending = {}
    detector._vehicle_only_seq = 0
    detector._warned_vehicle_only_no_zone = False
    detector._capture_seq = 0
    detector._presence_dumper = None
    detector._engine = None
    detector.vehicle_only_seen_raw = 0
    detector.vehicle_only_static_dropped = 0
    detector._buckets = {}
    detector._rescue_buckets = {}
    detector._confirmed_encounter_epoch = {}
    detector._dwell = {}
    detector._frame_wh = (100, 100)
    return detector


# 6. Duplicate SDK boxes from one capture count as one vote.
detector = _detector()
now = time.time()
frames = iter([[_candidate(now), _candidate(now)], [_candidate(now + 0.2)]])
detector._run_one = lambda frame: next(frames)
_, first = detector.process_frame(object())
_, second = detector.process_frame(object())
check("one capture with duplicate boxes cannot confirm", len(first), 0)
check(
    "second independent capture confirms",
    sum(isinstance(item, ConfirmedIdentification) for item in second),
    1,
)


# 7. A missing capture breaks continuous dwell time.
detector = _detector(min_zone_seconds=10.0)
frames = iter([[_candidate(1000.0)], [], [_candidate(1020.0)]])
detector._run_one = lambda frame: next(frames)
detector.process_frame(object())
detector.process_frame(object())
detector.process_frame(object())
check("dwell entry resets across absent capture",
      detector._zone_entry_epoch.get((None, "ABC123")), 1020.0)


# 8. Every emitted diagnostic has a heartbeat counter; otherwise a backend
# contract mismatch is invisible until someone audits the dead-letter DB.
counters = DetectionCounters()
counters.record_failure("below_threshold")
counters.record_failure("unreadable")
counter_snapshot = counters.snapshot_and_reset()
check(
    "below-threshold misses reach heartbeat telemetry",
    counter_snapshot["failed_below_threshold_since_last"], 1,
)
check(
    "unreadable misses reach heartbeat telemetry",
    counter_snapshot["failed_unreadable_since_last"], 1,
)


# 9. Production presence owns plateless vehicles, with vehicle boxes as direct
# evidence and duplicate SDK boxes collapsed inside a capture.
import numpy as np  # noqa: E402

presence_cfg = Presence(
    enabled=True,
    iou_match_threshold=0.3,
    track_timeout_seconds=1.0,
    min_track_frames=2,
    motion_area_fraction=0.12,
    color_enabled=False,
    color_split_distance=0.55,
    motion_boxes_enabled=False,
    driveoff_requires_plate=True,
)
tracker = PresenceTracker(SimpleNamespace(
    presence=presence_cfg,
    detection_zone=SimpleNamespace(
        enabled=False, x_min_pct=0.0, x_max_pct=1.0,
        y_min_pct=0.0, y_max_pct=1.0,
    ),
))
frame = np.zeros((120, 160, 3), dtype="uint8")
vehicle_only_candidate = SimpleNamespace(
    bbox=(0, 0, 0, 0), vehicle_bbox=(20, 20, 120, 100),
    detection_confidence=0.0, ocr_confidence=0.0,
    normalized=None, in_zone=True,
)
tracker.observe(
    frame, [vehicle_only_candidate, vehicle_only_candidate], now=100.0
)
check("duplicate vehicle boxes create one presence track",
      tracker.active_track_count(), 1)
tracker.observe(frame, [vehicle_only_candidate], now=100.2)
driveoffs = list(tracker.reap(now=102.0))
check("SDK vehicle box counts a plateless car without motion guessing",
      len(driveoffs), 1)
check("plateless classification remains explicit",
      driveoffs[0].had_plate_candidate if driveoffs else None, False)

with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
    json.dump(example, handle)
    handle.flush()
    production_cfg = load(pathlib.Path(handle.name))

zone_detector = Detector(
    production_cfg, enable_vehicle_only_tracking=False
)
zone_detector._engine = SimpleNamespace(infer=lambda _frame: [RawPlateRead(
    text="", detection_confidence=0.0, ocr_confidence=0.0,
    bbox=(0, 0, 0, 0), vehicle_bbox=(20, 20, 120, 100),
    vehicle_confidence=0.99,
)])
zone_candidate = zone_detector._run_one(frame)[0]
check("vehicle box, not zero plate box, controls the detection zone",
      zone_candidate.in_zone, True)

try:
    hash_plate("abc 123")
    raw_hash_rejected = False
except ValueError:
    raw_hash_rejected = True
check("hasher refuses text that bypassed normalization",
      raw_hash_rejected, True)


failed = [item for item in results if not item[1]]
print(f"{len(results) - len(failed)}/{len(results)} passed")
raise SystemExit(1 if failed else 0)
