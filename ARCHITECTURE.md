# Architecture

How this system actually works, as of the Plate Recognizer Stream cutover.

**This file is machine-checked.** `tools/test_architecture_doc.py` asserts its
load-bearing claims against the code and fails if they drift. If you change
the engine list, the shipping gates, or the capture ownership rules, that test
breaks and this file must be updated with them. Prose that no test can check
is marked *(unverified)* so you know what to trust.

---

## What this is

A drive-thru camera that recognises licence plates, hashes them **on-device**,
and sends only the hash to AWS. Raw plate text never leaves the Pi. That is a
pending-patent claim and a `SECURITY.md` requirement, not a preference.

One unit per site. Currently: `serv-unit-001` at `ljs-louisville-001`
(Louisville, KY) and `serv-unit-002` at `qdoba-pr-001` (offline since
2026-07-15).

## Engines

`alpr.engine` in `config.json` selects the recogniser. Registered values:

| value | recognition runs | camera owned by |
|---|---|---|
| **`pr_stream`** ← shipping | Stream container | MediaMTX (`NullCamera` here) |
| `ultimatealpr` / `ultimate_alpr` | in-process SDK | this process (Picamera2) |
| `fast_alpr` | in-process ONNX | this process (Picamera2) |

Engines in `ENGINES_WITH_OWN_CAPTURE` (currently just `pr_stream`) must NOT
open the sensor here — libcamera hands it to exactly one consumer, and
competing for it starves the RTSP feed the engine reads.

## Data flow

```
IMX296 sensor (1456x1088, 4:3)
      │
      ▼  libcamera
MediaMTX ──► rtsp://127.0.0.1:8554/cam        systemd: mediamtx.service
      │      software H264 (see TRAP 1)
      ▼
Plate Recognizer Stream (Docker, --network host)
      │      recognition happens HERE; config.ini in ~/stream
      ▼  webhook POST, urlencoded (see TRAP 2)
http://127.0.0.1:8129/  ◄── loopback ONLY; carries RAW PLATE TEXT
      │      plate_reader/stream_engine.py
      ▼
detector.py    identity gates / dwell / cooldown
normalizer.py  NORMALIZATION_SPEC v1.0        ── unchanged, and must stay so
hasher.py      SHA-256                        ── the identity
sender.py      SQLite queue, retry, dead-letter
      │
      ▼  cellular or WiFi
AWS API Gateway ──► Lambda ──► DynamoDB
```

Everything downstream of `stream_engine.py` is engine-agnostic. Swapping the
recogniser does not touch voting, hashing, delivery, or the heartbeat — that
was the design goal of the swap and is what makes rollback cheap.

The isolated home Reolink bench uses the same `pr_stream` capture ownership,
with Stream reading the camera's native RTSP main feed directly instead of
MediaMTX. `NullCamera` supplies configured 3840x2160 geometry; it is not a
camera-health signal. `tools/reolink_bench/status.py` reads independent PR
health and aggregate local queue counts. Bench delivery is disabled at the
Sender boundary, and the LJS route geometry remains uncalibrated shadow
evidence. This does not change the shipping source or engine list. See
`tools/reolink_bench/README.md` for this host-specific installation and its
remaining hardware acceptance checks.

The home bench also supports the user-authorized Tapo C113 experiment in
`tools/tapo_bench/`. PR reads its local Wi-Fi RTSP main feed; measured source
geometry is 2304x1296 H.264 at nominal 15 fps. The bench app retains its identity,
hashing, confidence gates, and disabled backend delivery. Reolink capture is
disabled while the same Pi's Ethernet port is used for router recovery.

An explicitly user-authorized temporary home-test viewer lives in
`tools/tapo_bench/live_detections/`. It is an exception to the raw-text boundary
for this bench session only: a second loopback webhook holds plate candidates
in bounded memory and an authenticated Mac LAN page displays them on the
operator's phone over a direct Ethernet SSH tunnel. No images or raw event
files are exported. The production listener, hashing, gates, and disabled
bench cloud delivery remain unchanged. A viewer confirmation requires an
unambiguous hash-and-timestamp match to a new confirmed local queue visit;
an OCR score alone is never a confirmation. The viewer is outside the fleet
installer and updater. Its runbook documents teardown and display limits.

The home bench's optional Wi-Fi camera network is implemented in
`tools/camera_network/`. It uses a manual-address NetworkManager access point
and DHCP without an advertised router or DNS server, rather than shared/NAT
mode. An independent nftables forward filter blocks both camera interfaces
from internet transit over IPv4 and IPv6, and an output filter prevents camera
subnets from being routed through another interface. NetworkManager requires
that guard at startup. The unchanged cellular supervisor has a bench-specific
entry point, isolated runtime state, explicit per-link DNS, and an orderly
shutdown hook that releases only its owned QMI session. Tailscale supplies
management over cellular; it does not advertise the camera subnets. These
host-local files are separate from the production application updater.
User-authorized temporary onboarding internet exceptions are operational
overrides to this baseline; the Tapo exception was removed before RTSP tests.
The current Tapo runbook records the open hotspot and router Ethernet overrides.

## The three vehicle outcomes

With `detection_mode = vehicle`, every route-qualified result falls into
exactly one:

| what Stream saw | goes to | becomes |
|---|---|---|
| vehicle + plate whose detector vote **confirms** | `/visit` | confirmed visit: hash + vehicle type/make/model/colour |
| vehicle + plate that never confirms | `/visit` **and** `/detection-event` | one `insufficient_confidence` vehicle **and** one local full-frame audit case |
| vehicle, no plate at all | `/visit` | anonymous vehicle (`no_visible_plate`) plus one local full-frame audit case |

