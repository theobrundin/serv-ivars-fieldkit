"""Thin wrapper over Picamera2 with safe init/teardown and detection-zone
visualization. Keeping Picamera2 details isolated here makes detector.py
testable on a Mac without the Pi-specific stack.
"""
import time
from pathlib import Path

from .config import Camera as CameraCfg, Capture as CaptureCfg, DetectionZone
from .log import get

log = get(__name__)


class CameraError(Exception):
    pass


class Camera:
    """Owns the Picamera2 instance. Use as a context manager or call close()."""

    def __init__(
        self,
        cfg: CameraCfg,
        capture_cfg: CaptureCfg | None = None,
    ) -> None:
        self.cfg = cfg
        self.capture_cfg = capture_cfg
        self._cam = None

    def open(self) -> None:
        try:
            # Local import: Picamera2 isn't available on dev machines (Macs)
            from picamera2 import Picamera2
        except ImportError as e:
            raise CameraError(
                f"picamera2 not installed: {e}. Run scripts/install.sh."
            ) from e

        try:
            self._cam = Picamera2()
            width = (
                self.capture_cfg.width
                if self.capture_cfg is not None else self.cfg.resolution_width
            )
            height = (
                self.capture_cfg.height
                if self.capture_cfg is not None else self.cfg.resolution_height
            )
            fps = (
                self.capture_cfg.target_fps
                if self.capture_cfg is not None else float(self.cfg.framerate)
            )
            frame_us = max(1, int(round(1_000_000.0 / fps)))
            # This is a continuous inference stream, not a still capture. A
            # video configuration provides a real buffer queue and fast noise
            # reduction, while queue=False ensures capture_array() waits for a
            # current frame instead of returning a stale queued image.
            config = self._cam.create_video_configuration(
                main={
                    "size": (width, height),
                    "format": "RGB888",
                },
                controls={"FrameDurationLimits": (frame_us, frame_us)},
                buffer_count=4,
                queue=False,
            )
            self._cam.configure(config)
            self._cam.start()
            # Give the sensor a moment to settle (auto-exposure / focus stabilization)
            time.sleep(1.0)
            log.info(
                "camera started: %dx%d @ ~%dfps",
                width,
                height,
                fps,
            )
        except Exception as e:
            raise CameraError(f"failed to start Picamera2: {e}") from e

    def capture(self):
        """Return a single frame as a numpy array (H, W, 3) in RGB."""
        if self._cam is None:
            raise CameraError("camera not opened")
        return self._cam.capture_array()

    def close(self) -> None:
        if self._cam is not None:
            try:
                self._cam.stop()
                self._cam.close()
            except Exception as e:
                log.warning("camera close error (ignored): %s", e)
            finally:
                self._cam = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()


class NullCamera:
    """Stand-in for engines that own the capture themselves.

    Plate Recognizer Stream reads the CSI device through MediaMTX, so this
    process must NOT open Picamera2 — libcamera hands the sensor to exactly
    one consumer, and grabbing it here would starve the RTSP feed that Stream
    depends on.

    The detection loop is still the authority on pacing and still calls
    `capture()` once per tick, so this returns a correctly-shaped black frame
    at the configured rate. Only `frame.shape` is ever read downstream (the
    detection-zone test in `_run_one`); the engine ignores the frame entirely
    because Stream already did the inference. Allocated once and reused —
    nothing writes to it, and a fresh 1456x1088x3 array per tick would be
    pure garbage-collector churn.
    """

    def __init__(self, capture_cfg: CaptureCfg) -> None:
        self._cfg = capture_cfg
        self._interval = 1.0 / max(float(capture_cfg.target_fps), 0.1)
        self._frame = None
        self._next_at = 0.0

    def open(self) -> None:
        try:
            import numpy as np
            self._frame = np.zeros(
                (self._cfg.height, self._cfg.width, 3), dtype=np.uint8
            )
        except Exception as e:                       # pragma: no cover
            raise CameraError(f"numpy unavailable for NullCamera: {e}") from e
        log.info(
            "NullCamera active (%dx%d @ %.1ffps): capture is owned by the "
            "recognition engine, not this process",
            self._cfg.width, self._cfg.height, self._cfg.target_fps,
        )

    def capture(self):
        if self._frame is None:
            raise CameraError("camera not opened")
        # Pace the loop so vote windows and cooldowns keep their real-time
        # meaning even though there is no hardware clocking us.
        now = time.monotonic()
        if self._next_at and now < self._next_at:
            time.sleep(self._next_at - now)
        self._next_at = max(now, self._next_at) + self._interval
        return self._frame

    def close(self) -> None:
        self._frame = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()


