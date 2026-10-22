# Plate Recognizer Stream — confidence gate tuning

**Date:** 2026-08-16
**Unit:** `servCamOG` (Pi 5 4GB, test bench)
**Footage:** `dtfootagefinal.mp4`, transcoded to 1280x720 @ 10fps, played once
in real time over RTSP (MediaMTX) — 1071s, single pass, no looping.
**Stream:** v1.64.0, `sample = 2` (eFPS 5), `memory_decay = 0`, `mmc = no`.
**Ground truth:** `validation/dtfootagefinal_ground_truth.csv` — 30 labelled
events: 21 `conventional`, 8 `no_plate`, 1 `carvana_placard`.
**Captured:** 153 reads, every result field retained (`/tmp/reads.jsonl`).

Reads are attributed to the nearest labelled event within ±12s. Stream's
`position_sec` is its own internal clock (offset ~2999s), so the single
remaining unknown — where the first read sits in the video — was **fitted**
from the data rather than assumed: the best offset (14.0s) matches 23 of 30
labelled events.

---

## Headline numbers

| gate (det/ocr) | cars read | recall | FP on `no_plate` | FP on placard |
|---|---|---|---|---|
| 0.50 / 0.70 *(current)* | 21/21 | 100% | **2** | 0 |
| 0.50 / 0.95 | 21/21 | 100% | 0 | 0 |
| 0.50 / 0.98 | 21/21 | 100% | 0 | 0 |

**Every readable plate was found, at every gate tested.** Recall is not the
constraint — precision is. The two false positives were `no_plate` cars 5 and
21, both reading at exactly 0.910, and both disappear at ocr ≥ 0.95. The
Carvana placard produced no reads at all, which is the correct behaviour.

Score distribution over all 153 reads:

```
ocr : min 0.723   p25 0.954   p50 0.994   p75 0.999   max 1.000
det : min 0.547   p25 0.880   p50 0.905   p75 0.934   max 0.934
```

Real plates cluster hard at 0.99–1.000; the worst *best-read* across the 21
conventional cars was 0.997. That leaves a wide, comfortable separation
between genuine reads (≥0.997) and the false positives (0.910) — a gate
anywhere in 0.92–0.99 separates them perfectly on this footage.

## DECISION — ship these for LJS

```
alpr.thresholds.app_detect_min          0.50 -> 0.75
alpr.thresholds.app_ocr_min             0.70 -> 0.85
thresholds.min_group_detection_confidence    -> 0.75
thresholds.min_group_ocr_confidence          -> 0.85
thresholds.high_ocr_single_frame        0.80 -> 0.95
vote_min_frames                              2   (unchanged)
```

**These are Plate Recognizer's scale and MUST ship atomically with the engine
swap.** LJS is running `ultimatealpr` today, where 0.50/0.70 are correctly
calibrated. Writing these numbers onto the live config before the engine
changes would gate ultimateALPR's scores against PR's distribution and break
recognition immediately. One change, both together, or neither.

**18 Aug 2026 — det floor 0.75 → 0.70.** A white Infiniti with OCR 1.00
and a real 3-character vanity plate sat at det 0.74 and never admitted.
OCR stays 0.85; group det matches the per-frame floor. RAV4-class misses
at det 0.61 are still below this.

### 20 Aug field misses — add a narrow consensus rescue

The day's vehicle ledger showed 77 identified of 81 identifiable vehicles
(95.06%), with one true no-visible-plate vehicle excluded from that KPI. The
four identifiable misses were all saved locally and inspected at original
resolution. PR found and OCR'd a plate in every case; none entered a normal
vote:

| episode evidence | reason it stays / changes |
|---|---|
| 1 read, det 0.825 / OCR 0.800 | one sample cannot establish text agreement |
| 2 reads, best joint det 0.661 / OCR 0.855 | two samples below the det gate are not enough |
| 2 reads, best joint det 0.798 / OCR 0.779 | two samples below the OCR rescue floor |
| 6 reads, best joint det 0.748 / OCR 0.826 | candidate for repeated exact-agreement recovery |

This is not evidence for lowering the normal 0.70/0.85 gates. The production
test is a second, PR-specific path configured under `alpr.thresholds`:

```text
consensus_rescue_min_frames = 3
consensus_rescue_detect_min = 0.65
consensus_rescue_ocr_min = 0.80
```

