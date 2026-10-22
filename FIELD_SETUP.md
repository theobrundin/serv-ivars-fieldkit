# Ivar's location 001 — operator setup and acceptance

**Current state: cloud identities and operator software installed in sandbox;
field production activation is not complete.** Do not ship based on a green PR
process alone. Remaining gates are listed below with measured evidence.

For a new operator, use [OPERATOR_HANDOFF.md](OPERATOR_HANDOFF.md) first.
Initial adoption and the open-Wi-Fi update are both installed; the installation
commands retained below are historical maintenance references, not onboarding
steps. Current installed release is
`29b16c8626b31866663bf3bf3b74513717a64cd2`, verified 22 October 2026.

This is a dedicated operator workflow for the existing Tapo C113 + Raspberry
Pi 5 + Sixfab unit. Its recognition core comes from the reviewed Serv source;
it is not a new recognizer or a fleet-wide installer. The current physical
hostname remains `servTest` because the existing device-specific services use
that condition. AWS identity is independent of the Linux hostname.

## Daily operation, after the reviewed install

Install Tailscale and FFmpeg (including `ffplay`) on your computer. Join the
authorized Serv tailnet and have your personal SSH public key enrolled on this
Pi. Repository access does not confer device access. The CLI pins this Pi's
public SSH host key in `fieldkit/known_hosts`, obtained through the existing
verified SSH session. There are no private SSH keys in the repository. A host-
key mismatch needs an identity review; never disable host-key checking.

```sh
./serv status
./serv doctor
./serv view                     # shows uplink and asks before cellular video
./serv view --seconds 60        # bounded live view; close the player to stop
./serv metrics --minutes 15     # aggregate local audit outcomes, no raw OCR
./serv image                   # current brightness/contrast/saturation/sharpness
./serv image --brightness 50    # change, read back, restore if rejected
./serv network cellular        # prefer cellular, retain fallback
./serv network wifi            # requires a working second Wi-Fi adapter
./serv mode production --lane-empty
./serv mode sandbox --lane-empty
./serv ssh
```

The preview lasts at most five minutes. `--allow-cellular` is an explicit
per-session opt-in for unattended commands. At the measured 1.28 Mb/s, budget
roughly 10 MB/minute plus transport overhead. Closing the preview leaves PR
running. The camera-to-Pi video always uses the private `wlan0` link; only an
explicit remote preview sends video through Tailscale. AWS receives hashes and
descriptive vehicle attributes, not video or raw plate text.

Wi-Fi preference does **not** guarantee a cellular-free Tailscale path:
Tailscale can roam onto fallback routes. The viewer therefore refuses without
consent whenever cellular routes are available. A strict Wi-Fi-only viewing
session needs cellular routes withdrawn through a separately verified network
cutover; this release does not automatically do that remotely. Without a
second adapter, leave the Pi on cellular: `wlan0` must keep broadcasting the
camera network. Ethernet is also an independent recovery/uplink option.

The C113 currently exposes four image sliders through ONVIF. Optical zoom,
motorized focus and a working manual bitrate change were not exposed by the
tested firmware. Adjust aim and distance physically. Digital enlargement
cannot recover missing plate detail. Image controls read back every change
and restore the previous settings if the camera ignores it.

## Identities and private files

| Use | Device | Location |
|---|---|---|
| Production | `serv-ivars-001` | `ivars-001` |
| Cloud synthetic tests | `serv-ivars-001-sandbox` | `ivars-001-sandbox` |
| Existing camera bench | `serv-unit-bench-reolink` | `bench-reolink-house` |

AWS's current schema represents the location through the device's scoped
`location_id`; there is no separate Locations table. Device metadata names the
restaurant `Ivar's` and location number `001`. Both new device keys are unique.
Secrets stay in private files and never belong in Git, command arguments or a
handoff chat. Existing LJS/Qdoba identities are untouched.

Location 001 is **Lakewood Seafood Bar**, 10114 S Tacoma Way, Tacoma, WA 98499,
(253) 588-4773. The user supplied these hours: Sunday–Thursday 11am–9pm;
Friday/Saturday 11am–10pm, in `America/Los_Angeles`. Capture warms up at
10:45am; counting starts at 11am. The public site settings are in
`fieldkit/sites/ivars-001.json`; credentials are separate.

On Pi: `/home/serv/ivars-staging` contains private commissioning inputs and
reports. After installation: `/etc/serv-field/profiles/{sandbox,production}.json`
are root-private. The running application keeps its standard private config
and separate production/dry-run SQLite queues. The installer refuses a
pre-existing nonempty production queue so an old device cannot be replayed as
Ivar's. No bench queue is copied into the production queue.

## Commissioning sequence

1. Confirm the restaurant's opening/closing times and IANA time zone. Lakewood's
   schedule is now supplied above. The shared parser supports complete weekly
   schedules, overnight windows, closed days and 15-minute capture warmup.
   Existing daily configurations retain their behaviour.
