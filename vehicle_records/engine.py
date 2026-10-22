"""Engines that yield the descriptive fields, not just the plate.

Two implementations, one interface:

  RichUltimateAlprEngine   the real SDK. aarch64 Linux only — there is no
                           macOS build. This is what the CM5 performance
                           test exercises.
  ReplayEngine             replays rich reads captured from a previous real
                           SDK run (JSONL from capture.py). Runs anywhere,
                           needs no SDK, and drives the FULL downstream
                           pipeline with genuine engine output — which is how
                           the record architecture can be inspected on a Mac
                           without pretending to have an OCR model.

Why a subclass rather than a fork
---------------------------------
RichUltimateAlprEngine extends the production adapter instead of
reimplementing it, so it inherits the licence resolution, the trial-mode '*'
redaction guard, the confidence-scale conversion and the confidences-layout
validation. Those are the parts that are silently catastrophic when wrong,
and a fork of them would be a second thing to keep correct.

What it adds: the SDK already returns car.color and car.makeModelYear when
their classifiers are enabled, and the production adapter DISCARDS them — it
parses only car.warpedBox and car.confidence. `klass_vcr_enabled` is on in
the shipped config, so colour is being computed and thrown away today.
"""
from __future__ import annotations

import json
import pathlib
import sys
from dataclasses import dataclass
from typing import Optional

_REPO = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from plate_reader.engines import (  # noqa: E402
    RawPlateRead, RedactedPlateTextError, UltimateAlprEngine,
)


# RawPlateRead is @dataclass(frozen=True); Python forbids a non-frozen
# dataclass inheriting from a frozen one, so this must be frozen too.
@dataclass(frozen=True)
class RichPlateRead(RawPlateRead):
    """A RawPlateRead plus the descriptive fields the SDK already computes."""

    color: Optional[str] = None
    color_confidence: Optional[float] = None
    make: Optional[str] = None
    make_confidence: Optional[float] = None
    model: Optional[str] = None
    model_confidence: Optional[float] = None

    def to_json(self) -> dict:
        return {
            "text": self.text,
            "det": self.detection_confidence,
            "ocr": self.ocr_confidence,
            "bbox": list(self.bbox) if self.bbox else None,
            "vbox": list(self.vehicle_bbox) if self.vehicle_bbox else None,
            "vconf": self.vehicle_confidence,
            "jurisdiction": self.jurisdiction,
            "color": self.color, "color_conf": self.color_confidence,
            "make": self.make, "make_conf": self.make_confidence,
            "model": self.model, "model_conf": self.model_confidence,
        }

    @staticmethod
    def from_json(d: dict) -> "RichPlateRead":
        return RichPlateRead(
            text=d.get("text", "") or "",
            detection_confidence=float(d.get("det") or 0.0),
            ocr_confidence=float(d.get("ocr") or 0.0),
            bbox=tuple(d["bbox"]) if d.get("bbox") else (0, 0, 0, 0),
            vehicle_bbox=tuple(d["vbox"]) if d.get("vbox") else None,
            vehicle_confidence=d.get("vconf"),
            jurisdiction=d.get("jurisdiction"),
            color=d.get("color"), color_confidence=d.get("color_conf"),
            make=d.get("make"), make_confidence=d.get("make_conf"),
            model=d.get("model"), model_confidence=d.get("model_conf"),
        )


def _best_entry(entries):
    """Highest-confidence entry from an SDK classifier list.

    Returns the whole entry, not one field, so make and model are read from
    the SAME entry — picking each independently could pair the make from one
    candidate with the model from another and invent a vehicle that was never
    proposed.

    This is per-FRAME. Cross-frame reconciliation is the record's job (see
    the record's atomic make/model and highest-confidence color fields.
    """
    if not entries:
        return None, None
    best = max(entries, key=lambda e: float(e.get("confidence") or 0.0))
    conf = best.get("confidence")
    # SDK confidences are 0-100; this layer speaks 0.0-1.0 like everything
    # else downstream. Mixing the two makes every threshold meaningless.
    return best, (float(conf) / 100.0 if conf is not None else None)


