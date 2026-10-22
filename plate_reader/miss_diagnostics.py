"""Persist one best full frame for locally auditable recognition outcomes.

Plate misses carry a drawn plate box; vehicles for which Stream produced no
plate stay uncropped and unannotated. A JPEG and its JSON sidecar share a stem
and count as one audit case. Images and raw OCR metadata stay on-device.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .log import get
from .audit_evidence import evidence_timing

log = get(__name__)

_warned_mkdir = False


def copy_frame(frame):
    """Return a full-frame copy suitable for later JPEG write, or None."""
    try:
        if frame is None:
            return None
        return frame.copy()
    except Exception:
        return None


def annotate_bbox(
    frame,
    bbox: Optional[tuple[int, int, int, int]],
):
    """Draw the plate box on a full-frame copy. Mutates and returns frame."""
    if frame is None or bbox is None:
        return frame
    try:
        import cv2
        x1, y1, x2, y2 = [int(v) for v in bbox]
        if x2 > x1 and y2 > y1:
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 255), 2)
    except Exception:
        pass
    return frame


def joint_rank(cleared_det: bool, ocr: float, det: float) -> tuple:
    """Same ordering as `_EpisodeStats._offer`: prefer det-clearing, then OCR."""
    return (1 if cleared_det else 0, float(ocr), float(det))


def save_miss_plate(
    *,
    directory: str,
    image,
    episode_id: str,
    outcome: str,
    det: float,
    ocr: float,
    cleared_det: bool,
    raw_text: str,
    detected_at_iso: str,
    episode_end: str,
    reads: int,
    bbox: Optional[tuple[int, int, int, int]] = None,
    normalized: Optional[str] = None,
    encounter_id: str | None = None,
) -> Optional[str]:
    """Write full-frame JPEG + JSON sidecar. Returns the JPEG path or None."""
    global _warned_mkdir
    if image is None:
        return None
    try:
        import cv2  # local; same dependency as camera.py
    except Exception as e:
        log.debug("miss-plate save skipped (no cv2): %s", e)
        return None

    out_dir = Path(directory)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        if not _warned_mkdir:
            log.warning("miss-plate dir %s unusable: %s", out_dir, e)
            _warned_mkdir = True
        return None

    stamp = (episode_end or detected_at_iso or "").replace(":", "").replace("-", "")
    stamp = stamp.replace(".", "")[:15] or time.strftime("%Y%m%dT%H%M%S")
    eid = (episode_id or "unknown")[:8]
    safe_outcome = "".join(c if c.isalnum() or c in "-_" else "_" for c in outcome)[:32]
    stem = f"{stamp}_{safe_outcome}_d{det:.2f}_o{ocr:.2f}_{eid}"
    jpg_path = out_dir / f"{stem}.jpg"
    json_path = out_dir / f"{stem}.json"

    try:
        to_write = annotate_bbox(image, bbox)
        ok = cv2.imwrite(
            str(jpg_path), to_write, [int(cv2.IMWRITE_JPEG_QUALITY), 85]
        )
        if not ok:
            log.warning("miss-plate imwrite failed: %s", jpg_path)
            return None
        meta: dict[str, Any] = {
            "episodeId": episode_id,
            "outcome": outcome,
            "bestJointDet": round(det, 4),
            "bestJointOcr": round(ocr, 4),
            "bestJointClearedDet": cleared_det,
            "rawText": raw_text,
            "normalized": normalized,
            "detectedAt": detected_at_iso,
            "episodeEnd": episode_end,
            "readsInEpisode": reads,
            "bbox": list(bbox) if bbox is not None else None,
            "image": jpg_path.name,
            "fullFrame": True,
        }
        if outcome == "confirmed":
            # A successful OCR vote precedes the route gate and may never
            # become traffic. Keep the legacy outcome, but make that split
            # explicit and joinable to the physical encounter from capture.
            meta.update({
                "encounterId": encounter_id,
                "recognitionEncounterId": encounter_id,
                "recognitionOutcome": outcome,
                "trafficDecision": "not_evaluated",
            })
        json_path.write_text(json.dumps(meta, indent=2) + "\n")
        return str(jpg_path)
    except Exception as e:
        log.warning("miss-plate save failed: %s", e)
        return None


def tag_confirmed_traffic_decision(
    *,
    image_path: str | None,
    recognition_encounter_id: str | None,
    encounter_id: str | None,
    decision: str,
    route_metadata: dict | None = None,
) -> bool:
    """Annotate one existing OCR case without changing traffic or its image.

    ``visit_queued`` means the local sender accepted the enqueue call, not
    that AWS delivered it. Missing/pruned evidence stays missing. Image time
    is never inferred from an encounter, trajectory or terminal decision.
    """
    if not image_path:
        return False
    json_path = Path(image_path).with_suffix(".json")
    temp_path = json_path.with_suffix(f".json.{uuid.uuid4().hex}.tmp")
    try:
        meta = json.loads(json_path.read_text())
        if meta.get("image") != Path(image_path).name:
            return False
        source_owner = meta.get("recognitionEncounterId") or meta.get("encounterId")
        if source_owner and source_owner != recognition_encounter_id:
            return False
        # This helper only owns the OCR-confirmed path. Do not relabel a
        # neighboring or anonymous case because its filename was supplied.
        if meta.get("recognitionOutcome", meta.get("outcome")) != "confirmed":
            return False
        meta.update({
            "recognitionOutcome": "confirmed",
            "recognitionEncounterId": recognition_encounter_id,
            "encounterId": encounter_id or recognition_encounter_id,
            "trafficDecision": decision,
            "trafficDecisionAt": datetime.now(timezone.utc).isoformat(),
        })
        route = route_metadata or {}
        # Each update replaces one decision snapshot. Never keep geometry or
        # summary fields from an older proof when the current owner is gone
        # or a concurrent hook selected a different track.
        for key in (
            "firstSeenAt", "lastSeenAt", "routeStatus", "routeReason",
            "triggeredAt", "routeTrackFingerprint", "trajectoryPoints",
            "dwellSeconds", "routeTrajectoryBboxes", "routeTrajectoryTimestamps",
            "routeZones", "trajectoryTerminalBbox", "trajectoryTerminalObservedAt",
        ):
            meta.pop(key, None)
        # Closed projection: never serialize the outcome or engine profile,
        # which can contain identity data unrelated to route provenance.
        fields = {
            "first_seen_at": "firstSeenAt", "last_seen_at": "lastSeenAt",
            "route_status": "routeStatus", "route_reason": "routeReason",
            "route_qualified_at": "triggeredAt",
            "route_track_fingerprint": "routeTrackFingerprint",
            "trajectory_points": "trajectoryPoints", "dwell_seconds": "dwellSeconds",
            "route_trajectory_bboxes": "routeTrajectoryBboxes",
            "route_trajectory_timestamps": "routeTrajectoryTimestamps",
        }
        for source, target in fields.items():
            if source in route:
                meta[target] = route[source]
        zones = {target: route[source] for source, target in (
            ("route_entry_zone", "entry"), ("route_board_zone", "board"),
            ("route_terminal_exit_zone", "terminalExit"),
        ) if source in route}
        if zones:
            meta["routeZones"] = zones
        boxes = route.get("route_trajectory_bboxes")
        stamps = route.get("route_trajectory_timestamps")
        if boxes:
            meta["trajectoryTerminalBbox"] = boxes[-1]
        if stamps:
            meta["trajectoryTerminalObservedAt"] = stamps[-1]
        image_at = meta.get("diagnosticImageObservedAt") if meta.get(
            "imageTimestampSource") == "provider_webhook" else None
        meta.update(evidence_timing(image_at, stamps[-1] if stamps else None))
        temp_path.write_text(json.dumps(meta, indent=2) + "\n")
        temp_path.replace(json_path)
        return True
    except FileNotFoundError:
        return False
    except Exception as e:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        log.warning("confirmed audit decision update failed: %s", e)
        return False


def save_unidentified_vehicle(
    *,
    directory: str,
    jpeg: bytes | None,
    encounter_id: str,
    outcome: str,
    vehicle_confidence: float,
    first_seen_at: str,
    last_seen_at: str,
    vehicle_type: str | None = None,
    make: str | None = None,
    model: str | None = None,
    color: str | None = None,
    make_confidence: float | None = None,
    model_confidence: float | None = None,
    color_confidence: float | None = None,
    vehicle_bbox: tuple[int, int, int, int] | None = None,
    route_status: str | None = None,
    route_reason: str | None = None,
    route_track_fingerprint: str | None = None,
    trajectory_points: int = 0,
    dwell_seconds: float | None = None,
    route_trajectory_bboxes: tuple[
        tuple[int, int, int, int], ...
    ] = (),
    route_trajectory_timestamps: tuple[str | None, ...] = (),
    route_entry_zone: tuple[float, float, float, float] | None = None,
    route_board_zone: tuple[float, float, float, float] | None = None,
    route_terminal_exit_zone: tuple[
        float, float, float, float
    ] | None = None,
    diagnostic_image_observed_at: str | None = None,
    traffic_classification: str | None = None,
    plate_candidate: bool = False,
    plate_detection_confidence: float | None = None,
    plate_ocr_confidence: float | None = None,
) -> Optional[str]:
    """Write one original full-frame JPEG for local vehicle evidence.

    Stream's multipart webhook already contains the compressed original. Keep
    it compressed while the encounter is open, validate it at terminal save,
    and write those exact bytes without another lossy JPEG encode. Deliberately
    draw no box: this dataset is for checking whether the whole scene actually
    made a plate identifiable. ``plate_candidate`` records presence only; this
    path accepts and writes no plate text or hash.
    """
    global _warned_mkdir
    if not jpeg:
        return None
    try:
        import cv2
        import numpy as np
        image = cv2.imdecode(
            np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR
        )
    except Exception as e:
        log.debug("unidentified-vehicle save skipped (JPEG decode): %s", e)
        return None
    if image is None:
        log.warning("unidentified-vehicle JPEG decode failed")
        return None

    out_dir = Path(directory)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        if not _warned_mkdir:
            log.warning("audit-case dir %s unusable: %s", out_dir, e)
            _warned_mkdir = True
        return None

    stamp = (
        (last_seen_at or first_seen_at or "")
        .replace(":", "")
        .replace("-", "")
    )
    stamp = stamp.replace(".", "")[:15] or time.strftime("%Y%m%dT%H%M%S")
    eid = (encounter_id or "unknown")[:8]
    safe_outcome = "".join(
        c if c.isalnum() or c in "-_" else "_" for c in outcome
    )[:32]
    stem = f"{stamp}_{safe_outcome}_v{vehicle_confidence:.2f}_{eid}"
    jpg_path = out_dir / f"{stem}.jpg"
    json_path = out_dir / f"{stem}.json"
    try:
        jpg_path.write_bytes(jpeg)
        meta: dict[str, Any] = {
            "encounterId": encounter_id,
            "outcome": outcome,
            "vehicleConfidence": round(vehicle_confidence, 4),
            "firstSeenAt": first_seen_at,
            "lastSeenAt": last_seen_at,
            "vehicleType": vehicle_type,
            "make": make,
            "model": model,
            "color": color,
            "makeConfidence": make_confidence,
            "modelConfidence": model_confidence,
            "colorConfidence": color_confidence,
            "vehicleBbox": (
                list(vehicle_bbox) if vehicle_bbox is not None else None
            ),
            "routeStatus": route_status,
            "routeReason": route_reason,
            "routeTrackFingerprint": route_track_fingerprint,
            "trajectoryPoints": int(trajectory_points or 0),
            "dwellSeconds": dwell_seconds,
            "routeTrajectoryBboxes": [
                list(box) for box in route_trajectory_bboxes
            ],
            "routeTrajectoryTimestamps": list(
                route_trajectory_timestamps
            ),
            "routeZones": {
                "entry": (
                    list(route_entry_zone)
                    if route_entry_zone is not None else None
                ),
                "board": (
                    list(route_board_zone)
                    if route_board_zone is not None else None
                ),
                "terminalExit": (
                    list(route_terminal_exit_zone)
                    if route_terminal_exit_zone is not None else None
                ),
            },
            "diagnosticImageObservedAt": diagnostic_image_observed_at,
            "imageTimestampSource": "provider_webhook",
            "trajectoryTerminalBbox": (
                list(route_trajectory_bboxes[-1])
                if route_trajectory_bboxes else None
            ),
            "trajectoryTerminalObservedAt": (
                route_trajectory_timestamps[-1]
                if route_trajectory_timestamps else None
            ),
            "trafficClassification": traffic_classification,
            "image": jpg_path.name,
            "fullFrame": True,
            # Presence only: whether the encounter ever carried a candidate.
            # No raw text or hash is written by this vehicle-evidence path.
            "plateCandidate": bool(plate_candidate),
            "maxPlateDetectionConfidence": plate_detection_confidence,
            "maxPlateOcrConfidence": plate_ocr_confidence,
            # No candidate from the model is not proof that a human could
            # never see a plate. Keep the legacy outcome for compatibility,
            # but explicitly describe its evidence limitation for review.
            "plateVisibilityAssessment": "not_human_verified",
            **evidence_timing(diagnostic_image_observed_at,
                              route_trajectory_timestamps[-1]
                              if route_trajectory_timestamps else last_seen_at),
        }
        json_path.write_text(json.dumps(meta, indent=2) + "\n")
        return str(jpg_path)
    except Exception as e:
        log.warning("unidentified-vehicle save failed: %s", e)
        return None


def _matching_case_sidecars(
    *,
    directory: str,
    episode_id: str | None = None,
    encounter_id: str | None = None,
) -> list[tuple[bool, bool, float, Path, dict, Path | None]]:
    """Newest local cases matching either stable physical identifier."""
    if not episode_id and not encounter_id:
        return []
    out_dir = Path(directory)
    if not out_dir.is_dir():
        return []
    matches = []
    try:
        paths = list(out_dir.glob("*.json"))
    except Exception:
        return []
    for json_path in paths:
        try:
            meta = json.loads(json_path.read_text())
            episode_match = bool(
                episode_id and meta.get("episodeId") == episode_id
            )
            encounter_match = bool(
                encounter_id and meta.get("encounterId") == encounter_id
            )
            if not episode_match and not encounter_match:
                continue
            image_name = meta.get("image")
            image_path = (
                json_path.parent / image_name
                if isinstance(image_name, str) and image_name else None
            )
            matches.append((
                episode_match,
                encounter_match,
                json_path.stat().st_mtime,
                json_path,
                meta,
                image_path,
            ))
        except Exception:
            continue
    # Prefer a complete case, then Detector's plate-evidence episode, then the
    # newest case. This preserves the strongest one-frame audit evidence when
    # an older build happened to write both plate-miss and anonymous cases.
    matches.sort(
        key=lambda item: (
            bool(item[5] and item[5].is_file()),
            item[0],
            item[2],
        ),
        reverse=True,
    )
    return matches


def _update_and_coalesce_cases(
    matches: list[tuple[bool, bool, float, Path, dict, Path | None]],
    updates: dict,
) -> Optional[str]:
    """Atomically update the best case, then remove proven duplicates."""
    if not matches:
        return None
    _, _, _, json_path, meta, image_path = matches[0]
    meta.update(updates)
    temp_path = json_path.with_suffix(".json.tmp")
    try:
        temp_path.write_text(json.dumps(meta, indent=2) + "\n")
        temp_path.replace(json_path)
        if image_path is None or not image_path.is_file():
            # Do not remove alternate evidence until the retained primary is
            # a complete JPEG+sidecar pair. The caller can persist a fallback.
            return None
        for _, _, _, duplicate_json, duplicate_meta, duplicate_image in (
            matches[1:]
        ):
            if duplicate_image is not None and duplicate_image != image_path:
                duplicate_image.unlink(missing_ok=True)
            duplicate_json.unlink(missing_ok=True)
        return str(image_path)
    except Exception as e:
        temp_path.unlink(missing_ok=True)
        log.warning("audit case update failed: %s", e)
        return None


def tag_existing_terminal_case(
    *,
    directory: str,
    encounter_id: str,
    episode_id: str | None,
    outcome: str,
    plate_candidate: bool,
) -> Optional[str]:
    """Retag an already-emitted anonymous case with later plate evidence."""
    matches = _matching_case_sidecars(
        directory=directory,
        episode_id=episode_id,
        encounter_id=encounter_id,
    )
    route_matches = [
        match for match in matches
        if (
            match[4].get("trafficClassification")
            or match[4].get("routeStatus") == "rejected"
        )
    ]
    updates = {
        "encounterId": encounter_id,
        "plateCandidate": bool(plate_candidate),
        "lateEvidence": True,
    }
    if route_matches:
        # Route labels are the purpose of the shadow audit. Keep their case
        # and classification authoritative while recording the later OCR
        # result separately, then coalesce any newly written miss duplicate.
        updates["lateRecognitionOutcome"] = outcome
        matches = route_matches + [
            match for match in matches if match not in route_matches
        ]
    else:
        updates["outcome"] = outcome
    return _update_and_coalesce_cases(
        matches,
        updates,
    )


def remove_existing_vehicle_cases(
    *, directory: str, encounter_id: str, preserve_image_path: str | None = None
) -> int:
    """Remove stale anonymous cases while preserving labeled route evidence."""
    matches = _matching_case_sidecars(
        directory=directory,
        encounter_id=encounter_id,
    )
    route_matches = [
        match for match in matches
        if (
            match[4].get("trafficClassification")
            or match[4].get("routeStatus") == "rejected"
        )
    ]
    preserved = next((match for match in matches if (
        preserve_image_path and match[5] == Path(preserve_image_path)
    )), None)
    if route_matches:
        updates = {"lateIdentified": True, "plateCandidate": True}
        if preserved is not None:
            # The route-labeled image remains the evidence image. Carry only
            # the later recognition decision, never its timestamp as though
            # it belonged to the retained JPEG.
            for key in (
                "recognitionOutcome", "recognitionEncounterId",
                "trafficDecision", "trafficDecisionAt",
            ):
                if key in preserved[4]:
                    updates[key] = preserved[4][key]
        retained = _update_and_coalesce_cases(
            route_matches,
            updates,
        )
        if retained is None:
            # Route labels are the purpose of this canary. If their retained
            # evidence is damaged, preserve every alternate rather than risk
            # deleting the only reviewable case.
            return 0
        retained_path = Path(retained)
        for match in matches:
            if match in route_matches:
                continue
            _, _, _, json_path, _, image_path = match
            try:
                if image_path is not None and image_path != retained_path:
                    image_path.unlink(missing_ok=True)
                json_path.unlink(missing_ok=True)
            except Exception as e:
                log.warning("late-identified audit cleanup failed: %s", e)
        return max(0, len(matches) - 1)

    removed = 0
    for _, _, _, json_path, _, image_path in matches:
        if preserve_image_path and image_path == Path(preserve_image_path):
            continue
        try:
            if image_path is not None:
                image_path.unlink(missing_ok=True)
            json_path.unlink(missing_ok=True)
            removed += 1
        except Exception as e:
            log.warning("late-identified audit cleanup failed: %s", e)
    return removed


def tag_existing_route_case(
    *,
    directory: str,
    episode_id: str | None,
    encounter_id: str,
    traffic_classification: str,
    route_status: str,
    route_reason: str | None,
    route_track_fingerprint: str | None,
    trajectory_points: int,
    dwell_seconds: float | None,
    route_trajectory_bboxes: tuple[
        tuple[int, int, int, int], ...
    ] = (),
    route_trajectory_timestamps: tuple[str | None, ...] = (),
    route_entry_zone: tuple[float, float, float, float] | None = None,
    route_board_zone: tuple[float, float, float, float] | None = None,
    route_terminal_exit_zone: tuple[
        float, float, float, float
    ] | None = None,
    diagnostic_image_observed_at: str | None = None,
    plate_candidate: bool,
) -> Optional[str]:
    """Add final route evidence to an existing plate-miss case.

    Detector already owns the one saved image for a below-threshold plate.
    Enrich that local sidecar instead of creating a second audit case for the
    same physical encounter. Existing raw OCR fields remain local and are not
    copied into any outbound object.
    """
    matches = _matching_case_sidecars(
        directory=directory,
        episode_id=episode_id,
        encounter_id=encounter_id,
    )
    # This operation retains the selected JPEG. The current vehicle can have
    # a newer frame, but its timestamp must not be attached to the old bytes.
    # Detector's detectedAt is episode-start time, not necessarily the time
    # of its best frame. Missing image provenance therefore remains unknown.
    retained = matches[0][4] if matches else {}
    image_at = retained.get("diagnosticImageObservedAt") if retained.get(
        "imageTimestampSource") == "provider_webhook" else None
    return _update_and_coalesce_cases(
        matches,
        {
            "encounterId": encounter_id,
            "trafficClassification": traffic_classification,
            "routeStatus": route_status,
            "routeReason": route_reason,
            "routeTrackFingerprint": route_track_fingerprint,
            "trajectoryPoints": int(trajectory_points or 0),
            "dwellSeconds": dwell_seconds,
            "routeTrajectoryBboxes": [
                list(box) for box in route_trajectory_bboxes
            ],
            "routeTrajectoryTimestamps": list(
                route_trajectory_timestamps
            ),
            "routeZones": {
                "entry": (
                    list(route_entry_zone)
                    if route_entry_zone is not None else None
                ),
                "board": (
                    list(route_board_zone)
                    if route_board_zone is not None else None
                ),
                "terminalExit": (
                    list(route_terminal_exit_zone)
                    if route_terminal_exit_zone is not None else None
                ),
            },
            "diagnosticImageObservedAt": image_at,
            "imageTimestampSource": "provider_webhook" if image_at else "unknown",
            "latestAvailableFrameObservedAt": diagnostic_image_observed_at,
            **evidence_timing(image_at, route_trajectory_timestamps[-1]
                              if route_trajectory_timestamps else None),
            "trajectoryTerminalBbox": (
                list(route_trajectory_bboxes[-1])
                if route_trajectory_bboxes else None
            ),
            "trajectoryTerminalObservedAt": (
                route_trajectory_timestamps[-1]
                if route_trajectory_timestamps else None
            ),
            "plateCandidate": bool(plate_candidate),
        },
    )


def prune_miss_plates(
    directory: str,
    retain_hours: float,
    max_cases: int = 0,
) -> int:
    """Prune audit cases by age and/or newest-case cap.

    A JPEG and its JSON sidecar count as one case. ``0`` disables each limit.
    Returns the number of files removed, preserving the legacy return shape.
    """
    if retain_hours <= 0 and max_cases <= 0:
        return 0
    out_dir = Path(directory)
    if not out_dir.is_dir():
        return 0
    cutoff = time.time() - retain_hours * 3600.0 if retain_hours > 0 else None
    removed = 0
    try:
        cases: dict[str, list[Path]] = {}
        for path in out_dir.iterdir():
            if path.suffix.lower() not in (".jpg", ".jpeg", ".json"):
                continue
            cases.setdefault(path.stem, []).append(path)

        # Treat the JPEG and sidecar as an indivisible case. The newest file's
        # timestamp wins so an interrupted/late sidecar write cannot orphan its
        # image during age pruning.
        case_mtimes: dict[str, float] = {}
        for stem, paths in cases.items():
            try:
                case_mtimes[stem] = max(p.stat().st_mtime for p in paths)
            except Exception:
                continue

        remove_stems = {
            stem for stem, mtime in case_mtimes.items()
            if cutoff is not None and mtime < cutoff
        }

        if max_cases > 0:
            retained = [
                (stem, mtime) for stem, mtime in case_mtimes.items()
                if stem not in remove_stems
            ]
            excess = len(retained) - max_cases
            if excess > 0:
                remove_stems.update(
                    stem for stem, _mtime in sorted(
                        retained, key=lambda item: item[1]
                    )[:excess]
                )

        for stem in remove_stems:
            for path in cases.get(stem, []):
                try:
                    path.unlink(missing_ok=True)
                    removed += 1
                except Exception:
                    continue
    except Exception as e:
        log.debug("miss-plate prune error: %s", e)
    if removed:
        log.info("pruned %d audit-case file(s) from %s", removed, out_dir)
    return removed


def _nonzero_box(bbox) -> bool:
    if bbox is None:
        return False
    try:
        x1, y1, x2, y2 = bbox
    except Exception:
        return False
    return x2 > x1 and y2 > y1


class PresenceFrameDumper:
    """Background JPEG writer for every frame that contains a plate or vehicle.

    Copies the numpy frame and returns immediately so 10fps inference is not
    blocked on disk. Drops if the queue is full.
    """

    def __init__(self, directory: str) -> None:
        self.directory = Path(directory)
        self._q: queue.Queue = queue.Queue(maxsize=12)
        self._stop = threading.Event()
        self._seq = 0
        self._dropped = 0
        self._written = 0
        self._thread = threading.Thread(
            target=self._loop, name="presence-frame-dump", daemon=True
        )
        self._thread.start()

    def submit(self, frame, candidates) -> None:
        if frame is None or not candidates:
            return
        plates = []
        vehicles = []
        for c in candidates:
            bbox = getattr(c, "bbox", None)
            vb = getattr(c, "vehicle_bbox", None)
            if _nonzero_box(bbox):
                plates.append({
                    "text": getattr(c, "raw_text", "") or "",
                    "normalized": getattr(c, "normalized", None),
                    "det": round(float(getattr(c, "detection_confidence", 0.0)), 4),
                    "ocr": round(float(getattr(c, "ocr_confidence", 0.0)), 4),
                    "bbox": [int(v) for v in bbox],
                    "rejectedBy": getattr(c, "rejected_by", None),
                })
            if _nonzero_box(vb):
                vehicles.append({
                    "bbox": [int(v) for v in vb],
                    "conf": getattr(c, "vehicle_confidence", None),
                })
        if not plates and not vehicles:
            return
        captured = ""
        if candidates:
            captured = getattr(candidates[0], "captured_at_iso", "") or ""
        try:
            self._seq += 1
            self._q.put_nowait({
                "frame": frame.copy(),
                "plates": plates,
                "vehicles": vehicles,
                "seq": self._seq,
                "capturedAt": captured,
            })
        except queue.Full:
            self._dropped += 1
            if self._dropped in (1, 10, 50) or self._dropped % 100 == 0:
                log.warning(
                    "presence-frame dump queue full; dropped %d frame(s)",
                    self._dropped,
                )

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)
        if self._written or self._dropped:
            log.info(
                "presence-frame dump stopped: wrote=%d dropped=%d dir=%s",
                self._written, self._dropped, self.directory,
            )

    def _loop(self) -> None:
        try:
            import cv2
        except Exception as e:
            log.warning("presence-frame dump disabled (no cv2): %s", e)
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.warning("presence-frame dir %s unusable: %s", self.directory, e)
            return
        while not self._stop.is_set() or not self._q.empty():
            try:
                item = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._write(cv2, item)
                self._written += 1
            except Exception as e:
                log.warning("presence-frame write failed: %s", e)

    def _write(self, cv2, item: dict) -> None:
        stamp = (item.get("capturedAt") or "").replace(":", "").replace("-", "")
        stamp = stamp.replace(".", "")[:15] or time.strftime("%Y%m%dT%H%M%S")
        seq = int(item["seq"])
        n_p = len(item["plates"])
        n_v = len(item["vehicles"])
        stem = f"{stamp}_f{seq:06d}_p{n_p}_v{n_v}"
        jpg_path = self.directory / f"{stem}.jpg"
        json_path = self.directory / f"{stem}.json"
        img = item["frame"]
        for p in item["plates"]:
            annotate_bbox(img, tuple(p["bbox"]))
        for v in item["vehicles"]:
            try:
                x1, y1, x2, y2 = [int(x) for x in v["bbox"]]
                cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)
            except Exception:
                pass
        ok = cv2.imwrite(str(jpg_path), img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok:
            raise RuntimeError(f"imwrite failed: {jpg_path}")
        json_path.write_text(json.dumps({
            "seq": seq,
            "capturedAt": item.get("capturedAt"),
            "plates": item["plates"],
            "vehicles": item["vehicles"],
            "image": jpg_path.name,
            "fullFrame": True,
        }, indent=2) + "\n")
