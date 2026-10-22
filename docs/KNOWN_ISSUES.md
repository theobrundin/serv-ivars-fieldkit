# Known Issues (deferred, with fixes recorded)

Small, understood defects that are deliberately NOT being fixed right now
because they sit outside the scope of the work in flight. Each entry records
the fix so picking it up later is mechanical.

---

## 1. `tools/test_dwell_e2e.py` fails on import of the vote path

**Status:** pre-existing. Fails on `fb6c642` (the pre-migration baseline) and
on every commit since. **Not** introduced by the ultimateALPR migration.

**Symptom:**

```
File "plate_reader/detector.py", in process_frame
    len(bucket.candidates) >= self.cfg.thresholds.vote_min_frames
AttributeError: 'T' object has no attribute 'vote_min_frames'
```

**Cause:** the test defines its own stub threshold class `T` rather than using
`plate_reader.config.Thresholds`. When `vote_min_frames` was added to the real
`Thresholds` (default 2), the stub was not updated, so `process_frame()` hits
an attribute that the stub never grew.

**Fix (one line).** In the `T` dataclass in `tools/test_dwell_e2e.py`, add:

```python
vote_min_frames: int = 2
```

next to the other threshold defaults. Match the production default in
`plate_reader/config.py::Thresholds`.

**Why it is deferred:** the Stage 2a adapter refactor was verified by proving
its behaviour is identical to the baseline. Fixing a test mid-refactor would
have changed what "identical" meant and destroyed that argument. It stays
broken-and-known until the migration work is finished, and must not be fixed
as an incidental side effect of editing nearby code.

**Watch out:** this stub-drift can recur. Any future field added to
`Thresholds` without a default will break every stub-config test the same way.
Using `plate_reader.config.Thresholds` directly in tests would remove the whole
class of problem, but that is a larger change than this entry covers.
