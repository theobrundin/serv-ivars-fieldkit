# Stage 2b — ultimateALPR Pipeline Bring-up on `servCamOG`

**Date:** 2026-08-01
**Unit:** `servCamOG` (Pi 5, Debian 13 trixie, aarch64, 4 cores, 3 GB)
**Engine:** ultimateALPR SDK v3.13.2, licensed, CPU/TensorFlow, OpenVINO+NPU off
**Footage:** `test_footage.mp4`, 4698 frames @ 5 fps
**Status:** end-to-end read → normalize → hash **working on real footage**.

No cloud I/O occurred. The harness makes no network calls (verified: no
`sender`/`requests`/`http` imports), and the unit's config carries bench
placeholders, not production credentials.

---

## 1. Licensed init — confirmed

```
ultimateALPR-SDK version 3.13.2
INFO plate_reader.engines ultimateALPR engine initialised (licensed=True)
INFO plate_reader.engines license verified: unmasked output confirmed (probe read '3PEDLM4')
INFO plate_reader.engines ultimateALPR confidences layout verified: confidences[0] == min(per-char)
```

**No trial banner.** The `*` guard never fired across all 4698 frames, and no
plate string anywhere in the run contains `*`.

**Negative control** — same Pi, same binary, token withheld:

```
You're using an unlicensed version of ultimateALPR-SDK
INFO ultimateALPR engine initialised (licensed=False)
guard fired: RedactedPlateTextError
  Redacted plate text from unlicensed SDK: '3PEDLM*'
```

One variable, two outcomes: `3PEDLM4` licensed, `3PEDLM*` unlicensed. The
verification is not vacuous — it distinguishes the states, and the guard
catches the bad one. The redacted character is `4`.

---

## 2. Throughput — the honest numbers

### ⚠️ Correcting a figure quoted mid-run

An early reading of **276.7 ms/frame (3.61 fps)** was taken from the harness's
own wall-clock total and briefly reported as the headline. **That number is
real but it is not the production number**, and quoting it as such overstates
the cost by ~3.3×.

The harness runs three things production never ships: the association
diagnostic (`assoc_diag`), the per-track recorder (`TrackRecorder`), and
per-event console printing. Measuring only what production actually executes
gives a very different answer.

### Production-shaped path, dense-queue segment (frames 2800–3300)

| stage | mean | p50 | p95 | max |
|---|---|---|---|---|
| decode + cvtColor | 6.7 | 6.2 | 10.5 | 13.6 |
| detector (engine + vote + dwell) | 70.1 | 68.2 | 98.8 | 143.3 |
| presence tracker | 5.9 | 5.7 | 7.6 | 12.8 |
| **TOTAL per frame** | **82.7** | **80.7** | **112.2** | **155.6** |

Budget at 5 fps is 200 ms/frame:

- p50 **80.7 ms → FITS** (2.5× headroom)
- p95 **112.2 ms → FITS** (1.8× headroom)
- worst observed 155.6 ms → still inside budget
- sustained **12.1 fps**

Measured on a deliberately **dense** segment (queue present, multiple vehicles)
because OCR cost scales with the number of plates in frame — a quiet segment
would flatter the result.

### Reconciling the three figures

| figure | value | what it is |
|---|---|---|
| static bench (§8.5) | 106 ms | one still image, engine only |
| **production-shaped** | **82.7 ms** | decode + detector + presence, dense footage |
| harness end-to-end | 276.7 ms | above **plus** assoc diag, track recorder, printing |

The production path is *faster* than the 106 ms static bench, largely because
many real frames contain no readable plate and skip OCR entirely.

**Caveat:** this is still a replay, not a live capture. It excludes camera
acquisition, the sender/queue thread, heartbeat and telemetry, all of which
compete for the same 4 cores. The p95 headroom is real but should not be spent
before a live-camera measurement.

---

## 3. End-to-end pipeline behaviour

Full run: **4698 frames, 7 confirms (6 distinct hashes), 8 suppressed re-reads,
7 failures (all `low_confidence`), 13 drive-offs, 75 board triggers.**

```
f00047  CONFIRM  hash=483cef1ee19e det=1.00 ocr=0.90 frames=2
f01648  CONFIRM  hash=ae8ab02833e0 det=0.98 ocr=0.88 frames=2
f02961  CONFIRM  hash=d532120117e8 det=0.93 ocr=0.87 frames=2
f02966  CONFIRM  hash=d532120117e8 det=1.00 ocr=0.88 frames=2   <-- duplicate
f03179  CONFIRM  hash=74ebcc1d9f47 det=0.98 ocr=0.89 frames=2
f03184  CONFIRM  hash=bf6ba5349e91 det=1.00 ocr=0.87 frames=2
f04081  CONFIRM  hash=7073c4b0d503 det=1.00 ocr=0.89 frames=2
```

