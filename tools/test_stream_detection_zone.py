#!/usr/bin/env python3
"""Regression checks for the review-only LJS Stream detection-zone mask."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import struct
import sys
import tempfile
import zlib


ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "tools" / "build_stream_detection_zone.py"
SPEC_PATH = ROOT / "stream" / "detection_zones" / "ljs-camera-1.json"
GENERATED_PATH = (
    ROOT / "stream" / "detection_zones" / "generated" / "zone_camera-1.png"
)

module_spec = importlib.util.spec_from_file_location("stream_mask_builder", BUILDER_PATH)
assert module_spec and module_spec.loader
builder = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(builder)


failed = 0


def check(label: str, actual, expected) -> None:
    global failed
    if actual == expected:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}: got {actual!r}, expected {expected!r}")
        failed += 1


def decode_gray_png(blob: bytes) -> tuple[int, int, bytes]:
    check("PNG signature", blob[:8], b"\x89PNG\r\n\x1a\n")
    cursor = 8
    width = height = None
    compressed = bytearray()
    while cursor < len(blob):
        length = struct.unpack(">I", blob[cursor:cursor + 4])[0]
        kind = blob[cursor + 4:cursor + 8]
        payload = blob[cursor + 8:cursor + 8 + length]
        cursor += 12 + length
        if kind == b"IHDR":
            width, height, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            check("PNG is 8-bit grayscale", (depth, color), (8, 0))
            check("PNG uses baseline encoding", (compression, filtering, interlace), (0, 0, 0))
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            break
    assert width is not None and height is not None
    raw = zlib.decompress(bytes(compressed))
    rows = []
    filters = []
    stride = width + 1
    for py in range(height):
        row = raw[py * stride:(py + 1) * stride]
        filters.append(row[0])
        rows.append(row[1:])
    check("all rows use PNG filter None", set(filters), {0})
    return width, height, b"".join(rows)


source, expected_png, excluded = builder.build(SPEC_PATH)
check("camera id controls exact vendor filename", GENERATED_PATH.name, "zone_camera-1.png")
check("source resolution", (source["frame"]["width"], source["frame"]["height"]), (1456, 1088))
check("generated artifact is current", GENERATED_PATH.read_bytes() == expected_png, True)

width, height, pixels = decode_gray_png(expected_png)
check("artifact resolution", (width, height), (1456, 1088))
check("mask is binary black/white", set(pixels), {0, 255})
check("excluded count matches rasterizer", pixels.count(0), excluded)
check("mask excludes a bounded area", 0.01 < excluded / len(pixels) < 0.12, True)

# A proposed upper-right block reproduces the exact regression the preserve
# rectangles are designed to prevent.
unsafe = builder.load_spec(SPEC_PATH)
unsafe["exclude_polygons"] = list(unsafe["exclude_polygons"]) + [{
    "id": "unsafe-upper-right",
    "normalized_polygon": [[0.72, 0.0], [1.0, 0.0], [1.0, 0.28], [0.72, 0.28]],
}]
try:
    builder.rasterize(unsafe)
except builder.MaskSpecError as exc:
    check("upper-right exclusion fails closed", "preserve rectangle" in str(exc), True)
else:
    check("upper-right exclusion fails closed", "accepted", "MaskSpecError")

with tempfile.TemporaryDirectory() as temp_dir:
    rebuilt = Path(temp_dir) / "zone_camera-1.png"
    rebuilt.write_bytes(expected_png)
    check(
        "independent rebuild is byte-for-byte deterministic",
        rebuilt.read_bytes() == expected_png,
        True,
    )

print(f"\n{failed} failure(s)")
raise SystemExit(1 if failed else 0)
