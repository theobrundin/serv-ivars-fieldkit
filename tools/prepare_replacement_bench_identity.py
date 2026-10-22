#!/usr/bin/env python3
"""Prepare one isolated bench identity without rotating a production key.

Secrets are written only into private files (never process arguments). This
intentionally cannot activate serv-unit-001: that is a separate
cutover after the replacement passes acceptance and is installed at LJS.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import tempfile

DEVICE = "serv-unit-bench"
LOCATION = "bench-servcamog"


def aws(operation: str, payload: dict) -> dict:
    # This installed AWS CLI does not support streaming /dev/stdin input.
    # NamedTemporaryFile is mode600 and removed even when the CLI fails.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as request:
        json.dump(payload, request)
        request.flush()
        proc = subprocess.run(
            ["aws", "dynamodb", operation, "--region", "us-east-1",
             "--cli-input-json", "file://" + request.name, "--output", "json",
             "--no-cli-pager"], text=True, capture_output=True, timeout=45,
        )
    if proc.returncode:
        # SDK/CLI exceptions can contain request data. Never echo them here.
        raise RuntimeError(f"AWS {operation} failed; no credentials were printed")
    return json.loads(proc.stdout or "{}")


def prepare(directory: Path, apply: bool = False) -> None:
    directory.mkdir(mode=0o700, parents=False, exist_ok=True)
    s = directory.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_mode & 0o077 or s.st_uid != os.getuid():
        raise ValueError("output directory must be owned, real, and mode 700")
    key_path = directory / "bench-api-key.txt"
    if key_path.exists() or key_path.is_symlink():
        s = key_path.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_mode & 0o077 or s.st_uid != os.getuid():
            raise ValueError("existing key file must be owned, regular, and private")
        key = key_path.read_text().strip()
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("existing key file has invalid format")
    else:
        key = secrets.token_hex(32)
        fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(key + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    if not apply:
        print("Private bench key prepared; AWS unchanged; production unchanged")
        return
    old = aws("get-item", {"TableName": "Devices", "ConsistentRead": True,
                          "Key": {"device_id": {"S": DEVICE}}}).get("Item")
    expected = {"device_id": {"S": DEVICE}, "location_id": {"S": LOCATION},
                "status": {"S": "active"}, "api_key": {"S": key}}
    if old:
        if any(old.get(k) != value for k, value in expected.items()):
            raise ValueError("existing bench identity differs; refusing overwrite")
        print("Matching isolated bench identity already exists; production unchanged")
        return
    aws("put-item", {"TableName": "Devices", "Item": expected,
                     "ConditionExpression": "attribute_not_exists(device_id)"})
    print("Created isolated serv-unit-bench / bench-servcamog; production unchanged")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    prepare(args.directory, args.apply)
