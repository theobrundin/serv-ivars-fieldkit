# ultimateALPR Migration — Stage 1 Spike Findings

**Date:** 2026-07-30 (updated 2026-07-31 with Stage 2a measurements and
Doubango's answers — see §8)
**Author:** Claude Code (spike executed on Theo's Apple Silicon Mac)
**Status:** Stage 1 complete. Stage 2 **conditionally** blocked — see §1.
**Scope:** Feasibility only. No file under `plate_reader/` was modified.

Working directory confirmed at start: `/Users/theobrundin/Desktop/serv-pi`.
No nested `serv-pi/serv-pi/` exists on this machine.

> **Repo-state caveat:** the git root is `/Users/theobrundin/Desktop`, *not*
> `serv-pi`, and it reports "No commits yet on master". `serv-pi/` is currently
> an untracked subdirectory of a Desktop-wide repo with zero commits. Nothing in
> this spike depends on that, but a migration of this size should not start from
> an uncommitted tree — there is no baseline to diff or revert to.

Artifacts live outside the repo in `~/Desktop/alpr-spike/`:
`Dockerfile`, `poc_ualpr.py` (throwaway), `ultimateALPR-SDK/` (1.3 GB clone).

---

## 1. Go / No-Go on the Mac dev loop

**Split verdict. The engine runs; the validation loop does not.**

| Question | Verdict |
|---|---|
| Can the aarch64 SDK run on this Mac at all? | **GO** — confirmed, natively |
| Can the SWIG Python binding be built + imported? | **GO** — confirmed |
| Can we do engine/plumbing/vehicle-detection work here? | **GO** |
| Can we do the Stage-2 **normalization/hash diff** here? | **NO-GO** — see §1.3 |

### 1.1 The engine runs natively — evidence

`linux/arm64` container, no `--platform` flag, no QEMU. Actual output:

```
$ docker inspect alpr-spike --format '{{.Architecture}}/{{.Os}}'
arm64/linux

$ uname -m           # inside container
aarch64

$ ldd recognizer     # inside container
	libultimate_alpr-sdk.so => /sdk/binaries/linux/aarch64/libultimate_alpr-sdk.so
	libstdc++.so.6 => /lib/aarch64-linux-gnu/libstdc++.so.6
	libm.so.6 => /lib/aarch64-linux-gnu/libm.so.6
	libgcc_s.so.1 => /lib/aarch64-linux-gnu/libgcc_s.so.1
	libc.so.6 => /lib/aarch64-linux-gnu/libc.so.6
	...
```

Every dependency resolves. No `not found`, no missing TensorFlow, no broken
`libtbb.so` — the aarch64 tree does not have the defects the x86_64 tree has.
No SIGILL: the AVX/AVX-512 illegal-instruction crash that killed the emulated
x86_64 path does not occur, because nothing is emulated.

C++ `recognizer` on `assets/images/lic_us_1280x720.jpg`, **trial mode, no
`--tokendata`**, ran to completion and returned:

```json
{"duration":93,"frame_id":0,"plates":[{
  "car":{"confidence":99.60938,
         "color":[{"confidence":97.65625,"name":"b*****"}],
         "makeModelYear":[{"confidence":87.89062,"make":"bmw","model":"4*****"}],
         "warpedBox":[78.4,151.4,1095.8,151.4,1095.8,619.1,78.4,619.1]},
  "confidences":[89.93234,99.60938,90.45452,90.46402,89.93234,
                 89.96776,90.43755,90.58742,90.51079],
  "country":[{"code":"USA","confidence":99.60938,"klass":80,
              "name":"United States of America","state":"California"}],
  "text":"3PEDLM*",
  "warpedBox":[821.1,337.2,913.9,337.2,913.9,397.0,821.1,397.0]}]}
```

Engine self-reports **version 3.13.2**, OCR backend TensorFlow 1.12.3.

> **Version discrepancy — read this.** The task asked to confirm ≥ v3.11.2.
> `c++/ultimateALPR-SDK-API-PUBLIC.h` declares `3.10.5`
> (`VERSION_MAJOR 3 / MINOR 10 / MICRO 5`), which would *fail* that check. The
> shipped binary reports `3.13.2` at init. The header macro is stale relative to
> the binaries; the `.so` is stripped so it carries no version string. The
> authoritative number is the runtime banner: **3.13.2, which passes.** Do not
> gate CI on the header macro.

### 1.2 The Python binding builds and imports

```
$ cd /sdk/binaries/linux/aarch64
$ python3 /sdk/python/setup.py build_ext --inplace
building '_ultimateAlprSdk' extension
aarch64-linux-gnu-gcc -shared ... -lultimate_alpr-sdk -o _ultimateAlprSdk.so
$ ls -la _ultimateAlprSdk.so
-rwxr-xr-x 1 root root 549736 _ultimateAlprSdk.so

$ PYTHONPATH=/sdk/python python3 -c "import ultimateAlprSdk; print('OK')"
IMPORT OK
requestRuntimeLicenseKey reachable: True
engine methods: ['deInit','exifOrientation','init','process',
                 'requestRuntimeLicenseKey','warmUp']
```

Only compiler warnings (`register` storage class, one `strncpy` truncation) —
no errors. Two build gotchas, both real and both already hit:

1. `setup.py` **must** run from `binaries/<os>/<arch>/`, not `python/`. It uses
   `../../../python/...` and `../../../c++` relative paths. From `python/` it
   fails with `cc1plus: fatal error: /python/...SWIG_python.cxx: No such file`.
   The artifact lands in the *binaries* dir and must be copied to `python/`
   (or that dir added to `PYTHONPATH`).
2. `git clone` did **not** preserve the exec bit on the shipped binaries.
   `./recognizer` → `Permission denied`. Fix in-container (`cp` + `chmod +x`)
   rather than chmod-ing the host tree.

### 1.3 Why Stage 2's validation loop is blocked: trial mode redacts plate text

This is the finding that matters most, and it was not anticipated in the task
brief.

**In unlicensed/trial mode the SDK replaces the final character of every plate
string with `*`** — and masks other string fields too (`color: "b*****"`,
`model: "4*****"`). Measured across four SDK sample images via `poc_ualpr.py`:

| Image | `text` | after `normalize()` v1.0 | SHA-256 |
|---|---|---|---|
| lic_us_1280x720 | `3PEDLM*` | `3PEDLM` (6) | `1264830ef8594787…` |
| nyc | `FBE348*` | `FBE348` (6) | `28df13cb0ae569bf…` |
| multi | `3020CE*` | `3020CE` (6) | `c55536779086b216…` |
| multi | `34IN7*` | `34IN7` (5) | `33813fb819503ba0…` |
| multi | `2046X*` | `2046X` (5) | `f1cd3ff7922a29bd…` |

**Every one produced a valid-looking hash.** This is the dangerous part:
NORMALIZATION_SPEC v1.0 rule 2 strips `*` as a non-alphanumeric character, and
the survivor is 5–6 chars, which clears the rule-3 minimum of 4. The redaction
therefore **fails silently** — no exception, no rejection, no log line. It
manufactures a confident, wrong, permanently-wrong vehicle identity.

Consequences:

- A Mac-side dual-engine normalization diff against `test_footage.mp4` would be
  comparing fast-alpr's real strings against ultimateALPR's **truncated** ones.
  Every row would differ, for a reason that has nothing to do with OCR quality.
  The diff would be worthless and actively misleading.
- Any Stage-2 harness run on this Mac must **hard-fail on `*` in raw text**
  rather than normalize it. Recommended guard, in the adapter and the harness,
  before any hashing: `if "*" in raw_text: raise/skip`.

I could **not** verify that a licensed runtime removes the masking, because
verifying it requires activating a license, and activation is irreversible and
machine-bound. Per the task rules I did not generate a runtime key or use
`--tokendata`. The unlicensed banner makes licensing the overwhelmingly likely
cause, but it is an inference, not a measurement. **This is the first question
for `tech@doubango.org` (§6).**

### 1.4 What this means practically

- Stage 2 **adapter development, API plumbing, vehicle-detection work, and
  performance measurement** can all proceed on this Mac today.
- Stage 2 **accuracy/hash validation cannot**, on this machine, in this mode.
  It needs either (a) written confirmation + a licensed runtime somewhere, or
  (b) the licensed Pi.
- The harness itself must move **inside the container** for any ualpr run —
  there is no native macOS build (§2), so `tools/replay_harness.py` cannot
  `import ultimateAlprSdk` from the host Python at all.

---

## 2. Task 1 — SDK inventory

Cloned to `~/Desktop/alpr-spike/ultimateALPR-SDK` (outside the serv-pi tree),
`--depth 1`, HEAD `972506c` (2025-11-06), 1.3 GB.

**Platform folders under `binaries/`:**

```
binaries/android/jniLibs      binaries/linux/aarch64      binaries/osx
binaries/ios                  binaries/linux/armv7l       binaries/raspbian/armv7l
binaries/jetson/aarch64       binaries/linux/x86_64       binaries/windows/x86_64
```

**macOS/Darwin/arm64-Apple build: confirmed absent.** Your belief was correct,
but there is a trap: **`binaries/osx/` exists.** It contains exactly one file,
`.gitkeep`, and nothing else. A `find` for `*darwin*`, `*macos*`, `*apple*`,
`*.dylib` across the whole repo returns **zero** results. The directory is an
empty placeholder — do not let its presence suggest a macOS path exists.

**`python/` contents** — as you believed, no wheel:

| File | Notes |
|---|---|
| `ultimateAlprSdk.py` | 8.5 KB pure-python SWIG wrapper |
| `setup.py` | distutils + `Cython.Distutils.build_ext`; needs `cython3` |
| `ultimateALPR-SDK-API-PUBLIC-SWIG_python.cxx` | 209 KB generated SWIG source |
| `ultimateALPR-SDK-API-PUBLIC-SWIG_python.h` | header |
| `README.md` | build notes |

No `.whl`, no `pyproject.toml`, no `pip install` path. The extension must be
compiled per target, and it links against `libultimate_alpr-sdk.so` in the
binaries dir (`library_dirs=['.']`), which is why the working directory matters.

**`samples/python/recognizer/` exists**: `recognizer.py`, `README.md`, and
launcher scripts `run_linux_x64.sh`, `run_rpi_armv7l.sh`, `run_windows_x64.bat`.
There is **no aarch64 launcher** — the closest is `run_rpi_armv7l.sh`. Expected
`PYTHONPATH` (from `run_linux_x64.sh`):

```
PYTHONPATH=../../../binaries/linux/x86_64:../../../python
```

i.e. *both* the arch binaries dir and `python/`. For our target that becomes
`binaries/linux/aarch64:python`. The sample is single-image, PIL-based, and
reads `result.json()`.

**aarch64 binaries dir** — clean, no defects:

```
libultimate_alpr-sdk.so          16.8 MB   ELF 64-bit LSB shared object, ARM aarch64, stripped
libultimatePluginAmlogicNPU.so  100.8 KB
benchmark / recognizer / runtimeKey       (executables, exec bit not preserved by clone)
python_setup.sh / README.md
```

`find -maxdepth 1 -size -1k -type f` → only `python_setup.sh` (55 bytes, a
one-line wrapper). No flattened-symlink text files, no missing TensorFlow.
The x86_64 fixes (`libtbb.so` relink, TF 1.15 download) are **not** needed here
and must not be applied.

---

## 3. Task 3 — API surface mapped onto our needs

### 3.1 Lifecycle: global singleton vs. per-call object

This is the biggest structural difference and it has direct threading and
harness consequences.

| | fast-alpr | ultimateALPR |
|---|---|---|
| Shape | `alpr = ALPR(...)`, then `alpr.predict(frame)` | free functions on a **process-global** engine |
| Instances | many, independent | **exactly one per process** |
| Calls | `predict()` returns objects | `init(json)` → `process(...)` → `deInit()` |
| Config | constructor kwargs | one JSON string at `init` |

Python surface (verified reachable):

```python
ultimateAlprSdk.UltAlprSdkEngine_init(jsonConfig)      -> result
ultimateAlprSdk.UltAlprSdkEngine_process(imageType, data, w, h, stride, exifOri) -> result
ultimateAlprSdk.UltAlprSdkEngine_warmUp(imageType)     -> result
ultimateAlprSdk.UltAlprSdkEngine_deInit()              -> result
ultimateAlprSdk.UltAlprSdkEngine_requestRuntimeLicenseKey(rawInsteadOfJSON=False)
result.isOK() -> bool ; result.json() -> str
```

**Implications for us:**

- `Detector.open()` currently constructs a per-`Detector` engine. With
  ultimateALPR, two `Detector` instances in one process would share — and
  fight over — one global engine. A second `init()` without `deInit()` is
  undefined from our side.
- **`tools/replay_harness.py` is the immediate victim**: anything that
  constructs `Detector` more than once per process (A/B runs, parameter sweeps,
  the dual-engine diff itself) breaks. The adapter must own a module-level
  refcount/singleton guard, and `close()` must actually `deInit()`.
- Config is fixed at `init`. Any threshold we currently change per-instance and
  push into the engine now requires a full `deInit`/`init` cycle.
- `warmUp(imageType)` has no fast-alpr equivalent; worth calling at boot so the
  first real frame isn't slow.

### 3.2 Result structure and confidence semantics

Top level: `{"duration": int, "frame_id": int, "plates": [ ... ]}`.
Per entry: `text`, `confidences[]`, `warpedBox[]`, optional `car{}`,
optional `country[]`.

**Boxes are quads, not rectangles.** `warpedBox` is **8 floats** =
4 corners `[x1,y1, x2,y2, x3,y3, x4,y4]`, confirmed on every detection.
`detector.py` and `bbox_inside_zone()` expect `(x1,y1,x2,y2)`. The adapter must
reduce the quad to its axis-aligned bounding box
(`min/max` over the 4 xs and ys). In the samples observed the quads were
axis-aligned already, but that is not guaranteed once
`recogn_rectify_enabled` is on or plates are viewed at an angle.

**`confidences` semantics are UNDOCUMENTED and do not map 1:1 to characters.**
Measured relationship is consistently `len(confidences) == len(text) + 2`:

| text | len(text) | len(confidences) |
|---|---|---|
| `3PEDLM*` | 7 | 9 |
| `FBE348*` | 7 | 9 |
| `3020CE*` | 7 | 9 |
| `34IN7*` | 6 | 8 |
| `2046X*` | 6 | 8 |

I grepped the entire repo — `README.md`, `c++/ultimateALPR-SDK-API-PUBLIC.h`,
all samples — for `"confidences"`, "per-char", "character confidence". **Zero
hits.** The layout is not documented anywhere in the distribution. In the raw
JSON, `confidences[1]` was `99.60938`, exactly equal to `car.confidence`, which
hints the array interleaves non-character scores, but I will not guess.

**This blocks a faithful port of our single most important correctness line.**
`_to_scalar_confidence()` ([detector.py:60](../plate_reader/detector.py#L60))
takes `min()` across *per-character* confidences, and the file documents why
(synthetic example `TESTI234`: mean 0.971 passes, min 0.794 correctly rejects,
expected fixture `TEST1234`). Taking `min()` over an array with 2 unknown extra entries is
**not** the same gate. If one of the extras is a detection or whole-plate score
it will usually be high and harmless; if it can be low it will cause spurious
rejections. Either way we would no longer know what we are measuring.
Mitigation: set `recogn_score_type: "min"` in the engine config (supported, and
it matches our semantics) and treat the engine's own aggregate as authoritative
until Doubango clarifies the array. **Question 2 for Doubango (§6).**

### 3.3 Config key mapping onto `Thresholds`

Our `Thresholds` ([config.py:28](../plate_reader/config.py#L28)) vs SDK config:

| serv-pi | ultimateALPR | Notes |
|---|---|---|
| `min_detection_confidence` | `detect_minscore` (default 0.1) | **Scale differs:** SDK JSON config is 0–1, but returned confidences are **0–100**. Adapter must divide by 100 or our floats mean nothing. |
| `min_ocr_confidence` | `recogn_minscore` (default 0.3) | same scale caveat |
| *(min-per-char rule)* | `recogn_score_type: "min"` | direct semantic match — the SDK natively supports `min` vs mean |
| — | `pyramidal_search_enabled` / `_sensitivity` / `_minscore` / `_min_image_size_inpixels` | **No serv-pi equivalent.** Second-pass small-plate search. Default on. Costs time, raises small-plate recall. New tuning axis. |
| — | `recogn_rectify_enabled` (default true) | Dewarps angled plates. **No equivalent.** Likely relevant to our drive-thru geometry. Changing it changes OCR output → changes hashes. |
| `min_group_confidence`, `vote_*`, `dwell_*` | *(none)* | **Ours alone. Engine-independent. Stays in `detector.py` untouched.** |
| `min/max_normalized_length` | *(none)* | normalizer's, unaffected |

Observed engine defaults (from the C++ run's echoed `jsonConfig`):
`detect_minscore 0.1`, `recogn_minscore 0.3`, `recogn_score_type "min"`,
`pyramidal_search_enabled true`, `pyramidal_search_minscore 0.3`,
`car_noplate_detect_min_score 0.8`, `recogn_rectify_enabled true`,
`ienv_enabled` false on ARM / true on x86-64.

### 3.4 ⭐ It returns VEHICLE boxes — Problem 2 and the ConverseNow blocker

**This is the strategically significant finding and it is a clear win.**

fast-alpr returns plates only. That is precisely why Problem 2 (vehicle present,
no readable plate) is deferred and why the ConverseNow board-trigger timing work
is blocked.

ultimateALPR returns, in the **same** `process()` call, with no second model:

1. **Vehicle boxes with confidence**, via `car_noplate_detect_enabled: true`.
   On `nyc.jpg`, **4 of 5 entries had `text: ""` but a populated `car` object
   with confidence 85.9–99.6%.** That is exactly "a car is here, I cannot read
   its plate" — the Problem 2 signal, delivered directly.
2. **LPCI — country *and US state***: `{"code":"USA","state":"California"}`,
   `{"code":"USA","state":"Alaska"}`, plus `ESP`/`PRT` on the multi-country
   image. Jurisdiction without a separate model.
3. **VCR** — vehicle colour (masked in trial: `"b*****"`).
4. **VMMR** — make/model/year (`"make":"bmw"`, masked model `"4*****"`).

Enabling all of these cost **53–93 ms/frame** on this Mac's container. For
comparison, last session's full-video LPR-only scan on the same hardware ran
5368 frames at ~67 fps. Pi 5 will be slower — but this is one engine doing what
would otherwise need three.

Caveat: VCR/VMMR/LPCI are **classification over a detected vehicle**, not a
tracker. They do not replace `presence.py`. And note the extra entries with
empty `text` will flow into `_run_one()` as candidates with `normalized=None`
→ `below_threshold=True` → straight into the presence tracker. That is
*compatible* with today's code but changes candidate volume substantially
(5 entries where fast-alpr would yield 1). Presence/dwell behaviour must be
re-tuned, and `car_noplate_detect_enabled` should probably start **off** for a
like-for-like Stage-2 comparison, then be turned on deliberately as its own
piece of work.

### 3.5 `requestRuntimeLicenseKey` from Python — CONFIRMED

```
requestRuntimeLicenseKey reachable: True
engine methods: ['deInit','exifOrientation','init','process',
                 'requestRuntimeLicenseKey','warmUp']
```

Signature: `requestRuntimeLicenseKey(rawInsteadOfJSON=False)`. **We can generate
the Pi's runtime key from our own Python code and skip the C++ sample and the
shipped `runtimeKey` binary entirely.**

⚠️ I did **not** call it. It is reachable, not exercised. Calling it on this Mac
would burn one of 15 machine-bound activations on a machine we will never
deploy. Whoever runs it first should run it **on the target Pi**.

---

## 4. Task 4 — Normalization exposure (analysis only; spec unchanged)

Assessed against NORMALIZATION_SPEC v1.0. **No change to the spec is proposed.**

### 4.1 🔴 Cross-engine hash discontinuity — the largest exposure

**Not absorbed. This is a fleet-wide identity break, not an edge case.**

Spec rule 7 forbids character substitution, and justifies it explicitly:

> "fast-alpr's OCR is reproducible: the same image produces the same read, so as
> long as we are consistent, we still hash the same vehicle to the same value
> across visits even if our read of the plate is slightly off."

The entire safety argument for rule 7 is **intra-engine consistency**. The rule
is not "we read plates correctly", it is "we read them *wrongly but identically*
every time". Swapping the OCR engine invalidates the premise directly.

Every physical plate where ultimateALPR's read differs from fast-alpr's by even
one character produces a **different SHA-256**, and every vehicle hash currently
in production DynamoDB was produced by fast-alpr. At cutover those customers
appear brand new. The spec's own preamble calls this the catastrophic failure
mode ("the customer looks new on every future visit").

The exposure is not hypothetical: last session's measurements on
`dtfootage_5fps.mp4` showed the two engines disagreeing on real plates —
ultimateALPR read `CKU108…`, `BCB650…`, `D67507…`, `BH168…` where fast-alpr
produced nothing at all, and `ATV193*` where ground truth is `AIV1937`
(an `I`→`T` substitution). Better recall, *different strings*.

This is a migration-design problem, not a normalization bug — v1.0 cannot absorb
it because nothing in normalization is wrong. Stage 2 must decide between:
dual-hash overlap (spec change procedure steps 1–4), an accepted identity reset,
or cutover per-site. **It should be decided before the adapter is written, not
after**, because it determines whether Stage 2's success criterion is "matches
fast-alpr" or "beats fast-alpr".

### 4.2 🔴 Trial-mode `*` redaction — not absorbed, fails silently

Covered in §1.3. Rule 2 strips `*`; the 5–6 char remainder clears rule 3 and
hashes cleanly. **Measured, not theorised.** Mac-only *if* licensing is the
cause — unverified (§6 Q1). Requires an explicit `"*" in raw` guard in the
adapter regardless, as defence in depth.

### 4.3 🟢 Hyphens, spaces, dots — absorbed

Observed `text` values carry no separators (`3PEDLM*`, `3020CE*`, `FBE348*`).
Even if a separator appeared, rule 2 strips it. No exposure.

### 4.4 🟢 State / jurisdiction text — absorbed, and structurally safer

ultimateALPR returns jurisdiction in a **separate** `country[]` field
(`code`, `name`, `state`), never concatenated into `text`. Confirmed across
USA/ESP/PRT samples. No contamination risk today.
Forward-looking caution: `country[].state` is tempting to fold into the identity
for disambiguation. Doing so **changes the hash input** and is a spec v2.0
change under the rule-8 "no prefix/suffix" constraint. Do not do it casually.

### 4.5 🟡 Case — absorbed, but note the engine emits lowercase in places

`text` came back uppercase; but `make`/`color` are lowercase (`"bmw"`).
Rule 1 uppercases, so plate text is safe. Only relevant if VMMR/VCR values ever
enter the hash input — they must not.

### 4.6 🟡 Stacked characters and multi-line plates — UNDETERMINED

Not exercised: no stacked-character or multi-line US plate in the SDK's sample
set, and I did not run our own footage through in trial mode (it would be
`*`-corrupted anyway). Risk: if ultimateALPR emits multi-line plates in a
different **order** than fast-alpr (or inserts a newline/space), rule 2 removes
the whitespace but **cannot fix ordering** — a reordered string is a different
hash. Rule 2 absorbs the *separator*, not the *sequence*. Must be measured in
Stage 2 on real footage with a licensed runtime.

### 4.7 🟡 Confusable characters — feeds 4.1

Different architectures make different confusions (fast-plate-ocr MobileViTV2 vs
ultimateALPR's TF 1.12.3 OCR). The `AIV1937` → `ATV193*` observation is a live
example. Rule 7 forbids normalizing these away, correctly. This is not a
separate exposure so much as the mechanism behind §4.1.

### 4.8 🟡 Length distribution — watch the minimum

Trial reads normalized to 5–6 chars; `34IN7*` → `34IN7` (5). Real US plates are
6–8. Rule 3's floor of 4 is permissive enough that a *truncated* read still
passes. With a licensed engine this concern mostly evaporates, but it is another
argument for the explicit `*` guard rather than relying on length checks.

---

## 5. Proposed shape for Stage 2 (design sketch — not implemented)

**Principle: the vote, dwell, thresholding, and result types do not move.**

The coupling is far narrower than the task brief assumed. Repo-wide, `fast_alpr`
appears in exactly **two** places:

- [`plate_reader/detector.py:286`](../plate_reader/detector.py#L286) — the import inside `open()`
- [`tools/bench_vehicle_detector.py:91`](../tools/bench_vehicle_detector.py#L91) — a bench tool

Everything else is duck-typed `getattr(r.detection, ...)` / `getattr(r.ocr, ...)`
inside `_run_one()`. **No `fast_alpr` type escapes `_run_one()`.** Confirmed by
grep across the package and `tools/`.

Engine-independent and untouched by the swap: `_DwellRecord`, `_VoteBucket`,
`_dwell`, `note_vehicle_departed()`, `dwell_debug_snapshot()`,
`DetectionResult`, `ConfirmedIdentification` (incl. `suppressed`,
`suppress_reason`, `seconds_since_first_confirm`, `repeat_index`),
`FailedIdentification`, `process_frame()`, and `_to_scalar_confidence()` — with
the §3.2 caveat that what we *feed* it changes.

Engine-dependent, i.e. the entire seam: **`open()` and the inference half of
`_run_one()`.**

### 5.1 Adapter interface

New file `plate_reader/engines.py` (proposal — not created):

```python
@dataclass(frozen=True)
class RawPlateRead:
    """One plate from one frame, engine-neutral. Deliberately NOT a
    DetectionResult: no normalization, no zone test, no thresholding —
    those stay in detector.py so the swap cannot alter them."""
    text: str
    detection_confidence: float      # 0.0-1.0, adapter normalizes scale
    ocr_confidence: float            # 0.0-1.0, min-per-char semantics
    bbox: tuple[int, int, int, int]  # axis-aligned; quads reduced by adapter
    vehicle_bbox: tuple[int, int, int, int] | None = None   # ualpr only
    vehicle_confidence: float | None = None                 # ualpr only
    jurisdiction: str | None = None                         # ualpr LPCI only


class PlateEngine(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def infer(self, frame) -> list[RawPlateRead]: ...
    @property
    def name(self) -> str: ...


class FastAlprEngine:
    """Wraps today's behaviour verbatim, including the Darwin CPU-provider
    branch. Extracted, not changed."""

class UltimateAlprEngine:
    """Global-singleton engine. Module-level guard: refuse a second open()
    while one is live (see 3.1), deInit() on close(). Raises on '*' in text
    (see 1.3/4.2) rather than letting a redacted plate reach normalize()."""


def build_engine(cfg: Config) -> PlateEngine:
    """Selected by a new config key, defaulting to fast-alpr so existing
    deployments are byte-identical until explicitly opted in."""
```

`Detector` then changes minimally:

```python
def open(self) -> None:
    self._engine = build_engine(self.cfg)
    self._engine.open()

def _run_one(self, frame) -> list[DetectionResult]:
    t0 = time.monotonic()
    reads = self._engine.infer(frame)          # only line that knows the engine
    inference_ms = int((time.monotonic() - t0) * 1000)
    # ...thresholding / normalize() / bbox_inside_zone() / DetectionResult
    #    construction all UNCHANGED below this point...
```

`close()` is new and load-bearing: fast-alpr never needed it, ultimateALPR's
`deInit()` does. `main.py`'s SIGTERM path and the harness both need to call it.

### 5.2 Notes on the Darwin branch

[detector.py:299](../plate_reader/detector.py#L299) — a factual correction to the
task brief: the CoreML workaround is **not** an unguarded local-only patch. It
is already wrapped in `if platform.system() == "Darwin"`, with a comment noting
that the *previously hardcoded* version was the thing that had to be manually
reverted. **It is safe to ship as-is today.** In the refactor it moves verbatim
into `FastAlprEngine.open()`.

It has no ultimateALPR counterpart, and the reason matters: there is no macOS
build at all (§2). `UltimateAlprEngine` can never load on host macOS — only
inside the linux/arm64 container. So the dev loop changes shape: the harness
must run *in* the container for ualpr, whereas fast-alpr runs natively on the
host. Stage 2 should add a container entrypoint for `tools/replay_harness.py`
rather than assuming both engines are reachable from one interpreter.

### 5.3 Suggested Stage 2 order

1. Settle the §4.1 identity-continuity decision. It sets the success criterion.
2. Get Doubango's answer on `*` and on `confidences` (§6) — Q1 gates all
   accuracy work; Q2 gates the confidence gate port.
3. Extract `FastAlprEngine` behind the Protocol, change nothing else, confirm
   the harness is bit-identical on `test_footage.mp4`. **Zero-risk checkpoint.**
4. Add `UltimateAlprEngine` + container harness entrypoint, LPR-only
   (`car_noplate_detect_enabled: false`) for like-for-like.
5. Dual-engine diff — **only** on a licensed runtime.
6. Separately, as its own scoped work: vehicle detection / Problem 2 / board
   trigger (§3.4).

---

## 6. Open questions for `tech@doubango.org`

1. **(Blocking accuracy work.)** In unlicensed/trial mode the final character of
   every `text` is `*` (`3PEDLM*`, `FBE348*`), and `color`/`model` are masked
   (`b*****`, `4*****`). Confirm this is a licensing restriction and that a
   valid runtime license returns full plate text. Is there any evaluation mode
   that returns unmasked text without consuming an activation?
2. **(Blocking the confidence-gate port.)** What exactly is the `confidences`
   array? We consistently measure `len(confidences) == len(text) + 2`. Which
   indices are per-character, and what are the two extras? (`confidences[1]`
   equalled `car.confidence` in our sample.) Undocumented in the distribution.
3. Does `recogn_score_type: "min"` make the returned aggregate the true
   minimum across characters — i.e. can we rely on it instead of computing
   `min()` ourselves over the array in Q2?
4. Confidence scale: config `*_minscore` keys take 0–1, results return 0–100.
   Confirm, and confirm no key takes 0–100.
5. `requestRuntimeLicenseKey()` — is it safe to call repeatedly on the *same*
   machine without consuming additional activations from our 15? What exactly
   consumes a unit: the request, or first successful `init` with the token?
6. Header says `3.10.5`; binary reports `3.13.2`. Which is authoritative, and is
   the header macro simply stale in the public repo?
7. For multi-line / stacked-character US plates, what is the character order in
   `text`, and is any separator inserted? (Determines our hash stability — §4.6.)
8. `binaries/osx/` contains only `.gitkeep`. Is a macOS/Apple Silicon build
   planned? It would remove the container from our dev loop entirely.
9. Our license is `ANPR / eLinux / AArch64`, 15 units. Does that cover
   `binaries/linux/aarch64` on a Raspberry Pi 5 (Debian aarch64), or is
   `binaries/raspbian/*` the intended artifact? The raspbian folder is armv7l
   only, which is 32-bit and wrong for Pi 5.

---

## 7. What I could not determine

- Whether a licensed runtime removes `*` masking (would require an activation).
- The `confidences` array layout (undocumented; measured shape only).
- Real-footage accuracy of ultimateALPR vs fast-alpr on `test_footage.mp4` —
  impossible to assess in trial mode, since every string is truncated.
- Pi 5 throughput. All timings here (53–93 ms/frame with VCR+VMMR+LPCI on)
  are Apple Silicon container numbers and do not transfer.
- Behaviour on stacked/multi-line US plates (§4.6).
- Whether `pyramidal_search_*` and `recogn_rectify_enabled` help or hurt on our
  drive-thru geometry — new tuning axes with no serv-pi equivalent.

---

## 8. Post-spike updates (2026-07-31)

### 8.1 Harness baseline reference figures — CORRECTED

An earlier figure of **7 confirms / 27 suppressed** circulated for
`test_footage.mp4`. **It is stale and wrong.** Reproduced twice on the
unmodified baseline commit `fb6c642`, the real numbers are:

| metric | value |
|---|---|
| frames processed | 4698 |
| **confirms** | **5** |
| **suppressed re-reads** | **19** |
| failures | 2 |
| drive-offs | 13 |
| board triggers | 81 |

Confirmed hashes (stable across all runs):
`483cef1ee19e`, `7073c4b0d503`, `9b96748828d9`, `ae8ab02833e0`, `bf6ba5349e91`.

### 8.2 The harness is NOT deterministic — treat the output as a distribution

This was established by control experiment, not assumed: the **unmodified**
baseline was run twice and diffed against itself. It differs from itself.

```
run1: BOARD_TRIGGER track=1   plate=CFZ1E72    run2: plate=4141
run1: BOARD_TRIGGER track=106 plate=LSE2E58    run2: plate=L3E245
run1: f00061 FAIL low_confidence               run2: f00060 FAIL low_confidence
```

Cause: floating-point nondeterminism in multithreaded ONNX inference. Strong
reads are stable; borderline sub-threshold reads flip between runs.

**What is stable vs what is not:**

- **Stable — treat any movement as a hard failure:** confirmed hashes, and the
  counts of confirms / suppressions / failures / drive-offs / board triggers.
- **Not stable — expected to vary run to run:** `plate=` candidates on trigger
  lines (sub-threshold, never hashed), FAIL-line frame indices, stdout/stderr
  interleave order.

Wall-clock speed does **not** move the stable set: runs at 2792.8s and 1194.0s
(a 2.3× spread caused by CPU contention) produced identical confirms, hashes
and counts. This matters because `Detector.process_frame()` uses wall-clock
`time.time()` for vote windows and dwell, while the harness feeds the tracker
synthetic video time (`frame_idx / fps`) — a real coupling, but empirically not
one that perturbs confirmed identities at these speeds.

**Never compare harness runs with a plain `diff`.** Use
[`tools/diff_harness_runs.py`](../tools/diff_harness_runs.py), which encodes
exactly this normalization and fails hard on any hash or count movement.

### 8.3 Doubango answered the open questions from §6

Corrections to what this document originally recorded as unknown:

**Q2 — `confidences` layout is DOCUMENTED, not unknown (§3.2 superseded).**

```
[ OCR score, plate detection score, char[0] score, ..., char[N] score ]
```

= `2 + N`, matching the measured `len(confidences) == len(text) + 2`.
`confidences[0]` is the aggregate OCR score, `confidences[1]` is the **plate
detection score**, `confidences[2:]` are per-character. The spike's note that
`confidences[1]` equalled `car.confidence` was a coincidence of one clean
detection, not evidence of meaning.

**Q3 — `recogn_score_type` defaults to `median`, NOT `min` (§3.3 superseded).**
The spike misread the C++ sample's explicit override as the SDK default. It
must be set to `"min"` explicitly. With that set, `confidences[0]` is the
minimum across per-character scores — exactly the semantics of our
`_to_scalar_confidence()`. So `confidences[0] / 100` is used directly as
`ocr_confidence`; we do not recompute it. `UltimateAlprEngine` asserts
`confidences[0] ≈ min(confidences[2:])` on the first read, turning the vendor's
documentation claim into a verified property of the actual binary.

**Q6 — the runtime banner is authoritative.** Binary reports `3.13.2`; the
header macro `3.10.5` is stale. `3.13.2` satisfies the ≥3.11.2 requirement.
Gate on the runtime value, never the header.

**Q7 — read order is left→right, top→bottom, no separator inserted.**
Partially resolves §4.6. **Still open:** whether that ordering derives from
pre- or post-rectification geometry. If post, toggling
`recogn_rectify_enabled` could reorder characters on the same physical plate
and therefore change its hash. Until answered, that flag is pinned `false` and
is a **hash-stability setting, not a tuning knob**.

**Q8 / Q9 — target confirmed, and the container is permanent.**
`binaries/linux/aarch64` is correct for Pi 5 on 64-bit Raspberry Pi OS.
`binaries/raspbian/` is 32-bit only and wrong for Pi 5. **No macOS build is
planned**, so the container in `docker/` is permanent infrastructure for the
Mac dev loop, not a temporary workaround.

**Q1 — still open, and still the blocker for accuracy work.** Whether a
licensed runtime removes the `*` redaction is being treated as a *working
assumption*, not a verified fact. It gets verified on the Pi after activation.
Until then every ualpr read in trial mode is expected to trip the `*` guard,
and that is the correct behaviour.

### 8.4 Identity-continuity risk (§4.1) is CLOSED

Production DynamoDB will be wiped to zero before pilot. There are no legacy
vehicle hashes to preserve, so the cross-engine hash discontinuity described in
§4.1 has no victims. Stage 2's success criterion is **"beats fast-alpr"**, not
"matches fast-alpr". No dual-hash or migration-compatibility machinery is being
built. `NORMALIZATION_SPEC.md` stays locked at v1.0, plate text only.

§4.2 (the `*` redaction) is **not** closed by this — a redacted read
manufactures a wrong identity going *forward*, which zero legacy hashes does
nothing to help.

### 8.5 Licensed bench verification on `servCamOG` (2026-08-01)

Measured on the licensed Pi 5 — **not** the Mac container. Debian 13 (trixie),
aarch64, 4 cores, SDK **v3.13.2**, CPU/TensorFlow backend, OpenVINO and NPU
both off. These supersede the Mac-side numbers everywhere they disagree.

| Item | Status | Detail |
|---|---|---|
| Trial `*` masking removed by license | **RESOLVED** | `text: "3PEDLM4"`, no trial banner |
| `confidences[0] == min(confidences[2:])` | **RESOLVED** | verified with `recogn_score_type: "min"` |
| `recogn_score_type` default is `median` | **RESOLVED** | must be set explicitly; we do |
| License scope covers `linux/aarch64` on Pi 5 | **RESOLVED** | Debian 13 aarch64, works |
| Throughput fits the 5 fps budget | **RESOLVED** | see below |
| Mixed-layout / stacked-plate read order | **OPEN** | awaiting Doubango; keeps rectify off |

**Chosen production configuration: plates + car box + VCR (colour).**

| config | ms/frame | fps | verdict |
|---|---|---|---|
| plates + car box + VCR | **106** | ~9.4 | **chosen** — 1.9× headroom on the 200 ms budget |
| full LPCI + VMMR + VBSR stack | ~270 | 3.7 | rejected — does not fit 5 fps |

Two consequences worth stating plainly:

- **Problem 2 is covered for free.** The vehicle box ships with base detection;
  it needs no extra classifier and costs nothing beyond the 106 ms above. This
  is the deferred vehicle-only-detection capability, unblocked.
- **§1.3's core hazard is retired *on licensed units only*.** A licensed
  runtime returns unmasked text, so the redaction cannot silently manufacture
  identities there. The `*` guard stays in the code regardless — it is the
  thing that detects a token failing to load, which would otherwise degrade
  silently to trial mode and start writing wrong hashes.

The 106 ms figure is a **static-sample bench**, not the pipeline. The honest
production number must come from real footage with capture, dwell and vote
sharing the same 4 cores — measured in Stage 2b, see
[STAGE2B_PIPELINE_BRINGUP.md](STAGE2B_PIPELINE_BRINGUP.md).

`recogn_rectify_enabled` remains **false**. Doubango confirmed read order is
left→right, top→bottom, but whether that ordering derives from pre- or
post-rectification geometry is still unanswered. Until it is, this is a
**hash-stability flag, not a tuning knob** — flipping it could reorder
characters on the same physical plate and change its SHA-256.

---

## 9. Compliance with the task rules

- Trial mode only. **No runtime key generated**, no `--tokendata`, no
  `--tokenfile`. `requestRuntimeLicenseKey` was probed for reachability
  (`hasattr`) and never called.
- **Nothing under `plate_reader/` was modified.** Read-only inspection.
  All proposals live in this document.
- `config.json` was never opened, printed, copied, or committed.
- `tools/replay_harness.py` was not run in any mode.
- SDK cloned to `~/Desktop/alpr-spike/`, outside the serv-pi tree. The only file
  written inside the repo is this document.
- All terminal output quoted here is real and observed; nothing is reconstructed.
