#!/usr/bin/env python3
"""Non-networking validation helpers for the reviewed replacement installer.

This file is exported from the approved Git object before root executes it.
Never prints configuration values or credentials. No production activation.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile


class InvalidInput(ValueError):
    pass


API_BASE = "https://sb13iibr7f.execute-api.us-east-1.amazonaws.com"


def read_regular(path, *, private=False, max_size=256 * 1024 * 1024):
    """Read an opened regular inode, not a symlink/device; snapshot its bytes."""
    path = Path(path)
    if not path.is_absolute():
        raise InvalidInput("input paths must be absolute")
    # Reject symlinked ancestors as well as a symlink at the leaf. Inputs may
    # live in serv-owned directories: their bytes are copied before use.
    for part in (path, *path.parents):
        if part.is_symlink():
            raise InvalidInput("input path contains a symbolic link")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise InvalidInput("input is not a regular file")
        if private and stat.S_IMODE(info.st_mode) != 0o600:
            raise InvalidInput("credential-bearing input must have mode 0600")
        if info.st_size > max_size:
            raise InvalidInput("input exceeds permitted size")
        data = source.read(max_size + 1)
        if len(data) > max_size:
            raise InvalidInput("input exceeds permitted size")
        return data


def validate_bench(data):
    try:
        config = json.loads(data)
    except (ValueError, UnicodeError):
        raise InvalidInput("bench config is not valid JSON") from None
    if not isinstance(config, dict):
        raise InvalidInput("bench config must be an object")
    if config.get("device_id") != "serv-unit-bench":
        raise InvalidInput("bench install requires the dedicated bench device identity")
    if config.get("location_id") != "bench-servcamog":
        raise InvalidInput("bench install cannot target a production location")
    if not isinstance(config.get("alpr"), dict) or config["alpr"].get("engine") != "pr_stream":
        raise InvalidInput("replacement must select pr_stream with its own thresholds")
    if not isinstance(config.get("api_key"), str) or len(config["api_key"]) < 20:
        raise InvalidInput("bench config requires a fresh non-placeholder device credential")
    if config.get("api_base") != API_BASE:
        raise InvalidInput("device API differs from the reviewed endpoint")
    return config


def service_check(config_path, marker_path):
    """Bench marker existence is enough; the unprivileged service cannot read it.

    A later explicit human cutover removes the marker after fencing the old
    credential and installing a clean production configuration/queue. This
    helper never removes it and never communicates with AWS.
    """
    if os.path.lexists(marker_path):
        validate_bench(read_regular(config_path, private=True, max_size=1024 * 1024))


def validate_pr_env(path, expected_uid):
    data = read_regular(path, private=True, max_size=65536)
    if Path(path).stat().st_uid != expected_uid:
        raise InvalidInput("Stream credential file must belong to the service account")
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeError:
        raise InvalidInput("invalid Stream credential encoding") from None
    values = {}
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in {"TOKEN", "LICENSE_KEY"} or key in values:
            raise InvalidInput("invalid or duplicate Stream credential field")
        if not value.strip() or value != value.strip() or "\x00" in value:
            raise InvalidInput("invalid Stream credential value")
        values[key] = value
    if set(values) != {"TOKEN", "LICENSE_KEY"}:
        raise InvalidInput("both Stream credential fields are required")


def archive_members(data):
    """Reject links, special files, duplicate members and traversal BEFORE extraction."""
    archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    members = archive.getmembers()
    paths = set()
    total = 0
    for member in members:
        p = PurePosixPath(member.name)
        if p.is_absolute() or ".." in p.parts or not p.parts:
            archive.close()
            raise InvalidInput("unsafe archive member path")
        normalized = str(p)
        if normalized in paths:
            archive.close()
            raise InvalidInput("duplicate archive member")
        paths.add(normalized)
        if not (member.isfile() or member.isdir()):
            archive.close()
            raise InvalidInput("archive contains a link or special file")
        total += member.size
        if total > 512 * 1024 * 1024 or len(members) > 20000:
            archive.close()
            raise InvalidInput("archive exceeds permitted size")
    # A member cannot hide beneath another member that is a regular file.
    regular = {str(PurePosixPath(m.name)) for m in members if m.isfile()}
    for member in members:
        if any(str(p) in regular for p in PurePosixPath(member.name).parents):
            archive.close()
            raise InvalidInput("archive parent is not a directory")
    return archive, members


def extract_regular(data, destination):
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise InvalidInput("archive destination must be empty")
    archive, members = archive_members(data)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        for member in members:
            target = destination / PurePosixPath(member.name)
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            if member.isdir():
                target.mkdir(mode=0o755, exist_ok=True)
                continue
            # No ownership, mode or link from the tar is trusted. Executable
            # regular files retain only their execute bits, never setuid/setgid.
            with archive.extractfile(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    finally:
        archive.close()


def write_private(destination, data):
    destination = Path(destination)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    archive = sub.add_parser("extract")
    archive.add_argument("archive")
    archive.add_argument("destination")
    sub.add_parser("service-check")
    inputs = sub.add_parser("inputs")
    for name in ("bench-config", "stream-config", "mediamtx-config", "mediamtx-archive",
                 "mediamtx-sha256", "destination"):
        inputs.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        if args.command == "service-check":
            service_check("/home/serv/plate_reader/config.json", "/etc/serv/replacement-bench.json")
            return
        if args.command == "extract":
            extract_regular(read_regular(args.archive), args.destination)
            return
        target = Path(args.destination)
        target.mkdir(mode=0o700, exist_ok=False)
        bench = read_regular(args.bench_config, private=True, max_size=1024 * 1024)
        validate_bench(bench)
        stream = read_regular(args.stream_config, max_size=1024 * 1024)
        mtx = read_regular(args.mediamtx_config, max_size=1024 * 1024)
        artifact = read_regular(args.mediamtx_archive)
        if hashlib.sha256(artifact).hexdigest() != args.mediamtx_sha256:
            raise InvalidInput("MediaMTX archive checksum does not match reviewed checksum")
        extract_regular(artifact, target / "mediamtx")
        binary = target / "mediamtx" / "mediamtx"
        if not binary.is_file() or binary.is_symlink():
            raise InvalidInput("MediaMTX archive lacks its regular mediamtx binary")
        # A native ARM64 ELF prevents silently installing an x86 release.
        header = binary.open("rb").read(20)
        if len(header) < 20 or header[:4] != b"\x7fELF" or header[4:6] != b"\x02\x01" or header[18:20] != b"\xb7\x00":
            raise InvalidInput("MediaMTX binary is not a little-endian ARM64 ELF")
        write_private(target / "bench-config.json", bench)
        write_private(target / "config.ini", stream)
        write_private(target / "mediamtx.yml", mtx)
    except (OSError, InvalidInput, tarfile.TarError):
        # Do not include data parsed from credential-bearing input in stderr.
        parser.exit(1, "replacement input validation failed; no credential values were printed\n")


if __name__ == "__main__":
    main()
