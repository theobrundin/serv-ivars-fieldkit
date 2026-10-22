# Vehicle-Centric Record Architecture

The vehicle is the unit of processing, not the plate. Every car that passes
through produces **one record**. The plate hash is a *field* on that record —
the identity when present — and the record exists whether or not that field
is ever filled. Colour, make and model are further optional fields, populated
when their classifiers are enabled and the model produces output, null
otherwise.

This is a deliberate inversion. The vehicle is what is reliably in frame; the
plate often is not, through occlusion, camera angle, missing front plates, or
reads that never clear confidence. Structuring around the reliable
observation and treating the unreliable ones as optional fields is the
correct dependency direction.

---

## Quick start

### On a Mac (no SDK, no hardware)

The ultimateALPR SDK has **no macOS build**, so live inference cannot run
here. Instead, replay engine output captured from real hardware — this drives
the *entire* downstream pipeline (voting, dedup, tracking, records) with
genuine model output. Nothing is faked; only the model itself is absent.

```bash
python3 vehicle_records/selftest.py            # 59 assertions, no deps

python3 vehicle_records/run.py \
    --replay replay_clip.jsonl \
    --config config.example.json \
    --vr-config vehicle_records/config.example.json \
    --out records.jsonl
```

### On a CM5 / Pi (the performance test)

```bash
export LD_LIBRARY_PATH=/path/to/ultimateALPR-SDK/binaries/linux/aarch64
export PYTHONPATH=/path/to/ultimateALPR-SDK/python:$LD_LIBRARY_PATH

# 10-minute sustained benchmark, field-realistic pacing
python3 vehicle_records/bench.py \
    --video clip_5fps.mp4 \
    --config /path/to/config.json \
    --duration 600 --target-fps 5 --pace
```

Drop `--pace` to run flat out and find the board's ceiling instead of
confirming it holds the target rate.

---

## Note for the hardware contractor

**This benchmarks the real production pipeline, not a simplified model of
it.** `vehicle_records/` imports the shipping plate logic directly rather
than reimplementing it:

| Reused, unchanged | What it does |
|---|---|
| `plate_reader.detector.Detector` | exact-match group voting, confidence floors, dwell suppression, per-hash cooldown, near-match dedup |
| `plate_reader.normalizer.normalize` | NORMALIZATION_SPEC v1.0 |
| `plate_reader.hasher.hash_plate` | SHA-256, no salt |
| `plate_reader.engines.UltimateAlprEngine` | SDK adapter, licence handling, confidence scaling |

A vendored copy would have been easier to hand over but could drift from what
actually runs in the field, and a benchmark of stale code is worse than no
benchmark. Ship the whole repo.

### What the numbers mean

Production captures at **5 fps**, so the per-frame budget is **200 ms**.
Reference measurements on a Pi 5:

- plates + vehicle box + colour: **~106 ms/frame** (fits)
- adding LPCI + VMMR + VBSR: **~268 ms/frame** (does **not** fit)

`bench.py` reports p50/p95/max per frame, splits engine time from
record/tracking overhead, and tracks temperature and the sticky throttle
bits. **A good average with a throttled tail is a failing result** — on a Pi 5
with no active cooling we measured throttle onset at ~4 minutes under
sustained load, so runs shorter than several minutes prove nothing.

Record/tracking overhead is expected to be well under 1 ms/frame. If the
total is over budget, the cause is almost always the model configuration
(which classifiers are on), not this layer.

---

## The record

```json
{
  "record_id": "veh-a1b2c3d4-000001",
  "vehicle_detected": true,
  "plate_hash": "979ae4c32c82...",
  "plate_status": "confirmed",
  "color": "blue",
  "make": null,
  "model": null,
  "first_seen": "2026-08-04T16:15:05.000Z",
  "sent_at": "2026-08-04T16:15:11.000Z",
  "diagnostics": { "...": "not part of the cloud contract" }
}
```

`record_id` is a boot/session-scoped correlation id. It is **not** a customer
identity, means nothing across visits, and is safe to log.

### `plate_status`: three outcomes, not two

| Value | Meaning |
|---|---|
| `confirmed` | a plate was read and cleared the vote and confidence gates |
| `no_plate_visible` | no plate was ever detected on the vehicle at all |
| `low_confidence` | a plate was detected and read, but never cleared the gates |

The distinction between the last two is **load-bearing** and is preserved all
the way to the cloud. They describe different failures: `no_plate_visible` is
a fact about the physical world (occluded, angled away, absent);
`low_confidence` is a fact about the model (it saw a plate and could not read
it well enough). Conflating them makes a low identification rate
undiagnosable — there would be no way to tell whether cars are hiding their
plates (a mounting or geometry problem) or the model is failing on plates it
can plainly see (a threshold or model problem). Those have opposite fixes.

### Field reconciliation: highest confidence wins

