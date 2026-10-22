# LJS replacement: reviewed bench conversion, not production activation

The installer converts the existing servCamOG software stack without replacing
the SD card. It **does not** approve its own release. A human must first approve
the exact fetched channel-head SHA using the reviewed, root-installed updater.
If no root-owned updater exists, bootstrap that approved installer explicitly
as a separate human-reviewed step; do not execute an unattended mutable checkout
as root or let an agent create `/etc/serv/approved-deploy-sha`.

`scripts/install_ljs_replacement.sh --help` documents its explicit file inputs.
Every source file comes from that immutable approved Git object. MediaMTX comes
from a separately SHA256-verified ARM64 archive; Stream must already be cached
at a pinned repository digest. No moving `latest` download or existing-container
deletion is performed. A nonempty existing `stream` container stops installation
for review rather than being destroyed.

## What it installs and preserves

- The reviewed application, engine-specific PR gates, separate QMI telemetry,
  bounded cellular recovery, root-owned updater, MediaMTX and capture timer.
- Private bench configuration: `serv-unit-bench` at `bench-servcamog`. No LJS
  credential, production identity or home test visit is substituted into LJS.
- The original stopped `plate_reader` state directory, including SQLite queue,
  WAL/SHM, package, local images and configuration, is moved intact beneath a
  root-private `/var/lib/serv-replacement/backup.*` directory. The old Stream
  working directory and private license environment backup are preserved there.
  The prior UltimateALPR SDK and machine-bound token outside that state directory
  are not removed. Recovery remains possible; no SD-card wipe occurs.
- The active `/home/serv/pr-stream.env` is kept on the same physical machine.
  License validity still requires an actual running Stream check. Copying a
  config is not a license transfer and does not prove the subscription is valid.
- The existing hostname, SSH host keys and Tailscale node identity remain intact.
  The replacement does not impersonate the old Pi's node keys or tailnet IP.

The default installs and leaves camera services disabled/stopped. Explicit
`--start-bench` starts the new stack under the isolated bench identity and enables
camera startup. The bench configuration has no restaurant-hour shutdown window,
so it can be tested outside LJS opening hours. The production profile later
restores the 09:45 warmup, 10:00–22:00 Eastern schedule.

The root-owned `/etc/serv/replacement-bench.json` is both an installation record
and a runtime commissioning hold. While it exists, a root-owned `ExecStartPre`
helper refuses to start `plate-reader` with anything except the reviewed bench
device/location/API/engine. The service account only checks that the marker
exists; it cannot read or remove the mode-0600 root record. Cloud identity
isolation separately protects production. This installer never changes AWS keys
or uploads the old bench queue under another identity. A subsequent explicit
cutover must stop services, quarantine the bench queue, install the reviewed
production config, retire the old credential, remove the root hold, and then
start the replacement. This is an accidental-misconfiguration guard, not a
claim of protection from a compromised administrator or Docker-group member.

## Before calling the unit shipment-ready

1. Confirm the Stream license is valid, camera-1 receives full 1456×1088 frames
   at the reviewed 5 FPS/sample=1 baseline, loopback webhook parsing succeeds,
   and the application reaches `FRAME_PIPELINE_HEALTHY`.
2. Verify a bench heartbeat authenticates with the dedicated bench key; verify
   its `deviceId`/`locationId` in AWS without generating synthetic LJS visits.
3. Exercise an actual plate encounter and full-vehicle route with local images.
   Preserve the no-raw-plate-upload and one-physical-vehicle/one-Visit boundaries.
4. Verify temperature, current throttling, disk space, dropped frames, process
   restarts, delivery failures and the installed build hash under sustained load.
5. Keep WiFi as a rescue path while checking LTE-bound HTTPS and routing. Then
   perform a controlled LTE-only cold-boot/recovery test with an independently
   planned WiFi rollback. An active PDP session or modem registration is not
   proof that the Pi can reach AWS or Tailscale.
6. Confirm a second independent public-key SSH session before `--key-only`.
   Do not change Tailscale DNS, routes or node keys as an incidental install step.
7. Confirm which SIM/modem physically ships, its APN and account status. The
   old site's cellular failure is not proved solved merely by installing a fix.

## Onsite commissioning and cutover

Reuse the reviewed route/gate values only as a starting profile. A different
lens, mount or aim changes normalized route coordinates and pixels per plate.
Verify the full lane approach, rear plate opportunity, upper-right exit and menu
board in a fresh full-frame image before counting live LJS traffic. Measure dusk
exposure separately; do not silently activate the failed 1/300-second canary or
claim that frame rate alone eliminates motion blur.

Keep the old Pi powered off and preserve its SD card for outage/log recovery.
Use a fresh per-device LJS API key at explicit cutover, with the old key revoked,
while retaining `serv-unit-001`, `ljs-louisville-001`, unchanged normalizer/hasher,
and historical customer/Visit tables. Do not clone Tailscale private state. Verify
one live sender, cold boot, cellular HTTPS, fresh AWS heartbeat and route/KPI
behavior before leaving the replacement unattended.

If installation fails, new capture is stopped for inspection and the private
backup path is printed. Network services are not automatically rolled back and
old UltimateALPR is not automatically restarted next to Stream. Inspect the
failure while the bench WiFi rescue path remains available.
