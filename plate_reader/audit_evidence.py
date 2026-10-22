"""Bounded, local-only full-frame evidence. Never recognition/count authority."""
from dataclasses import dataclass, replace
from datetime import datetime
import math


def epoch(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        result = parsed.timestamp()
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class AuditFrame:
    camera: str
    observed_at: str
    epoch: float
    received_at: float
    jpeg: bytes


class AuditFrameCache:
    """Reuse already-received originals; no camera reader or extra inference.

    A cached frame may improve a case only when its camera and provider time
    match an actual sample in that case's native trajectory. The 50ms tolerance
    accommodates timestamp rounding, not a later snapshot of another car.
    Caller holds the engine lock. Caps apply to this shared cache, independently
    of the existing per-encounter evidence lifetime.
    """
    MAX_FRAMES = 16
    MAX_BYTES = 8 * 1024 * 1024
    TTL = 120.0
    SKEW = 0.05

    def __init__(self):
        self.frames = []

    def prune(self, now):
        self.frames = [f for f in self.frames if 0 <= now - f.received_at <= self.TTL]

    def add(self, camera, observed_at, jpeg, now):
        self.prune(now)
        stamp = epoch(observed_at)
        if not camera or stamp is None or not jpeg or len(jpeg) > self.MAX_BYTES:
            return
        # Conflicting images at one timestamp are not uniquely owned evidence.
        same = [f for f in self.frames if f.camera == camera and f.epoch == stamp]
        if same:
            if same[0].jpeg != jpeg:
                self.frames = [replace(f, jpeg=b"") if f is same[0] else f
                               for f in self.frames]
            return
        self.frames.append(AuditFrame(camera, observed_at, stamp, now, jpeg))
        while (len(self.frames) > self.MAX_FRAMES
               or sum(len(f.jpeg) for f in self.frames) > self.MAX_BYTES):
            self.frames.pop(0)

    def latest_on_track(self, camera, timestamps, boxes, after, now):
        self.prune(now)
        if not camera:
            return None
        cutoff = epoch(after)
        candidates = []
        for f in self.frames:
            if (not f.jpeg or f.camera != camera
                    or (cutoff is not None and f.epoch <= cutoff)):
                continue
            matches = []
            for stamp, box in zip(timestamps, boxes):
                when = epoch(stamp)
                if (when is not None and box is not None
                        and abs(when - f.epoch) <= self.SKEW):
                    matches.append(tuple(box))
            # Duplicate times with different boxes cannot locate a unique owner.
            if matches and len(set(matches)) == 1:
                candidates.append((f, matches[0]))
        return max(candidates, key=lambda item: item[0].epoch, default=None)


def evidence_timing(image_at, terminal_at):
    """Describe evidence gaps without claiming human plate visibility."""
    image, terminal = epoch(image_at), epoch(terminal_at)
    if image is None or terminal is None:
        return {"imageToTrackEndSeconds": None, "imageTiming": "unknown"}
    gap = terminal - image
    status = "near_track_end" if abs(gap) <= 1.0 else (
        "early_frame" if gap > 1.0 else "after_track_end"
    )
    return {"imageToTrackEndSeconds": round(gap, 3), "imageTiming": status}
