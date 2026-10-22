# Testing status

What has actually been run, against what, with what result — and what has
not been tested yet. Dated entries so this doesn't quietly go stale; update
it when the next round of testing lands rather than trusting memory.

All footage-based numbers below come from one clip: **`dtfootagefinal`**
(17:51, decimated to 5 fps → 5,357 frames), hand-labelled by Theo into
`Footage - Sheet1 (1).csv` — 29 rows, 20 with a readable plate, 9 flagged
missing-plate. A 30th, untagged car was found independently by two different
mechanisms (see below) and is believed real but is not in the label sheet.

---

## 1. Unit test suites (synthetic, no hardware, run on every change)

Eight suites under `tools/test_*.py`, all green as of the most recent commit:

| Suite | Covers |
|---|---|
| `test_pipeline_features.py` (76 assertions) | ROI, dwell zone, dedup, vehicle-only KPI, group-confidence-floor clamping, config validation |
| `test_dwell.py` | dwell suppression, scalar confidence extraction |
| `test_dwell_e2e.py` | end-to-end dwell scenarios (returning car, etc.) |
| `test_board_trigger.py` | board-arrival trigger, queue position |
| `test_motion_assoc.py` | motion-box association |
| `test_motion_boxes.py` | plate vs. motion track sourcing |
| `test_occlusion_hold.py` | FIFO occlusion hold logic |
| `test_track_report.py` | per-track instrumentation/reporting |

Plus **59 assertions** in `vehicle_records/selftest.py` (new architecture,
no SDK/video/network needed) — see §3.

**Run:** `for t in tools/test_*.py; do python3.11 "$t"; done` and
`python3.11 vehicle_records/selftest.py`.

---

## 2. Production pipeline — real footage, real hardware (2026-08-03 – 08-04)

Unit: `servCamOG` (Pi 5, 4-core, 4GB, bench unit). Engine: `ultimatealpr`,
licensed, `vehicle_detect_enabled=true`, `color_enabled=true`, LPCI/VMMR/VBSR
off (production profile).

### 2a. Plate recall — the group-confidence-floor fix

**Before the fix:** 13/20 plated cars confirmed. Root cause found via
`tools/diag_read_funnel.py`: `min_group_confidence` (0.80) sat behind looser
per-frame floors (det 0.50 / ocr 0.75), silently rejecting agreed-upon votes
that had already cleared the per-frame gate. Every rejection was also
mislabelled `vote_disagreement`, which is impossible by construction (buckets
are exact-string-keyed) — the logs blamed OCR quality for what was actually a
threshold bug.

**Fix:** split into `min_group_detection_confidence` / `min_group_ocr_confidence`
(0.50/0.75, aligned with the per-frame floors), `vote_min_frames` 2→3, new
honest failure reason `group_confidence_floor`.

**Result, full clip replay:** **21/21 confirms**, mapping to **20/20** labelled
plated cars plus the untagged 30th car (see §2c). All confirms landed within
0–3s of their labelled timestamp. Zero ambiguous double-identities.

### 2b. Vehicle-only KPI (missed-vehicle count)

Fixed a real telemetry bug (`record_failure("vehicle_only")` had no handling
branch — the count never reached the heartbeat) and a real double-count
(vehicle-only and drive-off events overlapped on `had_plate_candidate`).

**Result:** **9/9** — every one of the 9 hand-labelled missing-plate cars was
detected, in order, no false positives, no misses. Movement-gate evidence
(travel fraction, area growth ratio) attached to each event for auditability.

### 2c. Independent cross-validation — Plate Recognizer (Snapshot Cloud, "fast")

180 API calls, windowed ±1.5s around each of the 20 labelled plated-car
timestamps (quota-conscious: 1,151 calls remained on the account at the
time). Run locally on the Mac, not the Pi.

**Result:** Plate Recognizer found a plate in **20/20** windows.
**19/20 read the identical string** our pipeline confirmed. One real
one-character disagreement remained genuinely ambiguous and unresolved.

**Also resolved via this run:** the "21st confirm" at 0:59, outside all 29
labelled timestamps, was shown to be separate from car 2. Raw plate strings
are retained only in the private ground-truth workbook, not this repository.

### 2d. Thermal — 100ms vs. 200ms pacing (main.py pipeline, pre-vehicle-records)

10-minute sustained runs, `servCamOG`:

| Pacing | Result |
|---|---|
| 200ms (5 fps, production default) | 68–76°C, **zero throttling** |
| 100ms | Throttled within ~4 min; 82–86°C sustained for the rest of the window |

100ms was also tested earlier on `serv-ljs-001` (different unit): throttled
within ~90s, worse than `servCamOG`. **200ms is the only pacing verified safe
on real hardware, on either unit.**

---

## 3. Vehicle-centric record architecture (new, 2026-08-04)

Self-contained under `vehicle_records/`, imports the production `Detector`/
`normalizer`/`hasher` unchanged rather than reimplementing them.

### 3a. Self-tests — `vehicle_records/selftest.py`, 59 assertions, no hardware

