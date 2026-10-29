# Ivar's 001 route calibration candidate — 28 October 2026

Status: **active on Ivar's 001 as of 28 October 2026, 19:21:55 PDT**, release
`dc72c9dd0ffaf076802e9fa757389a9c619442aa`. The user instructed activation after
the known missed-arrival result and approval requirement were explained. The
earlier production acceptance failure remains unresolved; activation is not a
claim of validated counting accuracy. The first 120 cases informed calibration.
The validation sections below describe evidence collected before deployment.

The live Pi was checked at 20:57 UTC. Capture, PR, application, cellular and
Tailscale were healthy; pending and dead-letter queues were empty. Route
qualification was still in shadow mode, with inherited LJS geometry. Its
board zone occupies the top quarter of this camera view, including sky.
Simply making that policy authoritative would reject real customers.

Sixty retained audit cases and their original photos were exported with a
strict metadata allowlist. No raw OCR text, identity hashes, API keys or
camera credentials were exported. The camera's printed clock is wrong;
review captions use provider/Pi timestamps. Audit firstSeenAt/lastSeenAt can
describe recognition fragments; replay reconstructs native bounds from the
trajectory sample timestamps. Saved photos are individual frames, not video.

## Candidate rule

`presence.route_qualification.policy = "ivars_window_v1"` opts into the new
policy. Default `entry_board` retains the existing LJS algorithm. Config and
engine reject other site/device identities, dimensions and zone coordinates.
These are versioned values for this camera pose, not fleet defaults:

| Property | Value |
|---|---|
| Frame | 2304 × 1296 |
| Anchor | vehicle box bottom-center |
| Approach rectangle | x 0.40–0.95, y 0.55–0.74 |
| Window rectangle (`presence.board_zone`) | x 0.05–0.39, y 0.65–1.00 |
| Approach evidence | 2 consecutive samples; width ≥0.18 frame |
| Window evidence | 3 consecutive samples spanning ≥2 seconds; width ≥0.30 and visible height ≥0.48 |
| Route evidence | ≥8 unique samples; ≥0.08 left and ≥0.10 down between entry and window |
| Continuity | strictly increasing timestamps; gap ≤3 seconds; consecutive boxes overlap |

The policy deliberately does not reuse LJS upward-motion gates or its sparse
track rescues. It never counts an unproven route because OCR confidence is
high. Missing arrival after restart, ambiguous tracks, and incomplete evidence
can undercount; these are explicit rollout checks, not silently accepted cars.
Near-window occupancy proves a route, not a purchase. A nearby parking maneuver
could still resemble this route, so manual review is necessary.

Duplicate-time boxes are collapsed only if identical, or one continuation has
union IoU ≥0.50 with the prior sample and exceeds the runner-up by ≥0.30.
Geometry only decides this; no plate, make, model, colour or zone preference.
Unresolved ambiguity blocks association as well as counting. The cleaned track
is used for profile reconciliation and stored route evidence. Existing
encounter lifecycle/terminal ownership prevents a second visit on a departure;
qualification of the original encounter remains monotonic.

## Replay and human review

Run with Python 3.11+ against the private, allowlisted audit bundle:

```sh
python3 tools/ivars_route_review.py /path/to/index.json /path/to/replay.json \
  --html /path/to/Ivars-Trajectory-Review.html
python3 tools/test_ivars_route.py
```

For the 60-case 28 October bundle, candidate classifications are:
4 approach/window paths, 22 without approach proof, 4 without window dwell,
13 ambiguous, 5 discontinuous, and 12 with insufficient samples (including
8 without a retained trajectory). These are **not** accuracy or customer-count
claims. Cases are individual audit profiles/fragments, not necessarily unique
vehicles. The rule was developed on this set; validate on new held-out traffic.

