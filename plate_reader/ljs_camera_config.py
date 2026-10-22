"""Apply a reviewed LJS exposure/shutter profile atomically.

The original ``short`` canary left shutter duration automatic.  A 1 Sep
1/300-second canary froze exposure so aggressively that the night frame became
nearly black.  ``twilight`` is the reviewed low-light compromise: a fixed
1/60-second shutter while retaining the measured five-FPS source and automatic
gain.  Every profile has a semantic rollback and MediaMTX health must be
verified after its hot reload.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import tempfile
from datetime import datetime, timezone


DEFAULT_CONFIG = Path("/etc/mediamtx/mediamtx.yml")
EXPECTED_SOURCE = "rpiCamera"
EXPECTED_WIDTH = "1456"
EXPECTED_HEIGHT = "1088"
EXPECTED_FPS = "5"
MOTION_SHUTTER_US = "3333"
TWILIGHT_SHUTTER_US = "16667"
PROFILES = {
    "normal": {"rpiCameraExposure": "normal", "rpiCameraShutter": "0"},
    "short": {"rpiCameraExposure": "short", "rpiCameraShutter": "0"},
    "motion": {
        "rpiCameraExposure": "short",
        "rpiCameraShutter": MOTION_SHUTTER_US,
    },
    "twilight": {
        "rpiCameraExposure": "short",
        "rpiCameraShutter": TWILIGHT_SHUTTER_US,
    },
}
ALLOWED_MODES = frozenset(PROFILES)

_SETTING_RE = re.compile(
    r"^(?P<indent>\s*)(?P<key>rpiCamera[A-Za-z0-9]+)"
    r"(?P<spacing>\s*:\s*)(?P<value>[^#\r\n]*?)"
    r"(?P<suffix>\s*(?:#.*)?)(?P<newline>\r?\n?)$"
)
_SOURCE_RE = re.compile(
    r"^\s*source\s*:\s*(?P<value>[^#\r\n]*?)\s*(?:#.*)?$",
    re.MULTILINE,
)


def _settings(text: str) -> dict[str, list[tuple[int, re.Match[str]]]]:
    found: dict[str, list[tuple[int, re.Match[str]]]] = {}
    for index, line in enumerate(text.splitlines(keepends=True)):
        match = _SETTING_RE.match(line)
        if match:
            found.setdefault(match.group("key"), []).append((index, match))
    return found


def corrected_text(
    text: str,
    mode: str = "short",
) -> tuple[str, list[tuple[str, str, str]]]:
    """Return corrected YAML and exact ``(key, old, new)`` changes."""
    if mode not in ALLOWED_MODES:
        raise ValueError(f"unsupported exposure mode: {mode}")
    lines = text.splitlines(keepends=True)
    settings = _settings(text)

    def one(key: str) -> tuple[int, re.Match[str]]:
        matches = settings.get(key, [])
        if len(matches) != 1:
            raise ValueError(f"expected exactly one {key}, found {len(matches)}")
        return matches[0]

    _, source = one("rpiCameraWidth")
    if source.group("value").strip() != EXPECTED_WIDTH:
        raise ValueError("refusing non-LJS camera width")
    _, height = one("rpiCameraHeight")
    if height.group("value").strip() != EXPECTED_HEIGHT:
        raise ValueError("refusing non-LJS camera height")
    fps_index, fps = one("rpiCameraFPS")
    if fps.group("value").strip() != EXPECTED_FPS:
        raise ValueError("refusing unexpected LJS camera FPS")
    _, codec = one("rpiCameraCodec")
    if codec.group("value").strip() != "auto":
        raise ValueError("refusing camera config without codec=auto")
    for key in ("rpiCameraShutter", "rpiCameraGain"):
        matches = settings.get(key, [])
        if len(matches) > 1:
            raise ValueError(f"expected at most one {key}, found {len(matches)}")
        allowed = {"0"}
        if key == "rpiCameraShutter":
            allowed.update({MOTION_SHUTTER_US, TWILIGHT_SHUTTER_US})
        if matches and matches[0][1].group("value").strip() not in allowed:
            raise ValueError(
                f"refusing unreviewed fixed {key}"
            )
    source_matches = list(_SOURCE_RE.finditer(text))
    if (
        len(source_matches) != 1
        or source_matches[0].group("value").strip() != EXPECTED_SOURCE
    ):
        raise ValueError("expected exactly one Raspberry Pi camera source")

    changes: list[tuple[str, str, str]] = []
    missing: list[tuple[str, str, str]] = []
    for key in ("rpiCameraExposure", "rpiCameraShutter"):
        desired = PROFILES[mode][key]
        matches = settings.get(key, [])
        if len(matches) > 1:
            raise ValueError(f"duplicate {key} settings")
        if matches:
            index, match = matches[0]
            old = match.group("value").strip()
            if key == "rpiCameraExposure" and old not in {"normal", "short"}:
                raise ValueError(f"refusing unexpected exposure mode: {old}")
            if old == desired:
                continue
            lines[index] = (
                f"{match.group('indent')}{match.group('key')}"
                f"{match.group('spacing')}{desired}{match.group('suffix')}"
                f"{match.group('newline')}"
            )
            changes.append((key, old, desired))
            continue

        assumed = "normal" if key == "rpiCameraExposure" else "0"
        if desired != assumed:
            missing.append((key, f"<absent/{assumed}>", desired))

    newline = "\r\n" if lines[fps_index].endswith("\r\n") else "\n"
    indent = fps.group("indent")
    for offset, (key, old, desired) in enumerate(missing, start=1):
        lines.insert(fps_index + offset, f"{indent}{key}: {desired}{newline}")
        changes.append((key, old, desired))
    return "".join(lines), changes


def install(path: Path, corrected: str) -> Path:
    """Install atomically, preserving ownership/mode and a timestamped backup."""
    stat = path.stat()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = path.with_name(f"{path.name}.before-exposure-{stamp}")
    shutil.copy2(path, backup)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(corrected)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, stat.st_mode & 0o777)
        os.chown(temp, stat.st_uid, stat.st_gid)
        os.replace(temp, path)
    except Exception:
        temp.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)
        raise
    return backup


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--mode", choices=sorted(ALLOWED_MODES), default="short")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    original = args.config.read_text(encoding="utf-8")
    corrected, changes = corrected_text(original, args.mode)
    if not changes:
        print(f"LJS camera already has profile={args.mode}")
        return 0
    print(f"LJS camera profile={args.mode}; changing:")
    for key, old, new in changes:
        print(f"  {key}: {old} -> {new}")
    if args.check:
        print("check only; no file changed")
        return 0
    backup = install(args.config, corrected)
    print(f"installed atomically; backup={backup}")
    print("verify MediaMTX hot reload and Stream health before leaving the canary")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
