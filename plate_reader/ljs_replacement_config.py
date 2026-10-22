"""Build, but never activate, a credential-safe LJS replacement configuration.

This is an offline factory operation, not a way to promote a running unit.
The installer must keep production capture/delivery held until the replacement
is commissioned, retire the old credential, and start with a fresh queue.
The reviewed old LJS geometry is reproduced here; it is NOT proof that the new
lens, aim, mount, or menu-board timing is calibrated at the restaurant.

No API key is accepted on the command line or printed. Input credentials and
output configurations are regular mode-0600 files. Existing outputs are never
overwritten. No queue, audit image, license, Tailscale identity or hash changes
are performed by this module.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

from .config import load
from .ljs_camera_config import corrected_text as correct_camera
from .ljs_route_config import (
    APP_OCR_MIN,
    EXPECTED_DEVICE,
    EXPECTED_LOCATION,
    apply_phase,
)
from .ljs_stream_config import corrected_text as correct_stream


BENCH_DEVICE = "serv-unit-bench"
BENCH_LOCATION = "bench-servcamog"
API_BASE = "https://sb13iibr7f.execute-api.us-east-1.amazonaws.com"
STAGES = ("bench", "production")
_HEX_KEY = re.compile(r"[0-9a-f]{64}\Z")
_MAX_CREDENTIAL_BYTES = 65536


def _without_comments(value):
    if isinstance(value, dict):
        return {
            key: _without_comments(item)
            for key, item in value.items()
            if not key.startswith("//")
        }
    if isinstance(value, list):
        return [_without_comments(item) for item in value]
    return deepcopy(value)


def read_key(path: Path, *, bench_config: bool = False) -> str:
    """Read one private key file, or the existing bench config's key only.

    ``O_NOFOLLOW`` and a regular-file check avoid following a final symlink or
    blocking on a FIFO. Error messages deliberately do not echo file contents.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("credential input must be a regular mode-0600 file")
        if info.st_size > _MAX_CREDENTIAL_BYTES:
            raise ValueError("credential input exceeds the allowed size")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            text = handle.read(_MAX_CREDENTIAL_BYTES + 1)
        if len(text) > _MAX_CREDENTIAL_BYTES:
            raise ValueError("credential input exceeds the allowed size")
        if bench_config:
            try:
                raw = json.loads(text)
                if (
                    raw.get("device_id") != BENCH_DEVICE
                    or raw.get("location_id") != BENCH_LOCATION
                ):
                    raise ValueError
                key = raw.get("api_key")
            except (ValueError, AttributeError, TypeError):
                raise ValueError("credential input is not the expected bench config") from None
        else:
            key = text.strip()
        if not isinstance(key, str) or not _HEX_KEY.fullmatch(key):
            raise ValueError("device key must be 64 lowercase hexadecimal characters")
        return key
    finally:
        if fd != -1:
            os.close(fd)


def build_config(template: dict, key: str, *, stage: str) -> dict:
    """Recreate the reviewed field profile with isolated bench/site identity.

    The template is a reviewed repository input, not a copied field/bench
    config. Shared route rollout code supplies the site-specific corrections.
    Calling its two phases on this fresh in-memory candidate does not install
    anything or authorize production. Existing runtime configs are untouched.
    """
    if stage not in STAGES:
        raise ValueError("stage must be bench or production")
    if not isinstance(key, str) or not _HEX_KEY.fullmatch(key):
        raise ValueError("device key must be 64 lowercase hexadecimal characters")
    if not isinstance(template, dict):
        raise ValueError("template must be a JSON object")
    raw = _without_comments(template)
    # Refuse an engine/key/source swap hidden in a different source template.
    if raw.get("alpr", {}).get("engine") != "pr_stream":
        raise ValueError("replacement template must use the reviewed pr_stream engine")
    if raw.get("api_base") != API_BASE:
        raise ValueError("replacement template has an unexpected API endpoint")
    if raw.get("capture") != {"target_fps": 5, "width": 1456, "height": 1088}:
        raise ValueError("replacement template must retain full-sensor five-FPS capture")

    raw.update(device_id=EXPECTED_DEVICE, location_id=EXPECTED_LOCATION, api_key=key)
    # These UltimateALPR-only paths are not Stream licensing. Preserve the
    # physical machine's token separately; never claim the JSON licenses PR.
    raw["alpr"].pop("license_token_file", None)
    raw["alpr"].pop("assets_folder", None)
    apply_phase(raw, "shadow")
    apply_phase(raw, "authoritative")

    # Match both legacy and effective copies so inspection cannot mistake old
    # UltimateALPR values for the running PR gates. Hashing is unchanged.
    raw["thresholds"].update(
        min_detection_confidence=0.70,
        min_ocr_confidence=APP_OCR_MIN,
        min_group_detection_confidence=0.70,
        min_group_ocr_confidence=APP_OCR_MIN,
        vote_min_frames=2,
        vote_window_seconds=3.0,
    )
    raw["presence"].update(vote_min_frames=2, vote_window_seconds=3.0)
    raw["camera"].update(resolution_width=1456, resolution_height=1088, framerate=5)
    raw["diagnostics"].update(
        log_rejected_reads=False,
        save_presence_frames=False,
        miss_plate_dir="/home/serv/plate_reader/audit_cases",
        miss_plate_retain_hours=0,
    )
    raw["hours"] = {
        "timezone": "America/New_York",
        "open": "10:00",
        "close": "22:00",
        "warmup_minutes": 15,
    }
    if stage == "bench":
        raw.update(device_id=BENCH_DEVICE, location_id=BENCH_LOCATION)
        # Test outside restaurant hours without ever adopting production ID.
        # The installer owns service lifecycle when there is no hours block.
        raw.pop("hours")
        raw["diagnostics"]["miss_plate_dir"] = "/home/serv/plate_reader/bench_audit_cases"
    # apply_phase reuses shared zone constants; do not give callers aliases
    # through which another generated profile or a library constant can drift.
    return deepcopy(raw)


