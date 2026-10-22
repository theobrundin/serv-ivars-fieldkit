# LJS left-edge zone expansion backtest — 2026-08-26

## Decision under test

Two normalized x-minimums move to the left frame edge while every other
boundary remains fixed:

| Gate | Before | Proposed | Unchanged bounds |
|---|---:|---:|---|
| plate identity eligibility | 0.15 | 0.00 | x max 1.00; y 0.00–0.75 |
| board / menu area | 0.12 | 0.00 | x max 0.72; y 0.00–0.25 |

The board change is part of route qualification and ships shadow-only. The
identity change is independent: if copied to the live config, plate centroids
in the added strip can enter normal voting immediately even while route
qualification is shadow-only.

## Evidence processed

The audit hashed every image before analysis so copied folders could not
inflate the result.

- 1,609 relevant LJS image copies across the current Pi pull and historical
  LJS diagnostic folders.
- 1,231 full-resolution 1456×1088 copies.
- 1,069 unique full-resolution frames after SHA-256 deduplication.
- 607 unique plate boxes with usable full-frame coordinates.
- 601 unique vehicle boxes with usable full-frame coordinates.
- 358 images copied from the Pi's current audit directories: 169 full-frame
  copies (168 unique) and 189 retained legacy plate crops. Their capture dates
  span current and historical material. Crops were excluded from spatial
  membership tests.
- All 168 unique current-Pi full frames were visually reviewed with the old
  and proposed left boundaries overlaid.

The corpus spans more than one camera position. Current-Pi frames were
reviewed separately; older positions are sensitivity/adversarial evidence and
must not alone choose a live boundary.

The source roots were the Pi's `/home/serv/plate_reader/audit_cases` and
`/home/serv/plate_reader/miss_plates` directories plus these Desktop folders:
`LJS_2026-08-26_63_Vehicle_Review`,
`LJS_All_Plate_Audit_Photos_2026-08-26`, `ljs-presence-frames`,
`ljs-episode-frames-0815`, `ljs-stream-photos`,
`ljs-misses-2026-08-12`, and `ljs-misses-2026-08-13`.

Method: recursively enumerate images, record dimensions and SHA-256, attach
available JSON sidecars, deduplicate full frames by image hash, and compare
stored plate/vehicle bounding-box centroids against the before and proposed
normalized rectangles. Pre-existing crops were excluded from geometry.
Contact sheets containing every unique current-Pi full frame were then
reviewed visually. The working manifest and contact sheets contained raw site
evidence and therefore were deliberately kept out of Git.

## Paired result

### Plate identity eligibility: x 0.15 → 0.00

- 606 / 607 plate-box centroids were already inside the old x boundary.
- 1 / 607 was newly inside the proposed left strip.
- That one case was not a vehicle plate. It was a wall light in the old camera
  view, detected at 0.86 plate confidence and 0.35 OCR confidence. It remained
  below the OCR gate and would not confirm under the current thresholds.
- The current Pi full-frame corpus had zero plate centroids in the added
  strip; its minimum stored plate-centroid x was 0.17651.
- Visual review of all 168 unique current-Pi full frames found no readable
  plate in the added strip. The strip is mainly the blue post, rocks, curb or
  building edge in the current view.

This does **not** prove that the expansion has no recall value. Reads rejected
by `detection_zone` are discarded before the retained miss-image path, so the
stored corpus is selected against the exact candidates being sought.

### Board / menu area: x 0.12 → 0.00

- 547 / 601 stored vehicle-box centres were inside the old board rectangle.
- 0 / 601 were newly inside the added left board strip.
- The minimum stored vehicle-centre x was 0.25103.
- Visual review of all 168 unique current-Pi full frames found no vehicle whose
  centre was clearly in the added board strip.

The still-image result cannot prove route behavior. A terminal audit frame
does not show entry-before-board order, direction, later board arrival,
drive-off status, dwell association, duplicate suppression or trigger timing.

## Interpretation and rollout

No retained static case corresponding to a valid lane vehicle changed from
outside to inside under either wider boundary. One retained historical
nuisance candidate changed identity-zone membership. That is enough to require
measurement, but not enough to reject the operator's full-left geometry:
retained failures are heavily selected and the camera view has changed.

Therefore:

1. Keep the proposed board x-minimum at 0.00 and deploy it in genuine shadow
   mode. It cannot change Visits while shadowed.
2. Keep the proposed identity x-minimum at 0.00 in the release configuration,
   but treat its live copy as a monitored recall/precision change, not as a
   prerequisite for route tracking. Stream vehicle observations already use
   the full frame.
3. Enable Stream dwell/trajectory and collect real shadow tracks. Compare the
   old and new board boundaries on the same physical tracks.
4. Do not make route qualification authoritative until true-lane,
   upper-right-drive-off and background tracks have been manually labelled and
   the route confusion matrix is acceptable.
5. Measure identity availability relative to actual menu-board arrival. A
   completed dwell trajectory may be accurate but too late for voice context.

The audit scripts created no new crops and did not send images to AWS, Plate
Recognizer or Git. The visual review used local contact sheets through Codex.
Images were not redacted, and raw plate text was not included in this report.

## Later same-day operational decision

The numbered recommendation above records the state at the end of the static
backtest; it was not the final 26 Aug rollout decision. The running unit was
subsequently checked rather than inferred from the dashboard. Its bind-mounted
Stream configuration had `sample = 1`, `dwell_time = true`, and
`trajectory = true`. Between approximately 17:40 and 19:55 EDT, the unit
logged 75 completed shadow trajectories: 20 `entry_then_board_trajectory`,
43 `never_entered_drive_through`, and 12 `left_before_board_trigger`. Those
figures prove that the real trajectory hook and all three route branches were
active; by themselves they are not a ground-truth confusion matrix.

After reviewing the day's vehicle images and the known lane-versus-upper-right
examples, the operator explicitly authorized a guarded authoritative rollout.
The release therefore promotes the checked-in example, while the live unit
remains shadow-only until the separately approved code and config migration.
The promotion is coupled to lifecycle protections that did not exist during
the static backtest: native-trajectory-only non-overlap reconciliation,
per-sample timestamp ownership, rejection grace, one-webhook ownership,
late-identity resolution before anonymous finalization, and bounded departure
tombstones. A provider-associated plate inside the broad board rectangle stays
provisional until its completed trajectory resolves; this is required because
a plated vehicle can touch that rectangle and then exit through the calibrated
upper-right terminal band.

This is an operator-approved site decision, not a claim that 75 unlabeled
tracks establish a statistical error rate. The first authoritative window must
still be manually reviewed. Roll back to `shadow_only = true` if qualified
background/upper-right traffic appears, if a physical car produces multiple
Visits, or if a true lane car is suppressed.