It confirms only when three independent captures carry the **identical
normalized string and the same physical `encounter_id`**, every read clears
both rescue floors, and each read fails exactly one normal gate. Reads failing
both normal gates, mixed strings, mixed/missing encounter IDs, and one- or
two-frame glimpses remain misses. A confirmed encounter is terminal, so a
later OCR variant cannot create a second identity. Historical sidecars retain
only the episode's best read, not all per-read strings, so the six-read case is
a strong candidate rather than proof that this rule would have rescued it;
live telemetry is the deciding evidence.

Local diagnostic collection now retains the newest 300 outcome cases rather
than pruning by age. During the current canary the dataset contains one
full-resolution best frame for every confirmed identity and plate miss, plus
one later encounter-owned vehicle frame for every terminal `no_visible_plate`
encounter. This avoids freezing the audit on an early high-confidence vehicle
view before the rear plate becomes visible and permits accepted hashes to be
manually checked. A plate-seen terminal vehicle does not duplicate the Detector
miss already stored for that encounter. At the observed 1456×1088 JPEG size,
300 cases are roughly 90 MB.
`save_presence_frames` remains false because enabling it would save every
candidate frame and has a very different storage profile.

`high_ocr_single_frame` is **0.99** on the unit. A score below that still needs
the ordinary two-frame vote unless it qualifies for the stricter three-frame
rescue or the guarded provider-selected path documented below.

### 23 Aug field review — provider temporal selection

Reviewing all 210 retained vehicle episodes found 155 with only one Stream
recognition event. This did **not** mean the RTSP feed supplied one frame:
Stream health was 95%, and Stream emits a vehicle recognition event after
internal tracking rather than exposing every inference frame as an independent
webhook vote. This review assumed `sample = 1` was analyzing a 10fps source;
the 28 Aug audit below later found the live source delivering 5fps.

**Superseded configuration conclusion (retained as history):** this rollout
set both `max_prediction_delay = 3` and `memory_decay = 3`, on the assumption
that they formed one temporal-selection window and that the live source was
10fps. The initial app-side settlement used 0.25s quiet /
0.75s maximum bounds so the system would not add another three-second wait.
A 27 Aug field audit superseded those bounds after the same CR-V produced two
OCR variants 1.6 seconds apart in provider time and 3.115 seconds apart at the
local release boundary. Production now waits for 3.5s of OCR quiet, with a
4.0s hard maximum. This prevents the first alternate hash from escaping, but
it also delays actual identity/Visit delivery by up to four seconds. The route
payload retains the earlier board timestamp; that timestamp does not eliminate
delivery latency. In that rollout, one provider-selected result could confirm
only when it cleared the then-current
0.70/0.85 normal gates, had a stable vehicle encounter, contained at least five
normalized characters, and its plate center is at relative x ≥ 0.45 inside
the vehicle box. Across 2,878 historical localized reads, the 1st percentile
of real plate position was 0.608; the rare reads below 0.45 included measured
short junk/sticker text. This avoids changing the global vote rule for legacy
engines or trusting the previously observed 3–4 character fragments.

Webhook evidence is now `image = original`, not the default vehicle crop, and
specific nearby regions (`us-ky`, `us-in`, `us-oh`, `us-tn`) replace broad
`us`. These changes retain complete lighting/scene evidence and give Stream
the top 3–4 state priors recommended by the vendor.

### 30 Aug field correction: restore broad `regions=us`

The four-state list did not survive direct field validation. For one
human-readable audit frame whose ground truth was `7858JH`, Snapshot reproduced
Stream's incorrect `785BJH` result when given the KY/IN/OH/TN list. The same
original frame returned the correct `7858JH` at OCR 0.908 and detection 0.858
with broad `regions=us`. Two other readable audit frames still returned no
result under either setting, so this correction removes a demonstrated region
regression without claiming to solve every miss. LJS therefore restores
`regions=us`; state-specific regions are not an identity safety mechanism.

### 28 Aug correction — prediction delay is not memory decay

The 23 Aug conclusion above was wrong in two load-bearing ways and must not be
used as deployment guidance:

1. Plate Recognizer documents `max_prediction_delay` as the period in which
   Stream may collect predictions before returning its best result.
   `memory_decay` is instead the interval before it may detect the **same
   vehicle again**. The vendor default is 300 seconds and its documentation
   warns that low values can report one vehicle repeatedly. Equality is not a
   requirement or a desired tuning relationship.
2. On 28 Aug, the then-checked-in `stream/mediamtx.yml` requested 10 source
   fps, but the live LJS `/status/` response reported `received_fps = 5.0` with
   `sample = 1`. The deployed three-second selection window therefore covered
   about 15 analyzed frames, not the repeatedly asserted 30.

