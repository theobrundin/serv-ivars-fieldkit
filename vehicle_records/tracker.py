"""Vehicle-keyed tracking and the record lifecycle.

Why vehicle-keyed rather than plate-keyed
-----------------------------------------
The production presence tracker associates on the PLATE box, which is small,
often motion-blurred, and disappears entirely when a plate is occluded. On
measured footage its association broke on 64.6% of consecutive box pairs, so
single cars fragmented into several tracks and the vehicle count read 40
against ~30 real cars.

The SDK's car box is a far steadier association target: it is large, present
on every frame the vehicle is visible, and does not vanish when the plate
does. Keying the record on the vehicle is not just a schema change — it is
what makes "one car, one record" achievable at all.

This does NOT replace plate voting. Plate confirmation still comes from the
production Detector, unchanged and exact-match. This layer decides which
VEHICLE a confirmed plate belongs to.
"""
from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional

from .config import SendTrigger, VehicleRecordConfig
from .record import PlateStatus, VehicleRecord


def iou(a, b) -> float:
    """Intersection over union of two (x1, y1, x2, y2) boxes."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


@dataclass
class VehicleTrack:
    """One vehicle being followed across frames, with its live record."""

    track_id: int
    bbox: tuple
    record: VehicleRecord
    first_seen: float
    last_seen: float
    in_zone: bool = False
    was_in_zone: bool = False
    emitted: bool = False
    # Normalized plate strings seen on THIS vehicle, so a later confirm can be
    # attributed to the right track rather than the most recent one.
    seen_plates: set = field(default_factory=set)


class VehicleTracker:
    """Owns vehicle tracks, their records, and when records commit."""

    def __init__(self, cfg: VehicleRecordConfig) -> None:
        self.cfg = cfg
        self._tracks: dict[int, VehicleTrack] = {}
        self._next_id = 1
        # A short boot/session component prevents record ids from repeating
        # every time the service restarts. This is a correlation id, not a
        # customer identity.
        self._device_prefix = f"veh-{uuid.uuid4().hex[:8]}"
        # Records committed but not yet drained by the caller.
        self._outbox: deque[VehicleRecord] = deque()
        self.stats = {
            "tracks_created": 0,
            "records_emitted": 0,
            "dropped_min_frames": 0,
            "plate_attach_by_string": 0,
            "plate_attach_by_bbox": 0,
            "plate_attach_by_unique_fallback": 0,
            "plate_attach_ambiguous": 0,
            "plate_attach_failed": 0,
        }

    # ---------------- association ----------------
    def _match(self, bbox) -> Optional[VehicleTrack]:
        best, best_iou = None, 0.0
        for t in self._tracks.values():
            v = iou(bbox, t.bbox)
            if v > best_iou:
                best, best_iou = t, v
        if best is not None and best_iou >= self.cfg.iou_match_threshold:
            return best
        return None

    def _create_track(self, bbox, now: float) -> VehicleTrack:
        rid = f"{self._device_prefix}-{self._next_id:06d}"
        rec = VehicleRecord(record_id=rid, first_seen=now)
        t = VehicleTrack(track_id=self._next_id, bbox=tuple(bbox),
                         record=rec, first_seen=now, last_seen=now)
        self._tracks[self._next_id] = t
        self._next_id += 1
        self.stats["tracks_created"] += 1
        return t

    @staticmethod
    def _update_track(t: VehicleTrack, bbox, now: float, in_zone: bool) -> None:
        t.bbox = tuple(bbox)
        t.last_seen = now
        if not t.record.frozen:
            t.record.touch(now)
        t.in_zone = bool(in_zone)
        if in_zone:
            t.was_in_zone = True

    def observe_vehicles(
        self,
        observations: Iterable[tuple[tuple, Optional[float], bool]],
        now: float,
    ) -> list[VehicleTrack]:
        """Associate all vehicle boxes from one capture one-to-one.

        Matching boxes independently lets two boxes claim the same existing
        track in one frame. That silently merges two physical cars. A global
        greedy assignment over all IoU pairs guarantees one observation per
        track per capture; unmatched observations create new tracks.
        """
        obs = [(tuple(box), confidence, bool(in_zone))
               for box, confidence, in_zone in observations]
        if not obs:
            return []

        existing = [t for t in self._tracks.values() if not t.emitted]
        pairs = []
        for obs_i, (box, _confidence, _in_zone) in enumerate(obs):
            for t in existing:
                score = iou(box, t.bbox)
                if score >= self.cfg.iou_match_threshold:
                    pairs.append((score, obs_i, t.track_id))

        assignments: dict[int, VehicleTrack] = {}
        used_tracks: set[int] = set()
        for _score, obs_i, track_id in sorted(pairs, reverse=True):
            if obs_i in assignments or track_id in used_tracks:
                continue
            assignments[obs_i] = self._tracks[track_id]
            used_tracks.add(track_id)

        result = []
        for obs_i, (box, _confidence, in_zone) in enumerate(obs):
            t = assignments.get(obs_i)
            if t is None:
                t = self._create_track(box, now)
            self._update_track(t, box, now, in_zone)
            result.append(t)
        return result

    def observe_vehicle(self, bbox, confidence: Optional[float], now: float,
                        in_zone: bool) -> VehicleTrack:
        """Compatibility wrapper for a single-observation capture."""
        return self.observe_vehicles(
            [(tuple(bbox), confidence, in_zone)], now=now
        )[0]

    def note_plate_for_track(self, t: VehicleTrack, normalized: Optional[str],
                             readable: bool) -> None:
        """Attach plate evidence to an already-associated vehicle track."""
        if t.record.frozen:
            return
        t.record.plate_boxes_seen += 1
        if readable:
            t.record.plate_reads_attempted += 1
            t.record.note_plate_seen_but_unread()
        if normalized:
            t.seen_plates.add(normalized)

    # ---------------- plate attribution ----------------
    def note_plate_box(self, vehicle_bbox, normalized: Optional[str],
                       readable: bool) -> None:
        """Compatibility helper when the caller has only a vehicle box."""
        if vehicle_bbox is None:
            return
        t = self._match(vehicle_bbox)
        if t is None:
            return
        self.note_plate_for_track(t, normalized, readable)

    def attach_confirmed_plate(self, normalized: str, plate_hash: str,
                               now: float, vehicle_bbox=None) -> bool:
        """Attach a CONFIRMED plate to the vehicle that produced it.

        Exact plate evidence wins, with vehicle-box overlap as the tie-break.
        If the string was not retained, spatial overlap may attach it. A
        fallback is allowed only when exactly one unresolved live vehicle
        exists. With multiple candidates we refuse the guess: no identity is
        safer than assigning the right plate to the wrong customer.
        """
        live = [t for t in self._tracks.values()
                if not t.record.frozen
                and t.record.plate_status is not PlateStatus.CONFIRMED]

        exact = [t for t in live if normalized in t.seen_plates]
        if exact:
            t = max(
                exact,
                key=lambda x: (
                    iou(vehicle_bbox, x.bbox) if vehicle_bbox is not None else 0.0,
                    x.last_seen,
                ),
            )
            t.record.confirm_plate(plate_hash)
            self.stats["plate_attach_by_string"] += 1
            return True

        if vehicle_bbox is not None:
            spatial = [(iou(vehicle_bbox, t.bbox), t) for t in live]
            spatial = [(score, t) for score, t in spatial
                       if score >= self.cfg.iou_match_threshold]
            if spatial:
                _score, t = max(spatial, key=lambda item: item[0])
                t.record.confirm_plate(plate_hash)
                self.stats["plate_attach_by_bbox"] += 1
                return True

        if len(live) == 1:
            live[0].record.confirm_plate(plate_hash)
            self.stats["plate_attach_by_unique_fallback"] += 1
            return True

        if len(live) > 1:
            self.stats["plate_attach_ambiguous"] += 1
            return False

        self.stats["plate_attach_failed"] += 1
        return False

    # ---------------- descriptive fields ----------------
    def offer_description(self, vehicle_bbox, color=None, color_conf=None,
                          make=None, make_conf=None,
                          model=None, model_conf=None) -> None:
        if vehicle_bbox is None:
            return
        t = self._match(vehicle_bbox)
        if t is None or t.record.frozen:
            return
        if self.cfg.color_enabled:
            t.record.offer_color(color, color_conf)
        if self.cfg.make_model_enabled:
            t.record.offer_make_model(make, model, make_conf, model_conf)

    def offer_description_for_track(
        self, t: VehicleTrack, color=None, color_conf=None,
        make=None, make_conf=None, model=None, model_conf=None,
    ) -> None:
        """Offer classifier output to an already-associated vehicle track."""
        if t.record.frozen:
            return
        if self.cfg.color_enabled:
            t.record.offer_color(color, color_conf)
        if self.cfg.make_model_enabled:
            t.record.offer_make_model(make, model, make_conf, model_conf)

    # ---------------- lifecycle ----------------
    def _commit(self, t: VehicleTrack, now: float, trigger: str) -> None:
        if t.emitted or t.record.frozen:
            return
        if t.record.frames_observed < self.cfg.min_track_frames:
            self.stats["dropped_min_frames"] += 1
            t.emitted = True
            return
        if (t.record.plate_status is not PlateStatus.CONFIRMED
                and not self.cfg.emit_plateless):
            t.emitted = True
            return
        t.record.freeze(now=now, trigger=trigger)
        t.emitted = True
        self._outbox.append(t.record)
        self.stats["records_emitted"] += 1

    def tick(self, now: Optional[float] = None) -> None:
        """Apply the configured send trigger and close timed-out tracks."""
        now = time.time() if now is None else now
        trig = self.cfg.send_trigger

        for t in list(self._tracks.values()):
            if t.emitted:
                continue
            if trig is SendTrigger.INSTANT:
                self._commit(t, now, "instant")
            elif trig is SendTrigger.ZONE_ENTRY and t.was_in_zone:
                self._commit(t, now, "zone_entry")
            elif (trig is SendTrigger.ZONE_EXIT
                  and t.was_in_zone and not t.in_zone
                  and (now - t.last_seen) >= self.cfg.track_timeout_seconds):
                self._commit(t, now, "zone_exit")

        # Track closure. FRAME_EXIT commits here; the others use it to release
        # tracks that were already committed, and as the backstop for a
        # ZONE_EXIT track that left frame without ever clearing the zone.
        for tid, t in list(self._tracks.items()):
            if (now - t.last_seen) < self.cfg.track_timeout_seconds:
                continue
            if not t.emitted:
                self._commit(t, now, "frame_exit")
            self._tracks.pop(tid, None)

    def flush(self, now: Optional[float] = None) -> None:
        """End of stream: commit everything still open."""
        now = time.time() if now is None else now
        for tid, t in list(self._tracks.items()):
            if not t.emitted:
                self._commit(t, now, "flush_eof")
            self._tracks.pop(tid, None)

    def drain(self) -> Iterator[VehicleRecord]:
        """Take committed records. Each record is yielded exactly once."""
        while self._outbox:
            yield self._outbox.popleft()

    @property
    def active_tracks(self) -> int:
        return len(self._tracks)