The middle row is the subtle one. A car whose plate is too blurry to trust is
still a real car; counting it only as a diagnostic silently drops it from
traffic totals. The encounter layer in `stream_engine.py` owns a random
`encounter_id`, coalesces by exact candidate continuity, one uninterrupted
overlapping-geometry episode, and timestamp-owned native trajectories, and
passes that ID through detector votes and miss diagnostics. Make/model/colour
are enrichment only and never decide whether two observations are one physical
vehicle. Distinct result entries in one recognition webhook remain separate
simultaneous vehicles when their associated vehicle boxes differ. Plate
Recognizer represents additional plates on one vehicle as extra result entries
with the same exact vehicle box; those entries share one encounter and are
competing candidates from one provider observation, never extra vehicle counts
or independent OCR votes. A per-frame gate pass is only a candidate; the
encounter becomes identified only after detector.py's vote confirms. Every
encounter emits at most one terminal `/visit`.

The product recognition KPI uses only vehicles with identifiable plate
evidence: `vehicle_recognition_rate = confirmed / (confirmed +
insufficient_confidence)`. A `no_visible_plate` vehicle remains part of total
traffic and the unidentified-vehicle rate, but it is not a recognition miss
and cannot lower this KPI. The rate is `null` when that identifiable
denominator is zero.

The ordinary PR identity path is det ≥ 0.70 and OCR ≥ 0.80, with two
exact reads (or the 0.99 single-frame fast path). Production also accepts one
provider-selected result after Stream's 12-second temporal window, but only
when it belongs to a stable `encounter_id`, has at least 4 normalized
characters, clears the same normal gates, and the plate center is at least
0.45 of the way across its vehicle box. This site-calibrated geometry rejected
the measured short fragments and bumper/window text; it is not a global
one-frame vote. One narrower PR-only exception recovers a provider-selected
result that fails **only** the normal detection gate when det ≥ 0.35 and
OCR ≥ 0.98. It requires the same stable encounter, length and rear-plate geometry,
and settlement must have observed no competing normalized variant
for that encounter. A separate, PR-specific
consensus rescue may accept **three exact reads from the same `encounter_id`**
at det ≥ 0.65 and OCR ≥ 0.80 when exactly one ordinary gate failed. It never
combines different vehicles, mixed strings, both-gate failures, or reads with
no encounter ID. `consensus_rescue_min_frames = 0` disables it. These rescue
settings live in `alpr.thresholds` and must move with the engine and its normal
gates.

Quiet unidentified encounters remain local for 90 seconds so detector outcomes
and narrowly timestamp-owned recognition/dwell races can settle before one
terminal Visit. A provisional plate string alone cannot bridge a physical quiet
gap: only detector-confirmed keys receive long exact-key continuity. This keeps
a recurring weak OCR string on the following car from contaminating the prior
profile. Normal vote buckets, dwell entry and pending-miss episodes are keyed
by `encounter_id` plus plate candidate whenever Stream supplies physical
ownership. Two cars in one frame or within the old 12-second temporal fallback
therefore cannot vote together, erase each other's miss, or become one audit
case. Time-only grouping remains only for legacy reads with no encounter ID.
DetectionEvents remain diagnostic evidence linked by `encounter_id`; they are
never extra cars.

At LJS, a plate or vehicle detection is only a **candidate vehicle profile**.
Anonymous traffic requires Stream's completed dwell trajectory to show the
vehicle centre in the site-calibrated lower-right entry zone and later in the
full-left upper board zone. LJS requires at least 8 usable trajectory points,
at least 2.0 seconds of timestamped motion, two stable samples in each zone,
and minimum left/up progress. One field-calibrated fallback handles a provider
track that begins too near the entry/board boundary to show the ordinary
progress delta: it still requires ordered entry then board evidence, at least
20 total trajectory points, at least eight actual board-zone samples, and
eight continuous seconds in the board zone.
It cannot rescue a terminal upper-right exit. The complete track is scanned: a
terminal centre in the calibrated upper-right exit band is rejected even if an
earlier sample touched the board rectangle. A second, shorter OR path handles
the exact provider fragmentation measured in the 1 Sep audit: at least four trajectory points
over 0.5 seconds, ending with at least two consecutive terminal board
samples, plus the normal left/up progress. It must either contain an entry
sample before the terminal board streak or remain wholly inside the board zone.
The terminal upper-right test runs first, so this OR cannot recover a measured
drive-off. Three-point entry fragments remain pending for their later plated
continuation instead of becoming traffic. Sparse plate-free recognition boxes never qualify
traffic and a missing completed dwell fails closed instead of fabricating a
car. A provider plate whose associated vehicle is inside the board rectangle
is useful provisional trigger evidence, but it remains `pending` and cannot
create a Visit until the completed trajectory qualifies. This is necessary
because a plated vehicle can touch the broad board rectangle and then exit
upper-right. A conflicting plate overlapping a recently identified profile is
also marked as a candidate conflict until its own trajectory proves the route.
Traffic visible only in the upper band and vehicles whose completed path
terminates through the upper-right are excluded. Completed qualification is
monotonic: a later short provider
fragment cannot erase stronger full-route proof. A rejected fragment waits the
configured 60 seconds before suppression so a split follow-up track can still
qualify the same profile. The 31 Aug Caravan sequence supplied its qualified
continuation 21 seconds after the rejected predecessor; the old 10-second
window had already persisted a false route diagnostic. A recognized identity releases when its profile
qualifies. A pending route profile may remain local for 300 seconds: the 28 Aug
field canary observed a valid 121-point track arriving about 117 seconds after
the first recognition event, leaving unsafe margin under the previous
120-second timeout. Once an unidentified profile has a completed qualified
trajectory, it remains local through the ordinary 90-second identity-resolution
window, so a front/side observation is not finalized before its rear plate or
delayed detector outcome has had a chance to appear.
The first confirmed board sample is retained as `route_qualified_at`; a
recognized Visit sends it as optional `triggeredAt`, alongside route
status/reason, trajectory fingerprint and dwell evidence. The timestamp marks
the menu-board moment, but the current Visit is delivered only after completed
route proof. A future real-time voice-AI channel must consume provisional
board evidence separately without treating it as final traffic.

