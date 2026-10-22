"""HTTP client + local SQLite retry/dead-letter queue.

Design:
- Every payload is persisted before delivery. Retryable failures use bounded
  exponential backoff with jitter; permanent 4xx failures move to a local
  dead-letter table so one bad payload cannot block every later visit.
- The bounded queue drops low-priority heartbeats before detection events or
  visits. This prevents an outage from allowing health chatter to evict the
  customer events the unit exists to collect.
- TTL: rows older than queue_ttl_hours are dropped on every pass — stale
  visit data isn't worth sending to the cloud.
- Every queued row owns a UUID idempotency key, sent in the Idempotency-Key
  header on every retry. Existing backends may ignore it; backends that honor
  it can safely collapse a timeout-after-success retry.
"""
import hashlib
import json
import random
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import requests

from .config import Config
from .log import get

log = get(__name__)

QUEUE_DB_PATH = Path("/home/serv/plate_reader/queue.db")

_PRIORITY = {
    "/heartbeat": 10,
    "/detection-event": 20,
    "/visit": 30,
}

_PERMANENT_4XX = {400, 404, 405, 410, 413, 415, 422}
_RETRYABLE_4XX = {401, 403, 408, 425, 429}


class Sender:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._writes_enabled = getattr(cfg, "backend_writes_enabled", True)
        self._db_path = (
            QUEUE_DB_PATH if self._writes_enabled else
            QUEUE_DB_PATH.with_name(
                f"{QUEUE_DB_PATH.stem}.dry-run{QUEUE_DB_PATH.suffix}"
            )
        )
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "x-device-id": cfg.device_id,
            "x-api-key": cfg.api_key,
            "User-Agent": f"serv-pi/{cfg.sw_version}",
        })
        self._stop = threading.Event()
        self._sender_thread: threading.Thread | None = None
        self._db_lock = threading.Lock()
        self._init_db()

    # ---------- DB ----------

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._db_lock, sqlite3.connect(self._db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    enqueued_at REAL NOT NULL,
                    endpoint TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at REAL,
                    last_status_code INTEGER,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    priority INTEGER NOT NULL DEFAULT 20,
                    idempotency_key TEXT
                )
            """)
            # Upgrade databases created by releases before retry scheduling.
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(queue)")
            }
            migrations = {
                "next_attempt_at": (
                    "ALTER TABLE queue ADD COLUMN "
                    "next_attempt_at REAL NOT NULL DEFAULT 0"
                ),
                "priority": (
                    "ALTER TABLE queue ADD COLUMN "
                    "priority INTEGER NOT NULL DEFAULT 20"
                ),
                "idempotency_key": (
                    "ALTER TABLE queue ADD COLUMN idempotency_key TEXT"
                ),
            }
            for name, statement in migrations.items():
                if name not in columns:
                    conn.execute(statement)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dead_letter (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_queue_id INTEGER,
                    enqueued_at REAL NOT NULL,
                    failed_at REAL NOT NULL,
                    endpoint TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    status_code INTEGER,
                    reason TEXT NOT NULL,
                    idempotency_key TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_enqueued ON queue(enqueued_at)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_queue_ready "
                "ON queue(next_attempt_at, priority, enqueued_at)"
            )
            conn.commit()
        # Payloads contain pseudonymous vehicle identifiers and operational
        # metadata. They must not be readable by other local accounts.
        try:
            self._db_path.chmod(0o600)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{self._db_path}{suffix}")
                if sidecar.exists():
                    sidecar.chmod(0o600)
        except OSError as exc:
            log.warning("could not restrict queue database permissions: %s", exc)

    def _enqueue(self, endpoint: str, payload: dict[str, Any]) -> None:
        max_size = self.cfg.behavior.queue_max_size
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        # One physical encounter owns one stable delivery identity. If a
        # caller accidentally enqueues its terminal /visit twice, the cloud
        # receives the same idempotency key and visitId instead of minting a
        # second anonymous vehicle. Other endpoints retain queue-row UUIDs.
        encounter_key = (
            payload.get("encounterId") if endpoint == "/visit" else None
        )
        idempotency_key = str(encounter_key or uuid.uuid4())
        priority = _PRIORITY.get(endpoint, 20)
        with self._db_lock, sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("SELECT COUNT(*) FROM queue")
            count = cur.fetchone()[0]
            if count >= max_size:
                # Never let a lower-priority new payload evict more important
                # work already persisted. If all existing rows outrank the
                # newcomer, drop the newcomer instead (typically a heartbeat
                # arriving while a visit-only outage backlog is full).
                cur = conn.execute(
                    "SELECT id, endpoint FROM queue "
                    "WHERE priority <= ? "
                    "ORDER BY priority ASC, enqueued_at ASC LIMIT 1",
                    (priority,),
                )
                old_id = cur.fetchone()
                if old_id is None:
                    log.warning(
                        "queue full (%d); dropped incoming %s rather than "
                        "evicting higher-priority rows", max_size, endpoint,
                    )
                    return
                conn.execute("DELETE FROM queue WHERE id = ?", (old_id[0],))
                log.warning(
                    "queue full (%d); dropped oldest %s row to make room",
                    max_size, old_id[1],
                )
            conn.execute(
                "INSERT INTO queue "
                "(enqueued_at, endpoint, payload, next_attempt_at, priority, "
                "idempotency_key) VALUES (?, ?, ?, 0, ?, ?)",
                (time.time(), endpoint, encoded, priority, idempotency_key),
            )
            conn.commit()

    def _purge_stale(self) -> int:
        """Drop rows older than queue_ttl_hours. Returns count dropped."""
        cutoff = time.time() - self.cfg.behavior.queue_ttl_hours * 3600
        with self._db_lock, sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("DELETE FROM queue WHERE enqueued_at < ?", (cutoff,))
            conn.commit()
            return cur.rowcount

    def _drain_one(self) -> bool:
        """Try one ready row. Return True only when a request was attempted."""
        if not self._writes_enabled:
            return False
        ready_at = time.time()
        with self._db_lock, sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "SELECT id, endpoint, payload, attempts, enqueued_at, "
                "idempotency_key FROM queue WHERE next_attempt_at <= ? "
                "ORDER BY priority DESC, enqueued_at ASC LIMIT 1",
                (ready_at,),
            )
            row = cur.fetchone()
            if row is None:
                return False
            row_id, endpoint, payload, attempts, enqueued_at, idem_key = row
            if not idem_key:
                # Old migrated rows still get a stable key before their first
                # post under this release.
                idem_key = hashlib.sha256(
                    f"{row_id}:{endpoint}:{payload}".encode("utf-8")
                ).hexdigest()
                conn.execute(
                    "UPDATE queue SET idempotency_key = ? WHERE id = ?",
                    (idem_key, row_id),
                )
                conn.commit()

        try:
            payload_obj = json.loads(payload)
        except json.JSONDecodeError as e:
            log.error("corrupt queue row %d, dropping: %s", row_id, e)
            self._delete(row_id)
            return True

        disposition, status = self._post(endpoint, payload_obj, idem_key)
        # Preserve compatibility with small test fakes written against the old
        # (bool, status) private return shape.
        if isinstance(disposition, bool):
            disposition = "success" if disposition else "retry"
        now = time.time()
        if disposition == "success":
            self._delete(row_id)
        elif disposition == "permanent":
            self._move_to_dead_letter(
                row_id=row_id,
                enqueued_at=enqueued_at,
                endpoint=endpoint,
                payload=payload,
                attempts=attempts + 1,
                status=status,
                reason="permanent_http_error",
                idempotency_key=idem_key,
            )
        else:
            next_attempt = now + self._retry_delay(attempts + 1)
            self._record_attempt(
                row_id, attempts + 1, now, status, next_attempt
            )
        return True

    @staticmethod
    def _retry_delay(attempts: int) -> float:
        """Bounded exponential backoff with 0-25% jitter."""
        base = min(300.0, 2.0 ** min(max(attempts, 1), 8))
        return base + random.uniform(0.0, base * 0.25)

    def _delete(self, row_id: int) -> None:
        with self._db_lock, sqlite3.connect(self._db_path) as conn:
            conn.execute("DELETE FROM queue WHERE id = ?", (row_id,))
            conn.commit()

    def _record_attempt(
        self,
        row_id: int,
        attempts: int,
        when: float,
        status: int | None,
        next_attempt: float,
    ) -> None:
        with self._db_lock, sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE queue SET attempts = ?, last_attempt_at = ?, "
                "last_status_code = ?, next_attempt_at = ? WHERE id = ?",
                (attempts, when, status, next_attempt, row_id),
            )
            conn.commit()

    def _move_to_dead_letter(
        self,
        *,
        row_id: int,
        enqueued_at: float,
        endpoint: str,
        payload: str,
        attempts: int,
        status: int | None,
        reason: str,
        idempotency_key: str,
    ) -> None:
        with self._db_lock, sqlite3.connect(self._db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO dead_letter "
                "(original_queue_id, enqueued_at, failed_at, endpoint, "
                "payload, attempts, status_code, reason, idempotency_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row_id, enqueued_at, time.time(), endpoint, payload, attempts,
                 status, reason, idempotency_key),
            )
            conn.execute("DELETE FROM queue WHERE id = ?", (row_id,))
            # A forensic tail is useful; an unbounded error archive is not.
            conn.execute(
                "DELETE FROM dead_letter WHERE id NOT IN "
                "(SELECT id FROM dead_letter ORDER BY id DESC LIMIT 1000)"
            )
            conn.commit()
        log.error(
            "moved queue row %d (%s) to dead letter after status=%s",
            row_id, endpoint, status,
        )

    def queue_size(self) -> int:
        with self._db_lock, sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("SELECT COUNT(*) FROM queue")
            return cur.fetchone()[0]

    def dead_letter_size(self) -> int:
        with self._db_lock, sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("SELECT COUNT(*) FROM dead_letter")
            return cur.fetchone()[0]

    def queue_stats(self) -> dict[str, int | float]:
        """Non-sensitive queue health for heartbeat/operations."""
        with self._db_lock, sqlite3.connect(self._db_path) as conn:
            now = time.time()
            queued, oldest, max_attempts = conn.execute(
                "SELECT COUNT(*), MIN(enqueued_at), COALESCE(MAX(attempts), 0) "
                "FROM queue"
            ).fetchone()
            dead = conn.execute(
                "SELECT COUNT(*) FROM dead_letter"
            ).fetchone()[0]
        return {
            "queued": int(queued),
            "dead_letter": int(dead),
            "oldest_age_seconds": (
                round(max(0.0, now - oldest), 1) if oldest is not None else 0.0
            ),
            "max_attempts": int(max_attempts),
        }

    # ---------- HTTP ----------

    def _post(
        self,
        endpoint: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> tuple[str, int | None]:
        if not self._writes_enabled:
            return "disabled", None
        url = f"{self.cfg.api_base}{endpoint}"
        try:
            headers = (
                {"Idempotency-Key": idempotency_key}
                if idempotency_key else None
            )
            resp = self._session.post(
                url, data=json.dumps(payload), headers=headers, timeout=10.0
            )
            status = resp.status_code
            if 200 <= status < 300:
                return "success", status
            if status in {401, 403}:
                log.error(
                    "auth rejected by cloud (%d) for %s — device may be revoked. "
                    "payload will be retried.", status, endpoint,
                )
            elif status >= 500:
                log.warning("cloud 5xx for %s: status=%d", endpoint, status)
            else:
                log.warning("cloud 4xx for %s: status=%d", endpoint, status)
            if status in _PERMANENT_4XX or (
                400 <= status < 500 and status not in _RETRYABLE_4XX
            ):
                return "permanent", status
            return "retry", status
        except requests.RequestException as e:
            log.debug("post %s failed: %s", endpoint, e)
            return "retry", None

    # ---------- Public API ----------

    def auth_preflight(self) -> bool:
        """Send a single trivial heartbeat to verify auth works.
        Called at startup. Returns False on definite auth failure (401/403)
        so the caller can refuse to start. Network failures are not auth
        failures and we let those slide (the unit may boot offline)."""
        if not self._writes_enabled:
            log.warning("backend writes disabled; events remain in the separate local dry-run queue")
            return True
        url = f"{self.cfg.api_base}/heartbeat"
        try:
            resp = self._session.post(url, data="{}", timeout=10.0)
        except requests.RequestException as e:
            log.warning("auth preflight could not reach cloud: %s (proceeding anyway)", e)
            return True  # assume not an auth issue
        if resp.status_code in {401, 403}:
            log.error(
                "auth preflight failed with %d — device_id=%s api_key looks rejected. "
                "Check the Devices table in DynamoDB.",
                resp.status_code, self.cfg.device_id,
            )
            return False
        if 200 <= resp.status_code < 300:
            log.info("auth preflight OK (status %d)", resp.status_code)
        else:
            log.warning("auth preflight non-2xx status %d: %s",
                        resp.status_code, resp.text[:200])
        return True

    def send_visit(self, payload: dict[str, Any]) -> None:
        """Enqueue one terminal vehicle visit, identified or anonymous."""
        # Add a few last-mile fields that we always want on a /visit payload
        full = dict(payload)
        full.setdefault("locationId", self.cfg.location_id)
        full.setdefault("swVersion", self.cfg.sw_version)
        self._enqueue("/visit", full)

    def send_detection_event(self, payload: dict[str, Any]) -> None:
        """Enqueue a failed identification."""
        full = dict(payload)
        full.setdefault("locationId", self.cfg.location_id)
        full.setdefault("swVersion", self.cfg.sw_version)
        self._enqueue("/detection-event", full)

    def send_heartbeat(self, payload: dict[str, Any]) -> None:
        """Enqueue a heartbeat. Heartbeats are also queued so they aren't lost
        during transient outages — a backlogged unit will spit out its
        accumulated heartbeats when network returns, giving cloud-side a
        retroactive picture of what was happening."""
        self._enqueue("/heartbeat", payload)

    # ---------- Sender thread ----------

    def _sender_loop(self) -> None:
        log.info("sender thread started; queue size = %d", self.queue_size())
        last_purge = 0.0
        while not self._stop.is_set():
            try:
                # Periodically purge stale
                if time.time() - last_purge > 300:  # every 5 min
                    dropped = self._purge_stale()
                    if dropped:
                        log.warning("purged %d stale queue rows (older than TTL)", dropped)
                    last_purge = time.time()

                did_work = self._drain_one()
                if not did_work:
                    # Queue empty — wait a bit before checking again
                    self._stop.wait(1.0)
            except Exception as e:
                log.exception("sender loop error: %s", e)
                self._stop.wait(2.0)
        log.info("sender thread stopped")

    def start(self) -> None:
        if self._sender_thread is not None:
            return
        self._sender_thread = threading.Thread(
            target=self._sender_loop, name="sender", daemon=True
        )
        self._sender_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sender_thread is not None:
            self._sender_thread.join(timeout=5.0)
