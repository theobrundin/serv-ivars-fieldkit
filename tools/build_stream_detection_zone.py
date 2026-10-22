#!/usr/bin/env python3
"""Build and verify a Plate Recognizer Stream detection-zone mask.

The source of truth is a reviewed JSON geometry file.  The generated PNG is
strictly binary: white pixels are analyzed by Stream and black pixels are
excluded.  No camera image is needed to build it, which keeps raw site images
and plate text out of Git.

This tool deliberately validates preserve rectangles *after* rasterization.
That catches rounding errors as well as obviously unsafe polygons.
"""

from __future__ import annotations

import argparse
import binascii
import json
from pathlib import Path
import re
import struct
import sys
import zlib


ANALYZE = 255
EXCLUDE = 0
_CAMERA_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


class MaskSpecError(ValueError):
    """The reviewed mask specification is invalid or violates a guardrail."""


def _number(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise MaskSpecError(f"{label} must be a number")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise MaskSpecError(f"{label} must be in [0, 1], got {result}")
    return result


def load_spec(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaskSpecError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise MaskSpecError("top-level mask spec must be an object")
    if raw.get("schema_version") != 1:
        raise MaskSpecError("schema_version must be 1")

    camera_id = raw.get("camera_id")
    if not isinstance(camera_id, str) or not _CAMERA_ID.fullmatch(camera_id):
        raise MaskSpecError("camera_id must be a safe non-empty identifier")

    frame = raw.get("frame")
    if not isinstance(frame, dict):
        raise MaskSpecError("frame must be an object")
    for key in ("width", "height"):
        value = frame.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise MaskSpecError(f"frame.{key} must be a positive integer")

    exclusions = raw.get("exclude_polygons")
    if not isinstance(exclusions, list):
        raise MaskSpecError("exclude_polygons must be a list")
    seen_ids: set[str] = set()
    for index, exclusion in enumerate(exclusions):
        label = f"exclude_polygons[{index}]"
        if not isinstance(exclusion, dict):
            raise MaskSpecError(f"{label} must be an object")
        item_id = exclusion.get("id")
        if not isinstance(item_id, str) or not item_id or item_id in seen_ids:
            raise MaskSpecError(f"{label}.id must be non-empty and unique")
        seen_ids.add(item_id)
        polygon = exclusion.get("normalized_polygon")
        if not isinstance(polygon, list) or len(polygon) < 3:
            raise MaskSpecError(f"{label}.normalized_polygon needs >=3 points")
        for point_index, point in enumerate(polygon):
            point_label = f"{label}.normalized_polygon[{point_index}]"
            if not isinstance(point, list) or len(point) != 2:
                raise MaskSpecError(f"{point_label} must be [x, y]")
            _number(point[0], f"{point_label}[0]")
            _number(point[1], f"{point_label}[1]")

    preserves = raw.get("preserve_rectangles")
    if not isinstance(preserves, list) or not preserves:
        raise MaskSpecError("preserve_rectangles must be a non-empty list")
    for index, preserve in enumerate(preserves):
        label = f"preserve_rectangles[{index}]"
        if not isinstance(preserve, dict):
            raise MaskSpecError(f"{label} must be an object")
        rectangle = preserve.get("normalized_rect")
        if not isinstance(rectangle, list) or len(rectangle) != 4:
            raise MaskSpecError(f"{label}.normalized_rect must be [x1,y1,x2,y2]")
        x1, y1, x2, y2 = (
            _number(value, f"{label}.normalized_rect[{i}]")
            for i, value in enumerate(rectangle)
        )
        if x2 <= x1 or y2 <= y1:
            raise MaskSpecError(f"{label}.normalized_rect must have positive area")
    return raw


def _inside_polygon(x: float, y: float, polygon: list[list[float]]) -> bool:
    """Return whether one normalized pixel center is in a polygon."""
    inside = False
    previous = polygon[-1]
    for current in polygon:
        x1, y1 = previous
        x2, y2 = current
        crosses = (y1 > y) != (y2 > y)
        if crosses:
            crossing_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < crossing_x:
                inside = not inside
        previous = current
    return inside


def rasterize(spec: dict) -> tuple[int, int, bytearray]:
    width = spec["frame"]["width"]
    height = spec["frame"]["height"]
    pixels = bytearray([ANALYZE]) * (width * height)

    for exclusion in spec["exclude_polygons"]:
        polygon = [
            [float(point[0]), float(point[1])]
            for point in exclusion["normalized_polygon"]
        ]
        min_x = max(0, int(min(point[0] for point in polygon) * width) - 1)
        max_x = min(width, int(max(point[0] for point in polygon) * width) + 2)
        min_y = max(0, int(min(point[1] for point in polygon) * height) - 1)
        max_y = min(height, int(max(point[1] for point in polygon) * height) + 2)
        for py in range(min_y, max_y):
            ny = (py + 0.5) / height
            row = py * width
            for px in range(min_x, max_x):
                if _inside_polygon((px + 0.5) / width, ny, polygon):
                    pixels[row + px] = EXCLUDE

    _validate_preserves(spec, width, height, pixels)
    _validate_probe_points(spec, width, height, pixels)
    return width, height, pixels


def _pixel_bounds(rect: list[float], width: int, height: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = (float(value) for value in rect)
    # Select pixels by center.  The small +/-0.5 terms make the normalized
    # rectangle semantics independent of whether a boundary lands on a pixel.
    px1 = max(0, int(x1 * width - 0.5 + 0.999999999))
    py1 = max(0, int(y1 * height - 0.5 + 0.999999999))
    px2 = min(width, int(x2 * width - 0.5) + 1)
    py2 = min(height, int(y2 * height - 0.5) + 1)
    return px1, py1, px2, py2


def _validate_preserves(
    spec: dict, width: int, height: int, pixels: bytearray
) -> None:
    for preserve in spec["preserve_rectangles"]:
        bounds = _pixel_bounds(preserve["normalized_rect"], width, height)
        px1, py1, px2, py2 = bounds
        for py in range(py1, py2):
            row = py * width
            for px in range(px1, px2):
                if pixels[row + px] != ANALYZE:
                    item_id = preserve.get("id", "unnamed")
                    raise MaskSpecError(
                        f"exclusion overlaps preserve rectangle {item_id!r} "
                        f"at pixel ({px}, {py})"
                    )


def _point_pixel(point: list[float], width: int, height: int) -> tuple[int, int]:
    x = min(width - 1, int(float(point[0]) * width))
    y = min(height - 1, int(float(point[1]) * height))
    return x, y


def _validate_probe_points(
    spec: dict, width: int, height: int, pixels: bytearray
) -> None:
    for key, expected in (("white_probe_points", ANALYZE), ("black_probe_points", EXCLUDE)):
        probes = spec.get(key, [])
        if not isinstance(probes, list):
            raise MaskSpecError(f"{key} must be a list")
        for index, probe in enumerate(probes):
            if not isinstance(probe, dict):
                raise MaskSpecError(f"{key}[{index}] must be an object")
            point = probe.get("normalized_point")
            if not isinstance(point, list) or len(point) != 2:
                raise MaskSpecError(f"{key}[{index}].normalized_point must be [x,y]")
            _number(point[0], f"{key}[{index}].normalized_point[0]")
            _number(point[1], f"{key}[{index}].normalized_point[1]")
            px, py = _point_pixel(point, width, height)
            actual = pixels[py * width + px]
            if actual != expected:
                probe_id = probe.get("id", f"{key}[{index}]")
                raise MaskSpecError(
                    f"probe {probe_id!r} expected {expected}, got {actual} "
                    f"at pixel ({px}, {py})"
                )


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    body = chunk_type + payload
    return (
        struct.pack(">I", len(payload))
        + body
        + struct.pack(">I", binascii.crc32(body) & 0xFFFFFFFF)
    )


def encode_grayscale_png(width: int, height: int, pixels: bytearray) -> bytes:
    if len(pixels) != width * height:
        raise ValueError("pixel buffer has the wrong size")
    scanlines = bytearray()
    for py in range(height):
        scanlines.append(0)  # PNG filter type: None
        start = py * width
        scanlines.extend(pixels[start:start + width])
    return b"".join((
        b"\x89PNG\r\n\x1a\n",
        _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)),
        _png_chunk(b"IDAT", zlib.compress(bytes(scanlines), level=9)),
        _png_chunk(b"IEND", b""),
    ))


def build(spec_path: Path) -> tuple[dict, bytes, int]:
    spec = load_spec(spec_path)
    width, height, pixels = rasterize(spec)
    excluded = pixels.count(EXCLUDE)
    return spec, encode_grayscale_png(width, height, pixels), excluded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify output already matches the reviewed spec; do not write",
    )
    args = parser.parse_args()

    try:
        spec, png, excluded = build(args.spec)
    except MaskSpecError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    expected_name = f"zone_{spec['camera_id']}.png"
    if args.output.name != expected_name:
        print(
            f"ERROR: Plate Recognizer requires output filename {expected_name!r}",
            file=sys.stderr,
        )
        return 2

    if args.check:
        try:
            current = args.output.read_bytes()
        except OSError as exc:
            print(f"ERROR: cannot read {args.output}: {exc}", file=sys.stderr)
            return 1
        if current != png:
            print(f"ERROR: {args.output} is stale; rebuild it", file=sys.stderr)
            return 1
        action = "verified"
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(png)
        action = "wrote"

    total = spec["frame"]["width"] * spec["frame"]["height"]
    print(
        f"{action} {args.output}: {spec['frame']['width']}x"
        f"{spec['frame']['height']}, {excluded}/{total} pixels excluded "
        f"({excluded / total:.2%})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