The LJS entry rectangle is x `0.52–1.00`, y `0.26–1.00`; the board rectangle
ends at y `0.25`, so they remain disjoint. The y edge moved from `0.28` after a
1 Sep field track showed a real lane pickup with 48 points over 56.8 seconds:
its first centre was at y `0.313`, but its next was at y `0.2698`, which reset
the required two-sample entry streak under the old edge even though the truck
then dwelled at the board. The ordinary two-point zone requirement and its
duration/point floors remain intact; the narrow progressive-board OR above is
separate. Route direction and terminal upper-right rejection still apply to
both paths.

The 16 Sep replacement-camera audit adds a narrow short-route recovery: a
track with at least eight distinct timestamped samples and two confirmed
entry samples may measure progress from that stable entry pair instead of
only its last entry point. The existing terminal board streak, minimum
duration for the progressive OR, left/up progress and upper-right exit veto
still apply. For that OR only, identical-time samples are one observation;
conflicting boxes may resolve only to one >=70%-overlapping continuation when
every alternative is disjoint from the previous box. Ambiguity denies this
OR, not an independently proven ordinary route. Native samples are unchanged
for identity association and audit. Any possible terminal upper-right box
still vetoes qualification before coincident-sample cleanup.

The 25 Sep human-reviewed Jeep adds a separate LJS-only plated short-route
exception, `confirmed_plate_deep_entry_short_board`. It retains the ordinary
route floors and qualifies only after the existing 60-second rejection grace.
It requires 4–7 strictly timestamped native samples over 0.5–2 seconds, at least
three initial entry samples, monotonic left/up motion, 70% adjacent-box overlap,
and a terminal centre deep inside the left board zone (x ≤ 0.60, y ≤ 0.24).
The start must reach y ≥ 0.40 with left/up progress of at least 0.06/0.15.
One detector-confirmed 4–12-character candidate must own that exact endpoint:
the same observation needs det ≥ 0.70, OCR ≥ 0.98, timestamp agreement within
50ms and at least 70% box overlap. A competing candidate, simultaneous profile,
alias or merged fragment denies the exception. A known terminal upper-right
exit permanently vetoes this exception for the encounter, even if an older
short track arrives later. Other later completed evidence cancels the held
candidate. It adds no capture, inference, image retention or timer, and does
not provide real-time voice-AI delivery; an exit the provider never reports
cannot be ruled out. Changed site geometry or a shorter grace needs review.

Route filtering and physical-profile reconciliation are separate authorities.
With `profile_reconciliation_enabled`, a completed trajectory may merge an
exactly keyed rear profile with exactly one sample-timestamp-matched,
trajectory-overlapping plateless front profile even if route filtering itself
is in shadow. A plate-free qualified trajectory may likewise merge sequential
plateless fragments only when their profile windows do not overlap and their
latest boxes are owned by distinct trajectory samples within one second and
advance strictly through provider order. Concurrent queue
profiles, repeated plate candidates without that physical proof, and multiple
eligible fronts fail closed. A localization that clears neither the PR
detection gate nor its OCR gate remains local audit evidence, but it is not a
separate physical owner: a later exact rear plate may reconcile that fragment
through the completed native trajectory. Clearing either gate remains
conservative because an engine-specific rescue can still confirm it. If the
later plate does confirm, any aliased route-deferred miss is cancelled before
it can enter the KPI; the diagnostic image remains available for review.
Conversely, shadow route classification records
its own status: Shadow classification cannot create or release a Visit; it
also cannot suppress or delay one. An anonymous profile still emits its one
legacy Visit at the ordinary 90-second boundary; a metadata/image copy remains
in process only long enough
to add the later local `non_drive_through`, `drive_off`, or `route_unresolved`
audit tag. Later weak plate evidence still produces its diagnostic event and
coalesces the anonymous and plate-miss files into one local case; a late
confirmed identity removes an untagged stale anonymous audit case, preserves
any route-classified evidence with a `lateIdentified` marker, and cannot create
a second Visit. Every anonymous Visit enters a bounded process-local
terminal-owner ledger, preserving that one-Visit guard after the live route
profile is cleaned up. Reconciliation may redirect live aliases but cannot
rewrite the historical encounter ID already sent in that Visit. Because an
already-sent shadow Visit cannot be reclassified in place, shadow traffic/KPI
statistics remain provisional until route authority is promoted. The audit
tag is never a second Visit or KPI event.
Reconciliation is the separate operation that may still merge duplicate
profiles. The checked-in example remains shadow-first, while the explicitly
promoted live LJS configuration is authoritative. The 31 Aug field review
found one real 42-point/29.8-second menu-board truck rejected only because its
track began near a zone boundary; the guarded sustained-board fallback above
recovers that shape without admitting the observed 3--8 point upper-right
fragments. A qualified dwell hook that arrives
before its recognition hook is cached in RAM for 30 seconds and retried as
profiles arrive; rejected unmatched cross traffic is not cached. Stream's
dwell hook remains loopback-only because it can contain raw plate text. That
text is correlation evidence only and never bypasses normal confidence gates
or the hashing path.

After one authoritatively qualified unidentified Visit is emitted, the engine
keeps a metadata-only tombstone for 300 seconds. It can absorb an unplated,
candidate-free board-zone departure only when exactly one tombstone overlaps
the vehicle box by at least 70%. A fresh route-entered profile always takes
precedence. Once selected, the tombstone owns only that one continuous
departure episode and expires after one quiet window; an entry-zone
observation, a plated attempt, shadow mode, or an ambiguous match starts or
retains a separate profile. Tombstones keep no image bytes and no plate
candidate keys. This prevents a long menu-board stop followed by an upper-left
departure from becoming a second car without merging the next car entering the
lane. The plate identity eligibility rectangle still begins at the left frame
edge, includes the upper-right band, and ends at 75% frame height. Route
classification—not a destructive image crop—decides what counts, so Stream
retains the full frame and all possible plate evidence.

