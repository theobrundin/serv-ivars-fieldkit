# ultimateALPR Pipeline — Complete and Field-Ready

> Point-in-time bring-up record from 2026-08-01. The 2026-08-04 hardening
> release supersedes its separate production `vehicle_only` event path with a
> single vehicle-box presence lifecycle and one `drive_off` terminal event.

**Date:** 2026-08-01
**Target:** any Pi 5 on 64-bit Raspberry Pi OS / Debian, `binaries/linux/aarch64`
**Developed on:** `servCamOG` (Pi 5, Debian 13 trixie, Python 3.13.5, SDK v3.13.2)

Every deployment-specific value now lives in `config.json`. On install day the
only work is editing config values — **no code changes, no rebuilds**. Nothing
depends on `servCamOG`-specific state.

> **Defaults are inert.** ROI and dwell zone ship disabled / full-frame, so an
> existing config behaves exactly as before this work.

---

## 1. Config schema

All spatial values are **normalized 0.0–1.0**, never pixels.

The imx296 is 1456×1088 native, but capture resolution is per-unit and can
change. A pixel box written against one resolution silently means a different
region at another, and the failure is invisible — plates just stop being
detected, with no error. Normalized coordinates survive a resolution change.
Conversion to pixels happens **once**, internally, from the real frame size
(`config.NormBox.to_pixels`).

### `alpr`

| field | units | default | meaning |
|---|---|---|---|
| `engine` | — | `ultimatealpr` | `ultimatealpr` (alias `ultimate_alpr`) or `fast_alpr` |
| `license_token_file` | path | — | **PATH to the token file, never the token string.** Set-but-unreadable **fails startup**. |
| `assets_folder` | path | `/sdk/assets` | SDK models |

**`alpr.roi`** — where the **engine** looks (`detect_roi`).

| field | default | notes |
|---|---|---|
| `enabled` | `false` | false ⇒ full frame, identical to previous behaviour |
| `x`, `y` | `0.0` | normalized top-left |
| `w`, `h` | `1.0` | normalized size; must be > 0 and stay inside the frame |

**`alpr.thresholds`** — two independent layers.

| field | default | applied |
|---|---|---|
| `detect_minscore` | `0.1` | engine: plate-detection floor |
| `recogn_minscore` | `0.3` | engine: OCR floor |
| `recogn_score_type` | `"min"` | **must be `min`** — SDK default is `median` |
| `app_detect_min` | `0.50` | app: in `detector.py`, after the engine |
| `app_ocr_min` | `0.75` | app: in `detector.py`, after the engine |

Engine floors decide what the SDK *returns*; app floors decide what we *trust*.
Keeping them apart means weak reads stay visible for calibration without being
believed.

`recogn_score_type: "min"` is load-bearing. With it, `confidences[0]` **is** the
per-character minimum, verified against the real binary (delta 0.000000). With
the SDK's `median` default, one misread character is averaged away — the exact
failure the min-per-character gate exists to catch.

**`alpr.classifiers`**

| field | default | cost |
|---|---|---|
| `vehicle_detect_enabled` | `false` | free with base detection |
| `color_enabled` (VCR) | `false` | included in the 106 ms profile |
| `lpci_enabled` / `vmmr_enabled` / `vbsr_enabled` | `false` | **~268 ms/frame together — exceeds the 200 ms budget.** Config warns if enabled. |

**`alpr.engine_advanced`**

| field | default | notes |
|---|---|---|
| `rectify_enabled` | `false` | **Hash-stability flag, not a tuning knob.** Read order under rectification is unverified with Doubango; it may reorder characters and change a plate's SHA-256. Config logs a prominent warning if set. |
| `ienv_enabled` | `false` | night vision; also shifts vehicle box coords |
| `pyramidal_search_enabled` | `false` | the SDK's own sample ships this **on** at sensitivity 1.0; we do not inherit that |
| `pyramidal_search_sensitivity` | `0.28` | |
| `num_threads` | `-1` | auto; explicit 4 measured no better |
| `openvino_enabled` / `npu_enabled` | `false` | |

### `presence`

`vote_min_frames` (2), `vote_window_seconds` (3.0) — frames of *agreeing* reads
needed to confirm.

**`presence.dwell_zone`** — whether a detected vehicle **counts**. Distinct from
`alpr.roi`: the ROI decides where the engine *looks*, the dwell zone decides
what *counts*. This is the fix for exiting cars and background road traffic now
that the camera sits close to the menu board.