2. **Complete:** the production profile is prepared and installed privately,
   using `fieldkit/sites/ivars-001.json` and the installed weekday-aware parser.
   Do not regenerate keys or overwrite profiles for operator onboarding.
3. **Complete:** the same time zone for `ivars-001` is set in the existing AWS
   `LOCATION_TIMEZONES` environment mapping for AggregateVisits,
   AggregateDetectionEvents and Analytics, preserving other mappings and
   environment values. The existing default is Eastern time. Still verify local
   day/hour buckets before activation. Device metadata alone does not configure
   these functions. No cloud function code change is required for this mapping.
4. The operator explicitly requested that `SERV-Camera` remain open. Preserve
   that choice. **Complete:** the installed operator update records
   `allow_open_camera_wifi: true` in the root-owned unit manifest and permits
   this explicit site exception. Status
   still reports `camera_wifi_secured: false`. It grants no internet transit
   and does not waive day/night/lane acceptance. An open radio provides no
   Wi-Fi authentication or link encryption. Keep manual addressing, isolated
   DHCP and camera internet blocking. Recheck RTSP after a camera power cycle.
5. **Complete:** the human-approved host installation left the app in sandbox,
   installed the shared weekday-hours parser and IP-camera hours timer,
   preserved cellular session ownership, tightened root code permissions,
   enabled key-only OpenSSH over Tailscale, permitted local camera DHCP, and
   removed the temporary raw-text viewer from Stream. Do not rerun adoption.
6. At the actual mounting position, label real vehicle passes in daylight and
   darkness. Measure recognized, unreadable and false detections; include a
   returning vehicle, two close vehicles, cross traffic and a slow/stopped car.
   Calibrate geometry for this site. Current bench geometry is full-frame with
   inherited LJS route classification in shadow; it is not an Ivar's calibration.
7. Record actual evidence, not assumptions, in root-private
   `/etc/serv-field/commissioning.json`: `device_id`, `location_id`,
   `day_view_passed`, `night_view_passed`, `lane_counting_passed`, plus test
   timestamps and a link/path to the results. `doctor` and production activation
   refuse missing/failed acceptance. Changing the view invalidates that evidence.
8. With an empty lane, activate production and verify a real pass in AWS Visits
   and Customers. Verify queue drain, MMC, correct location, deduplication and
   a second visit. Power-cycle Pi + modem + camera together, then repeat. Test
   an actual WAN outage and restored delivery. Never inject a synthetic customer
   into `ivars-001` to make the acceptance test look complete.

**Historical initial-install reference — already completed.**
Human approval is required by AGENTS.md/SECURITY.md. An agent must not run this
command on the human's behalf or supply its own approval:

```sh
sudo /usr/bin/python3 -I /home/serv/ivars-release/fieldkit/install.py \
  --approved-sha FULL_REVIEWED_40_CHARACTER_COMMIT \
  --profiles /home/serv/ivars-staging/profiles \
  --preferred cellular --lane-empty
```

The checkout must exactly match the approved commit. Do not substitute a branch
name. The installer rejects a different Pi, an already-adopted host, drifted
application code, or a different cellular supervisor. The only allowed app
migration is `config.py` from the verified bench digest to the reviewed weekly
scheduler; every other application module must match. This is deliberately not
a generic new-Pi imaging tool. A versioned private pre-install backup is kept
under `/var/backups/serv-ivars-*`; it is a recovery input, not a tested automatic
rollback. Keep Tailscale access and local recovery available during installation.

## Evidence from 21 October 2026 (Pacific)

- After the user plugged in the Pi, Tailscale SSH worked over cellular with
  Ethernet down. PR, the app, cellular recovery and camera isolation started
  automatically. This observes one boot, not repeated field cold-boot acceptance.
- Main camera reception remained about 15 fps, PR health 95, zero buffered
  frames, zero throttle flags. Camera traffic routed locally through `wlan0`.
- All 22 installed application Python files and the cellular recovery core
  match this source byte for byte. Root-owned application files/directories
  retained group-write permission bits from bench setup; the reviewed installer
  removes those bits. That permission change was subsequently installed, as
  recorded in the installed sandbox acceptance below.
- A synthetic sandbox test used the installed PR payload parser, unchanged
  normalizer/hasher and real durable Sender. AWS received a hash, created a
  customer and retained MMC. Exact retry returned duplicate without a second
  visit. Invalid keys and cross-location reads were denied. The first HTTP
  delivery took 3,228 ms on this cellular connection; this is not camera-to-cloud
  recognition latency. Detector voting and camera OCR were not part of that test.
- The production key passed heartbeat and scoped customer-read authentication;
  the production location had zero customers. No production test visit was sent.