At a `behind_cars` site, Stream can split one moving car into a near/front
plateless result and a far/rear plated result when the boxes stop overlapping
and make/model changes. There is deliberately no live directed handoff: an
apparently unique predecessor can be the adjacent queue car. Only a completed
native trajectory may reconcile non-overlapping fragments, with the strict
sample-time, ordering and ambiguity guards above. The legacy
`presence.encounter_handoff_seconds` key remains accepted as a no-op so current
unit configs and rollback builds stay compatible. MMC agreement is neither
required nor sufficient. This is physical traffic correlation only; it never
changes or contributes to plate hashing.

Anonymous vehicles carry no plate, therefore no hash, therefore cannot become
a customer identity. `StreamVehicle` has no plate field at all, by
construction.

For recognized identities, Visits keep the encounter ID, first/last/identified
timestamps and nullable vehicle type/make/model/colour. Plate Recognizer scores
make/model as one atomic pair and colour independently; the encounter and
customer profile retain the highest-confidence pair and colour rather than the
latest guess. An unscored legacy value may fill an empty attribute but cannot
overwrite an existing one; that is the last non-null descriptive value fallback
for legacy data, not the scored-data selection rule. Customer statistics are
written before independent conditional colour and make/model updates, so a
weaker concurrent observation can lose without losing the visit count.
GetCustomers returns the same profile fields. They never enter normalization or
hash input, and they also never decide physical correlation.

The local audit dataset is deliberately case-based, not frame-based. During
the current bounded canary it includes each confirmed outcome as well as each
plate miss or plate-free vehicle, with one shared newest-300-case cap. Each
plate miss keeps Detector's single best original frame; `no_visible_plate`
advances its one retained original JPEG with the encounter so the terminal
evidence reflects the later rear/menu-board opportunity instead of an early
high-confidence full-vehicle frame. Plate-seen terminal encounters do not save
a second recognition-miss copy; any late duplicate is coalesced by encounter
and episode ownership. Confirmed outcomes retain their best original too, so
accepted hashes can be manually checked. Live presence observations never
retain image bytes. A route-gated identified
profile may retain one compressed original only until its completed route
qualifies or rejects. A final route rejection is saved locally as exactly one
`drive_off`, `non_drive_through`, or `route_unresolved` diagnostic. A proven
upper-right terminal exit is `drive_off`; a track that never entered the lane
is `non_drive_through`. Merely ending before two confirmed board samples does
not prove where the physical car went, so `left_before_board_trigger` and
sparse/missing/timeout evidence are explicitly unresolved rather than
overstated as known cross traffic. In
authoritative route mode these diagnostics never become a Visit, identity, or
KPI counter. Shadow mode deliberately still emits its legacy Visit while it
collects labels, so its traffic/KPI numbers remain provisional until the
classifier is reviewed and promoted. Production stores the newest 300 cases
under `/home/serv/plate_reader/audit_cases`; each case is one
full-resolution JPEG plus a local JSON sidecar. Sidecars include the generating
vehicle box, route status/reason, traffic classification, track fingerprint,
every completed trajectory box and timestamp, the three normalized route
zones, terminal trajectory box/time, the retained image timestamp,
candidate-presence boolean, and MMC confidence values. That lets a reviewer
distinguish a real terminal exit from a provider track split even though the
dwell payload itself carries no terminal JPEG. This route-evidence path stores no plate text or hash, and
neither file leaves the Pi automatically.

An OCR-confirmed sidecar records `recognitionEncounterId`, canonical
`encounterId`, `recognitionOutcome`, and a separate `trafficDecision`. The
legacy `outcome=confirmed` describes OCR confirmation, not a counted Visit.
Pending/rejected routes, route timeouts, and suppression decisions stay local;
`visit_queued` means local enqueue, not AWS delivery. At that decision the
sidecar retains the matching completed route proof when available. Replacing
or losing that proof clears stale geometry, and image time is never inferred
from route time. Late terminal coalescing preserves the decision while
retaining route-labeled evidence. Diagnostic-write failure never changes
traffic delivery.

Audit route status/reason and its supporting trajectory are retained together,
including through profile merges. A longer rejected fragment cannot overwrite
the geometry of a shorter qualifying proof while leaving the qualified label.
The image may still predate that trajectory: no-candidate means the provider
returned no candidate, not that a human verified an absent/unreadable plate.
Local sidecars explicitly label image timing and human visibility as unverified.
Plate detection/OCR maxima, when present, are independent encounter maxima,
not necessarily the confidence pair from one frame.

To recover later audit evidence without another video decoder or inference,
the route engine shares at most 16 already-decoded full-resolution webhook
JPEGs (8 MiB, 120-second receipt-time TTL) in process memory. A completed
matched trajectory may select a newer original only from the same camera and
within 50ms of an actual unambiguous sample timestamp. This is evidence-only;
it adds no detection, vote, association, count or identity. It cannot produce
frames Stream never sent, and a frame arriving after the track was applied
may not be used. A missing or early image remains explicitly unknown/early.
Retagging an existing detector case preserves its original JPEG and cannot
assign that JPEG the timestamp of a different, later available image.

## Traps that have already cost outages

**TRAP 1 — the Pi 5 has no H264 encoder.** Only `/dev/video19`
(`rpi-hevc-dec`, decode) exists; Pi 4's `bcm2835-codec-encode` is gone.
`rpiCameraCodec: hardwareH264` does not degrade gracefully — MediaMTX logs
`using hardware H264 encoder` and **exits 1 on every start**. Use `auto`.