Covers: the plate invariant (`plate_hash` settable only via `confirm_plate()`,
which sets status atomically); the tri-state `plate_status` and that a later
weak read never demotes a `CONFIRMED` record; highest-confidence-wins
reconciliation for colour/make/model and that this rule explicitly does
**not** apply to the plate; freeze-on-send (`RecordFrozenError` on any
mutation after commit); all four send triggers; vehicle-box IoU association;
plate→vehicle attribution by exact string vs. the recency fallback;
`min_track_frames` blip suppression; `emit_plateless` toggle; config
validation (rejects an unknown `send_trigger`).

**Not covered yet:** `pipeline.py`'s bbox-correlation logic (which recovers
colour/make/model from the engine's per-frame stash) has zero test coverage.
`engine.py`'s `RichPlateRead` JSON round-trip and `ReplayEngine` file-reading
are untested. No automated regression test exists for the full-clip result in
§3b — it was verified once, by hand.

### 3b. Full-clip replay — real captured engine output, run on the Mac

`vehicle_records/capture.py` recorded every raw SDK read from the full clip
on `servCamOG` (aarch64, real license) into a replay dataset; `run.py`
replayed it through the full pipeline on the Mac (no SDK needed for replay).

**Result:** **30 vehicle records** — matches Theo's original "30 cars" count
exactly. 21 confirmed, 9 unconfirmed (4 `low_confidence` + 5
`no_plate_visible`) —
matching the ground truth's missing-plate count of 9 exactly. **Zero**
ambiguous plate attribution (`21 by string, 0 by recency, 0 failed`). Vehicle
tracks: 32 created, 2 dropped as sub-`min_frames` blips, 30 emitted — a
substantial improvement over the old plate-keyed presence tracker, which
reported 40 "vehicles seen" for the same ~30 real cars (64.6% IoU association
breakage, measured separately).

Independently corroborates §2c/§2a: the suspected 30th car landed on its own
separate vehicle track from car 2 via SDK car-box geometry, a completely
different mechanism from the timestamp-based reasoning used earlier. It was
not yet confirmed by eye in the source video.

**Colour parsing confirmed working on real hardware:** 1,156/1,156 raw reads
in the capture carried a colour value. Production computes this
(`klass_vcr_enabled: true`) and discards it; the new `RichUltimateAlprEngine`
adapter is the first thing that reads it.

### 3c. CM5/Pi thermal + timing benchmarks — `vehicle_records/bench.py`

Both runs: `servCamOG`, 5 minutes, `--pace` (field-realistic, 5 fps target,
elapsed-aware sleep), looping the full clip as load.

| Config | Mean | p50 | p95 | Max | Achieved fps | Max sustainable | Temp max | Throttled |
|---|---|---|---|---|---|---|---|---|
| Colour only (production profile) | 48.1ms | 40.8ms | 80.2ms | 115.7ms | 4.85 | 20.82 fps | 69.2°C | `0x0` clean |
| + Make/Model (VMMR) | 63.1ms | 41.0ms | **219.8ms** | **307.0ms** | 4.78 | 15.84 fps | 72.5°C | `0x0` clean |

Record/tracking overhead itself: **0.01ms/frame** in both runs — the new
architecture is free relative to inference, which dominates by ~4,800×.

**Reading the VMMR result:** median barely moved (40.8→41.0ms) but p95 and
max nearly tripled. Working hypothesis (not verified frame-by-frame): VMMR
only does real work on frames with a vehicle actually present to classify;
empty-lane frames cost the same as before. `bench.py`'s own gate correctly
flags this as failing the strict "p95 under 200ms budget" bar, even though
mean throughput (4.78 fps) only modestly missed the 5.0 fps target.

Thermally, VMMR is nearly free (+3.3°C max vs. colour-only) — the cost is
latency variance, not heat.

---

## 4. Known gaps — not yet tested

**Benchmark:**
- No ARM clock-speed tracking in `bench.py` (only temp + sticky throttle
  bits) — a real blind spot; clock can dip before throttle bits latch.
- Only 5-minute runs so far for the vehicle-record architecture. The
  `serv-ljs-001` thermal history suggests throttling onset can take several
  minutes; 5 minutes at a comfortable margin doesn't rule out drift at 10+.
- `--pace`-off ("flat out") ceiling-finding mode has never been run for this
  architecture.
- LPCI and VBSR have never been benchmarked alone, and the full combination
  (colour + VMMR + LPCI + VBSR — the historical "~268ms/frame" reference
  point) has never been measured with the current code.
- Everything so far is `servCamOG` only. Nothing has run on `serv-ljs-001`
  (the actual production unit, different board/cooling history) with the new
  architecture.

**Accuracy:**
- VMMR's **speed** was benchmarked; its **output quality** was not. No
  `run.py` pass has been done with VMMR on to inspect real make/model values
  against this footage.
- The suspected 30th car at 0:59 has been corroborated by two
  independent mechanisms (timestamp-gap reasoning, and separate vehicle-box
  tracking) but never confirmed by eye in the source video.
- Car 8's one-character disagreement (ours vs. Plate Recognizer) is
  unresolved.

**Test coverage:**
- `vehicle_records/pipeline.py` (the bbox-correlation logic) and
  `vehicle_records/engine.py` (`RichPlateRead` serialization, `ReplayEngine`)
  have zero unit test coverage.
- The §3b full-clip result (30 records, 21/9 split) is not an automated
  regression test — a future change to the tracker could silently break it
  with nothing catching the regression.
- Vehicle re-identification after occlusion (a car briefly hidden, then
  reappearing) has never been exercised.