def build_stream_config(template: str) -> str:
    """Reuse reviewed 12/90/30-second corrections; keep the full local image.

    The source must be the repository template. The installer independently
    verifies active runtime config, Stream license and downloaded masks.
    """
    corrected, _ = correct_stream(template)
    # These do not depend on vendor packages and reject the common dangerous
    # template substitutions. Headers and scalar values are checked together.
    settings = []
    section = ""
    for line in corrected.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("["):
            section = line
        elif "=" in line:
            name, value = (part.strip() for part in line.split("=", 1))
            settings.append((section, name, value))
    required = (
        ("", "timezone", "America/New_York"),
        ("[cameras]", "regions", "us"),
        ("[cameras]", "image_format", ""),
        ("[[camera-1]]", "url", "rtsp://127.0.0.1:8554/cam"),
        ("[[camera-1]]", "sample", "1"),
        ("[[camera-1]]", "detection_mode", "vehicle"),
        ("[[camera-1]]", "mmc", "yes"),
        ("[[camera-1]]", "dwell_time", "true"),
        ("[[camera-1]]", "trajectory", "true"),
        ("[[camera-1]]", "webhook_targets", "serv-local"),
        ("[[serv-local]]", "url", "http://127.0.0.1:8129/"),
        ("[[serv-local]]", "image", "original"),
    )
    for expected in required:
        same_key = [item for item in settings if item[:2] == expected[:2]]
        if same_key != [expected]:
            raise ValueError("Stream template differs from the reviewed full-frame local profile")
    urls = [item for item in settings if item[1] == "url"]
    if len(urls) != 2:
        raise ValueError("Stream template must contain exactly one camera and local webhook")
    return corrected


def build_camera_config(template: str) -> str:
    """Use measured short/automatic-shutter, not the untested twilight canary."""
    corrected, _ = correct_camera(template, mode="short")
    for required in (
        r"^rtspAddress:\s*127\.0\.0\.1:8554\s*$",
        r"^\s+rpiCameraCodec:\s*auto\s*$",
        *(rf"^{service}:\s*no\s*$" for service in ("rtmp", "hls", "webrtc", "srt", "moq", "api", "metrics")),
    ):
        if len(re.findall(required, corrected, re.MULTILINE)) != 1:
            raise ValueError("camera template must retain local RTSP and software encoding")
    return corrected


def _validate_file(path: Path) -> None:
    try:
        load(path)
    except Exception:
        # Do not relay arbitrary config contents through validation errors.
        raise ValueError("generated configuration failed validation; output not installed") from None


def write_validated(raw: dict, path: Path | None = None) -> None:
    """Validate a private temporary file; atomically create an optional output.

    Linking the validated same-filesystem temporary file publishes all bytes
    at once and fails if an existing file/symlink races into the destination.
    The caller must explicitly handle backup/activation/ownership elsewhere.
    """
    if path is not None and os.path.lexists(path):
        raise ValueError("output already exists; choose a new staging path")
    directory = path.parent if path is not None else None
    fd, name = tempfile.mkstemp(prefix=".ljs-replacement-", dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(raw, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _validate_file(temporary)
        if path is not None:
            os.link(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--example", required=True, type=Path)
    parser.add_argument("--stage", required=True, choices=STAGES)
    credentials = parser.add_mutually_exclusive_group(required=True)
    credentials.add_argument("--key-file", type=Path)
    credentials.add_argument("--bench-key-config", type=Path)
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument("--output", type=Path)
    output.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.stage == "production" and args.bench_key_config:
        parser.error("production requires its separate key file, not the bench credential")
    try:
        key = read_key(
            args.key_file or args.bench_key_config,
            bench_config=args.bench_key_config is not None,
        )
        template = json.loads(args.example.read_text(encoding="utf-8"))
        raw = build_config(template, key, stage=args.stage)
        write_validated(raw, args.output)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        print("Replacement config rejected; check template, stage, private input permissions and unused output path.", file=sys.stderr)
        return 1
    print(f"Replacement {args.stage} configuration validated" + (" and staged mode0600." if args.output else "; check only."))
    print("No service, queue, credential registration or deployment approval was changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