**TRAP 2 — Stream's webhook docs are wrong.** They describe
`multipart/form-data`. With `image = no`, Stream v1.64 actually sends
`application/x-www-form-urlencoded` with a `json=` field. With an image mode
it is multipart (`json` plus `upload`). Parsing only one shape returns 200
and **silently drops every result**. `extract_json_field()` handles all
three encodings. Production uses `image = original` so plate_reader can keep
one highest-OCR full-frame still per plate episode and one best frame for a
plate-free terminal vehicle; it explicitly prefers the multipart `original`
part over crop-shaped legacy names. Stream's
`image_format` is empty so Stream itself does not dump every detection to disk.

**TRAP 3 — the sensor is 4:3.** Asking MediaMTX for 1280x720 makes libcamera
*crop* to 16:9, discarding ~25% of vertical field of view and 42% of pixels.
Plate legibility is pixels-on-plate. Match the sensor: 1456x1088.

**TRAP 3A — night blur is an exposure problem, not a frame-count gate.** The
29 Aug and 1 Sep audits kept the post, curb and landscaping sharp while moving
vehicles and plates smeared after 20:00. The first `short` exposure canary left
shutter duration automatic and did not freeze those plates. A 1 Sep `motion`
canary at 3333 microseconds (about 1/300 second) produced a nearly black frame:
mean luminance 2.7/255 versus 40.1/255 immediately after rollback. The reviewed
next step is `twilight` at 16667 microseconds (about 1/60 second). It gives five
times the exposure of the failed canary while retaining automatic gain and the
measured 5-FPS source/Stream sampling. `ljs_camera_config.py` applies it
atomically with a timestamped backup. Do not combine this canary with a
source-FPS, Stream-sampling, gain, zoom or preprocessing change: darkness/noise
and exact OCR accuracy must be measured separately. MediaMTX normally hot
reloads the profile without restarting Stream:

```sh
cd /opt/serv
sudo python3 -m plate_reader.ljs_camera_config --check --mode twilight
sudo python3 -m plate_reader.ljs_camera_config --mode twilight
curl -fsS http://127.0.0.1:8001/status/
journalctl -u mediamtx --since "2 minutes ago" --no-pager

# semantic rollback to the currently measured short/auto-shutter baseline:
sudo python3 -m plate_reader.ljs_camera_config --check --mode short
sudo python3 -m plate_reader.ljs_camera_config --mode short
curl -fsS http://127.0.0.1:8001/status/
```

The utility refuses unreviewed fixed shutters and fixed gain. The
3333-microsecond profile remains available only to reproduce the failed
lighting test; `twilight` is the active next canary. If 1/60 second still makes
the scene too dark, rollback immediately and add or advance the lane lighting
before trying to recover characters with software. If the config itself is damaged,
restore the exact backup printed by the utility through a same-filesystem
temporary file so MediaMTX observes one atomic replacement, then repeat the
health checks:

```sh
sudo install -o root -g root -m 0644 <printed-backup-path> \
  /etc/mediamtx/.mediamtx.yml.restore
sudo mv /etc/mediamtx/.mediamtx.yml.restore /etc/mediamtx/mediamtx.yml
```

Do not restart Stream for an exposure-only hot reload; a restart discards any
active provider encounter.

The separately staged `tools/night_shutter_policy/` host policy implements the
18 Oct user-requested persistent 1/25-second profile: 40000 microseconds from
19:00 to 22:00 America/New_York, with the exact automatic-short baseline restored
outside that window. It preserves automatic gain and 1456x1088/5 FPS, changes no
PR settings, and never starts or restarts capture. Its timer and pre-start unit
require a human-reviewed host installation; staging is not proof of activation.
It verifies live sensor-control estimates and Stream health, rolls back failed
or interrupted transitions, and refuses unrelated configuration drift. This is
a preferred profile, not a validated optimum or proof of improved recognition.
Disable that policy before using a different manual camera exposure profile.

**TRAP 4 — `max_prediction_delay` and `memory_decay` are different settings.**
The former controls how long Stream may collect predictions before forwarding
its selected result; the current canary uses `max_prediction_delay = 12`, or
about 60 analyzed frames at the measured 5 received FPS. The latter controls
when Stream may detect the **same vehicle again**; the vendor default is 300
seconds and warns that low values create repeat counts. LJS now uses
`memory_decay = 90`: the audited queue kept one car visible for more than 70
seconds, so a 30-second memory could re-emit one long order. The prior 300-second canary
suppressed later rear-plate opportunity, while 3- and 6-second canaries
fragmented provider tracks and produced an early anonymous profile plus later
recognized evidence. The 31 Aug full-frame audit then proved that
`report_static = yes` re-emitted stopped queue occupants at the same
~30-second cadence: images 13--17 were two physical cars represented by five
rows, and images 30--37 were two physical cars represented by eight rows. LJS
therefore uses `report_static = no`. A lane vehicle remains eligible while it
moves into its stop and again when it advances toward the menu board; its
completed dwell trajectory retains the physical route. These settings do not
change the measured 5 eFPS source or identity gates. Keep the two timing
responsibilities separate, verify `/status/` after deployment, and audit that
provider observations still produce one terminal Visit. The separate
app-side identity settlement adds the bounded delay described in TRAP 8.

**TRAP 5 — `presence.vote_min_frames` silently overrides
`thresholds.vote_min_frames`.** Setting only one runs a different vote than
you think. Set both. The example config records this as having cost a
24-minute validation run.

**TRAP 5A — gate changes need encounter and geometry evidence.** The 1 Sep
human audit lowered ordinary PR OCR from 0.85 to 0.80 while keeping detection
at 0.70 and two exact reads. That is not permission for arbitrary one-frame
weak reads. The configured consensus rescue remains an encounter-bound
three-frame path with its own PR-calibrated floors and requires exact text and
exactly one failed normal gate. The single-result high-OCR exception is not a
general weak-read path: it permits only a detection-gate failure at det ≥ 0.35
/ OCR ≥ 0.98 and retains the encounter, no-conflict, length and rear-plate
geometry safeguards.

