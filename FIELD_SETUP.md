# Ivar's location 001 — operator setup and acceptance

**Current state: cloud identities and operator software prepared; field
production activation is not complete.** Do not ship based on a green PR
process alone. Remaining gates are listed below with measured evidence.

This is a dedicated operator workflow for the existing Tapo C113 + Raspberry
Pi 5 + Sixfab unit. Its recognition core comes from the reviewed Serv source;
it is not a new recognizer or a fleet-wide installer. The current physical
hostname remains `servTest` because the existing device-specific services use
that condition. AWS identity is independent of the Linux hostname.

## Daily operation, after the reviewed install

Install Tailscale and FFmpeg (including `ffplay`) on your computer. Join the
authorized Serv tailnet and have your personal SSH public key enrolled on this
Pi. Repository access does not confer device access. Verify the Pi's SSH host
key through an existing trusted operator; never disable host-key checking.

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

On Pi: `/home/serv/ivars-staging` contains private commissioning inputs and
reports. After installation: `/etc/serv-field/profiles/{sandbox,production}.json`
are root-private. The running application keeps its standard private config
and separate production/dry-run SQLite queues. The installer refuses a
pre-existing nonempty production queue so an old device cannot be replayed as
Ivar's. No bench queue is copied into the production queue.

## Commissioning sequence

1. Confirm the restaurant's opening/closing times and IANA time zone. This
   version supports one daily window, including an overnight window, with
   15-minute capture warmup. Different weekday schedules require an extension
   before activation. Never use a guessed schedule as production configuration.
2. Prepare the production profile with `fieldkit.prepare_profiles` on the Pi,
   supplying the private production identity, time zone and confirmed hours.
   It validates through the actual application parser and changes no service.
3. Set the same time zone for `ivars-001` in the existing AWS
   `LOCATION_TIMEZONES` environment mapping for AggregateVisits,
   AggregateDetectionEvents and Analytics, preserving other mappings and
   environment values. The existing default is Eastern time. Verify local
   day/hour buckets before activation. Device metadata alone does not configure
   these functions. No cloud function code change is required for this mapping.
4. Secure `SERV-Camera` with WPA2/WPA3 and pair the camera to it again. The
   current bench hotspot is open. Do this with the Tapo phone app available and
   a working Tailscale/cellular management session. Keep manual addressing and
   isolated DHCP; do not replace the profile with NetworkManager's internet-
   sharing hotspot command. Recheck RTSP after pairing and a camera power cycle.
5. Review and install one exact commit using the human-run command below.
   This is a host install, not a Python-only fleet update. It leaves the app in
   sandbox mode, installs the IP-camera hours timer, preserves cellular session
   ownership, enables key-only Tailscale administration, permits local camera
   DHCP, and removes the temporary raw-text viewer from the Stream webhook list.
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
application code, or a different cellular supervisor. This is deliberately not
a generic new-Pi imaging tool. A versioned private pre-install backup is kept
under `/var/backups/serv-ivars-*`; it is a recovery input, not a tested automatic
rollback. Keep Tailscale access and local recovery available during installation.

## Evidence from 21 October 2026 (Pacific)

- After the user plugged in the Pi, Tailscale SSH worked over cellular with
  Ethernet down. PR, the app, cellular recovery and camera isolation started
  automatically. This observes one boot, not repeated field cold-boot acceptance.
- Main camera reception remained about 15 fps, PR health 95, zero buffered
  frames, zero throttle flags. Camera traffic routed locally through `wlan0`.
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
  a clean/no-bugs certification. New field tools: 19 tests pass. The retained
  bench-viewer suite has 15 passing tests. The new image-control reader was also
  exercised on the actual C113 (all four sliders read 50). The preview refusal
  path was tested without permitting or transferring camera video over cellular.
- The running camera remains at 2304×1296, 15 fps, quality 5, 1,280 kb/s. Two
  control implementations could not raise its bitrate. A prior distant CDU
  plate occupied only 86 pixels of width, so 15-metre readability is unresolved.

Still unverified: reviewed host installation, production activation, confirmed
restaurant hours/time zone, secured-camera re-pair, real onsite day/night
accuracy and traffic geometry, cloud local-day buckets, whole-unit cold boot
after conversion, and cellular/Wi-Fi failover with a second adapter.

The supplied PR trial expires at the end of 2026; arrange ongoing licensing
before then. This Pi kernel currently lacks the memory cgroup controller, so
Docker's memory flags do not enforce a hard RAM limit. Measured headroom is
useful but does not resolve that protection gap. Persistent idempotency across
all possible PR/app crash races is also not claimed by the existing core.