| field | default | notes |
|---|---|---|
| `enabled` | `false` | false ⇒ no positional requirement |
| `x`,`y`,`w`,`h` | full frame | normalized |
| `anchor` | `plate_center` | `plate_center` / `vehicle_center` / `vehicle_bottom` |
| `min_seconds_in_zone` | `0.0` | **time**-based, not frame-count — frame counts break when fps varies |
| `min_vehicle_box_area` | `0.0` | normalized area; depth proxy |

`vehicle_bottom` is usually the most stable anchor: it approximates ground
contact and doesn't jitter with plate-read noise.

`min_vehicle_box_area` is the practical stand-in for depth. A 2D image has no
true z; a car at the speaker occupies far more frame area than one driving past
in the background. Measured area is printed by `tools/calibrate.py`.

**`presence.dedup`**

| field | default | notes |
|---|---|---|
| `cooldown_seconds` | `120` | same **hash** cannot re-confirm within this window |
| `release_requires_absent_frames` | `3` | legacy frame-count absence latch |
| `release_requires_absent_seconds` | `0.0` | time-based absence; `0.0` keeps frame-count behaviour |
| `near_match_max_distance` | `1` | suppress a confirm within N edits of one just confirmed; `0` disables |

Two layers, because they catch different failures:

- **`cooldown_seconds`** keys on the **hash**, so it catches an *identical*
  repeat — the read flickering out and back.
- **`near_match_max_distance`** catches a *misread* of the same physical plate,
  which produces a **different** hash and walks straight past the cooldown.

This is a **visit-dedup** rule, not a normalization rule. Nothing about how a
plate is normalized or hashed changes; the pipeline simply declines to emit a
second visit for a string one edit from one just confirmed.
`NORMALIZATION_SPEC.md` is untouched and rule 7 (no character substitution)
still holds — we never rewrite a plate to make it match.

### `capture` / `diagnostics`

`capture.target_fps` (5), `width` (1456), `height` (1088).

`diagnostics.log_per_frame_timing` (false), `zone_preview_overlay` (true),
`log_rejected_reads` (false). Plate strings are PII-adjacent: they are logged at
**DEBUG only**, gated behind `log_rejected_reads`, never at INFO in production.

### Validation — fails loud, before the camera opens

A misconfigured unit refuses to start rather than running subtly wrong for a
week. Rejected with an explicit message:

- ROI / dwell zone outside `[0,1]`, `w`/`h` ≤ 0, or extending past the frame
- `enabled: true` with a degenerate box
- any threshold outside `[0,1]`
- `license_token_file` set but unreadable
- bad `anchor`, negative `cooldown_seconds`, `target_fps` ≤ 0
- `rectify_enabled: true` → loud warning about hash instability

---

## 2. What changed

**ROI** wired to the engine's `detect_roi`, converted from normalized at the
point of use. The SDK orders it `[left, right, top, bottom]` — **not**
`[x1,y1,x2,y2]`; getting that wrong searches the wrong region and looks exactly
like "the model stopped working". Tested at two resolutions to prove the
normalized box scales rather than silently meaning something else.

**Dwell zone** applied in `detector.py` before a read may enter the vote, with
configurable anchor, continuous-time requirement, and area floor.

**Dedup cooldown** — the fix for the §4.2 duplicate. After a hash confirms, the
same hash cannot re-confirm for `cooldown_seconds`. Deliberately independent of
presence/absence tracking, because read flicker is what defeated the absence
latch. Suppressed re-confirms still emit a `ConfirmedIdentification` with
`suppressed=True` and `suppress_reason="cooldown …"`, preserving the existing
shape.

**Vehicle-only events** — `VehicleOnlyIdentification`, a **separate type**, not
a flag. A plateless vehicle has no plate string and therefore no hash; making it
a distinct type means it *cannot* reach the hasher by forgetting to check a
boolean — there is simply no `normalized` field. Dispatched by `main.py` to
`/detection-event` (never `/visit`), and hard-gated on a configured dwell zone
— see §3.2 for why. Emitted only once a vehicle has been *gone* for
`track_timeout_seconds`, so a car still in frame is not reported before it has
had a chance to identify itself, and associated frame-to-frame by **box
overlap** rather than a fixed grid, so one moving car yields one event.

