"""The vehicle record: the unit of processing in the vehicle-centric design.

The inversion this implements
-----------------------------
The old design was plate-first: a read produced a visit, and a vehicle with
no readable plate needed a whole separate event type to exist at all. That
puts the unreliable observation (the plate) at the root of the data model and
the reliable one (the vehicle) nowhere.

Here the vehicle is the record. `plate_hash` is a FIELD on it — the identity
when present, absent otherwise — and the record exists either way. Color,
make and model are further optional fields, populated when their classifiers
are enabled and produce output, null when not.

The safety property that must survive the inversion
---------------------------------------------------
In the old design `VehicleOnlyIdentification` had no `normalized` field at
all, so a plateless vehicle *could not* be hashed into a customer identity —
the mistake was unrepresentable, not merely checked for. Collapsing to a
nullable `plate_hash` gives that up unless it is deliberately rebuilt, and a
wrong identity is the most expensive failure this system has (it is why the
'*'-redaction guard and the locked NORMALIZATION_SPEC exist).

It is rebuilt here as an enforced invariant: `plate_hash` is settable ONLY
through `confirm_plate()`, which requires a hash and sets the status in the
same operation. There is no code path that produces a record carrying a hash
without CONFIRMED status, and `validate()` re-checks it at the commit point.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class PlateStatus(str, Enum):
    """Three outcomes, deliberately not two.

    `NO_PLATE_VISIBLE` and `LOW_CONFIDENCE` describe different failures and
    must stay distinct all the way to the cloud:

      NO_PLATE_VISIBLE  a fact about the physical world — the plate was
                        occluded, angled away, or simply not there.
      LOW_CONFIDENCE    a fact about the model — it saw a plate and could
                        not read it well enough.

    Collapsing them makes a low identification rate undiagnosable: there is
    no way to tell whether cars are hiding their plates (a mounting or camera
    geometry problem) or the model is failing on plates it can plainly see (a
    threshold or model problem). Those have opposite fixes.

    str-valued so JSON serialization is the enum value, not "PlateStatus.X".
    """

    CONFIRMED = "confirmed"
    NO_PLATE_VISIBLE = "no_plate_visible"
    LOW_CONFIDENCE = "low_confidence"


class RecordFrozenError(RuntimeError):
    """Raised on any attempt to mutate a record after it has been sent.

    The send is the commit point and it is final. A record that could change
    after transmission would mean the cloud and the device disagree about
    what happened, with no way to tell which is right.
    """


@dataclass
class ConfidentField:
    """A descriptive field reconciled by highest-confidence-wins.

    A car dwells for many frames and a classifier can answer differently on
    each: silver on one frame, gray on the next. The rule for every
    DESCRIPTIVE field is that the highest-confidence reading survives.

    This rule is deliberately NOT used for the plate. A single high-confidence
    misread would become the canonical identity for the vehicle, which is
    exactly the failure multi-frame exact-match voting exists to prevent. The
    plate earns its value through agreement across frames; description does
    not need that rigor because it is never the identity.
    """

    value: Optional[str] = None
    confidence: float = -1.0
    observations: int = 0

    def offer(self, value: Optional[str], confidence: Optional[float]) -> bool:
        """Offer a reading. Returns True if it replaced the incumbent."""
        if value is None or value == "":
            return False
        conf = -1.0 if confidence is None else float(confidence)
        self.observations += 1
        if conf > self.confidence:
            self.value = value
            self.confidence = conf
            return True
        return False


@dataclass
class ConfidentMakeModel:
    """A make/model pair reconciled as one classifier observation.

    Selecting make and model independently can manufacture a pair the model
    never emitted (for example, a high-confidence BMW frame plus a different
    frame's high-confidence Civic label). The pair therefore rises or falls
    together using the weaker of its two confidence values.
    """

    make: Optional[str] = None
    model: Optional[str] = None
    confidence: float = -1.0
    observations: int = 0

    def offer(self, make: Optional[str], model: Optional[str],
              make_confidence: Optional[float],
              model_confidence: Optional[float]) -> bool:
        if not make or not model:
            return False
        make_conf = -1.0 if make_confidence is None else float(make_confidence)
        model_conf = -1.0 if model_confidence is None else float(model_confidence)
        confidence = min(make_conf, model_conf)
        self.observations += 1
        if confidence > self.confidence:
            self.make = make
            self.model = model
            self.confidence = confidence
            return True
        return False


@dataclass
class VehicleRecord:
    """One vehicle that passed through. One record, whatever we learned.

    `vehicle_detected` is always True — the record would not exist otherwise.
    It is carried explicitly anyway so the cloud schema reads the same for
    every record and nothing has to infer presence from the absence of a
    field.
    """

    # Device-scoped correlation id. NOT a customer identity and never usable
    # as one: it is unique per unit per boot, means nothing across visits, and
    # is safe to log. It exists so a record can be tied back to the track that
    # produced it while debugging, and so an emit-then-update transport could
    # key an idempotent upsert.
    record_id: str

    first_seen: float                     # epoch, vehicle entered the zone
    vehicle_detected: bool = True

    plate_hash: Optional[str] = None
    plate_status: PlateStatus = PlateStatus.NO_PLATE_VISIBLE

    color: Optional[str] = None
    make: Optional[str] = None
    model: Optional[str] = None

    sent_at: Optional[float] = None       # epoch, finalized and transmitted

    # --- internals: reconciliation state and diagnostics -------------------
    # Kept out of the wire shape (see to_dict) so the record the cloud sees is
    # exactly the specified shape, while the contractor's benchmark and our
    # own debugging still have what they need.
    _color: ConfidentField = field(default_factory=ConfidentField, repr=False)
    _make: ConfidentField = field(default_factory=ConfidentField, repr=False)
    _model: ConfidentField = field(default_factory=ConfidentField, repr=False)
    _make_model: ConfidentMakeModel = field(
        default_factory=ConfidentMakeModel, repr=False
    )
    _frozen: bool = field(default=False, repr=False)

    frames_observed: int = 0
    last_seen: float = 0.0
    plate_boxes_seen: int = 0             # any plate localized, readable or not
    plate_reads_attempted: int = 0        # reads that produced text
    send_trigger: Optional[str] = None    # which rule committed this record

    # ---------------- mutation guards ----------------
    def _assert_live(self) -> None:
        if self._frozen:
            raise RecordFrozenError(
                f"record {self.record_id} was sent at {self.sent_at} and is "
                f"frozen; nothing about it may change after transmission"
            )

    # ---------------- plate ----------------
    def confirm_plate(self, plate_hash: str) -> None:
        """Attach a CONFIRMED plate identity.

        The only path that may set `plate_hash`. Status and hash are set
        together so a hash can never exist without CONFIRMED status — the
        invariant that replaces the old type-level guarantee.
        """
        self._assert_live()
        if not isinstance(plate_hash, str) or len(plate_hash) != 64:
            raise ValueError(
                f"plate_hash must be a 64-char SHA-256 hex digest, got "
                f"{plate_hash!r}. The cloud rejects anything else, and a "
                f"malformed identity is worse than no identity."
            )
        self.plate_hash = plate_hash
        self.plate_status = PlateStatus.CONFIRMED

    def note_plate_seen_but_unread(self) -> None:
        """A plate box was localized but never cleared the gates.

        Promotes NO_PLATE_VISIBLE -> LOW_CONFIDENCE. Never demotes a
        CONFIRMED record: once a plate has been confirmed, later weak reads of
        the same vehicle say nothing about whether we identified it.
        """
        self._assert_live()
        if self.plate_status is PlateStatus.NO_PLATE_VISIBLE:
            self.plate_status = PlateStatus.LOW_CONFIDENCE

    # ---------------- descriptive fields ----------------
    def offer_color(self, value: Optional[str], confidence: Optional[float]) -> None:
        self._assert_live()
        if self._color.offer(value, confidence):
            self.color = self._color.value

    def offer_make(self, value: Optional[str], confidence: Optional[float]) -> None:
        self._assert_live()
        if self._make.offer(value, confidence):
            self.make = self._make.value

    def offer_model(self, value: Optional[str], confidence: Optional[float]) -> None:
        self._assert_live()
        if self._model.offer(value, confidence):
            self.model = self._model.value

    def offer_make_model(
        self,
        make: Optional[str],
        model: Optional[str],
        make_confidence: Optional[float],
        model_confidence: Optional[float],
    ) -> None:
        """Offer an atomic make/model pair from one inference result."""
        self._assert_live()
        if self._make_model.offer(
            make, model, make_confidence, model_confidence
        ):
            self.make = self._make_model.make
            self.model = self._make_model.model

    def touch(self, now: float) -> None:
        """Record that the vehicle was seen again this frame."""
        self._assert_live()
        self.frames_observed += 1
        self.last_seen = now

    # ---------------- commit ----------------
    def validate(self) -> None:
        """Re-check the invariants at the commit point.

        Cheap, and it runs on the one path where being wrong is permanent.
        """
        has_hash = self.plate_hash is not None
        is_confirmed = self.plate_status is PlateStatus.CONFIRMED
        if has_hash != is_confirmed:
            raise ValueError(
                f"record {self.record_id} violates the plate invariant: "
                f"plate_hash={'set' if has_hash else 'null'} but status="
                f"{self.plate_status.value}. A hash without CONFIRMED status "
                f"(or vice versa) means an identity we cannot justify."
            )
        if has_hash and len(self.plate_hash or "") != 64:
            raise ValueError(
                f"record {self.record_id} has a malformed plate_hash")

    def freeze(self, now: Optional[float] = None, trigger: str = "") -> None:
        """Commit and lock. Idempotent guard: freezing twice is an error."""
        if self._frozen:
            raise RecordFrozenError(
                f"record {self.record_id} is already frozen")
        self.validate()
        self.sent_at = time.time() if now is None else now
        self.send_trigger = trigger or self.send_trigger
        self._frozen = True

    @property
    def frozen(self) -> bool:
        return self._frozen

    # ---------------- serialization ----------------
    def to_dict(self, include_diagnostics: bool = True) -> dict[str, Any]:
        """The wire shape.

        The top level is exactly the specified record and nothing else, so the
        cloud contract stays clean. Everything useful-but-not-part-of-the-
        contract is nested under `diagnostics`, which a consumer can ignore
        wholesale.
        """
        out: dict[str, Any] = {
            "record_id": self.record_id,
            "vehicle_detected": self.vehicle_detected,
            "plate_hash": self.plate_hash,
            "plate_status": self.plate_status.value,
            "color": self.color,
            "make": self.make,
            "model": self.model,
            "first_seen": _iso(self.first_seen),
            "sent_at": _iso(self.sent_at),
        }
        if include_diagnostics:
            out["diagnostics"] = {
                "frames_observed": self.frames_observed,
                "seconds_tracked": (
                    round(self.last_seen - self.first_seen, 2)
                    if self.last_seen else 0.0
                ),
                "plate_boxes_seen": self.plate_boxes_seen,
                "plate_reads_attempted": self.plate_reads_attempted,
                "send_trigger": self.send_trigger,
                "color_confidence": _or_none(self._color.confidence),
                "make_confidence": _or_none(
                    max(self._make.confidence, self._make_model.confidence)
                ),
                "model_confidence": _or_none(
                    max(self._model.confidence, self._make_model.confidence)
                ),
                "color_observations": self._color.observations,
                "make_model_observations": self._make_model.observations,
            }
        return out


def _or_none(conf: float) -> Optional[float]:
    return None if conf < 0 else round(conf, 4)


def _iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    import datetime as _dt
    return (_dt.datetime.fromtimestamp(epoch, _dt.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))