**TRAP 6 — verify the source of each Stream setting.** The site-local
`/home/serv/stream/config.ini` is authoritative for this unit's camera,
inference and webhook parameters, so editing those values in the web UI does
nothing. The application updater exports only `plate_reader/`; it installs
neither the Stream config nor MediaMTX's `/etc` config. This caused a real
10-versus-5-FPS drift before the template was corrected: the 28 Aug live
`/status/` response reported 5 received FPS with `sample = 1`, while the old
checked-in `stream/mediamtx.yml` requested 10 FPS. Both now specify the measured
5-FPS source baseline; verify live status independently after every change.
Detection zones are a special case: live v1.64 logs `Loading detection zones
from web`. The message alone is not proof that a usable mask is active. Verify
the running camera ID, resolution and actual mask after restart; the vendor's
mask convention is white=analyze and black=ignore. Preserve the complete lane
approach and plate path. At LJS, route-qualified 28 Aug plates were observed in
the same upper-right pixels used by later drive-offs, so a static upper-right
exclusion is unsafe: keep that overlap corridor white and let the completed
trajectory remain the route authority. A mask cannot express “exclude only if
this vehicle later exits upper-right.”

**TRAP 7 — plateless vehicles have no identity key, and Stream webhooks are
sparse predictions rather than analyzed frames.** A car can produce one or
several irregular vehicle events even though Stream analyzes every sampled
frame. They are coalesced per encounter in `stream_engine.py` by overlapping
geometry and
unambiguous physical-path evidence—never by MMC. The quiet window is 12s so
measured Stream gaps (5–8s on a dwelling truck) do not split one car into many
encounters. At route-gated sites, the native completed dwell trajectory is the
terminal physical track; unresolved profiles never become anonymous merely
because the first selected prediction lacked a plate. A candidate-free box may
continue only one uninterrupted sub-quiet episode; geometry never resumes an
old profile after quiet, and authoritative route mode never geometry-attaches
candidate-free evidence to an identified profile. Such fragments require their
own native route proof and therefore cannot become duplicate traffic merely
from box overlap. A **below-gate plate** after that quiet window is a new
identity attempt—do not absorb it into the previous confirmed car (zoomed-FOV
boxes overlap; 18 Aug 1:47 RAV4). A qualified anonymous terminal instead uses
the narrow board-departure tombstone described above; it is not a general
same-colour or same-class deduplicator. Miss episodes stay open while Stream is
still posting that car. LJS uses a 90-second `memory_decay` so a long order
cannot become a second provider result after 30 seconds, while the 12-second
selection window reaches the audited menu-board view before that memory starts.
Static re-reporting is disabled because the 31 Aug audit
showed it manufactures new provider tracks at the decay boundary; the
encounter ledger, route proof and terminal guards must collapse legitimate
moving observations to one physical Visit. Only a completed detector vote
marks an encounter identified; later weak frames of the same vehicle must not
become anonymous visits.

Stream may retry an exact recognition webhook when its HTTP response is lost.
The listener fingerprints the canonical body and suppresses retries in a
bounded six-hour process-local cache, so one provider observation cannot become
two votes or vehicle observations during normal operation. Independently
timestamped provider events that settle into one local queue drain remain
independent votes and are processed in provider-time order; duplicate boxes
within one provider event remain one vote. Restart-durable exactly-once ingest
is not claimed: it requires a transactional persistent inbox plus persisted
encounter/vote state, not a standalone “seen” bit that could lose a car after a
crash.

**TRAP 8 — a plate read is provisional until its vehicle encounter settles.**
Stream has emitted a 3--4 character fragment shortly before a fuller 6--7
character read of the same car. First-confirm-wins then preserves the wrong
hash perfectly. The 27 Aug field audit also found two variants for one stopped
vehicle 1.6 seconds apart, while the two local release observations were
3.115 seconds apart. After Stream's temporal window, encounter-bound reads
therefore wait for 3.5s of OCR quiet (4.0s hard maximum); exact agreement
chooses the candidate, and an independently confirmable, equally supported
fuller substring wins over its fragment. Only that settled candidate enters
detector voting and hashing. Unrelated strings
are ranked by agreement and confidence, not length. The hold retains at most
one JPEG per variant and four per encounter, so even the checked-in 10 eFPS
target cannot create a frame-memory spike. This does delay actual
identity/Visit delivery by up to four seconds; `triggeredAt` preserves the
earlier board time but does not make
delivery instantaneous.
Recognition reads retain Stream's provider timestamp through settlement and
queue drain; detector buckets use that time rather than the later local dequeue
time. This preserves evidence order and the original `detectedAt`, while the
engine continues to use a separate monotonic clock for in-process lifetimes.

**TRAP 9 — board-trigger shadow and route-count shadow are different controls.**
Stream owns the sensor, so `plate_reader` receives a `NullCamera` frame and the
webhook bridge publishes the real Stream vehicle boxes. Top-level
`presence.shadow_only` controls the legacy presence/board-trigger side effects;
nested `presence.route_qualification.shadow_only` controls whether route status
can release or suppress traffic. Profile reconciliation has its own explicit
switch and may coalesce duplicates without granting route-count authority.
Conflating these controls either restores inflated traffic or silently turns a
calibration trigger into a count filter. A route-qualified recognized Visit
can carry `triggeredAt` to AWS, but actually releasing voice-AI context still
requires a measured timing contract and an explicit downstream endpoint.

## Invariants — breaking these is a data-integrity incident

1. **Raw plate text never leaves the device.** The webhook listener refuses to
   bind a non-loopback address.