**Confidence** — `confidences[0] / 100` used directly as OCR confidence. We do
**not** recompute `min()` over the raw array: index 1 is the plate *detection*
score, not a character score. The startup assertion that
`confidences[0] ≈ min(confidences[2:])` is retained.

### Bug fixes

1. **`test_dwell_e2e.py` fixed at the root cause.** It was failing on the
   baseline commit. Rather than adding the one missing field, it now builds
   config from the **real** `plate_reader.config` dataclasses, so future schema
   fields arrive with their defaults and this class of stub drift cannot recur.
2. **Effective engine config logged at INFO** on startup. The Stage 2b run had
   no record of what was actually set, which made it unreproducible.
3. **No SDK sample defaults inherited.** The `recognizer` sample ships
   `pyramidal_search_enabled: true` @ 1.0 and `rectify: true`. Our adapter
   writes every key explicitly from `config.json`; asserted by test.
4. **Plate string alongside hash**, gated behind `log_rejected_reads` / DEBUG.
5. **4-char junk reads** (`P311`) — `NORMALIZATION_SPEC.md` unchanged. Regression
   test proves a 4-char *low-confidence* read never confirms while the same
   string at high confidence does; the confidence gate is what protects here,
   not the length rule.
6. **`chmod +x`** on SDK binaries in `scripts/setup_ualpr.sh` (git drops it).
7. **`LD_LIBRARY_PATH`** set in the systemd unit — the *dynamic linker*, not
   Python, resolves `libultimate_alpr-sdk.so`.
8. **Python portability.** The binding is ABI-specific to the Python minor
   version and is now built **on the target unit against its own python3**,
   never shipped pre-built. `_install_imp_shim()` is conditional on
   `sys.version_info >= (3, 12)`, so it is a no-op where `imp` still exists and
   cannot mask real import errors on bookworm/3.11. Python version is reported
   at startup.

---

## 3. Test results

All eight suites green (121 assertions) — the first time the whole suite has passed:

```
test_board_trigger.py      12/12 passed
test_dwell.py              14/14 passed
test_dwell_e2e.py          passed   (was failing on the baseline commit)
test_motion_assoc.py        8/8  passed
test_motion_boxes.py        9/9  passed
test_occlusion_hold.py     16/16 passed
test_pipeline_features.py  55/55 passed   (new)
test_track_report.py        7/7  passed
```

`tools/test_pipeline_features.py` covers ROI (disabled, enabled, cross-resolution
scaling, reaching the engine JSON), dwell zone (in/out, disabled, both vehicle
anchors, area proxy), the cooldown, vehicle-only events, the 4-char junk read,
sample-default isolation, and all config-validation failures.

Both dedup tests **reproduce the original bug before proving the fix**, so they
cannot pass vacuously:

- cooldown: with `cooldown_seconds: 0` the scripted flicker yields 2 confirms
  (the old bug); with `120` it yields 1 confirm + 1 suppressed.
- near-match: with `near_match_max_distance: 0` the `CHR7067` → `CHR706`
  sequence yields 2 identities; with `1` it yields 1 + 1 suppressed. A guard
  test asserts two genuinely different plates still both confirm.

### Real-footage validation on `servCamOG`

Three runs over `test_footage.mp4` (4698 frames), licensed, default config
except where noted:

| run | pyramidal | near-match | confirms | distinct hashes | spurious |
|---|---|---|---|---|---|
| A | off | — | 5 | 5 | 0 |
| B | **on** | off | 6 | 6 | **1** |
| C | **on** | **on** | 5 | 5 | **0** |

**Run B is the important one.** Enabling pyramidal search recovered a sixth
confirm — and that sixth confirm was a *spurious identity*:

```
f02959  CONFIRM  hash=d532120117e8  plate=CHR7067
f02964  CONFIRM  hash=68d2065f19cd  plate=CHR706    <-- same car, 1.0s later
```

A truncated read of a plate already confirmed. Different string ⇒ different
hash ⇒ the cooldown could not see it. **This answers the open question from
`STAGE2B_PIPELINE_BRINGUP.md` §4.1: the 6 hashes did NOT map to 6 distinct
vehicles.** The `EV1527`/`GE1527` observation was the same failure seen in
weak trigger-line reads; here it reached a confirm.

Run C, with near-match dedup on, shows both layers doing distinct jobs:

```
f02959  CONFIRM   hash=d532120117e8  plate=CHR7067
f02962  ~SUPPRESS hash=d532120117e8  (cooldown 0.9s < 120.0s)
f02964  ~SUPPRESS hash=68d2065f19cd  plate=CHR706
                  (near_match of CHR7067 confirmed 1.5s ago, edit distance <= 1)
f02966  ~SUPPRESS hash=d532120117e8  (cooldown 2.1s < 120.0s)
```

The original f02961/f02966 duplicate is gone in every run.

Confirmed plates read cleanly and unmasked: `BFZ1622`, `CEV1527`, `CSC2658`,
`CHR7067`, `RNS8456`.

**Pyramidal search recommendation:** default stays **off**. It costs ~25% frame
time (see §3.1) and, on this footage, bought one extra confirm that was
spurious. It is now *safe* to enable — near-match dedup catches the failure it
introduced — so it is a genuine recall/latency trade to revisit on real
site footage, not a trap.

### 3.1 Throughput (replay, production-shaped)

Frames 2800–3300, dense queue, on `servCamOG`:

| stage | mean | p50 | p95 | max |
|---|---|---|---|---|
| decode + cvtColor | 6.4 | 5.8 | 10.0 | 14.4 |
| detector (engine + vote + dwell) | 50.0 | 46.5 | 74.2 | 97.9 |
| presence tracker | 5.5 | 5.4 | 6.2 | 7.3 |
| **TOTAL** | **61.9** | **58.4** | **85.9** | **108.2** |

**p95 85.9 ms against a 200 ms budget; 16.2 fps sustained.**

This is *faster* than the 82.7 ms measured in Stage 2b, and the reason matters:
that run silently inherited the SDK's `pyramidal_search_enabled: true` default.
Writing every engine key explicitly from config both removed a hidden cost and
made the number reproducible.

### 3.2 Vehicle-only events (Problem 2) — wired, but NOT validatable on this footage

`VehicleOnlyIdentification` is now dispatched by `main.py` to
`/detection-event` with `outcome: "vehicle_only"` — the same channel drive-offs
use. It carries **no `licensePlate` field**, and cannot: the type has no
`normalized` attribute, so there is nothing to hash. That is enforced
structurally, not by a runtime check.

**Measured result, and it is a negative one.** `test_footage.mp4` contains 2
genuinely plateless vehicles. With `dwell_zone.enabled: false` the pipeline
emitted **20 vehicle-only events**, including one tracked for **207.68 s across
775 frames** — a *parked* car. Re-running with a hand-guessed dwell zone
(lower-left, `vehicle_bottom`, `min_vehicle_box_area: 0.06`) still gave 19,
with the same parked car.

Two conclusions:

1. **A vehicle-only event is meaningless without a dwell zone**, so emitting
   them is now hard-gated on `presence.dwell_zone.enabled`. With a full-frame
   zone every parked car, every vehicle on the road behind the lane, and every
   exiting car qualifies as "an unidentified customer". The detector logs a
   one-shot warning explaining this when vehicle detection is on and the zone
   is off.
2. **The count cannot be validated on this footage at all.**
   `test_footage.mp4` was shot at the old camera position and shows a car park
   and a public road, not a drive-thru lane. Any dwell zone I pick is a guess,
   and tuning against it would produce a number that means nothing at the real
   site. The mechanism is correct and unit-tested (9 assertions incl. a moving
   vehicle producing exactly one event); its *thresholds* need site footage.

A likely additional filter, deferred deliberately: a **maximum** dwell. A real
drive-thru customer is present for seconds to a couple of minutes; the 207 s
and 155 s entries above are stationary background vehicles. Adding that now
would be tuning against unrepresentative footage, so it is recorded rather than
implemented.

### Licence gate

```
PASS  token configured    -> "license verified: unmasked output confirmed
                              (probe read '3PEDLM4')", licensed=True, no banner
FAIL  token withheld      -> "FAIL: no licence token configured", exit 1
```

Caught a real regression during this work: moving the token into the `alpr`
block left `_resolve_license_token_file()` reading the removed top-level key, so
the unit silently ran **trial mode with a token configured**. The gate flagged
it. That is exactly the silent degradation the guard exists for, and it is why
"init succeeded" is not accepted as proof of licensing.

---

## 4. On-site calibration runbook

Roughly 15 minutes at the unit.