def save_zone_preview(
    frame,
    zone: DetectionZone,
    out_path: Path,
    roi=None,
    dwell_zone=None,
    overlay: bool = True,
) -> None:
    """Save a JPEG of the current frame with the configured regions drawn on.

    Written at boot so aim AND config can be verified remotely in one glance —
    scp the image back rather than needing a live session on site.

    `roi` and `dwell_zone` are normalized config.NormBox objects; they are
    converted to pixels here, against the real frame, which is the single
    place that conversion should happen.
    """
    try:
        import cv2  # provided by python3-opencv
        import numpy as np  # noqa: F401  (comes with cv2)

        img = frame.copy()
        h, w = img.shape[:2]
        x1 = int(w * zone.x_min_pct)
        x2 = int(w * zone.x_max_pct)
        y1 = int(h * zone.y_min_pct)
        y2 = int(h * zone.y_max_pct)

        # OpenCV is BGR; our frame is RGB
        bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        if zone.enabled:
            cv2.rectangle(bgr, (x1, y1), (x2, y2), (0, 0, 255), 3)
            cv2.putText(
                bgr, "DETECTION ZONE", (x1 + 6, y1 + 28),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2,
            )
        else:
            cv2.putText(
                bgr, "ZONE DISABLED", (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2,
            )

        legend = ["red = detection zone"]
        if overlay and roi is not None and getattr(roi, "enabled", False):
            rx1, ry1, rx2, ry2 = roi.to_pixels(w, h)
            cv2.rectangle(bgr, (rx1, ry1), (rx2, ry2), (0, 255, 255), 3)
            cv2.putText(
                bgr, "ALPR ROI", (rx1 + 6, ry1 + 56),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2,
            )
            legend.append("yellow = alpr.roi (engine search area)")
        if overlay and dwell_zone is not None and getattr(dwell_zone, "enabled", False):
            dx1, dy1, dx2, dy2 = dwell_zone.to_pixels(w, h)
            cv2.rectangle(bgr, (dx1, dy1), (dx2, dy2), (255, 0, 255), 3)
            cv2.putText(
                bgr,
                f"DWELL ZONE ({getattr(dwell_zone, 'anchor', '?')})",
                (dx1 + 6, dy1 + 84),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 255), 2,
            )
            legend.append("magenta = presence.dwell_zone (counts as ordering)")

        for i, line in enumerate(legend):
            cv2.putText(
                bgr, line, (10, h - 12 - (len(legend) - 1 - i) * 24),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2,
            )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out_path), bgr)
        log.info("saved zone preview to %s", out_path)
    except Exception as e:
        log.warning("could not save zone preview: %s", e)


def bbox_inside_zone(
    bbox: tuple[int, int, int, int],
    frame_shape: tuple[int, int],
    zone: DetectionZone,
) -> bool:
    """Return True if the plate bbox (x1, y1, x2, y2) is majority-inside the zone.

    'Majority' = centroid of the bbox is inside the zone. We use centroid
    rather than full containment so a plate that's partially clipped at the
    zone edge still counts.
    """
    if not zone.enabled:
        return True

    h, w = frame_shape[:2]
    x1, y1, x2, y2 = bbox
    cx = (x1 + x2) / 2.0 / w
    cy = (y1 + y2) / 2.0 / h
    return (
        zone.x_min_pct <= cx <= zone.x_max_pct
        and zone.y_min_pct <= cy <= zone.y_max_pct
    )