2. **The hash algorithm in `normalizer.py` and `hasher.py` never changes.**
   Uppercase, strip non-alnum, SHA-256 of that exact string, no salt.
   Change those steps and every returning customer becomes a new customer,
   silently. The validity length floor (currently 3–12) is a gate, not hash
   input: relaxing 4→3 does not rewrite existing hashes. Engine swaps must
   preserve hash continuity — Stream's lowercase output hashes identically
   to ultimateALPR's uppercase form.
3. **Make/model/colour are never hash input.** Descriptive only, so a
   misclassification cannot split or merge a customer.
4. **An anonymous visit must never carry a `licensePlate`.**
   `visit_schema.mjs` rejects it outright.
5. **Confidence gates are engine-specific.** PR's OCR median is 0.99;
   ultimateALPR's gates assumed 0.75. Engine and thresholds move together or
   not at all.

## Where things live

| path | what |
|---|---|
| `plate_reader/stream_engine.py` | Stream webhook receiver, vehicle coalescing |
| `plate_reader/detector.py` | voting, dwell, cooldowns, miss episodes |
| `plate_reader/ljs_stream_config.py` | guarded, backup-producing correction for LJS's site-local Stream INI |
| `plate_reader/ljs_camera_config.py` | guarded, backup-producing LJS short/normal exposure canary |
| `plate_reader/normalizer.py`, `hasher.py` | identity. do not touch |
| `plate_reader/sender.py` | SQLite queue, retry, dead-letter |
| `plate_reader/camera.py` | `Camera` (Picamera2) and `NullCamera` |
| `stream/` | MediaMTX config + unit, Stream `config.ini`, run script |
| `aws/lambdas/` | source mirrors of the deployed Lambdas |
| `docs/STREAM_THRESHOLD_TUNING.md` | why the gates are what they are |
| `config.json` `hours` | local open window; LJS is 10:00–22:00 America/New_York. Outside it, `/visit` is not sent. Heartbeat continues. On `pr_stream`, **MediaMTX and Stream stop** at close and start `warmup_minutes` before open (LJS: 22:00 off, 09:45 on). Sites without `hours`, or engines that own the sensor themselves, leave capture running. |
| `SECURITY.md` | trust boundaries, deploy authority |

## Deploying

For an isolated bench run, `backend_writes_enabled = false` suppresses every
backend request at the Sender boundary, including startup authentication.
Events remain in a bounded, private `queue.dry-run.db`; enabling delivery later
opens the separate normal `queue.db` and cannot replay the bench events. This
does not disable local Stream webhooks or the on-device hashing pipeline.
The setting defaults to true for compatibility with existing field units.

Deployment is **two steps, not one** — approving records consent, applying
does the work:

```sh
sudo /usr/local/sbin/serv-update --approve <full-40-char-sha>   # consent
sudo /usr/local/sbin/serv-update                                # apply
```

The commit must already be fetched locally, and must equal the head of the
tracked channel in `/home/serv/plate_reader/.update_channel`. An agent must
not self-approve — that gate exists specifically so a push cannot become fleet
root execution.

The pull updater deliberately does not overwrite the credential-bearing,
site-local `config.json`. After the approved route code is installed, LJS uses
the validated, backup-producing config utility. New sites start in shadow:

```sh
cd /opt/serv
sudo python3 -m plate_reader.ljs_route_config --phase shadow
sudo systemctl restart plate-reader
```

The shadow phase enables physical-profile reconciliation, bounded all-outcome
evidence and the stricter classifier/identity-settlement values but cannot
suppress traffic. Do not run the authoritative phase until reviewed geometry
stops false-rejecting real drive-through vehicles. LJS has already passed that
explicit promotion gate. An additive route release may update its exact prior
authoritative baseline in place, but the utility refuses any unreviewed or
drifted configuration:

```sh
cd /opt/serv
sudo python3 -m plate_reader.ljs_route_config --phase authoritative
sudo systemctl restart plate-reader
```

Stream's bind-mounted INI is a separate deployment boundary. After the
application release containing the correction is approved and installed, the
LJS utility previews and atomically applies only
`max_prediction_delay = 12`, `memory_decay = 90`, `max_dwell_delay = 30`,
`report_static = no`, and `plates_per_vehicle = 1`; it refuses an unexpected camera/source and creates a
mode-600 backup. It intentionally does not restart Stream, because a restart
while a car is mid-lane can lose that encounter:

```sh
cd /opt/serv
sudo python3 -m plate_reader.ljs_stream_config --check
sudo python3 -m plate_reader.ljs_stream_config
# wait until the lane is empty, then:
docker restart stream
curl -fsS http://127.0.0.1:8001/status/
```

Rollback uses the exact backup path printed by the utility, followed by the
same empty-lane restart. The review mask under `stream/detection_zones/` is not
installed by this command and remains a separate, image-backed decision.

Rollback: tag `v2.1.0-ultimatealpr`, plus restoring that engine's gates.
Engine and thresholds together.

## Replacement provisioning and cellular recovery

The capture-hours helper imports the shared configuration module inside a
`ProtectSystem=strict` sandbox. File logging may therefore fail with `EROFS`.
The shared logger falls back to stderr/journald for this read-only case, just
as for unavailable permissions; unexpected I/O failures still propagate.
Do not grant the root scheduler write access to the application's rotating
log: root-created rotation files could then block the unprivileged app.
`install_ljs_held_app_patch.sh` applies only this log.py package delta under
exact human approval while the replacement's shipping hold remains intact.

`plate_reader.ljs_replacement_config` builds private staged configurations;
it does not deploy them. `scripts/install_ljs_replacement.sh` is a separately
reviewed **host installation**, not the Python-only updater. It requires the
human-approved exact Git object, snapshots the old bench state privately,
and installs the stack from that immutable object. Bench operation uses
`serv-unit-bench` / `bench-servcamog`, never the Louisville identity. The
replacement's old bench queue must not be replayed under `serv-unit-001`.

