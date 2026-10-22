"""Apply the reviewed LJS Plate Recognizer Stream canary safely.

The application updater deliberately does not overwrite Stream's site-local
``config.ini``. This utility changes only the reviewed temporal/vehicle
settings, refuses an unexpected camera/source, writes a mode-600 backup, and
atomically replaces the file. It does not restart Stream; the operator can
wait for an empty lane before doing that separately.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import tempfile


EXPECTED_CAMERA = "camera-1"
EXPECTED_URL = "rtsp://127.0.0.1:8554/cam"
TARGET_VALUES = {
    "max_prediction_delay": "12",
    "memory_decay": "90",
    "max_dwell_delay": "30",
    "plates_per_vehicle": "1",
    "report_static": "no",
}
INSERT_IF_MISSING = {"report_static"}

_CAMERA_HEADER_RE = re.compile(r"^\s*\[\[([^]]+)\]\]\s*$")
_SECTION_HEADER_RE = re.compile(r"^\s*\[[^[]")
_SETTING_RE = re.compile(
    r"^(?P<indent>\s*)(?P<key>[A-Za-z0-9_]+)(?P<spacing>\s*=\s*)"
    r"(?P<value>[^#;\r\n]*?)(?P<suffix>\s*(?:[#;].*)?)(?P<newline>\r?\n?)$"
)


def corrected_text(text: str) -> tuple[str, list[tuple[str, str, str]]]:
    """Return corrected INI text plus ``(key, old, new)`` changes.

    Stream uses nested ``[[camera-id]]`` sections that are not standard
    ``configparser`` syntax.  A narrow line edit preserves its formatting and
    every unrelated site-local value.
    """

    lines = text.splitlines(keepends=True)
    camera_start: int | None = None
    camera_end = len(lines)

    for index, line in enumerate(lines):
        match = _CAMERA_HEADER_RE.match(line)
        if match:
            if camera_start is not None:
                camera_end = index
                break
            if match.group(1).strip() == EXPECTED_CAMERA:
                camera_start = index + 1
                continue
        if camera_start is not None and _SECTION_HEADER_RE.match(line):
            camera_end = index
            break

    if camera_start is None:
        raise ValueError(f"missing [[{EXPECTED_CAMERA}]] camera section")

    found: dict[str, tuple[int, re.Match[str]]] = {}
    for index in range(camera_start, camera_end):
        match = _SETTING_RE.match(lines[index])
        if not match:
            continue
        key = match.group("key")
        if key in {"url", *TARGET_VALUES}:
            if key in found:
                raise ValueError(
                    f"duplicate {key!r} in [[{EXPECTED_CAMERA}]] section"
                )
            found[key] = (index, match)

    url_entry = found.get("url")
    if url_entry is None:
        raise ValueError(f"missing url in [[{EXPECTED_CAMERA}]] section")
    actual_url = url_entry[1].group("value").strip()
    if actual_url != EXPECTED_URL:
        raise ValueError(
            f"refusing unexpected [[{EXPECTED_CAMERA}]] url {actual_url!r}"
        )

    missing = sorted(set(TARGET_VALUES) - set(found) - INSERT_IF_MISSING)
    if missing:
        raise ValueError(
            f"missing required setting(s) in [[{EXPECTED_CAMERA}]]: "
            + ", ".join(missing)
        )

    changes: list[tuple[str, str, str]] = []
    for key, new_value in TARGET_VALUES.items():
        if key not in found:
            continue
        index, match = found[key]
        old_value = match.group("value").strip()
        if old_value == new_value:
            continue
        lines[index] = "".join(
            (
                match.group("indent"),
                key,
                match.group("spacing"),
                new_value,
                match.group("suffix"),
                match.group("newline"),
            )
        )
        changes.append((key, old_value, new_value))

    for key in sorted(INSERT_IF_MISSING - set(found)):
        # Put a missing optional setting at the end of the camera section,
        # before its separating blank lines. Match the camera's indentation
        # and newline convention instead of reformatting the site-local file.
        insert_at = camera_end
        while insert_at > camera_start and not lines[insert_at - 1].strip():
            insert_at -= 1
        url_match = url_entry[1]
        newline = "\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"
        lines.insert(
            insert_at,
            f"{url_match.group('indent')}{key} = {TARGET_VALUES[key]}{newline}",
        )
        changes.append((key, "(missing)", TARGET_VALUES[key]))

    return "".join(lines), changes


def install(path: Path, corrected: str) -> Path:
    """Back up and atomically install one already-validated config string."""

    stat = path.stat()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = path.with_name(f"{path.name}.before-stream-{stamp}")
    shutil.copy2(path, backup)
    os.chmod(backup, 0o600)

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
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/home/serv/stream/config.ini"),
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    original = args.config.read_text(encoding="utf-8")
    corrected, changes = corrected_text(original)
    if not changes:
        print("LJS Stream config already has the reviewed correction")
        return 0

    print("LJS Stream config correction; changing:")
    for key, old_value, new_value in changes:
        print(f"  {key}: {old_value} -> {new_value}")
    if args.check:
        print("check only; no file changed")
        return 0

    backup = install(args.config, corrected)
    print(f"installed atomically; backup={backup}")
    print("restart when the lane is empty: docker restart stream")
    print("then verify: curl -fsS http://127.0.0.1:8001/status/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
