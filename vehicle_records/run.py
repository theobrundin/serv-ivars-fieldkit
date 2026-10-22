#!/usr/bin/env python3
"""Run the vehicle-record pipeline and write one JSON record per vehicle.

Two sources:

  --replay FILE.jsonl   captured real engine output. Runs ANYWHERE, including
                        a Mac with no SDK. Use this to inspect the record
                        shape and the record logic.

  --video FILE.mp4      live inference with the real SDK. aarch64 Linux only.
                        Use this on the CM5 / Pi. Needs --config pointing at a
                        production config.json for the licence token.

Examples
--------
    # Mac: inspect records from captured reads
    python3 vehicle_records/run.py \
        --replay replay_clip.jsonl \
        --config config.example.json \
        --vr-config vehicle_records/config.example.json \
        --out records.jsonl

    # CM5/Pi: real inference
    export LD_LIBRARY_PATH=/home/serv/ultimateALPR-SDK/binaries/linux/aarch64
    export PYTHONPATH=/home/serv/ultimateALPR-SDK/python:$LD_LIBRARY_PATH
    python3 vehicle_records/run.py \
        --video clip_5fps.mp4 \
        --config /home/serv/plate_reader/config.json \
        --out records.jsonl
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile
import time

_REPO = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from vehicle_records.config import VehicleRecordConfig  # noqa: E402
from vehicle_records.pipeline import VehicleRecordPipeline  # noqa: E402
from vehicle_records.record import PlateStatus  # noqa: E402


def _load_prod_cfg(path: str, for_replay: bool = False):
    """Load a production config, filling placeholder identity fields.

    The harness only needs the thresholds / alpr / presence blocks; device_id
    and friends exist purely to satisfy validation.

    `for_replay` drops `alpr.license_token_file`, and ONLY that mode may do
    so. The licence guard exists because an unlicensed SDK silently redacts
    the last plate character, which normalize() then strips, producing a
    confident WRONG identity — so it must never be relaxed for a run that
    actually performs inference. Replay performs none: it reads back plate
    text already decoded by a licensed engine, so there is no SDK to run in
    trial mode and nothing for the guard to protect. The live --video path
    below loads the config untouched and keeps the guard in full force.
    """
    from plate_reader.config import load as load_cfg
    raw = json.loads(pathlib.Path(path).read_text())
    raw["device_id"] = raw.get("device_id", "serv-unit-vr")
    if "REPLACE_WITH" in str(raw.get("device_id", "")):
        raw["device_id"] = "serv-unit-vr"
    raw["api_key"] = "a" * 64
    raw["location_id"] = "vehicle-records-harness"
    if for_replay:
        raw.get("alpr", {}).pop("license_token_file", None)
        raw.pop("license_token_file", None)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="serv-vr-", delete=False
    ) as handle:
        tmp = pathlib.Path(handle.name)
        handle.write(json.dumps(raw))
    try:
        tmp.chmod(0o600)
        return load_cfg(tmp)
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--video", help="live inference (aarch64 + SDK only)")
    src.add_argument("--replay", help="captured reads JSONL (runs anywhere)")
    ap.add_argument("--config", required=True,
                    help="production config.json (thresholds, licence path)")
    ap.add_argument("--vr-config", default=None,
                    help="vehicle-record config JSON; defaults are used if "
                         "omitted (send_trigger=zone_exit)")
    ap.add_argument("--out", default="records.jsonl")
    ap.add_argument("--fps", type=float, default=5.0,
                    help="source fps, for frame-index -> timestamp")
    ap.add_argument("--max-frames", type=int, default=0)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    prod_cfg = _load_prod_cfg(args.config, for_replay=bool(args.replay))
    vr_cfg = (VehicleRecordConfig.load(args.vr_config) if args.vr_config
              else VehicleRecordConfig())

    print(f"send_trigger={vr_cfg.send_trigger.value}  "
          f"min_track_frames={vr_cfg.min_track_frames}  "
          f"emit_plateless={vr_cfg.emit_plateless}")
    th = prod_cfg.thresholds
    print(f"plate gates (UNCHANGED from production): per-frame det>="
          f"{th.min_detection_confidence} ocr>={th.min_ocr_confidence} | "
          f"group det>={th.group_detection_floor} ocr>={th.group_ocr_floor} "
          f"| vote {th.vote_min_frames} frames / {th.vote_window_seconds}s")

    # zone_exit needs a zone. Without one every read is "in zone" by
    # definition, so the vehicle never exits and the trigger can never fire —
    # records then commit on track timeout instead. That is a safe fallback
    # (it is even later, so the plate has more time to confirm), but it must
    # not look like the zone trigger is working when no zone exists.
    from vehicle_records.config import SendTrigger
    if (vr_cfg.send_trigger in (SendTrigger.ZONE_EXIT, SendTrigger.ZONE_ENTRY)
            and not prod_cfg.dwell_zone.enabled):
        print(f"  NOTE: send_trigger={vr_cfg.send_trigger.value} but "
              f"presence.dwell_zone is DISABLED, so there is no zone to "
              f"enter or leave. Records will commit on track timeout "
              f"(frame_exit) instead. Calibrate a dwell zone to use the "
              f"zone triggers.")

    engine = None
    frames_iter = None
    total_frames = 0

    if args.replay:
        from vehicle_records.engine import ReplayEngine
        engine = ReplayEngine(args.replay)
        engine.open()
        total_frames = engine.total_frames
        print(f"replay source: {total_frames} frames of captured reads")

        class _Blank:
            shape = (engine.frame_h, engine.frame_w, 3)
        frames_iter = (_Blank() for _ in range(total_frames))
    else:
        import cv2
        from vehicle_records.engine import RichUltimateAlprEngine
        cap = cv2.VideoCapture(args.video)
        if not cap.isOpened():
            print(f"ERROR: cannot open {args.video}", file=sys.stderr)
            return 2
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        fh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        engine = RichUltimateAlprEngine(prod_cfg)
        engine.set_frame_size(fw, fh)

        def _gen():
            while True:
                ok, bgr = cap.read()
                if not ok:
                    break
                yield cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            cap.release()
        frames_iter = _gen()
        print(f"video source: {total_frames} frames at {fw}x{fh}")

    pipe = VehicleRecordPipeline(prod_cfg, vr_cfg, engine=engine)
    pipe.open()

    n_records = 0
    by_status = {s.value: 0 for s in PlateStatus}
    out_f = open(args.out, "w")
    t0 = time.time()
    idx = 0

    def _emit(rec):
        nonlocal n_records
        out_f.write(json.dumps(rec.to_dict()) + "\n")
        out_f.flush()
        n_records += 1
        by_status[rec.plate_status.value] += 1
        if not args.quiet:
            print(f"  RECORD {rec.record_id}  status={rec.plate_status.value:16s} "
                  f"plate={(rec.plate_hash or '-')[:12]:12s} "
                  f"color={rec.color or '-':10s} "
                  f"make={rec.make or '-':8s} model={rec.model or '-':8s} "
                  f"frames={rec.frames_observed} trigger={rec.send_trigger}")

    try:
        for frame in frames_iter:
            if args.max_frames and idx >= args.max_frames:
                break
            # Virtual clock: frame index / fps. Deterministic, and it makes
            # replay runs reproducible instead of depending on how fast the
            # machine happens to be.
            now = idx / args.fps
            for rec in pipe.process_frame(frame, now=now):
                _emit(rec)
            idx += 1
            if not args.quiet and idx % 500 == 0:
                print(f"  ... frame {idx}/{total_frames}", flush=True)
        for rec in pipe.finish(now=idx / args.fps):
            _emit(rec)
    finally:
        out_f.close()
        pipe.close()

    el = time.time() - t0
    s = pipe.summary()
    print("\n" + "=" * 68)
    print("VEHICLE RECORDS")
    print("=" * 68)
    print(f"  frames processed      : {s['frames']} in {el:.1f}s")
    print(f"  records emitted       : {n_records}")
    for k, v in by_status.items():
        print(f"      {k:18s}: {v}")
    print(f"  plate confirms        : {s['confirms']} "
          f"({s['confirms_suppressed']} suppressed re-reads)")
    tr = s["tracker"]
    print(f"  vehicle tracks created: {tr['tracks_created']}")
    print(f"  dropped (min_frames)  : {tr['dropped_min_frames']}")
    print(f"  plate->vehicle attach : {tr['plate_attach_by_string']} by string, "
          f"{tr['plate_attach_by_bbox']} by box, "
          f"{tr['plate_attach_by_unique_fallback']} unique fallback, "
          f"{tr['plate_attach_failed']} failed")
    if tr["plate_attach_ambiguous"]:
        print(f"      ambiguous/refused     : {tr['plate_attach_ambiguous']}")
        print("      NOTE: these identities were deliberately not guessed.")
    t = s["timing"]
    print(f"  timing/frame          : engine {t['engine_ms_per_frame']}ms  "
          f"track {t['track_ms_per_frame']}ms  "
          f"total {t['total_ms_per_frame']}ms")
    if args.replay:
        print("      (replay: engine time is NOT real inference cost —")
        print("       use bench.py with --video on target hardware)")
    print(f"\nrecords -> {args.out}")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
