"""Wires the production plate logic into vehicle-centric records.

What is reused, unchanged
-------------------------
Everything that decides an IDENTITY comes from the production package and is
not reimplemented here:

  plate_reader.normalizer.normalize   NORMALIZATION_SPEC v1.0
  plate_reader.hasher.hash_plate      SHA-256, no salt
  plate_reader.detector.Detector      exact-match group voting, the group
                                      confidence floors, dwell suppression,
                                      per-hash cooldown and near-match dedup

That is deliberate. The contractor's benchmark and any output shown to anyone
must exercise the code that actually runs in the field, not a copy of it that
can drift. This module adds a layer on top; it changes nothing underneath.

What is new
-----------
The vehicle becomes the record (tracker.py), the plate becomes a field on it,
and colour / make / model are reconciled by highest-confidence-wins.
"""
from __future__ import annotations

import pathlib
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

_REPO = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from plate_reader.detector import (  # noqa: E402
    ConfirmedIdentification, Detector,
)
from plate_reader.hasher import hash_plate  # noqa: E402

from .config import VehicleRecordConfig  # noqa: E402
from .record import VehicleRecord  # noqa: E402
from .tracker import VehicleTracker, iou  # noqa: E402


@dataclass
class StageTiming:
    """Per-stage wall time, summed across frames. The contractor's CM5 test
    needs to know WHERE the time goes, not just the total."""

    engine_ms: float = 0.0
    vote_ms: float = 0.0
    track_ms: float = 0.0
    frames: int = 0

    def as_dict(self) -> dict[str, Any]:
        n = max(1, self.frames)
        total = self.engine_ms + self.vote_ms + self.track_ms
        return {
            "frames": self.frames,
            "engine_ms_per_frame": round(self.engine_ms / n, 2),
            "vote_ms_per_frame": round(self.vote_ms / n, 2),
            "track_ms_per_frame": round(self.track_ms / n, 2),
            "total_ms_per_frame": round(total / n, 2),
            "max_sustainable_fps": (
                round(1000.0 / (total / n), 2) if total > 0 else None
            ),
        }