The engine-independent logic all flowed correctly with the ultimateALPR result
shape: two-frame voting confirmed on exact-string agreement, dwell suppression
fired 8 times, `low_confidence` failures were emitted on expiry, and every
confirm produced a real SHA-256 via `plate_reader.hasher.hash_plate`. No
crashes, no malformed-result warnings, no `*` leakage.

Detection confidences are notably high (0.93–1.00) versus OCR confidences
clustered tight at 0.87–0.90.

---

## 4. Read / normalization observations

**No change to `NORMALIZATION_SPEC.md` is proposed.** These are exposures to be
aware of, recorded per the Stage 2b brief.

Distinct plate strings read across the run:

```
BF71622   CNH9957   CSC7658   EV1527   GE1527   HNS8456   P311   T
```

### 4.1 🔴 Same physical plate read two ways → two identities

`EV1527` and `GE1527` are almost certainly the same car (fast-alpr read this
plate as `CEV1527`). Both are ≥4 characters, so both survive normalization and
hash to **different** vehicle identities. The leading character is the unstable
one — ultimateALPR appears to clip or misread the first glyph on this plate.

Spec v1.0 cannot absorb this: rule 7 forbids substitution, and correctly so.
It is a read-stability issue, not a normalization issue.

### 4.2 🔴 A duplicate confirm of one car, 5 frames apart

`d532120117e8` confirmed at **f02961 and again at f02966** — 1.0 s apart at
5 fps. Downstream that is **two visits for one car**.

Mechanism: dwell release requires the latch
`dwell_release_requires_absent_frames = 3`. ultimateALPR's read pattern on this
plate flickered — the plate dropped out of the candidate set for ≥3 consecutive
frames, latching `left_frame_since_confirm`, then reappeared and re-confirmed.
The dwell logic behaved exactly as specified; the *input* was flappier than the
logic assumes.

All 8 suppressions show reason `absent 0/3`, i.e. plates that never went absent
at all. So the suppression path works — this one car's read genuinely gapped.

**This is the most consequential finding in the run.** It is a tuning question
(raise the absence threshold, or require absence over a time window rather than
a frame count), not a spec question. It should be settled before pilot, since
it inflates visit counts.

### 4.3 🟡 4-character junk reads clear the length floor

`P311` is 4 characters and therefore passes rule 3's minimum. `T` is 1 and is
correctly rejected. A weak partial read that lands on exactly 4 characters
becomes a hashable identity. The vote gate (2 agreeing frames + confidence
floors) is what actually protects here, not the length rule.

### 4.4 🟢 No redaction, no separators, no case issues

No `*` in any read. No hyphens, spaces or jurisdiction text contaminating
`text` — ultimateALPR keeps country/state in a separate field, which we do not
read. Rules 1 and 2 had nothing to do.

---

## 5. Environment fixes required to get here

Three things blocked the Pi and are now handled in code. All will recur on
every Debian 13 unit.

1. **The adapter had no licence support at all.** `_build_json_config()` never
   emitted `license_token_file`, so the Pi would have run in trial mode and
   redacted every plate. Now wired via the **file path** only — the token
   string never enters config, argv (`ps`/shell history), logs, or the repo. A
   configured-but-unreadable path **raises** instead of falling through to
   trial mode.

2. **The SWIG binding could not be built.** Pi has only Python 3.13.5: no
   Cython, and `distutils` is gone. Compiled the pre-generated SWIG source
   directly with `g++` — which is all `setup.py` does — producing a working
   254 KB `_ultimateAlprSdk.so`. No new build dependencies.

3. **The vendor wrapper cannot import on Python ≥ 3.12.** `ultimateAlprSdk.py`
   (SWIG 2.0.9) does `import imp` at line 13 — removed in 3.12 — *outside* its
   own try block, so it dies before reaching its working fallback.
   `_install_imp_shim()` registers a minimal stand-in whose `find_module`
   raises `ImportError`, routing the wrapper down its documented
   `import _ultimateAlprSdk` path. Vendor code untouched, so an SDK update
   cannot silently revert it.

Runtime also needs `LD_LIBRARY_PATH` to include
`binaries/linux/aarch64` — `PYTHONPATH` alone is not enough, because the
dynamic linker resolves `libultimate_alpr-sdk.so`, not Python.

---

## 6. Open items

| item | status |
|---|---|
| Duplicate confirm from read flicker (§4.2) | **needs a decision before pilot** |
| First-character instability (§4.1) | needs more footage to size |
| Live-camera throughput (vs replay) | unmeasured |
| Mixed-layout read order | OPEN with Doubango; `recogn_rectify_enabled` stays false |
| systemd/service integration for the ualpr path | not started |

`car_noplate_detect_enabled` and `klass_vcr_enabled` were both **on** for this
run (the chosen production profile). LPCI/VMMR/VBSR remain off and are not
exposed in config.
