#!/usr/bin/env python3
"""Fail-closed patch for the deployed Heartbeat Lambda.

Heartbeat predates the repository handoff, so deployment first downloads the
live package and adds fields at one exact, reviewed anchor. A changed or
already-unexpected handler is rejected instead of being overwritten.
"""

from __future__ import annotations

import argparse
from pathlib import Path


ANCHOR = '            "sw_version":     str(body.get("sw_version", "")),\n'
INSERTION = ANCHOR + """\
            "build_git_sha": str(body.get("build_git_sha", "")),
            "runtime_code_sha256": str(body.get("runtime_code_sha256", "")),
            "throttled_hex": str(body.get("throttled_hex", "")),
            "under_voltage_now": str(body.get("under_voltage_now", "")),
            "throttled_now": str(body.get("throttled_now", "")),
            "arm_clock_mhz": str(body.get("arm_clock_mhz", "")),
            "process_cpu_pct": str(body.get("process_cpu_pct", "")),
            "process_rss_mb": str(body.get("process_rss_mb", "")),
            "failed_group_floor_since_last":
                str(body.get("failed_group_floor_since_last", "")),
            "failed_below_threshold_since_last":
                str(body.get("failed_below_threshold_since_last", "")),
            "failed_unreadable_since_last":
                str(body.get("failed_unreadable_since_last", "")),
            "drive_offs_since_last":
                str(body.get("drive_offs_since_last", "")),
            "vehicle_only_since_last":
                str(body.get("vehicle_only_since_last", "")),
            "delivery_queue_depth":
                str((body.get("delivery_queue") or {}).get("queued", "")),
            "dead_letter_depth":
                str((body.get("delivery_queue") or {}).get("dead_letter", "")),
"""


def patch_source(source: str) -> str:
    # A second reviewed deployment downloads the already-hardened live
    # package. Accept only our exact prior insertion, once; any partial or
    # independently modified provenance block still fails closed.
    if source.count(INSERTION) == 1:
        if source.count('"runtime_code_sha256":') != 1:
            raise ValueError("Heartbeat provenance block is ambiguous")
        compile(source, "heartbeat.py", "exec")
        return source
    if '"runtime_code_sha256":' in source:
        raise ValueError("Heartbeat source contains an unknown provenance block")
    count = source.count(ANCHOR)
    if count != 1:
        raise ValueError(f"expected exactly one Heartbeat anchor; found {count}")
    return source.replace(ANCHOR, INSERTION, 1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    source = args.path.read_text(encoding="utf-8")
    patched = patch_source(source)
    compile(patched, str(args.path), "exec")
    args.path.write_text(patched, encoding="utf-8")
    print("Heartbeat patch validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
