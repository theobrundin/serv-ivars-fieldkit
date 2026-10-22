# LJS Plate Recognizer detection-zone review

## Status

**Review only; not deployed.** The files here do not change the live Pi or the
online Plate Recognizer configuration. `serv-update` also does not install the
site-local Stream working directory.

Plate Recognizer defines its mask as a PNG named `zone_<camera-id>.png` in the
Stream working directory. It must match the source resolution and use only
black (exclude) and white (analyze). Stream must be restarted to load it. The
official reference is:

<https://guides.platerecognizer.com/docs/stream/configuration/#detection-zone>

## Safety decision

A static upper-right exclusion is **not safe for this camera**. It cannot
express “ignore a vehicle only if it later exits upper-right.” A mask knows
pixel position, not route order or direction.

The 28 Aug route-qualified corpus contains real plate boxes in the proposed
upper-right exclusion, including one at pixels `(1236,144)-(1306,219)` in a
1456×1088 frame. Its center is approximately `(0.873,0.167)`. Other true-lane
plates appear near `(0.692,0.145)` and across the left/menu-board corridor.
Blacking the terminal upper-right rectangle would therefore discard real plate
evidence before the completed trajectory can distinguish lane traffic from a
drive-off.

The review mask keeps all of these regions white:

- the full-left plate-identity rectangle, x `0.00–1.00`, y `0.00–0.75`;
- the full-left menu-board rectangle, x `0.00–0.72`, y `0.00–0.25`;
- the lower-right entry rectangle, x `0.52–1.00`, y `0.26–1.00`;
- the entire upper-right overlap corridor, including the terminal-exit band.

Only the unambiguous bottom-left landscaping wedge is black. It is below the
plate-identity region, left of the route-entry region, and inset from the curb.
This is intentionally a small optimization. Completed Stream trajectory plus
the app route classifier—not this mask—must continue to reject vehicles that
later exit upper-right.

## Reproducible build and checks

The reviewed geometry is readable JSON, not an opaque hand-painted bitmap:

- source: `stream/detection_zones/ljs-camera-1.json`
- builder: `tools/build_stream_detection_zone.py`
- generated artifact: `stream/detection_zones/generated/zone_camera-1.png`
- regression check: `tools/test_stream_detection_zone.py`

Build and verify:

```sh
python3 tools/build_stream_detection_zone.py \
  stream/detection_zones/ljs-camera-1.json \
  --output stream/detection_zones/generated/zone_camera-1.png
python3 tools/build_stream_detection_zone.py \
  stream/detection_zones/ljs-camera-1.json \
  --output stream/detection_zones/generated/zone_camera-1.png \
  --check
python3 tools/test_stream_detection_zone.py
```

Render a local review overlay without committing the camera frame. Red means
excluded; everything untinted remains analyzed:

```sh
mkdir -p /tmp/ljs-zone-review
ffmpeg -y \
  -i /path/to/current-1456x1088-full-frame.jpg \
  -i stream/detection_zones/generated/zone_camera-1.png \
  -filter_complex "[1:v]format=gray,negate,lut=y='val*0.45'[a];color=c=red:s=1456x1088,format=rgb24[r];[r][a]alphamerge[shade];[0:v][shade]overlay" \
  -frames:v 1 /tmp/ljs-zone-review/ljs-camera-1-mask-overlay.png
```

Before any future deployment, repeat the overlay against a same-day full
frame and replay a labelled clip containing at least true-lane approaches,
true menu-board stops, upper-right exits, and background traffic. Confirm the
running camera ID is exactly `camera-1` and the Stream source is still
1456×1088. Deployment and restart require a separate operator decision.