- Architecture: 75 passes. Stream: 331 passes. AWS Python: 39 passes. Pipeline:
  106 passes and the four documented baseline failures. These do not constitute
  a clean/no-bugs certification. New field tools: 20 tests pass. The retained
  bench-viewer suite has 15 passing tests. The new image-control reader was also
  exercised on the actual C113 (all four sliders read 50). The preview refusal
  path was tested without permitting or transferring camera video over cellular.
- The running camera remains at 2304×1296, 15 fps, quality 5, 1,280 kb/s. Two
  control implementations could not raise its bitrate. A prior distant CDU
  plate occupied only 86 pixels of width, so 15-metre readability is unresolved.

The Lakewood schedule update adds 10 tests (30 field-tool tests now pass),
including every minute of a complete week, UTC/local weekday boundaries,
daylight saving, overnight windows, warmup across midnight and refusal of an
unrecognized app migration. Preflight verified 22 AppleDouble sidecars left by
the original Mac tar; the installer tolerates only their known names and binary
metadata header, while unexpected Python modules still fail. Architecture (75), Stream (331) and AWS (39)
remain passing; the pipeline retains exactly the same four baseline failures.
AWS Device metadata now contains the supplied site address, phone and hours.
All three live Lambda environment mappings read back `America/Los_Angeles`
for `ivars-001`. Evaluating the previously verified shared cloud helper with
those live mappings gives the correct summer/winter local day/hour and the
25-hour November DST day. This is configuration verification, not a real
production Visit/aggregation test. The scheduler was subsequently installed
by the human operator as recorded below.

## Installed sandbox acceptance, 21 October 2026, 23:28 Pacific

The human installed release `768983ae4177ddc0bcc555c74f215655893ecd6d`.
All installed application and operator Python files matched that release;
root code ownership and permissions passed. PR received 14.8 fps, health 95,
with zero buffered frames. Camera RTSP routed locally through `wlan0`;
cellular was the default at metric 50 and Tailscale SSH worked. The hours
timer was enabled and its oneshot completed successfully. Evaluating the
installed scheduler against the private production profile passed all 10,080
minutes of a complete week. Sandbox continues all-day capture until activation.

The installed parser/hash/Sender cloud test passed again: private sandbox
customer, descriptive MMC, queue drain, exact-retry deduplication, invalid-key
rejection and cross-location denial. Its first HTTP request took 2,526 ms;
it bypassed camera OCR, detector voting and the running app. No production
Visit was sent. `doctor` verified production authentication, local camera
routing, synchronized time and Tailscale; site acceptance remains incomplete.

Post-install inspection caught `50-cloud-init.conf` overriding the intended
SSH password setting in `60-serv-hardening.conf`. The same approved settings
were moved to `00-serv-hardening.conf`, backed up, syntax-checked and reloaded.
Effective SSH now disables password login; a new public-key session succeeded.
The source hardening script now uses that precedence and verifies readback.
The firewall allows SSH only on Tailscale and camera DHCP on `wlan0`; camera
forwarding and wrong-interface camera routes remain blocked. The open hotspot
was not reconfigured. The operator update was separately human-approved and
installed afterward, as recorded below.

**Historical operator-update reference — already completed.** The original
incremental update used this command after the exact commit was staged. It
requires its original base release and must not be rerun on the current host:

```sh
sudo /usr/bin/python3 -I /home/serv/ivars-release/fieldkit/update_operator.py \
  --approved-sha FULL_REVIEWED_40_CHARACTER_COMMIT --allow-open-camera-wifi
```

The updater requires the exact installed base release, verified application
and operator bytes, a clean approved checkout, and the sandbox configuration.
It snapshots the old operator package and unit manifest, then writes the new
operator package and explicit policy. An ordinary write failure restores old
files; sudden-power-loss recovery is not claimed. The 36 field-tool tests pass,
including an isolated filesystem test of successful adoption of this policy
and rollback after a simulated final-manifest write failure. The running app,
camera network and recognition code are not changed by this update.

## Installed operator update, 22 October 2026, 00:36 Pacific verification

The human installed release `29b16c8626b31866663bf3bf3b74513717a64cd2`.
Readback confirms `camera_wifi_open_authorized: true` while
`camera_wifi_secured: false`; acceptance checks remain mandatory. The unit is
still in sandbox with its bench identity. PR is receiving 15 fps, health 95,
zero buffered frames, and the camera route is local through `wlan0`.
The app, Stream, cellular supervisor and camera isolation are active;
Tailscale is reachable through cellular, and there are no throttle flags.
The dry-run queue is local evidence, not a production delivery count.

Still unverified: production activation, real onsite day/night
accuracy and traffic geometry, cloud local-day buckets, whole-unit cold boot
after conversion, and cellular/Wi-Fi failover with a second adapter.

The supplied PR trial expires at the end of 2026; arrange ongoing licensing
before then. This Pi kernel currently lacks the memory cgroup controller, so
Docker's memory flags do not enforce a hard RAM limit. Measured headroom is
useful but does not resolve that protection gap. Persistent idempotency across
all possible PR/app crash races is also not claimed by the existing core.
