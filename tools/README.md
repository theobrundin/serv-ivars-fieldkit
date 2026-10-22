# tools/ — offline validation

## replay_harness.py

Validates the presence / drive-off / color-split logic against known ground
truth. Runs the **real** `PresenceTracker` and (in video mode) the **real**
`Detector` vote logic — so a scenario that passes here exercises the same code
that runs in the field.

### Synthetic mode (runs anywhere — laptop, CI, no camera, no fast-alpr)

```
python tools/replay_harness.py synthetic
```

Runs the scripted scenario suite and asserts expected outcomes:
clean-identified (no drive-off), dirty-plate (one drive-off), empty lane,
brief noise (filtered), nose-to-tail color split (two drive-offs), same-color
(no false split), and the red hue-wraparound edge case. Exit code 0 = all pass.
Use this as a regression gate before every deploy.

### Video mode (bench machine or Pi with fast-alpr + opencv installed)

```
python tools/replay_harness.py video --config config.json \
    --video lane_dinner_rush.mp4 --fps 5
```

Replays real lane footage frame-by-frame and prints every CONFIRM / FAIL /
DRIVEOFF with a frame index. It ends with an estimated total-vehicle count
(confirms + drive-offs).

**Ground-truth workflow:** record ~1 hour of a real lane, manually count the
actual cars, then compare to the harness estimate:
- estimate **over** true count → phantom drive-offs; tighten `motion_area_fraction`
  / `min_track_frames`.
- estimate **under** true count → still missing cars; loosen thresholds or
  evaluate adding a small vehicle-detection model.

Precision (are the drive-offs real?) and recall (did we catch every car?) are
what decide whether drive-off counts are trustworthy enough to promote to a
live KPI. Do this on one shadow unit before turning drive-offs into a metric.