Agent review distinguishes the actual target from other vehicles in each
photo. T55 and T56 target background cars in opposite directions; the blue
SUV visible close to the camera is a different object. The HTML now outlines
the saved detection target separately from the moving trajectory sample.
Review-only fragment links require mutual unique endpoints, a 0–2 second gap,
union IoU ≥0.70, area ratio 0.70–1.40 and bottom-anchor displacement ≤0.04.
They establish T13→T15 (0.203s / 0.933 IoU), T28→T29 (1.667s / 0.976), and
T54→T57 (1.603s / 0.952). They do not merge cloud identities or authorize visits.

A second 60-case batch has no overlap with the calibration set. Its tall pickup
H07 exposed a real bottom-edge bias: foreground truck boxes can end above
0.78 even at the window. The revised window uses bottom ≥0.65, x ≤0.39, and
visible height ≥0.48 as well as width ≥0.30; approach must precede it with
≥0.10 down and ≥0.08 left progress. H07 now qualifies, while the towed trailer
H10 does not independently qualify. Background white car H06 remains excluded.
The revised candidate qualifies 4 first-batch and 3 second-batch paths. Because
the second batch informed this adjustment, it is no longer an untouched test
set. Tests also reverse an actual trajectory and flatten its box heights;
neither opposite-direction movement nor broad distant boxes qualify.

H49 (sedan) and H53 (Jeep) have arrival photos but zero retained trajectory
samples. H56 is the Jeep's window/departure track without an approach. Do not
fabricate a connection across the missing geometry. Existing app logs show
completed tracks ignored for no_unique_candidate during this period, but logs
do not retain boxes proving which ignored track belongs to these arrivals.
A local, bounded passive webhook diagnostic exports only allowlisted boxes,
timestamps and original JPEGs; raw OCR remains in RAM on the Pi. No application
restart or webhook destination change is needed. Missing evidence and current
association failures must be resolved before declaring counting reliable.

The candidate now retains every processed native trajectory before association
can discard it, under `diagnostics.miss_plate_dir/route-tracks` when the Ivar's
policy and unidentified-frame diagnostics are enabled. Retention is 200 records
and 24 hours, at most 2048 samples and 512 KiB per record, with explicit
truncation. The allowlist excludes raw plate text, identity hashes, appearance
and JPEGs. Files are atomic and mode 0600. Matching uses the complete in-memory
track; truncated audit evidence cannot qualify anything. Disk failure is logged
without interrupting the pipeline. Native geometric records can now explain a
missing association independently from the rolling 60-photo audit buffer.

The first passive inspection lost six TCP request segments; those incomplete
requests were discarded, not treated as absent detections. After increasing the
receive buffer/read size, the second 120-second inspection captured seven
complete requests (four dwell hooks) with zero observed transport gaps. This
quiet interval did not include a new near-window customer, so it cannot prove
the cause of H49/H53's missing associations or validate customer recall.

The updated desktop file includes automatic assessments, fragment links, target
boxes, direction and optional corrections for all 120 records. It does not
require the user to classify every case. Overlays remain static-photo evidence,
not synchronized video. No full-frame images or private audit exports enter Git.

Geometry-only fixtures in `tools/fixtures/ivars_20261028_geometry.json` retain
engineering regression expectations. Images and live metadata stay private,
outside Git. No OCR threshold or static detector mask is changed. Full-frame
native tracking preserves approach evidence; route authority controls whether
that evidence can become a Visit.

## Rollout requirements

### Fresh validation, 28 October evening: HOLD

A third batch captured at 01:24 UTC on 29 October contains 60 audit fragments
with no overlap with either earlier batch. The unchanged candidate qualifies
six tracks, rejects 28 without approach proof and eight without window dwell,
and holds out 12 ambiguous, four insufficient and two discontinuous tracks.
This is not a 60-car ground-truth denominator. Eight selected original photos
were recovered and inspected; the earlier full-photo export timed out over
cellular, and two requested images had already left the rolling audit store.

