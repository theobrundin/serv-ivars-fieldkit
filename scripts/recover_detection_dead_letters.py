#!/usr/bin/env python3
"""Safely requeue now-supported detection diagnostics.

Dry-run is the default. The deliberately excluded ``vehicle_only`` outcome is
ambiguous and must never be replayed as a vehicle or diagnostic automatically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from pathlib import Path


DEFAULT_DB = Path("/home/serv/plate_reader/queue.db")
SAFE_OUTCOMES = {
    "below_threshold",
    "unreadable",
    "group_confidence_floor",
}


def eligible_rows(conn: sqlite3.Connection):
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(dead_letter)")
    }
    recovered_clause = (
        "AND recovered_at IS NULL" if "recovered_at" in columns else ""
    )
    rows = conn.execute(
        "SELECT id, enqueued_at, endpoint, payload, idempotency_key "
        "FROM dead_letter WHERE endpoint = '/detection-event' "
        f"{recovered_clause} ORDER BY id"
    ).fetchall()
    result = []
    for row in rows:
        try:
            outcome = json.loads(row[3]).get("outcome")
        except (json.JSONDecodeError, TypeError):
            continue
        if outcome in SAFE_OUTCOMES:
            result.append((*row, outcome))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    uri = f"file:{args.db}?mode={'rw' if args.apply else 'ro'}"
    with sqlite3.connect(uri, uri=True, timeout=15) as conn:
        rows = eligible_rows(conn)
        counts: dict[str, int] = {}
        for *_, outcome in rows:
            counts[outcome] = counts.get(outcome, 0) + 1
        print(json.dumps({"eligible": len(rows), "outcomes": counts}, sort_keys=True))
        if not args.apply or not rows:
            return 0

        conn.execute("BEGIN IMMEDIATE")
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(dead_letter)")
        }
        if "recovered_at" not in columns:
            conn.execute("ALTER TABLE dead_letter ADD COLUMN recovered_at REAL")
        if "recovery_note" not in columns:
            conn.execute("ALTER TABLE dead_letter ADD COLUMN recovery_note TEXT")

        recovered = 0
        already_queued = 0
        recovery_time = time.time()
        for dead_id, enqueued_at, endpoint, payload, idem_key, outcome in rows:
            if not idem_key:
                idem_key = hashlib.sha256(
                    f"dead-letter:{dead_id}:{endpoint}:{payload}".encode("utf-8")
                ).hexdigest()
            duplicate = conn.execute(
                "SELECT 1 FROM queue WHERE idempotency_key = ? LIMIT 1",
                (idem_key,),
            ).fetchone()
            if duplicate:
                conn.execute(
                    "UPDATE dead_letter SET recovered_at = ?, recovery_note = ? "
                    "WHERE id = ?",
                    (time.time(), "already present in delivery queue", dead_id),
                )
                already_queued += 1
                continue
            conn.execute(
                "INSERT INTO queue "
                "(enqueued_at, endpoint, payload, attempts, last_attempt_at, "
                "last_status_code, next_attempt_at, priority, idempotency_key) "
                "VALUES (?, ?, ?, 0, NULL, NULL, 0, 20, ?)",
                # A recovery is a new delivery attempt. Reusing the original
                # timestamp would make an otherwise valid row immediately
                # eligible for the sender's queue TTL purge. The original
                # enqueue time remains preserved in dead_letter.
                (recovery_time, endpoint, payload, idem_key),
            )
            conn.execute(
                "UPDATE dead_letter SET recovered_at = ?, recovery_note = ? "
                "WHERE id = ?",
                (recovery_time, f"requeued supported outcome: {outcome}", dead_id),
            )
            recovered += 1
        conn.commit()
        print(json.dumps(
            {"already_queued": already_queued, "requeued": recovered},
            sort_keys=True,
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