class RichUltimateAlprEngine(UltimateAlprEngine):
    """The production adapter, plus colour / make / model / jurisdiction."""

    name = "ultimate_alpr_rich"

    # The production Detector converts RawPlateRead -> DetectionResult and
    # drops the descriptive fields. Rather than fork the detector, each frame's
    # rich reads are stashed here so the pipeline can correlate them back by
    # plate bbox after process_frame() returns.
    last_reads: list = []

    def __init__(self, cfg=None) -> None:
        super().__init__(cfg)
        self.last_reads = []

    def infer(self, frame) -> list[RichPlateRead]:
        if not self._opened or self._sdk is None:
            raise RuntimeError("engine.open() not called")

        self.last_call_ok = False
        h, w = frame.shape[:2]
        try:
            result = self._sdk.UltAlprSdkEngine_process(
                self._sdk.ULTALPR_SDK_IMAGE_TYPE_RGB24,
                frame.tobytes(), w, h, 0, 1,
            )
        except Exception:
            return []
        if not result.isOK():
            return []
        try:
            payload = json.loads(result.json())
        except Exception:
            return []

        out: list[RichPlateRead] = []
        for p in payload.get("plates", []) or []:
            text = p.get("text", "") or ""

            # Same guard as production, restated rather than inherited because
            # this method replaces infer() wholesale. An unlicensed SDK
            # redacts the last character; normalize() would strip the '*' and
            # the survivor clears the length floor, producing a confident
            # WRONG identity that nothing downstream can detect.
            if "*" in text:
                raise RedactedPlateTextError(
                    f"Redacted plate text from unlicensed SDK: {text!r}. "
                    "Refusing to hash — normalize() would strip the '*' and "
                    "produce a confident wrong identity."
                )

            car = p.get("car") or {}
            vehicle_bbox = (
                self._quad_to_bbox(car.get("warpedBox"))
                if car.get("warpedBox") else None
            )
            vehicle_conf = (
                float(car["confidence"]) / 100.0
                if car.get("confidence") is not None else None
            )

            color_e, color_c = _best_entry(car.get("color"))
            mmy_e, mmy_c = _best_entry(car.get("makeModelYear"))
            country_e, _ = _best_entry(p.get("country"))

            jurisdiction = None
            if country_e:
                code, state = country_e.get("code"), country_e.get("state")
                # Diagnostics only. NORMALIZATION_SPEC v1.0 rule 8 is plate
                # text and nothing else in the hash input, so jurisdiction
                # must never reach the identity.
                jurisdiction = f"{code}/{state}" if state else code

            confidences = p.get("confidences", []) or []
            common = dict(
                bbox=self._quad_to_bbox(p.get("warpedBox")),
                vehicle_bbox=vehicle_bbox,
                vehicle_confidence=vehicle_conf,
                jurisdiction=jurisdiction,
                color=(color_e or {}).get("name"), color_confidence=color_c,
                make=(mmy_e or {}).get("make"), make_confidence=mmy_c,
                model=(mmy_e or {}).get("model"), model_confidence=mmy_c,
            )

            if not text or len(confidences) < self._CONF_CHARS_FROM:
                # Localized but unreadable: a real vehicle observation with no
                # usable plate. Still emitted — this is precisely the
                # NO_PLATE_VISIBLE / LOW_CONFIDENCE signal.
                out.append(RichPlateRead(
                    text="", detection_confidence=0.0, ocr_confidence=0.0,
                    **common))
                continue

            self._validate_confidences_layout(confidences, text)
            out.append(RichPlateRead(
                text=text,
                detection_confidence=float(confidences[self._CONF_DETECT]) / 100.0,
                ocr_confidence=float(confidences[self._CONF_OCR]) / 100.0,
                **common))
        self.last_reads = out
        self.last_call_ok = True
        return out


class ReplayEngine:
    """Replays rich reads captured from a real SDK run (see capture.py).

    Every read is genuine model output recorded on real hardware from real
    footage — nothing here is invented. What replay does NOT reproduce is
    inference cost, so it is useless for performance work; that is what the
    CM5 benchmark with the real engine is for.

    Format: JSONL, one object per frame:
        {"frame": 123, "reads": [ {...RichPlateRead...}, ... ]}
    """

    name = "replay"

    last_reads: list = []

    def __init__(self, path: str) -> None:
        self._path = path
        self._by_frame: dict[int, list[RichPlateRead]] = {}
        self._cursor = 0
        self.frame_w = 1920
        self.frame_h = 1080

    def set_frame_size(self, w: int, h: int) -> None:
        self.frame_w, self.frame_h = w, h

    def open(self) -> None:
        self._by_frame.clear()
        with open(self._path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if obj.get("_meta"):
                    self.frame_w = obj.get("frame_w", self.frame_w)
                    self.frame_h = obj.get("frame_h", self.frame_h)
                    continue
                self._by_frame[int(obj["frame"])] = [
                    RichPlateRead.from_json(r) for r in obj.get("reads", [])
                ]
        self._cursor = 0

    def close(self) -> None:
        self._by_frame.clear()

    def infer(self, frame=None) -> list[RichPlateRead]:
        """Frame-indexed replay. The frame argument is ignored — the cursor
        advances once per call so the caller's loop is identical to a live
        engine's."""
        out = self._by_frame.get(self._cursor, [])
        self._cursor += 1
        self.last_reads = out
        return out

    def reset(self) -> None:
        self._cursor = 0

    @property
    def total_frames(self) -> int:
        return (max(self._by_frame) + 1) if self._by_frame else 0
