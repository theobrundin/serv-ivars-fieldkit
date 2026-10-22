#!/usr/bin/env python3
"""Offline replay + scenario harness for the presence / drive-off pipeline.

Why this exists
---------------
Drive-off and color-split logic must be validated against KNOWN ground truth
before their numbers are ever trusted as a KPI. You cannot tune this on a live
lane blind. This harness runs the REAL detector voting + REAL PresenceTracker
over controlled input and checks the outcomes against what should happen.

Two modes
---------
1. synthetic  (default; runs anywhere, no camera, no fast-alpr, no Pi):
     Scripted scenarios feed hand-built frames + plate detections through the
     real PresenceTracker (and, where relevant, the real Detector vote logic
     via injected candidates). Each scenario declares expected drive-offs /
     confirms, and the harness asserts them. This is your regression suite.

2. video  (bench machine or Pi with fast-alpr installed):
     Replays a real video file frame-by-frame through the REAL Detector
     (fast-alpr) + PresenceTracker, printing every confirm / failure /
     drive-off with timing. Use this on recorded lane footage where you know
     the true car count, to measure precision/recall of drive-off detection.

     python tools/replay_harness.py video --config config.json \
         --video lane_dinner_rush.mp4 --fps 5

Both modes share the same downstream logic, so a scenario that passes in
synthetic mode exercises the identical code path that runs in the field.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Make the package importable whether run from repo root or tools/.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import numpy as np  # noqa: E402

from plate_reader.config import load as load_config, Config  # noqa: E402
from plate_reader.detector import DetectionResult  # noqa: E402
from plate_reader.presence import PresenceTracker  # noqa: E402


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def _load_cfg(path: str | None) -> Config:
    """Load config, filling placeholder identity fields so validation passes.
    We only need the thresholds/presence blocks for the harness."""
    p = Path(path) if path else _REPO / "config.json"
    raw = json.loads(p.read_text())
    raw["device_id"] = "serv-unit-harness"
    raw["api_key"] = "a" * 64
    raw["location_id"] = "harness-loc-001"
    tmp = Path("/tmp/_harness_cfg.json")
    tmp.write_text(json.dumps(raw))
    return load_config(tmp)


def _body_frame(rgb, h=300, w=400, body=(80, 150, 150, 250)):
    """A frame with a solid colored vehicle-body block (for color tests)."""
    f = np.zeros((h, w, 3), dtype="uint8")
    y1, y2, x1, x2 = body
    f[y1:y2, x1:x2] = rgb
    return f


def _cand(bbox, normalized="WEAK", det=0.5, ocr=0.5, below=True, in_zone=True):
    """Build a DetectionResult as the detector would emit (incl. sub-threshold)."""
    iso = "2026-07-20T00:00:00.000Z"
    return DetectionResult(
        raw_text=normalized or "",
        normalized=normalized,
        detection_confidence=det,
        ocr_confidence=ocr,
        bbox=bbox,
        inference_ms=50,
        captured_at_epoch=time.time(),
        captured_at_iso=iso,
        in_zone=in_zone,
        below_threshold=below,
    )


# --------------------------------------------------------------------------
# Synthetic scenarios
# --------------------------------------------------------------------------

@dataclass
class Scenario:
    name: str
    expect_drive_offs: int
    expect_split: bool
    run: object  # callable(tracker) -> None (drives observe/mark_identified)
    note: str = ""


def _quiet_timeout(tracker: PresenceTracker):
    """Advance past the track timeout, then poke reap via an empty observe."""
    time.sleep(tracker._timeout + 0.15)


def scenario_clean_identified(cfg):
    """A car read cleanly should NOT be a drive-off."""
    def run(t: PresenceTracker):
        for _ in range(4):
            t.observe(_body_frame((200, 30, 30)), [_cand((170, 150, 230, 180))])
            time.sleep(0.02)
        t.mark_identified("WEAK")  # identify the plate the track saw
        _quiet_timeout(t)
    return Scenario("clean_identified_car", 0, False, run,
                    "car read successfully -> no drive-off")


def scenario_dirty_driveoff(cfg):
    """A car with only weak sub-threshold reads, never identified -> drive-off."""
    def run(t: PresenceTracker):
        for _ in range(4):
            t.observe(_body_frame((30, 30, 200)), [_cand((170, 150, 230, 180))])
            time.sleep(0.02)
        _quiet_timeout(t)
    return Scenario("dirty_plate_driveoff", 1, False, run,
                    "weak reads only, no confirm -> one drive-off")


def scenario_empty_lane(cfg):
    """No plate boxes, no motion -> nothing."""
    def run(t: PresenceTracker):
        for _ in range(5):
            t.observe(_body_frame((0, 0, 0)), [])
            time.sleep(0.02)
        _quiet_timeout(t)
    return Scenario("empty_lane", 0, False, run,
                    "no cars -> no events")


def scenario_brief_noise(cfg):
    """A one-frame blip (bird/pedestrian) below min_track_frames -> dropped."""
    def run(t: PresenceTracker):
        t.observe(_body_frame((30, 200, 30)), [_cand((170, 150, 230, 180))])
        _quiet_timeout(t)
    return Scenario("brief_noise_below_minframes", 0, False, run,
                    "single-frame blip -> filtered by min_track_frames")


def scenario_nose_to_tail_split(cfg):
    """Two differently-colored cars whose plate boxes overlap -> split into 2."""
    def run(t: PresenceTracker):
        for _ in range(3):
            t.observe(_body_frame((200, 30, 30)), [_cand((170, 150, 230, 180))])
            time.sleep(0.02)
        for _ in range(3):
            t.observe(_body_frame((30, 30, 200)), [_cand((172, 151, 232, 181))])
            time.sleep(0.02)
        _quiet_timeout(t)
        t.observe(_body_frame((0, 0, 0)), [])  # nudge reap eligibility
    return Scenario("nose_to_tail_color_split", 2, True, run,
                    "red then blue on merged track -> 2 drive-offs via split")


def scenario_same_color_no_split(cfg):
    """Two same-color cars nose-to-tail: color can't tell them apart ->
    stays merged as 1 (conservative; no fabricated split)."""
    def run(t: PresenceTracker):
        for _ in range(3):
            t.observe(_body_frame((200, 30, 30)), [_cand((170, 150, 230, 180))])
            time.sleep(0.02)
        for _ in range(3):
            t.observe(_body_frame((205, 25, 35)), [_cand((172, 151, 232, 181))])
            time.sleep(0.02)
        _quiet_timeout(t)
        t.observe(_body_frame((0, 0, 0)), [])
    return Scenario("same_color_no_false_split", 1, False, run,
                    "two reds -> 1 merged (color stays silent, no false split)")


def scenario_red_wraparound(cfg):
    """Two reds on opposite sides of the hue wraparound must NOT split."""
    def run(t: PresenceTracker):
        # RGB chosen so OpenCV hue lands near 0 vs near 179.
        for _ in range(3):
            t.observe(_body_frame((200, 20, 25)), [_cand((170, 150, 230, 180))])
            time.sleep(0.02)
        for _ in range(3):
            t.observe(_body_frame((200, 25, 20)), [_cand((172, 151, 232, 181))])
            time.sleep(0.02)
        _quiet_timeout(t)
        t.observe(_body_frame((0, 0, 0)), [])
    return Scenario("red_hue_wraparound_no_split", 1, False, run,
                    "red wraparound edge case -> no false split")


SCENARIOS = [
    scenario_clean_identified,
    scenario_dirty_driveoff,
    scenario_empty_lane,
    scenario_brief_noise,
    scenario_nose_to_tail_split,
    scenario_same_color_no_split,
    scenario_red_wraparound,
]


def run_synthetic(cfg: Config, fast: bool) -> int:
    """Run all synthetic scenarios. Returns process exit code (0 = all pass)."""
    # Shrink timeouts so the suite runs fast, without changing logic.
    cfg.presence.track_timeout_seconds = 0.25
    cfg.presence.min_track_frames = 2

    passed = 0
    failed = 0
    print("=" * 68)
    print("SYNTHETIC SCENARIO SUITE")
    print(f"  timeout={cfg.presence.track_timeout_seconds}s "
          f"min_frames={cfg.presence.min_track_frames} "
          f"color={'on' if cfg.presence.color_enabled else 'off'} "
          f"split_dist={cfg.presence.color_split_distance}")
    print("=" * 68)

    for factory in SCENARIOS:
        sc = factory(cfg)
        tracker = PresenceTracker(cfg)
        drive_offs = []
        # Drive the scenario; collect drive-offs as they're reaped.
        sc.run(tracker)
        drive_offs.extend(tracker.reap())
        n = len(drive_offs)
        any_split = any(d.split_from_color for d in drive_offs)

        ok = (n == sc.expect_drive_offs) and (any_split == sc.expect_split)
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        colors = [d.dominant_color for d in drive_offs]
        print(f"[{status}] {sc.name}")
        print(f"        expected drive_offs={sc.expect_drive_offs} split={sc.expect_split}")
        print(f"        actual   drive_offs={n} split={any_split} colors={colors}")
        if sc.note:
            print(f"        note: {sc.note}")
        if not ok:
            print(f"        !! MISMATCH — investigate before trusting the metric")

    print("-" * 68)
    print(f"RESULT: {passed} passed, {failed} failed, {len(SCENARIOS)} total")
    return 0 if failed == 0 else 1


# --------------------------------------------------------------------------
# Video replay (real fast-alpr; bench/Pi only)
# --------------------------------------------------------------------------

def run_video(cfg: Config, video_path: str, fps: float) -> int:
    """Replay a real video through the real Detector + PresenceTracker.

    Requires fast-alpr + opencv installed (bench machine or Pi). Prints every
    confirm / failure / drive-off with a frame index and wall time so you can
    line events up against the footage and count real cars for ground truth.
    """
    try:
        import cv2
    except ImportError:
        print("ERROR: opencv (cv2) required for video mode.", file=sys.stderr)
        return 2

    # Import the real detector lazily so synthetic mode never needs fast-alpr.
    from plate_reader.detector import (
        Detector, ConfirmedIdentification, FailedIdentification,
    )
    from plate_reader.hasher import hash_plate

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"ERROR: cannot open video: {video_path}", file=sys.stderr)
        return 2

    detector = Detector(cfg)
    detector.open()
    tracker = PresenceTracker(cfg)

    frame_interval = 1.0 / fps if fps > 0 else 0.0
    frame_idx = 0
    n_confirm = n_fail = n_driveoff = n_trigger = 0
    n_vehicle_only = 0
    n_vo_had_plate = 0   # subset that ALSO reaches the drive-off path
    t_start = time.time()

    # Association diagnostic: does IoU break as the plate shrinks?
    from assoc_diag import AssocStats, center_distance_norm
    assoc = AssocStats(iou_threshold=cfg.presence.iou_match_threshold)
    prev_dom_box = None  # largest in-zone plate box from the previous frame

    # Per-track instrumentation (harness only — production Pi never records).
    from plate_reader.track_report import TrackRecorder, TrackSummary
    recorder = TrackRecorder()
    tracker.attach_recorder(recorder)
    track_meta: dict[int, dict] = {}   # track_id -> observed size range etc.
    n_suppressed = 0

    print("=" * 68)
    print(f"VIDEO REPLAY: {video_path}  (sampling ~{fps} fps)")
    print("=" * 68)

    while True:
        ret, frame_bgr = cap.read()
        if not ret:
            break
        frame_idx += 1
        recorder.set_frame(frame_idx)
        # fast-alpr / our pipeline expect RGB (Picamera2 gives RGB888).
        frame = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)

        video_now = frame_idx / fps if fps > 0 else time.time()
        candidates, outcomes = detector.process_frame(frame)

        # Second line of defence for the ultimateALPR path. The engine adapter
        # already raises on '*'-redacted text, but assert it here too: a
        # redacted read that reached this point would be stripped to a plausible
        # 5-6 char string by normalize() and hashed into a confident wrong
        # identity, with nothing downstream able to notice. Raise, never warn.
        for _c in candidates:
            if "*" in (_c.raw_text or ""):
                raise RuntimeError(
                    f"Redacted plate text reached the harness: "
                    f"{_c.raw_text!r} at frame {frame_idx}. Refusing to "
                    "continue — see docs/ULTIMATEALPR_SPIKE_FINDINGS.md §1.3."
                )

        triggers = tracker.observe(frame, candidates, now=video_now)
        for tg in triggers or []:
            n_trigger += 1
            print(f"  f{frame_idx:05d}  TRIGGER  track={tg.track_id} "
                  f"reason={tg.reason} plate={(tg.plate_normalized or '?')[:8]} "
                  f"qpos={tg.queue_position} depth={tg.queue_depth}")

        # --- association diagnostic ---
        # Take the largest in-zone plate box this frame as the "dominant car".
        # If it plausibly follows the previous frame's box (centers within ~1.5
        # box-diagonals, i.e. not a wholly different car across the frame),
        # measure the IoU vs center-distance for that consecutive pair.
        in_zone_boxes = [
            c.bbox for c in candidates
            if getattr(c, "in_zone", True) and getattr(c, "bbox", None)
        ]
        dom_box = None
        if in_zone_boxes:
            dom_box = max(
                in_zone_boxes,
                key=lambda b: (b[2] - b[0]) * (b[3] - b[1]),
            )
            if prev_dom_box is not None:
                # Only count as a same-car pair if not absurdly far apart.
                if center_distance_norm(prev_dom_box, dom_box) <= 1.5:
                    assoc.observe_pair(prev_dom_box, dom_box)
        prev_dom_box = dom_box

        for o in outcomes:
            if isinstance(o, ConfirmedIdentification):
                try:
                    h = hash_plate(o.normalized)[:12]
                except Exception:
                    h = "?"
                # mark_identified runs for suppressed re-reads too: the car IS
                # still present and identified, so it must not later be
                # reported as a drive-off just because we didn't re-count it.
                tracker.mark_identified(o.normalized)
                # Plate text is PII-adjacent, so it is shown only when the
                # operator explicitly asked for rejected-read detail (i.e.
                # they are calibrating). Without it a hash cannot be mapped
                # back to a vehicle, which made the Stage 2b report impossible
                # to audit — two reads of one plate look like two vehicles.
                _plate = (f" plate={o.normalized}"
                          if cfg.diagnostics.log_rejected_reads else "")
                if o.suppressed:
                    n_suppressed += 1
                    print(f"  f{frame_idx:05d}  ~SUPPRESS hash={h}{_plate} "
                          f"repeat=#{o.repeat_index} ({o.suppress_reason})")
                else:
                    n_confirm += 1
                    print(f"  f{frame_idx:05d}  CONFIRM  hash={h}{_plate} "
                          f"det={o.detection_confidence:.2f} ocr={o.ocr_confidence:.2f} "
                          f"frames={o.frame_count}")
            elif isinstance(o, FailedIdentification):
                n_fail += 1
                print(f"  f{frame_idx:05d}  FAIL     {o.outcome} "
                      f"top_det={o.top_detection_confidence:.2f} "
                      f"top_ocr={o.top_ocr_confidence:.2f}")

        # Vehicles tracked but never identified (Problem 2).
        # Deliberately uses the DETECTOR's clock (wall time), not video_now:
        # _note_vehicle_only() stamps records from time.time() inside
        # process_frame, so passing video time here would compare two
        # different clocks and the timeout would be meaningless.
        for vo in detector.collect_vehicle_only(
            timeout_seconds=cfg.presence.track_timeout_seconds,
            min_frames=cfg.presence.min_track_frames,
        ):
            n_vehicle_only += 1
            n_vo_had_plate += 1 if vo.had_plate_candidate else 0
            print(f"  f{frame_idx:05d}  VEHICLE-ONLY frames={vo.frame_count} "
                  f"in_zone={vo.seconds_in_zone}s "
                  f"conf={vo.vehicle_confidence if vo.vehicle_confidence else '?'} "
                  f"travel={vo.travel_fraction:.3f} "
                  f"growth={vo.area_growth_ratio:.2f} "
                  f"had_plate={vo.had_plate_candidate} "
                  f"(no plate ever read)")

        for d in tracker.reap(now=video_now):
            n_driveoff += 1
            # Tell the detector a vehicle finished passing, so a dwell-
            # suppressed plate can release early in a slow queue.
            try:
                detector.note_vehicle_departed(None)
            except Exception:
                pass
            print(f"  f{frame_idx:05d}  DRIVEOFF frames={d.frame_count} "
                  f"dur={d.duration_seconds:.1f}s had_plate={d.had_plate_candidate} "
                  f"color={d.dominant_color} split={d.split_from_color} "
                  f"reason={d.end_reason} board={d.passed_board}")

        if frame_interval:
            time.sleep(frame_interval)

    # Flush vehicles still pending at end of footage, or a car present in the
    # final frames would never be reported.
    for vo in detector.collect_vehicle_only(
        min_frames=cfg.presence.min_track_frames, flush=True
    ):
        n_vehicle_only += 1
        n_vo_had_plate += 1 if vo.had_plate_candidate else 0
        print(f"  f{frame_idx:05d}  VEHICLE-ONLY (eof) frames={vo.frame_count} "
              f"in_zone={vo.seconds_in_zone}s "
              f"travel={vo.travel_fraction:.3f} "
              f"growth={vo.area_growth_ratio:.2f} (no plate ever read)")

    # Flush any tracks still open at end of footage — advance video time
    # far past every timeout and hold window (deterministic, no sleeping).
    eof_now = (frame_idx / fps if fps > 0 else time.time()) \
        + cfg.presence.track_timeout_seconds + 1.0
    # First pass closes normal expiries (may create holds); second pass at a
    # much later time closes anything still held.
    for d in tracker.reap(now=eof_now):
        n_driveoff += 1
        print(f"  f{frame_idx:05d}  DRIVEOFF (eof) frames={d.frame_count} "
              f"color={d.dominant_color} split={d.split_from_color} "
              f"reason={d.end_reason}")
    for d in tracker.reap(now=eof_now + max(
            cfg.presence.track_timeout_seconds * 20, 121.0)):
        n_driveoff += 1
        print(f"  f{frame_idx:05d}  DRIVEOFF (eof-held) frames={d.frame_count} "
              f"color={d.dominant_color} reason={d.end_reason}")

    cap.release()
    # Release the inference engine (see Detector.close()). Matters for the
    # ultimateALPR path, where the engine is a process-global singleton.
    detector.close()
    elapsed = time.time() - t_start
    print("-" * 68)
    print(f"Frames processed: {frame_idx} in {elapsed:.1f}s")
    print(f"Confirms: {n_confirm}   Failures: {n_fail}   Drive-offs: {n_driveoff}")
    print(f"Vehicle-only (tracked, never identified): {n_vehicle_only}")
    print(f"Board triggers: {n_trigger}")
    if n_suppressed:
        print(f"Suppressed re-reads: {n_suppressed}  "
              f"(same car still in frame — NOT counted as visits)")
    # Vehicle count = tracks backed by actual PLATE evidence.
    #
    # This deliberately no longer counts every closed motion track. Motion
    # contours fire on pedestrians, foliage and parked cars, so the old
    # "confirms + drive-offs" estimate read 136 vehicles on footage with ~30
    # real cars — 87 of its 119 drive-offs had never seen a plate. Counting
    # unverified motion blobs as departed customers is a billing claim we
    # cannot support, so plateless tracks are reported separately as motion
    # events and excluded from the vehicle count.
    total_vehicles = n_confirm + n_driveoff
    print(f"Vehicles seen (plate-backed: confirms + drive-offs): {total_vehicles}")
    print(f"  identified          : {n_confirm}")
    print(f"  left unidentified   : {n_driveoff}")
    print()
    print("NOTE: motion-only tracks (no plate ever seen) are NOT counted as")
    print("vehicles — they are pedestrians/foliage/parked cars as often as")
    print("they are real. Plateless CARS are covered by the vehicle-detector")
    print("KPI below instead, which uses the engine's own car box rather than")
    print("motion contours as evidence.")
    print()

    # ---------------- THE KPI ------------------------------------------
    # Cars that went through and were never matched to a plate. These are
    # exactly the cars the FIFO queue cannot tie to an order.
    raw = getattr(detector, "vehicle_only_seen_raw", 0)
    dropped = getattr(detector, "vehicle_only_static_dropped", 0)
    print("=" * 68)
    print("KPI — VEHICLES THROUGH WITH NO PLATE REGISTERED")
    print("=" * 68)
    if not cfg.alpr.classifiers.vehicle_detect_enabled:
        print("  UNAVAILABLE: alpr.classifiers.vehicle_detect_enabled is OFF,")
        print("  so the engine never reports a car box and a plateless car is")
        print("  invisible. This number is NOT zero — it is unmeasured.")
    else:
        print(f"  counted (reported to AWS)     : {n_vehicle_only}")
        print(f"  raw unidentified vehicle boxes: {raw}")
        print(f"  dropped as static/parked      : {dropped}")
        if dropped == 0 and raw > 0:
            print("    (gate rejected nothing here — it is UNEXERCISED on")
            print("     this clip, not proven correct. A clip with parked")
            print("     cars in frame is what would actually test it.)")
        # vehicle-only and drive-off overlap exactly on had_plate_candidate:
        # a localized-but-unreadable plate reaches both paths. Only the
        # no-plate-box subset can be added without counting a car twice.
        vo_no_box = n_vehicle_only - n_vo_had_plate
        print()
        print(f"  of those, plate box seen but unreadable: {n_vo_had_plate}"
              f"  <- ALSO counted as drive-offs")
        print(f"  of those, no plate box at all          : {vo_no_box}"
              f"  <- unique to this path")
        print(f"  DISJOINT total unidentified vehicles   : "
              f"{vo_no_box + n_driveoff}  (= {vo_no_box} + {n_driveoff} "
              f"drive-offs)")
        vo_cfg = cfg.vehicle_only
        print(f"  movement gate: travel>={vo_cfg.min_travel_fraction} "
              f"OR growth>={vo_cfg.min_area_growth_ratio}"
              f"{'  (DISABLED)' if not vo_cfg.enabled else ''}")
        print()
        print("  Compare 'counted' against YOUR hand count of cars in this")
        print("  clip whose plate is not readable:")
        print("    counted < hand count  => misses. Lower min_travel_fraction")
        print("      / min_area_growth_ratio, or check the raw number: if raw")
        print("      matches your count, the movement gate is too strict.")
        print("    counted > hand count  => noise. If raw is far above your")
        print("      count, tighten the gate or set min_vehicle_confidence.")
        print("  Both numbers are printed precisely so the gate can be tuned")
        print("  from the gap rather than guessed at.")
    print("=" * 68)
    print()
    print("NEXT STEP: compare 'Vehicles seen' against a manual count of cars")
    print("WITH VISIBLE PLATES. Over => false captures (tighten the OCR gate).")
    print("Under => missed reads (check plate pixel width / camera framing).")
    print()
    print("-" * 68)
    print(assoc.report())

    # --- per-track report, built from recorded lifecycle events ---
    summaries: list[TrackSummary] = []
    born = {e.track_id: e for e in recorder.events if e.event == "born"}
    reaped = {e.track_id: e for e in recorder.events if e.event == "reaped"}
    per_track_attempts: dict[int, int] = {}
    for a in recorder.attempts:
        if a.chosen_track is not None:
            per_track_attempts[a.chosen_track] = (
                per_track_attempts.get(a.chosen_track, 0) + 1
            )

    for tid, b in sorted(born.items()):
        r = reaped.get(tid)
        detail = r.detail if r else "open"
        if detail.startswith("identified"):
            outcome = "identified"
        elif detail.startswith("drive_off"):
            outcome = "drive_off"
        elif detail.startswith("dropped"):
            outcome = "dropped"
        else:
            outcome = "open"
        boxes = [a.bbox for a in recorder.attempts if a.chosen_track == tid]
        if b.bbox:
            boxes.append(b.bbox)
        sizes = [min(bx[2] - bx[0], bx[3] - bx[1]) for bx in boxes if bx]
        info = tracker.closed_track_info.get(tid, {})
        summaries.append(TrackSummary(
            track_id=tid,
            born_frame=b.frame,
            last_frame=r.frame if r else frame_idx,
            frames=per_track_attempts.get(tid, 0) + 1,
            outcome=outcome,
            had_plate="plate=True" in b.detail,
            top_det=info.get("top_det", 0.0),
            top_ocr=info.get("top_ocr", 0.0),
            duration_s=info.get("duration_s", 0.0),
            color=info.get("color"),
            split_from_color=(info.get("end_reason") == "color_split"),
            min_plate_px=min(sizes) if sizes else 0.0,
            max_plate_px=max(sizes) if sizes else 0.0,
            assoc_attempts=per_track_attempts.get(tid, 0),
            assoc_near_misses=0,
        ))

    print(recorder.report(summaries))

    # --- CSV export for offline analysis ---
    try:
        import os
        base = os.path.splitext(os.path.basename(video_path))[0]
        apath, epath = recorder.write_csv(f"{base}_tracks.csv")
        print()
        print(f"CSV written: {apath}")
        print(f"CSV written: {epath}")
    except Exception as e:
        print(f"(CSV export failed: {e})")

    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Serv presence/drive-off harness")
    sub = ap.add_subparsers(dest="mode", required=False)

    ap_syn = sub.add_parser("synthetic", help="run scripted scenarios (default)")
    ap_syn.add_argument("--config", default=None)
    ap_syn.add_argument("--fast", action="store_true", help="reserved")

    ap_vid = sub.add_parser("video", help="replay a real video (bench/Pi)")
    ap_vid.add_argument("--config", default=None)
    ap_vid.add_argument("--video", required=True)
    ap_vid.add_argument("--fps", type=float, default=5.0)

    args = ap.parse_args()
    mode = args.mode or "synthetic"

    if mode == "synthetic":
        cfg = _load_cfg(getattr(args, "config", None))
        return run_synthetic(cfg, getattr(args, "fast", False))
    elif mode == "video":
        cfg = _load_cfg(args.config)
        return run_video(cfg, args.video, args.fps)
    else:
        ap.print_help()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