class VehicleRecordPipeline:
    """One frame in, zero or more finalized VehicleRecords out."""

    def __init__(self, prod_cfg, vr_cfg: VehicleRecordConfig,
                 engine=None) -> None:
        self.cfg = prod_cfg
        self.vr_cfg = vr_cfg
        # The REAL detector: real thresholds, real voting, real dedup.
        self.detector = Detector(
            prod_cfg, enable_vehicle_only_tracking=False
        )
        if engine is not None:
            # Injected engine (replay, or the rich SDK adapter). Detector
            # normally builds its own from config; overriding it is how the
            # same voting logic runs against captured reads on a Mac.
            self.detector._engine = engine
        self.tracker = VehicleTracker(vr_cfg)
        self.timing = StageTiming()
        self._opened = False
        self.stats = {
            "frames": 0,
            "confirms": 0,
            "confirms_suppressed": 0,
            "vehicle_observations": 0,
        }

    def open(self) -> None:
        if self.detector._engine is None:
            self.detector.open()
        else:
            eng = self.detector._engine
            if hasattr(eng, "open"):
                eng.open()
        self._opened = True

    def close(self) -> None:
        try:
            self.detector.close()
        except Exception:
            pass
        self._opened = False

    def process_frame(self, frame, now: Optional[float] = None
                      ) -> Iterator[VehicleRecord]:
        now = time.time() if now is None else now

        t0 = time.perf_counter()
        candidates, outcomes = self.detector.process_frame(frame, now=now)
        t1 = time.perf_counter()

        # The detector drops colour/make/model when it converts RawPlateRead
        # into DetectionResult. Recover them from the engine's stash for this
        # frame, keyed by plate bbox (unique within a frame in practice).
        rich_by_bbox = {}
        for rr in getattr(self.detector._engine, "last_reads", []) or []:
            if rr.bbox:
                rich_by_bbox[tuple(rr.bbox)] = rr
        # Detector.process_frame does inference AND voting together, so they
        # cannot be separated without reaching inside it. Attributed to
        # `engine` because inference dominates by an order of magnitude
        # (~106ms vs sub-ms), and mislabelling the small term is harmless
        # while splitting the big one would need a fork of the detector.
        self.timing.engine_ms += (t1 - t0) * 1000.0

        # ---- vehicles ----
        t2 = time.perf_counter()
        accepted = []
        for c in candidates:
            vbox = getattr(c, "vehicle_bbox", None)
            if vbox is None:
                continue
            vconf = getattr(c, "vehicle_confidence", None)
            if (self.vr_cfg.min_vehicle_confidence > 0.0
                    and (vconf or 0.0) < self.vr_cfg.min_vehicle_confidence):
                continue
            in_zone = bool(getattr(c, "in_dwell_zone", True))
            accepted.append((c, tuple(vbox), vconf, in_zone))

        # Collapse duplicate/pyramidal detections of the same vehicle before
        # one-to-one association. The high IoU threshold avoids merging two
        # adjacent real cars while stopping one SDK frame from creating two
        # records for effectively identical vehicle boxes.
        unique_observations = []
        for _c, vbox, vconf, in_zone in accepted:
            duplicate_i = next(
                (i for i, (box, _conf, _zone) in enumerate(unique_observations)
                 if iou(vbox, box) >= 0.85),
                None,
            )
            if duplicate_i is None:
                unique_observations.append((vbox, vconf, in_zone))
            else:
                box, incumbent_conf, incumbent_zone = unique_observations[duplicate_i]
                best_conf = max(incumbent_conf or 0.0, vconf or 0.0)
                unique_observations[duplicate_i] = (
                    box, best_conf, incumbent_zone or in_zone
                )

        observed_tracks = self.tracker.observe_vehicles(
            unique_observations, now=now
        )
        self.stats["vehicle_observations"] += len(unique_observations)

        for c, vbox, _vconf, _in_zone in accepted:
            if not observed_tracks:
                continue
            t = max(observed_tracks, key=lambda track: iou(vbox, track.bbox))

            # A plate box on this vehicle. `readable` means text came out at
            # all — that is what separates LOW_CONFIDENCE (saw a plate,
            # couldn't read it) from NO_PLATE_VISIBLE (never saw one).
            bbox = getattr(c, "bbox", None)
            has_plate_box = bbox is not None and tuple(bbox) != (0, 0, 0, 0)
            if has_plate_box:
                self.tracker.note_plate_for_track(
                    t,
                    normalized=getattr(c, "normalized", None),
                    readable=bool(getattr(c, "raw_text", "")),
                )

            # Descriptive fields, when the engine supplies them. Null stays
            # null: a field whose classifier is off is simply never offered.
            rr = rich_by_bbox.get(tuple(bbox)) if bbox else None
            if rr is not None:
                self.tracker.offer_description_for_track(
                    t,
                    color=rr.color, color_conf=rr.color_confidence,
                    make=rr.make, make_conf=rr.make_confidence,
                    model=rr.model, model_conf=rr.model_confidence,
                )

        # ---- confirmed plates attach to their vehicle ----
        for o in outcomes:
            if not isinstance(o, ConfirmedIdentification):
                continue
            if getattr(o, "suppressed", False):
                # Dwell-suppressed re-read: the same car, still present. Not a
                # new identity and must not overwrite anything.
                self.stats["confirms_suppressed"] += 1
                continue
            self.stats["confirms"] += 1
            try:
                h = hash_plate(o.normalized)
            except Exception:
                continue
            self.tracker.attach_confirmed_plate(
                o.normalized, h, now,
                vehicle_bbox=getattr(o, "vehicle_bbox", None),
            )

        self.tracker.tick(now)
        t3 = time.perf_counter()
        self.timing.track_ms += (t3 - t2) * 1000.0
        self.timing.frames += 1
        self.stats["frames"] += 1

        yield from self.tracker.drain()

    def finish(self, now: Optional[float] = None) -> Iterator[VehicleRecord]:
        """End of stream: commit every record still open."""
        self.tracker.flush(now)
        yield from self.tracker.drain()

    def summary(self) -> dict[str, Any]:
        return {
            "frames": self.stats["frames"],
            "confirms": self.stats["confirms"],
            "confirms_suppressed": self.stats["confirms_suppressed"],
            "vehicle_observations": self.stats["vehicle_observations"],
            "tracker": dict(self.tracker.stats),
            "timing": self.timing.as_dict(),
        }