Production activation is a separate commissioning step. It preserves
`serv-unit-001`, `ljs-louisville-001`, and all vehicle-hash behavior, rotates
only that device's cloud credential, and checks the new optics and route
geometry onsite. A bench conversion is not evidence of shipping readiness.
The detailed acceptance checklist is in `docs/LJS_REPLACEMENT_2026-09-12.md`.

`scripts/install_ljs_identity_cutover.sh` is the separately human-approved
identity handover for the converted replacement. It stops the application and
capture, archives the entire bench state directory including SQLite WAL/SHM,
and installs the canonical LJS configuration with fresh queue state. It assigns
hostname `serv-ljs-001` without cloning SSH/Tailscale private state. It keeps
the application `.deployed_sha` truthful and records the host cutover separately.
The current boot is held by `/run/serv-ljs-shipping-hold` conditions on capture,
application and update units; Docker restart is disabled for the stopped Stream
container. The next boot clears that runtime hold and the ordinary capture-hours
timer restores Stream during LJS hours. Consequently, **any subsequent boot**
can activate production: do not reboot in the test room after the handover.
`tools/prepare_ljs_identity_cutover.py` stages a private new credential and uses
a conditional `UpdateItem` to change only `api_key` on the existing device item
after matching held-host proof. Neither staging nor installing the host performs
that cloud update automatically. The key handover must be verified before
shipping; identity handover is not a claim of completed optical/LTE acceptance.

MediaMTX v1.21 enables MoQ by default; `stream/mediamtx.yml` explicitly sets
`moq: no` to preserve the loopback-only raw-video boundary. The Stream launcher
does not source its credential file as shell code, does not pull by default,
and never removes an existing container automatically. Replacement installs
pin the tested ARM64 image digest. Docker administrators can still inspect
container environment credentials; `--env-file` hides process arguments,
not credentials from privileged Docker inspection.

Cellular/systemd changes require the reviewed host installer. The updater's
manual-install gate includes `scripts/cellular-connect.sh`,
`scripts/serv-cellular-supervisor`, and the cellular/network-priority units.
An applied Python package SHA alone is not proof the network supervisor was
updated. During replacement installation, `SERV_INSTALL_START_NETWORK=0`
defers cellular/telemetry starts until old ownership is stopped and the
replacement files are installed. Ordinary manual installs explicitly restart
the cellular service; `enable --now` alone would leave an old process running.

The replacement cold-boot check found another startup trap: USB enumeration
alone does not prove that modem control requests will succeed. Before a new
retained WDS client allocation, the supervisor now requires a bounded,
non-allocating CTL service-version query that advertises WDS. Readiness failures
retry with backoff, without allocating clients or setting the unknown-client
latch. Only source-verified pre-allocation device-create/open failures may
retry after an attempted allocation; ambiguous allocation failures remain
latched. Fixed failure categories are logged; bounded detailed output stays
in a root-private runtime diagnostic. A successful later readiness check does
not erase an existing uncertain allocation.

`scripts/install_cellular_patch.sh` is a separate human-approved, bench-only
incremental host patch. It preserves the camera/application configuration,
cloud identity and existing uncertainty state, and records its helper version
separately from the application `.deployed_sha`. Installing it cannot prove
cold-boot recovery: the already-latched replacement still requires a genuine
Pi-plus-modem power cycle and repeated cellular acceptance.

The September outage's exact root cause remains unproven without the failed
unit's logs. The old watchdog's missing CID/packet-handle ownership and
unbounded recovery calls are real bugs, but fixing them is not proof they
explain every symptom. Cellular-only DNS, AWS/Tailscale access, controlled
recovery and cold-boot acceptance are mandatory before shipping a replacement.

## Known-unverified *(unverified)*

The Ivar's IP-camera operator kit lives in `fieldkit/` with the `./serv`
client. Its manual host installer requires a human-approved exact commit,
checks the physical Tailscale identity and existing application/cellular-core
bytes, and retains the exact modem session state. It is outside the fleet
updater. Installation leaves backend delivery disabled. Production activation
uses a separate private profile, confirmed open hours, secured camera Wi-Fi,
AWS authentication and recorded site acceptance. This kit has not yet passed
installed field acceptance; see `FIELD_SETUP.md`.

The IP-camera hours timer controls `serv-tapo-stream.service` directly; it must
not invoke the IMX296/MediaMTX capture helper. A boot-local condition marker is
created only when the current profile permits capture. The app and heartbeat
remain running when capture is closed. A preview is a separate bounded SSH
stream and never stops PR. Tailscale can use a cellular fallback even when
Wi-Fi has the preferred default route, so the preview refuses any available
cellular route unless the operator explicitly permits it for that session.
No field raw-text detection page is installed. Normalization, hashing and
MMC's descriptive-only role are unchanged.

- Stream v1.64 reports that it loads detection zones from the web, but the
  28 Aug audit did not prove that a correctly sized camera-1 mask was active.
  Treat the whole frame as analyzed until the actual mask is inspected after a
  restart.
- The route geometry has been checked against saved lane and upper-right paths,
  but the board timestamp's accuracy as a voice-AI release point has not yet
  been validated against a continuous lane traversal.
- A qualified dwell track that races ahead of recognition is retained only in
  process memory for 30 seconds. A plate-reader restart in that interval can
  still lose the association; a persistent inbound/outbox ledger is the next
  step if measured restart loss is material.
- Rejected-route audit evidence is retried in-process until its JPEG/sidecar is
  acknowledged and is capped at 64 cases. An application restart or sustained
  disk failure can lose that diagnostic tag; it does not create or lose a
  traffic Visit. Persist it before restart if field evidence shows that window
  is material.
- Night lighting remains a measured recall risk. The short-exposure canary is
  running but has not yet been measured across the 20:00–22:00 blur window.
- The Lovable dashboard is not wired for `estimated_additional_vehicles` or
  the new `/visit` fields.