Validation case V38 (`20261029T010752_route_unresolved_v0.94_26f8d623.json`)
shows a silver hatchback approaching the lane. Its 116-point native track
continues into sustained window occupancy. A 3.669-second gap and disjoint
boxes in its earlier background portion cause the complete record to fail
`trajectory_discontinuous`. A diagnostic replay of the continuous suffix from
sample index 13 qualifies. This demonstrates an overly broad rejection, not
permission to concatenate tracks or choose boxes using the desired outcome.
A future fix must isolate continuous evidence and preserve unique physical
ownership before using a segment for counting or profile reconciliation.
The diagnostic suffix experiment did not change the candidate or live Pi.

V31 is real opposite-direction travel and is correctly excluded. V50 and V52
are different customers, not duplicates. V60's photograph is an early approach
frame; its later trajectory reaches the window, so the low-position photo box
alone must not label it background traffic. V05 is a departure continuation of
the earlier qualifying minivan track. Earlier missing approach associations
H49/H53 remain unresolved. Twilight samples do not validate night accuracy.

Re-run checks: stream 382 passed, architecture 79 passed, AWS 39 passed;
pipeline 106 passed with the same four known baseline failures. No code,
configuration, approval marker or service changes were made on the Pi.
The desktop `Ivars Zone Validation — Oct 28.html` and
`Ivars Zone Validation Results.txt` preserve the findings for the next review.

Read-only deployment inspection also confirmed that this adopted Pi has no
`/usr/local/sbin/serv-update` or `.update_channel` at the documented locations.
The fleet updater targets `/opt/serv` and `plate-reader.service`, whereas this
unit uses `/opt/serv-reolink` and `serv-reolink-app.service`. Do not install or
run that updater unchanged. A reviewed site-specific deployment path and the
human exact-release approval remain prerequisites, after acceptance is fixed.

### Required before deployment

1. Verify the current camera pose against the inspected evidence and validate representative real customers,
   background crossings, parking maneuvers, departure fragments and close
   following cars. Collect independent traffic and night examples. Daytime
   stills do not validate night operation or unobserved cars.
2. Stage this versioned policy in **shadow** using the candidate patch below,
   preserving other config and identity/threshold values. Do not install an
   entire example config. Review new raw/provider ownership, qualification,
   duplicate suppression and missed-customer rates before authority.
3. Prepare an exact reviewed release and rollback before asking for human
   approval. The repository deployment gate requires the human to run
   `sudo /usr/local/sbin/serv-update --approve <full-40-char-sha>`; the commit
   must be fetched and equal the configured channel head. Do not self-approve
   or bypass this with copying code into the live installation. Verify the
   Ivar's installation/channel actually supports this app update; the
   fieldkit-only operator updater does not deploy `plate_reader` code.
4. Switch only with an empty lane or closed hours. Restart loses in-memory
   route/identity context; a car already at the window cannot prove arrival.
   Never replay old shadow Visits as new qualified customers. Record a clean
   activation timestamp so analytics can distinguish provisional historical
   traffic from future route-qualified traffic.
5. After activation verify camera/PR, fresh owned trajectories, one terminal
   Visit per customer, AWS receipt, queue health and representative exclusions.
   Preserve backup config and exact previous release for rollback.

Candidate **partial patch**, not a complete config or activation command:

```json
{
  "presence": {
    "board_zone": {"x_min_pct":0.05,"y_min_pct":0.65,"x_max_pct":0.39,"y_max_pct":1.0},
    "route_qualification": {
      "enabled":true,
      "policy":"ivars_window_v1",
      "shadow_only":true,
      "profile_reconciliation_enabled":true,
      "terminal_exit_zone":null,
      "entry_zone":{"x_min_pct":0.40,"y_min_pct":0.55,"x_max_pct":0.95,"y_max_pct":0.74}
    }
  }
}
```

Legacy LJS progress/rescue parameters remain inactive under this policy. Keep
the existing route lifecycle timeouts. Do not change camera_position as part
of this patch: that setting has other detector semantics requiring separate
review. The candidate does not rewrite existing AWS counts/customer records.