**1 — Mount and aim.** Fix the camera at its final position. Confirm the lane is
in frame:

```bash
sudo systemctl restart plate-reader
scp serv@<unit>:/home/serv/plate_reader/zone_preview.jpg .
```

With `diagnostics.zone_preview_overlay: true` this image shows the ROI (yellow)
and dwell zone (magenta) drawn on the live frame — aim and config verified in
one glance, remotely.

**2 — Sample real cars.** With a car at the speaker:

```bash
cd ~/serv-pi && python3 tools/calibrate.py --out cal.jpg
```

It prints every plate and vehicle box in **both** pixel and normalized
coordinates, the vehicle area (your `min_vehicle_box_area` candidate), the
`vehicle_bottom` anchor point, and a copy-pasteable `alpr.roi` block. Repeat for
several cars — one frame is one car's position.

**3 — Set the ROI.** Paste the suggested block into `config.json`, set
`enabled: true`, restart, re-run `calibrate.py` to confirm plates still land
inside it.

**4 — Watch an exiting car.** This is the step that earns the dwell zone:

```bash
python3 tools/live_monitor.py
```

One line per detection: plate, confidences, vehicle box, `zone=IN/OUT`, and the
**gate that rejected it**. Watch a car exit and a car pass on the road; note
where their anchors sit versus a car actually ordering.

**5 — Set the dwell zone.** Choose `anchor: vehicle_bottom` (most stable), draw
the zone around where ordering cars sit, and set `min_vehicle_box_area` just
below the smallest *ordering* car's area from step 2. Re-run `live_monitor.py`
and confirm exiting/passing cars now show `zone=OUT`.

**6 — Verify.**

```bash
bash scripts/verify_ualpr_license.sh    # must print PASS
sudo systemctl restart plate-reader
journalctl -u plate-reader -f
```

The startup log prints the **effective engine config**, the Python version, and
the resolved `detect_roi` in pixels. If a car confirms and an exiting car does
not, the unit is calibrated.

---

## 5. Deployment

`scripts/setup_ualpr.sh` — idempotent: clones the SDK, restores exec bits,
builds the binding **against the unit's own python3** (falling back to a direct
`g++` compile when Cython/distutils are absent, as on Debian 13), installs the
token `chmod 600`, and verifies the import through `plate_reader.engines` so the
`imp` shim is exercised the way production does.

`scripts/verify_ualpr_license.sh` — the licensed-init gate. Wired into
`install.sh` step 6b, which runs only when config selects ultimateALPR.

`systemd/plate-reader.service` — carries `LD_LIBRARY_PATH` and `PYTHONPATH`.
systemd does not inherit an interactive shell environment, and omitting these
fails in two confusing ways: the linker cannot find
`libultimate_alpr-sdk.so` even though it is plainly on `PYTHONPATH`.

---

## 6. Not done / open

| item | status |
|---|---|
| **Live-camera throughput** | **NOT MEASURED — no camera attached to `servCamOG`** (`rpicam-hello` reports "No cameras available!"). All timing is replay-based: 61.9 ms/frame mean, p95 85.9 ms. Camera acquisition, sender and heartbeat threads are **not** in that number. Must be measured on the field unit before pilot. |
| Service start / reboot survival | **Not verified** — the unit has no camera, so `plate-reader.service` cannot complete boot. The unit file carries the required env; that it *starts* is unproven. |
| Mixed-layout read order | OPEN with Doubango; `rectify_enabled` stays `false`. |
| First-character instability | **Resolved as a duplicate-identity risk** (§3, run B) and now suppressed by near-match dedup. The underlying *read* instability remains — ultimateALPR still clips leading characters on some plates — but it no longer creates a second visit. |
| Near-match false merges | Two genuinely different plates within 1 edit, arriving inside the 120 s cooldown, would be merged into one visit. Tested as a guard (`ABC1234` vs `XYZ9999` both confirm), but plates one edit apart in the same queue window are possible in principle. Set `near_match_max_distance: 0` to disable if it ever misfires. |
| Dwell-zone values for the new geometry | Must be set on site via the runbook — no footage from the new mount position exists yet. |

The new deployment geometry (camera close to the board, oblique rear plates,
near-stationary vehicles) is **not represented** in `test_footage.mp4`, which was
shot at the old position. The dwell-zone machinery is built and tested, but its
*values* cannot be chosen from existing footage.