A car dwells for many frames and a classifier can answer differently on each —
silver on one frame, gray on the next. Every **descriptive** field keeps the
highest-confidence reading seen so far and replaces it only when a
higher-confidence one arrives. Colour, make and model reconcile independently.

This rule deliberately does **not** apply to the plate. A single
high-confidence misread would become the canonical identity for the vehicle,
which is exactly what multi-frame exact-match voting exists to prevent. The
plate earns its value through agreement across frames; description does not
need that rigor because it is never the identity.

### The plate invariant

In the previous design a plateless vehicle had *no field to hash into*, so
writing one as a customer identity was unrepresentable rather than merely
checked for. Collapsing to a nullable `plate_hash` gives that up unless it is
rebuilt deliberately, and a wrong identity is the most expensive failure this
system has.

It is rebuilt as an enforced invariant: `plate_hash` is settable **only**
through `confirm_plate()`, which requires a 64-char digest and sets the status
in the same operation, and `validate()` re-checks it at the commit point.
There is no path that produces a record carrying a hash without `confirmed`
status. See the first block of `selftest.py`.

---

## When the record is sent, and the lock

Configured by `send_trigger`:

| Trigger | Commits when |
|---|---|
| `instant` | first frame the vehicle is detected |
| `zone_entry` | vehicle enters the dwell zone |
| **`zone_exit`** | vehicle leaves the dwell zone — **default** |
| `frame_exit` | vehicle leaves frame entirely |

Two rules:

1. **Until sent, fields keep updating.** A plate can confirm after colour was
   observed; a higher-confidence make can replace an earlier one.
2. **Once sent, the record is frozen.** The send is the commit point and it is
   final. Any mutation afterwards raises `RecordFrozenError`.

`zone_exit` is the default because of the interaction with plate
confirmation. A plate does not confirm until several agreeing frames have
accumulated. A trigger that fires earlier commits the record before the plate
can resolve, and the car is sent as plateless when it was about to be
identified a moment later — a self-inflicted miss that looks exactly like a
model failure in the metrics.

> **If `presence.dwell_zone` is disabled** (the current production state on
> `serv-ljs-001`), there is no zone to enter or leave, so the zone triggers
> can never fire and records commit on track timeout instead. `run.py` prints
> a note when this applies. The fallback is safe — later still means more time
> for the plate to confirm — but it should not be mistaken for the zone
> trigger working.

---

## Files

| File | Purpose |
|---|---|
| `record.py` | `VehicleRecord`, `PlateStatus`, highest-confidence reconciliation, freeze |
| `tracker.py` | vehicle-keyed tracking, plate→vehicle attribution, send triggers |
| `pipeline.py` | wires the production `Detector` into records; per-stage timing |
| `engine.py` | rich SDK adapter (colour/make/model) + replay engine |
| `capture.py` | record real engine output into a replay dataset (aarch64) |
| `run.py` | CLI → `records.jsonl` |
| `bench.py` | CM5/Pi performance + thermal benchmark |
| `selftest.py` | 59 assertions, no SDK/video/network needed |

### Why vehicle-keyed tracking

The production presence tracker associates on the **plate** box, which is
small, often motion-blurred, and vanishes entirely when a plate is occluded.
On measured footage its association broke on 64.6% of consecutive box pairs,
so single cars fragmented into several tracks and the vehicle count read 40
against ~30 real cars. The SDK's **car** box is a far steadier target: large,
present on every frame the vehicle is visible, and it does not disappear when
the plate does.

`run.py` reports how plates were attributed to vehicles:

```
plate->vehicle attach : 18 by string, 2 by recency, 0 failed
```

"by recency" is a fallback *guess*. A high count there means attribution is
unreliable and association needs work — it is counted rather than hidden.

---

## What does not change

Normalization and hashing are untouched. The plate, when present, flows
through the same NORMALIZATION_SPEC v1.0 path and the same hashing as today.
The vote logic is unchanged and remains exact-match. Dedup and near-match
suppression are unchanged. This architecture promotes the vehicle to the
primary record and demotes the plate to a field on it; it does not alter how
the plate itself is read, normalized, matched, or hashed.

## Deferred, by decision

- **The endpoint and API contract.** The intended target is one unified
  vehicle event to a single endpoint, with `plate_status` telling the cloud
  what kind of event it is, replacing the current `/visit` + `/detection-event`
  split. The record shape is compatible with either and forces nothing now.
- **Order matching for plateless cars.** A plate-identified car is keyed by
  its hash. A plateless one has no such key — only timing (it was at the
  speaker at a given moment) and possibly colour. Timing works only if cars
  are matched in strict sequence, and colour alone is far too weak, since
  "silver sedan" describes thousands of vehicles. This is a cloud-side
  problem, but the weakness of the available signal is real and whatever is
  designed later has to account for it.
