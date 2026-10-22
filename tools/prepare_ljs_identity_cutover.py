#!/usr/bin/env python3
"""Rotate only the existing Louisville credential after held-host preparation.

This local operator utility never creates a device, copies a Tailscale identity,
changes history, approves a deployment, or starts production capture. Staging
is read-only in AWS. Rotation requires a separately reviewed install's private
host-ready proof and leaves its shipping hold intact. Secrets exist only in
mode-0600 files and captured memory, never CLI arguments or printed exceptions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile

DEVICE = "serv-unit-001"
LOCATION = "ljs-louisville-001"
HOSTNAME = "serv-ljs-001"
REGION = "us-east-1"
API_BASE = "https://sb13iibr7f.execute-api.us-east-1.amazonaws.com"
KEY = {"device_id": {"S": DEVICE}}
_KEY_RE = re.compile(r"[0-9a-f]{64}\Z")
_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_MAX_BYTES = 1024 * 1024
_FIELDS = ("device_id", "location_id", "status", "api_key", "created_at", "type")


class CutoverError(RuntimeError):
    """Only fixed, non-secret messages may be propagated to the operator."""


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def private_directory(path: Path, *, create: bool = False) -> None:
    if create:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.getuid()):
        raise CutoverError("Staging directory must be owned, real and mode 0700")


def read_private(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid() or info.st_size > _MAX_BYTES):
            raise CutoverError("Input must be an owned regular mode-0600 file within size limit")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            data = handle.read(_MAX_BYTES + 1)
        if len(data) > _MAX_BYTES:
            raise CutoverError("Input exceeds size limit")
        return data
    finally:
        if fd >= 0:
            os.close(fd)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(read_private(path))
    except (ValueError, UnicodeError):
        raise CutoverError("Private JSON input is invalid") from None
    if not isinstance(value, dict):
        raise CutoverError("Private JSON input must be an object")
    return value


def create_private(path: Path, data: bytes) -> None:
    # Publish all bytes atomically and never replace an existing file/symlink.
    fd, name = tempfile.mkstemp(prefix=".cutover-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.unlink(name)


def aws(operation: str, payload: dict) -> dict:
    if operation not in ("get-item", "update-item"):
        raise CutoverError("Unsupported cloud operation")
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as request:
            os.fchmod(request.fileno(), 0o600)
            json.dump(payload, request)
            request.flush()
            result = subprocess.run(
                ["aws", "dynamodb", operation, "--region", REGION,
                 "--cli-input-json", "file://" + request.name,
                 "--output", "json", "--no-cli-pager"],
                capture_output=True, text=True, timeout=45,
            )
        if result.returncode:
            raise CutoverError("AWS request failed; captured output withheld")
        response = json.loads(result.stdout or "{}")
        if not isinstance(response, dict):
            raise CutoverError("Unexpected AWS response shape")
        return response
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise CutoverError("AWS request failed; captured output withheld") from None


def cloud_record() -> dict:
    names = {"#" + name: name for name in _FIELDS}
    response = aws("get-item", {
        "TableName": "Devices", "Key": KEY, "ConsistentRead": True,
        "ProjectionExpression": ", ".join(names), "ExpressionAttributeNames": names,
    })
    item = response.get("Item")
    if (not isinstance(item, dict) or item.get("device_id") != {"S": DEVICE}
            or item.get("location_id") != {"S": LOCATION}
            or item.get("status") != {"S": "active"}):
        raise CutoverError("Existing active Louisville device record does not match")
    credential = item.get("api_key")
    candidate = credential.get("S") if isinstance(credential, dict) else None
    if not isinstance(candidate, str) or not _KEY_RE.fullmatch(candidate):
        raise CutoverError("Existing device credential format is invalid")
    return item


def key_file(path: Path) -> str:
    try:
        key = read_private(path).decode("ascii").strip()
    except UnicodeError:
        raise CutoverError("Private key has invalid format") from None
    if not _KEY_RE.fullmatch(key):
        raise CutoverError("Private key has invalid format")
    return key


def load_stage(directory: Path) -> tuple[dict, str, str]:
    private_directory(directory)
    manifest = read_json(directory / "cutover.json")
    old = key_file(directory / "old-api-key.txt")
    new = key_file(directory / "new-api-key.txt")
    if (manifest.get("version") != 1 or manifest.get("device_id") != DEVICE
            or manifest.get("location_id") != LOCATION or manifest.get("region") != REGION
            or not _SHA_RE.fullmatch(str(manifest.get("reviewed_sha", "")))
            or manifest.get("old_key_sha256") != digest(old)
            or manifest.get("new_key_sha256") != digest(new) or old == new
            or not isinstance(manifest.get("unchanged_attributes"), dict)):
        raise CutoverError("Private cutover stage is inconsistent")
    return manifest, old, new


def state(item: dict, old: str, new: str) -> str:
    current = item["api_key"]["S"]
    if secrets.compare_digest(current, old):
        return "old"
    if secrets.compare_digest(current, new):
        return "new"
    raise CutoverError("Cloud credential differs from both staged credentials; refusing overwrite")


def stage(directory: Path, reviewed_sha: str) -> dict:
    if not _SHA_RE.fullmatch(reviewed_sha):
        raise CutoverError("A full reviewed 40-character commit is required")
    private_directory(directory, create=True)
    if os.path.lexists(directory / "cutover.json"):
        manifest, old, new = load_stage(directory)
        if manifest["reviewed_sha"] != reviewed_sha:
            raise CutoverError("Existing stage belongs to a different reviewed release")
        result = verify(directory)
        return {**result, "operation": "stage", "aws_changed": False, "reused_stage": True}
    if any(os.path.lexists(directory / name) for name in ("old-api-key.txt", "new-api-key.txt")):
        raise CutoverError("Partial private stage exists; preserve it for operator recovery")
    item = cloud_record()
    old = item["api_key"]["S"]
    new = secrets.token_hex(32)
    if old == new:
        raise CutoverError("Generated key unexpectedly matched current key; nothing changed")
    manifest = {
        "version": 1, "device_id": DEVICE, "location_id": LOCATION, "region": REGION,
        "reviewed_sha": reviewed_sha, "old_key_sha256": digest(old), "new_key_sha256": digest(new),
        "unchanged_attributes": {k: v for k, v in item.items() if k != "api_key"},
    }
    create_private(directory / "old-api-key.txt", (old + "\n").encode())
    create_private(directory / "new-api-key.txt", (new + "\n").encode())
    create_private(directory / "cutover.json", (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode())
    return {"operation": "stage", "device_id": DEVICE, "credential_state": "old", "aws_changed": False}


def validate_host(directory: Path, config_path: Path, proof_path: Path) -> tuple[dict, str, str]:
    manifest, old, new = load_stage(directory)
    data = read_private(config_path)
    try:
        config = json.loads(data)
    except (ValueError, UnicodeError):
        raise CutoverError("Staged production config is invalid") from None
    if (not isinstance(config, dict) or config.get("device_id") != DEVICE
            or config.get("location_id") != LOCATION or config.get("api_base") != API_BASE
            or config.get("api_key") != new):
        raise CutoverError("Staged production config does not match this identity and key")
    proof = read_json(proof_path)
    required = {
        "device_id": DEVICE, "location_id": LOCATION, "hostname": HOSTNAME,
        "reviewed_sha": manifest["reviewed_sha"], "config_sha256": digest(data),
        "api_key_sha256": digest(new), "queue_isolated": True, "shipping_hold": True,
        "phase": "host_prepared_cloud_pending",
    }
    # Exact type checks matter: JSON 1 must not substitute for a boolean proof.
    if any(proof.get(k) != v or type(proof.get(k)) is not type(v) for k, v in required.items()):
        raise CutoverError("Host-ready proof does not match held production configuration")
    return manifest, old, new


def verify(directory: Path) -> dict:
    manifest, old, new = load_stage(directory)
    item = cloud_record()
    current = state(item, old, new)
    unchanged = {k: v for k, v in item.items() if k != "api_key"}
    changed = sorted(k for k in set(unchanged) | set(manifest["unchanged_attributes"])
                     if unchanged.get(k) != manifest["unchanged_attributes"].get(k))
    if changed:
        raise CutoverError("Projected device metadata changed since staging; operator review required")
    return {
        "operation": "verify", "device_id": DEVICE, "location_id": LOCATION,
        "credential_state": current, "changed_projected_attributes": ["api_key"] if current == "new" else [],
        "production_capture_activated": False,
    }


def rotate(directory: Path, config_path: Path, proof_path: Path) -> dict:
    manifest, old, new = validate_host(directory, config_path, proof_path)
    before = verify(directory)
    if before["credential_state"] == "new":
        return {**before, "operation": "rotate", "aws_changed": False, "already_rotated": True}
    try:
        aws("update-item", {
            "TableName": "Devices", "Key": KEY,
            "UpdateExpression": "SET #api_key = :new",
            "ConditionExpression": "attribute_exists(#device_id) AND #location_id = :location AND #status = :active AND #api_key = :old",
            "ExpressionAttributeNames": {"#device_id": "device_id", "#location_id": "location_id", "#status": "status", "#api_key": "api_key"},
            "ExpressionAttributeValues": {":new": {"S": new}, ":old": {"S": old}, ":location": {"S": LOCATION}, ":active": {"S": "active"}},
            "ReturnValues": "NONE",
        })
    except CutoverError:
        # A timed-out response may still have committed. Read before retrying;
        # never blindly roll back and never overwrite a third-party rotation.
        after = verify(directory)
        if after["credential_state"] != "new":
            raise CutoverError("Rotation not confirmed; old key remains and held host must stay stopped") from None
        return {**after, "operation": "rotate", "aws_changed": True, "response_reconciled": True}
    after = verify(directory)
    if after["credential_state"] != "new":
        raise CutoverError("Rotation verification failed; held host must stay stopped")
    return {**after, "operation": "rotate", "aws_changed": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("stage", "rotate", "verify"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--reviewed-sha")
    parser.add_argument("--config-file", type=Path)
    parser.add_argument("--host-ready", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.operation == "stage":
            if not args.reviewed_sha:
                raise CutoverError("Stage requires --reviewed-sha")
            result = stage(args.directory, args.reviewed_sha)
        elif args.operation == "rotate":
            if not args.config_file or not args.host_ready:
                raise CutoverError("Rotation requires --config-file and --host-ready")
            result = rotate(args.directory, args.config_file, args.host_ready)
        else:
            result = verify(args.directory)
    except (CutoverError, OSError, ValueError, TypeError, KeyError):
        # Even filesystem paths and provider exceptions may contain secrets.
        print("Cutover operation refused or unverified; private artifacts preserved. No credentials printed.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