See Plate Recognizer's official
[Stream configuration reference](https://guides.platerecognizer.com/docs/stream/configuration/)
for these setting definitions and its
[Stream FAQ](https://guides.platerecognizer.com/docs/stream/help/faq/)
for effective-FPS guidance.

The setting definitions above remain correct, but the vendor default is not
the right site policy by itself. LJS first deployed the following combination:

```ini
sample = 1
max_prediction_delay = 3
memory_decay = 300
plates_per_vehicle = 1
```

### 28 Aug post-deployment canary — restore rear-plate reacquisition

The first live 300-second canary showed why repeat suppression and encounter
deduplication cannot be the same authority in this product. Stream first
reported a valid lane vehicle from the front/side, before any plate was visible.
Its 121-point qualified trajectory arrived roughly 117 seconds after that first
event, but no later rear-plate recognition event was available. The application
had already proved that repeated provider observations can be reconciled into
one physical encounter; preventing those observations upstream removed the
evidence needed to identify the car.

LJS therefore initially overrode the vendor repeat-count default with:

```ini
sample = 1
max_prediction_delay = 3
memory_decay = 3
plates_per_vehicle = 1
```

This was not a return to the old claim that prediction delay and memory decay
form one window. They remain independent. Three-second memory meant Stream
may reacquire the same physical vehicle as its view improves; route/profile
reconciliation, identified-profile retention, and terminal guards must ensure
those independent observations still produce one Visit. The canary contract is
therefore **more provider observations, but no repeated terminal vehicles or
AWS encounter IDs**.

### 30 Aug live correction — preserve a useful trajectory between reacquisitions

The first three-second field vehicle disproved the last sentence above. Stream
reported its front/side at 13:35:48 before the rear plate was visible, then
reported the correct plate 3.4 seconds later and repeated that plated profile
twice more. The identified path deduplicated those three plate observations,
but the first front fragment remained a separate `no_visible_plate` Visit.
The retained full frame confirmed it was the same physical gray vehicle. A
three-second expiry therefore improved OCR opportunity while breaking the
provider trajectory needed to prevent duplicate traffic.

The prior LJS canary was:

```ini
sample = 1
max_prediction_delay = 3
memory_decay = 6
plates_per_vehicle = 1
```

Six seconds is not coupled to prediction delay. At the measured 5 received
FPS it gives the provider about 30 source frames to carry the vehicle from the
front/side toward the board, while still allowing a later rear-plate
reacquisition far sooner than the 300-second default. Acceptance requires both
better exact plate recall and exactly one physical Visit; either result alone
is a failure.

At the measured 5 received FPS, that prediction setting selected from about 15
analyzed frames per observation. `plates_per_vehicle = 1` matches this
product's one rear-government-plate identity model. Allowing two makes Stream
emit extra result entries and creates an unnecessary dealer-tag/sticker
ambiguity.

### 30--31 Aug canary — later selection, then static-repeat correction

The completed 15-image operator audit identified the remaining lifecycle
failure directly: several real lane vehicles were saved just before their rear
plate became visible, every failed episode exposed only one selected provider
read, and real drive-through tracks were also false-rejected by the current
route geometry. The source already delivered the vendor-recommended 5 eFPS, so
raising FPS or lowering identity gates would not address that evidence.

The next bounded canary therefore changes the temporal policy while leaving
source FPS, sampling, confidence gates and route authority unchanged:

```ini
sample = 1
max_prediction_delay = 12
memory_decay = 90
max_dwell_delay = 30
plates_per_vehicle = 1
report_static = no
```

Twelve prediction seconds expose about 60 analyzed frames for Stream's selected
result and move the audited one-read miss from its pre-board selection into the
menu-board window. Ninety seconds of repeat memory covers the observed queue
occupant that remained visible for more than 70 seconds, without returning to
the 300-second canary that suppressed all later rear opportunity. The first version of this canary used
`report_static=yes`. The 31 Aug 37-row full-frame audit falsified that choice:
stopped queue occupants were emitted as fresh provider tracks at an almost
exact 30-second cadence. Two audited sequences alone contained 13 rows for
four physical cars (nine duplicate rows), including seven rows for the same
two simultaneous queue occupants. `report_static=no` removes those stationary
re-reports; vehicles remain eligible while moving into and out of their stop,
and completed dwell trajectories preserve route evidence. This is not a
confidence relaxation and does not change sampling or the twelve-second
best-prediction window. The first post-change window must confirm that later
rear reads are retained before route filtering is made count-authoritative.

### 24 Aug field review — high-OCR localization rescue

The first post-rollout window produced 12 identified of 14 identifiable
vehicles. Full-frame review showed that both rejected candidates contained
the correct OCR text. The first was a clean Kentucky rear plate at det 0.39 /
OCR 1.00; the second was an oblique plate at det 0.64 / OCR 0.82. Those are
not the same risk class and must not share a broad threshold reduction.

Production now has one additional PR-only path for the first class:

```text
provider_high_ocr_rescue_enabled = true
provider_high_ocr_rescue_detect_min = 0.35
provider_high_ocr_rescue_ocr_min = 0.99
```

It accepts only a provider-selected result that fails the normal detection
gate alone. A stable `encounter_id`, at least five normalized characters,
relative plate center x ≥ 0.45 inside the vehicle box, and no competing
normalized variant observed during encounter settlement are mandatory. The
0.64 / 0.82 case still fails both normal gates and remains a miss pending
independent exact agreement. Ordinary 0.70 / 0.85 gates were unchanged by
that 24 Aug decision; the 1 Sep field correction below supersedes them.

### Why these and not the video-derived numbers

This document's footage and the LJS field eval disagree, and **LJS wins,
because the disagreement is about case mix rather than calibration.**

| gates | this video | LJS field eval (221 frames) |
|---|---|---|
| grp 0.85 / fast 0.95 | 21/21 cars, 2 extra ids, **1 FP** | **165/176 (94%)**, 6 hard disagrees |
| grp 0.95 / fast 0.99 | 21/21 cars, 2 extra ids, **0 FP** | **133/176 (76%)**, 2 hard disagrees |

The stricter group floor removes this video's one false positive, but on LJS
it drops real plates that PR scored *exactly right* in the 0.87–0.94 band —
`537MY`, `845Y`, `AFK55`, `886XTP`, `GMONY`, `T1X25`, `E487545`, `235PY`.
That is a 76% confirm rate at a site whose plates genuinely live in that band.

The footage here has plates clustering at 0.99+ and contains a no-plate
vehicle that confirms as a customer; it is a useful **stress test** for that
failure, not a source of LJS thresholds. Score calibration appears to
transfer between the two datasets; case mix does not.

### Day-one watch

If no-plate confirms appear at LJS, raise **`min_group_ocr_confidence` to
0.90 first** — not 0.95, and do not touch the fast path. The generic
instinct to "tighten first" is wrong for this site: LJS's observed failure
mode in the field eval was choppy duplicates and background vehicles, and
thresholds do not fix either. `dwell_zone` does, and it is currently
disabled there.

## Why 0.98 and not 0.95: multiple confirmable strings

Our detector buckets by **exact** normalized string and confirms a bucket
once `vote_min_frames` reads agree. So if one vehicle yields two *different*
strings that each clear the gate enough times, that car confirms **twice** —
one physical car becoming two customer identities.

At ocr ≥ 0.95, three cars produced multiple high-confidence strings:

```
car  6:  bww7132 x4 (best .999)   8ww7132 x1 (best .960)
car 16:  bxv8862 x9 (best .999)   18862   x1 (best .992)
car 15:  d675070 x4 (best .999)   v8862  x3 (best .996)
         8862    x1 (best .999)   d67507d x1 (best .994)
```

Cars 6 and 16 are safe under `vote_min_frames = 2` — the wrong string appears
only once and never reaches a confirm. **Car 15 is not**: `v8862` appears 3
times at ≥0.95 and would confirm alongside `d675070`.

Raising the gate to 0.98 drops `v8862` below two qualifying reads:

```
gate 0.95:  21/21 reachable,  1 car with >1 confirmable string
gate 0.98:  21/21 reachable,  0 cars with >1 confirmable string
gate 0.99:  21/21 reachable,  0 cars with >1 confirmable string
```

**Caveat worth stating plainly:** `v8862`/`8862` on car 15 and `bxv8862` on
car 16 share the same digits, and cars 15 and 16 are only 21s apart. These are
very likely the *same physical vehicle* (car 16) read early while in the
background of car 15's window — not a misread at all, but a real car caught by
the ±12s attribution window. If so, the "duplicate identity" risk here is
partly an artifact of this analysis. It does not change the recommendation
(0.98 is correct either way), but it does point at something real: **the
drive-thru has background traffic in frame, and `dwell_zone` is currently
disabled at LJS.** That zone is the mechanism intended to reject vehicles that
are not at the order point, and it is worth revisiting separately.

## Resolved: the single-frame fast path

Settled at **0.95** by the LJS eval (see the decision section above). The
analysis below is retained because it explains *why* the fast path cannot
simply be disabled, which remains true on both datasets.

### Original open question

`high_ocr_single_frame` (0.85) confirms on one read without waiting for
agreement. With Stream's distribution this interacts badly:

- **Car 1 produced exactly one read in total** (`b09667g`, 0.999). With the
  fast path disabled it can never satisfy `vote_min_frames = 2`, and recall
  drops to 20/21.
- With the fast path enabled at any value ≤ the admit gate, **every** admitted
  read confirms immediately, voting never runs, and the first string wins —
  which is precisely the protection car 15 needs.

Three options, in order of preference:

1. **Keep the fast path, set it high (~0.995).** Car 1 (0.999) still confirms
   on one frame; car 15's `v8862` (0.996) does not, and must win a vote.
   Retains 21/21. Risk: tuned to three decimals on n=21 — a genuine single-read
   car at 0.994 would be missed.
2. **Disable the fast path** (`high_ocr_single_frame = 0`). Strongest
   precision, costs car 1 → 20/21 recall.
3. **Leave it at 0.85.** Simplest, lowest latency, accepts first-read-wins.

This is a precision-vs-recall call with real business weight (a wrong hash is
a wrong customer identity, which also poisons cross-visit history), so it
should be an explicit decision rather than a default.

## Also worth considering

`min_normalized_length` is 3 (was 4). 3-character vanity plates are real
at this site; 1–2 character strings stay rejected. A truncated longer
read that happens to be 3+ characters can still pass — that is accepted
in exchange for not dropping real short plates.

## Reproducing

```sh
# on the Pi: capture harness -> mediamtx -> ffmpeg publisher -> stream
python3 /tmp/capture_reads.py &
./mediamtx /tmp/mediamtx-bench.yml &
ffmpeg -re -i train720p10.mp4 -c copy -f rtsp rtsp://127.0.0.1:8554/cam &
docker start stream

# on a workstation
python3 analyze_thresholds.py reads.jsonl validation/dtfootagefinal_ground_truth.csv
```

**Limitation:** the ground-truth file's `expected_plate` column is empty, so
none of this verifies the actual characters — only that a read occurred where
a car was, and how the strings cluster. Filling that column in would turn this
from a precision/recall estimate into a true accuracy measurement, and is the
single highest-value thing that could be added to the dataset.

## 21 Aug production decision (historical): keep sample=1 at 10fps

The Pi has reached the thermal-control boundary under the 10fps / `sample=1`
production workload: the audit observed 79--81 C and a sticky `0xe0000`
throttle history. That is a real reliability problem, but the same audit does
not justify halving recognition work yet. Four of the six identifiable misses
had only one PR read in their saved episode; `sample=2` could discard that one
opportunity. There is no same-site, same-day sample=1 versus sample=2 A/B yet,
so claiming that every-frame inference does or does not improve recognition
would outrun the evidence.

The 21 Aug decision was to keep the camera at 10fps and Stream at `sample=1`,
add bounded vehicle-first identity settlement, and address cooling. A future
A/B should compare matched operating windows at sample=1 and sample=2 using
identifiable recognition, reads per encounter, temperature, current/sticky
throttle flags, CPU and false-identity review. If sample=2 preserves the
recognition lower bound, it becomes the preferred lower-thermal setting.

## 31 Aug rollout decision: 5fps source, sample=1, temporal lifecycle canary

The current release keeps the source at the measured 5fps and Stream sampling
at `sample=1`, while applying the 12-second prediction, 90-second repeat memory
and static-vehicle settings above. Duplicate suppression and profile
reconciliation remain active. LJS route filtering is authoritative after its
separate reviewed promotion; the exact completed-trajectory geometry remains
the count authority. Roll back the temporal canary if one physical lane
vehicle produces multiple terminal Visits, health or temperature regresses,
or static/background traffic overwhelms the bounded audit set.

Only after this behavior is known should a separate `sample=2` canary run—and
only after measuring the actual source FPS. On the checked-in and live 5fps
source, `sample=2` would analyze only 2.5 eFPS, below the vendor's 4–5 eFPS guidance
for traffic below 30mph. Do not run that canary while the source remains 5fps.
At a verified 10fps source it remains a resource/reliability candidate, not an
accuracy improvement: sampling every other frame can discard the only sharp
frame in a weak episode. Promote it only if matched, manually reviewed windows
preserve identifiable-plate recognition and wrong-identity/duplicate rates
while showing a material thermal benefit.

## 1 Sep field correction: audited 0.80 OCR floor and motion shutter

The 1 Sep human audit found real drive-through candidates at det/OCR 0.754 /
0.813 and a clear rear plate at 0.467 / 0.984. Production therefore keeps the
ordinary PR detection floor at 0.70, lowers both ordinary OCR floors from 0.85
to 0.80, and lowers the guarded detection-only high-OCR rescue from 0.99 to
0.98. Ordinary identity still needs two exact reads; the high-OCR exception
still requires one stable encounter, at least five normalized characters,
rear-plate geometry, no competing variant, and failure of the detection gate
alone. This does not admit the audited 0.731 / 0.770 or 0.853 / 0.769 reads on
one frame; those need independent exact evidence or better capture.

The same audit showed sharp stationary posts and curbs but smeared moving
vehicles after roughly 20:00 even with the `short` exposure profile. That is
motion blur, not focus blur. A 1 Sep fixed-shutter canary at 3333 microseconds
(approximately 1/300 second) failed: the empty-lane full frame had mean
luminance 2.7/255, versus 40.1/255 after immediate rollback to automatic
shutter. The next isolated camera canary therefore keeps source FPS at 5 and
`sample=1`, leaves gain automatic, and uses 16667 microseconds (approximately
1/60 second). This provides five times the light of the failed canary while
still shortening the automatic night exposure. It must be evaluated during
the problem window and rolled back to `short` if plate contrast collapses.
Advancing or adding lane illumination is the durable companion fix; deblurring
or sharpening is not an identity authority and cannot reconstruct characters
lost during exposure.

The dashboard is not deployment authority for this unit's numeric Stream
settings. `/home/serv/stream/config.ini` is a hand-maintained bind-mounted
file, `config_change_restart=no`, and the application updater exports only
`plate_reader/`; it neither installs `stream/config.ini.template` nor restarts
the Stream container. A local configuration edit therefore needs its own
timestamped backup, an explicit Stream restart and an explicit restore/restart
rollback. Do not delete the local file to import the dashboard's stale values.
Detection-zone masks are the exception: live Stream v1.64 loads those from the
web at container startup. The startup log alone does not prove that a correctly
sized per-camera mask became active. After restart, verify the actual camera ID,
1456x1088 dimensions and mask artifact; white is analyzed and black ignored.
Keep the full entry-to-board plate path white. In the 28 Aug route-qualified
corpus, real plate boxes occupied the upper-right overlap corridor (including
one centered near normalized `(0.873, 0.167)`), so do not statically mask the
upper-right terminal band at LJS. A pixel mask cannot distinguish a true-lane
car from a vehicle that later exits there; completed trajectory remains the
route authority. The review-only generated mask in
`stream/detection_zones/` excludes only the unambiguous bottom-left landscaping
wedge. Keep any reviewed mask unchanged during canaries so it does not become
a third experimental variable.

The 30 Aug manual 3→6 change has an exact rollback artifact at
`/home/serv/stream/config.ini.before-memory6-20260830T174520Z`. Restore it only
when the lane is empty, restart Stream, and verify `/status/`; it contains the
3-second predecessor. The deployed pre-release utility still targets 3 seconds,
so do not run it against the canary until the corrected release is installed.

## Deferred future plan (#5): independent total-traffic ground truth

Saved detection photos can score the identities the pipeline attempted; they
cannot prove how many vehicles passed through without producing any saved
event. On a future validation day, record a local-only continuous reference
view and hand-label at least 300 lane vehicles across daylight, dusk and night.
Match those vehicles to encounter IDs and terminal outcomes, then report these
as separate denominators:

- total vehicle capture = tracked lane vehicles / hand-counted lane vehicles;
- identifiable recognition = correct identities / vehicles whose plate is
  visibly identifiable in the reference view;
- identifiable miss rate = identifiable but not correctly identified / all
  visibly identifiable vehicles;
- duplicate rate and wrong-identity rate per physical vehicle.

The reference media and any readable plate pixels remain on the Pi or an
explicitly approved secure review machine; raw plate text still never enters
AWS. This is the test required to substantiate 99% total vehicle capture and
the >=97% identifiable-recognition claim. It is deliberately deferred from
the current live logic change so the two experiments do not contaminate each
other.
