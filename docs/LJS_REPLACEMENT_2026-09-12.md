# Louisville replacement: servCamOG

## Status and scope

The host and cloud logical-identity handover completed on 12 September at
approximately 16:25 UTC. **Capture remains shipping-held; this is not a claim
of complete shipping readiness.** The final verification found a capture-hours
logging/sandbox startup bug; its reviewed application repair is prepared
separately and must be installed before shipment.

Verified: hostname `serv-ljs-001`; original `Devices/serv-unit-001` and
`ljs-louisville-001` preserved; only the device credential rotated; old key
rejected with HTTP403; new key authenticated. Genuine LTE-bound LJS heartbeats
returned HTTP200 at 16:32:46 UTC and, with both Wi-Fi and Ethernet absent,
16:34:58 UTC. No vehicle events or invented recognition counters were sent.
Fresh Tailscale/modem/host telemetry replaced the old device telemetry. The
temporary `serv-unit-bench` record was set inactive, retaining its history and
preventing future bench-monitor alerts. Already queued emails may still arrive.

The physical Tailscale IP remains `100.68.10.100`; the actual MagicDNS name is
`serv-ljs-001-1.tail37021d.ts.net` because the old node still owns the unsuffixed
name. No SSH/Tailscale private identity was cloned. A normal shipping boot will
release the current runtime hold; do not reboot for additional home testing.

Historical preparation details follow.
The intended final logical identity remains `serv-unit-001` at
`ljs-louisville-001`, so customer hashes, return history and dashboard location
remain unchanged. `normalizer.py` and `hasher.py` are not modified.

The owner's clarified delivery requirement is a plug-in replacement for the
existing Louisville unit, not a second restaurant device. The final hostname
and intended tailnet display name are `serv-ljs-001`; the existing
`Devices/serv-unit-001` item must be updated in place, never deleted/recreated
or replaced by the bench item. Existing customer/Visit history and location
relationships stay intact. Bench mode is temporary acceptance-test isolation,
not the configuration to hand over as a completed replacement.

Logical identity is distinct from credentials and hardware telemetry. A new
per-device API key can authenticate the same existing device record; do not
copy the old heartbeat, alert state, cellular address or health readings as
replacement telemetry. Preserve this physical Pi's SSH/Tailscale private keys.
The desired tailnet name must be resolved deliberately at cutover rather than
cloning the failed node's private identity or assuming the old IP transfers.

The production configuration builder is paired with the separately approved
`scripts/install_ljs_identity_cutover.sh` and the local cloud operator tool
`tools/prepare_ljs_identity_cutover.py`. Changing the IDs alone or removing the
bench guard alone is not a cutover. Any final remote commissioning must require
no shell commands, SD-card work or disassembly by the restaurant installer;
onsite mounting/view validation is still necessary. Availability of these tools
does not mean the host/cloud cutover has actually run.

The replacement was reached over key-authenticated SSH at `100.68.10.100` on
12 September 2026. It initially ran the old UltimateALPR bench application,
had no `/opt/serv` production installation or `serv-update` executable, and
required a sudo password. It has an IMX296 camera, ARM64 Raspberry Pi OS
Trixie, a Telit USB modem and a cached ARM64 Plate Recognizer Stream image.
Wi-Fi was the preferred route; LTE had its own address and default route.

A read-only LTE-bound HTTPS request reached AWS with TLS and returned HTTP
404 from an unauthenticated route. This proves some LTE transport worked at
that moment; it does **not** prove authenticated application delivery,
cellular-only DNS, recovery or cold-boot readiness. Tailscale currently owns
`/etc/resolv.conf`, advertises public Cloudflare resolvers, and the saved
underlying resolver points to the bench Wi-Fi router. Do not overwrite that
resolver file or assume Wi-Fi-assisted DNS proves cellular independence.

## Deliberate separation during preparation

- Bench authentication is `serv-unit-bench` / `bench-servcamog`, using a new
  private device key. It is not authorized as Louisville traffic.
- The builder can stage a production configuration, but the replacement
  installer rejects production IDs and never rotates the live LJS key.
- Existing bench state (including SQLite WAL/SHM and audit images) is kept on
  the Pi in a private backup. It must not be replayed under the LJS identity.
- Preserve this Pi's own SSH host keys, Tailscale node state and machine-bound
  UltimateALPR token. Never copy the old Pi's private host/network identity.
- Do not rename/retire the old tailnet node or revoke its key as a shortcut to
  connectivity. Retire its **AWS device credential at final cutover** so two
  physical units cannot send new traffic as `serv-unit-001`.
- Keep the old failed unit/SD card intact. Any outstanding historical queue
  recovery is a separate reconciliation, not part of bench activation.

## Software baseline

The generated configuration preserves the reviewed LJS application gates and
authoritative route classifier, 1456×1088 full-sensor capture, 5 source FPS,
Stream `sample=1`, broad `regions=us`, selected original full-frame evidence,
`max_prediction_delay=12`, `memory_decay=90`, `max_dwell_delay=30`, and
`report_static=no`. MMC remains descriptive and outside identity hashing.

The camera starts with short/automatic exposure, not the unvalidated fixed
twilight shutter profile. Replacement optics and lighting must be checked
before tuning. Old route polygons are a starting calibration, **not proof
that a different mount/lens has the same geometry**.

MediaMTX is the pinned official ARM64 v1.21.0 archive:

`a8113b5928ba1a934b81557b61b8a07954b76921a4b567d54c7f086f8b39d9a2`

The existing cached Stream artifact is pinned as:

`platerecognizer/alpr-stream@sha256:b3acfbd966127bfc78869ccd46ed68bc4d93d674909c32f97bdc6f4da1422b24`

Version/license/MMC status still require runtime verification. Do not infer
valid licensing from an image being cached or a credential file existing.
Do not automatically pull a moving image tag or remove an existing container.
RTSP and the raw-plate webhook remain loopback-only; MediaMTX's newly enabled
MoQ listeners must be explicitly disabled.

## Installation authority

Use `scripts/install_ljs_replacement.sh` only after the human approves the
exact full Git SHA. The installer checks the root-owned approval, exports the
immutable commit into a private root staging area, checks artifact digests,
and backs up the old bench setup. It cannot approve itself.

This is a **manual host install**, not an ordinary Python-only OTA. Updating
`.deployed_sha` through the normal updater cannot install a new cellular
supervisor, systemd units, MediaMTX or resolver policy. The updater must block
such releases until the separate reviewed install is completed.

No automatic full-OS upgrade, SD reflash, blind modem USB reset, daily reboot,
firewall disable, Tailscale security bypass or production activation is part
of this operation. Wi-Fi stays available during preparation.

## Acceptance before shipping

Do not mark the replacement ready until all of the following are recorded:

1. New root-owned cellular executable/helper and installed systemd unit match
   the reviewed release. Check the running process, not just the Git SHA.
2. Plate Recognizer license is valid **on this hardware**, camera/Stream health
   is good, received FPS is about five, resolution is 1456×1088, and MMC is
   available. Subscription transfer/reset may require the PR account owner.
3. A real local camera frame and controlled vehicle/plate test traverse the
   pipeline. Only hashes and safe attributes go to the isolated bench device.
4. Bench heartbeat reaches AWS with the new credential. No Louisville Visit
   or DetectionEvent is created by testing.
5. With a time-bounded automatic Wi-Fi restoration scheduled first, test
   **cellular-only** DNS, authenticated AWS heartbeat and a fresh SSH session
   over Tailscale. Keep the current SSH session as rescue until verified.
6. Test failed PDP/host-state recovery without Wi-Fi assistance. Validate
   repeated failure withdraws the unhealthy cellular default, alternative
   connectivity is retained, and healthy cellular can recover again.
7. Perform a controlled service restart, warm OS reboot and a genuine cold boot (including
   modem power removal if testing unowned legacy sessions). Confirm there is
   no dependence on stale Wi-Fi DNS or retained modem state.
8. Run a sustained camera/LTE soak. Record temperature, current throttling,
   restarts, queue depth and timestamps. Mock tests are not hardware proof.
9. Confirm exactly which modem/SIM ships in the replacement. If transferring
   Louisville's old modem/SIM, the complete assembly must be retested.

## Shipping and onsite cutover

- Do not ship while the replacement still requires hardware/console repairs.
- At the owner's explicit request, prepare the exact production identity
  before shipping. The human-approved identity installer stops capture/app,
  preserves the whole bench state (including queue/WAL/SHM), installs the
  production profile with fresh queue state, and enables the normal next-boot
  services without restarting cellular, Wi-Fi or Tailscale.
- The current-boot hold `/run/serv-ljs-shipping-hold` blocks app, MediaMTX,
  capture-hours and update units; the stopped Stream container has restart
  policy `no`. This is deliberately a NEXT-BOOT activation, not a geofence.
  **Any subsequent boot, including a bench reboot, releases the hold.** Do not
  reboot for more home testing after the identity handover.
- Retrieve the root-owned safe `/etc/serv/identity-cutover-ready.json`, verify
  its exact configuration/key fingerprints and that services remain held, and
  conditionally rotate only `api_key` in the existing `Devices/serv-unit-001`
  item using the local operator tool. It refuses another device/site, metadata
  drift, a different current key, or mismatched host proof. Confirm a safe
  authenticated heartbeat over LTE and verify the old key is rejected before
  shutdown/shipment. Never put the old telemetry onto the replacement.
- At installation, preserve the intended orientation and height. Verify a
  full-resolution live frame, focus, plate pixel size, lane path, menu-board
  zone and upper-right exit. Check long vehicles and two-car occlusions.
- Dashboard credentials are separate and must not be changed. No raw plates
  are sent to AWS. The cloud tool only changes the camera key, never recreates
  the device, and never rewrites customer/Visit history.
- Assign the requested hostname/tailnet display name `serv-ljs-001`, resolving
  any old-node name conflict deliberately without cloning its private state;
  logical AWS identity does not require the old tailnet IP.
- Restore LJS hours: America/New_York, capture warmup 09:45, open10:00,
  close22:00. Night capture stops, while networking/heartbeat stay available.
- Verify qualified traffic and rejected routes against actual lane traversals
  before relying on the old polygons or reporting a recognition percentage.

This procedure aims for a physically simple swap, but software alone cannot
verify lens focus or mounting geometry before the camera is installed.

## Prepared-release verification

On 12 September, the staged bench credential passed the application's normal
empty-heartbeat authentication preflight from servCamOG (HTTP200). No vehicle
event was sent and the active old bench configuration was not replaced.

Offline tests passed: cellular37; installer21; replacementconfig15; Stream
launcher7; benchidentity5; architecture66; Streamengine331; AWS39. The broader
pipeline suite remains at100pass/4documented pre-existing failures. Local
tests used Python3.11; the Mac's default Python3.9 cannot run the application.

The cellular recovery helper now retains real WDS client/packet handles,
bounds subprocesses, distinguishes a busy telemetry lock from network failure,
repairs host configuration before recreating an owned session, and requires
three failed checks before recovery/two successes before route promotion.
It validates modem/interface association and preserves other network paths.

These changes are not yet on-device acceptance. Unknown allocations pause
instead of leaking unlimited WDS clients. Legacy unowned sessions are not
force-stopped with invented handles. Warm reboot can preserve modem power
while clearing `/run` ownership, so test that case explicitly. DNS remains
externally managed until cellular-only cold-boot behavior is verified. No
automatic radio/USB reset or OS reboot is included as an untested workaround.

## Bench acceptance observations — 12 September, 05:43–05:52 UTC

The human ran the checksum-verified installer and approved exact release
`4ad0ab71e0a145daf3a13d80305217e1d43e59b5`. Installation completed;
the private old-state backup is `/var/lib/serv-replacement/backup.2sLceE`.
The package phase paused at needrestart's service-selection dialog, not an
installation failure. No installer rerun was required.

Verified on the running replacement:

- Stream v1.64.0 accepted its one-camera license and reported camera health
  100%, 1456×1088 resolution and 5.0–5.2 received FPS. Actual MMC output and
  plate recognition remain untested; license acceptance alone does not prove
  those features.
- MediaMTX, Stream, plate-reader and Tailscale remained active. The application
  reached `FRAME_PIPELINE_HEALTHY`; no application traceback or error was
  observed in this short interval. Temperature was 54–55°C with
  `get_throttled=0x0`. Queue and dead-letter counts were both zero.
- A real RTSP frame decoded on-device to exactly 1,584,128 grayscale pixels.
  Mean luminance was 5.66/255 and 93.7% of pixels were below 10/255. The user
  confirmed the room was dark. No exposure changes were made and no frame or
  raw plate data was exported.
- Installed root-owned cellular helper/unit and unchanged normalizer/hasher
  hashes matched the reviewed source. The supervisor process was the new
  `/usr/local/sbin/serv-cellular-supervisor`, not the legacy watchdog.
- Bench AWS identity was `serv-unit-bench` / `bench-servcamog`. The production
  hold remained root-owned mode0600; LJS activation/key rotation did not occur.

For the short LTE-only test, a root five-minute restoration timer was armed
for the existing Wi-Fi profile before disconnecting wlan0 at approximately
05:49:33 UTC. While wlan0 had no address, Ethernet was down and the only
external default was wwan0:

- Fresh Tailscale SSH connections succeeded.
- Normal AWS/Tailscale DNS resolution succeeded. A separate direct DNS query
  to 1.1.1.1 returned a matching response, RCODE0 and two answers; this did not
  depend on a local resolver cache or the bench Wi-Fi router.
- Two explicit authenticated bench heartbeat requests returned HTTP200.
  Read-only AWS checks confirmed `last_heartbeat` at 05:49:53.896 UTC and
  05:50:46.030 UTC.
- A supervised cellular-service restart preserved working LTE. Stream still
  reported health100 and 5 FPS afterward.

Wi-Fi restoration was started early; its address and metric20 default were
verified again at 05:51:39 UTC along with a fresh SSH connection and all four
core services active. The temporary restoration timer was stopped. The
installed nightly update-check timer had been enabled but not started by the
installer; it was started without changing release approval or update policy.

**Not a shipping pass yet:** supervisor status remained
`connected_legacy_unowned`, `owned_session=false`, with zero new connection
attempts. This proves healthy legacy-session preservation and cellular-only
transport, not creation/recovery of an owned CID/packet handle. A genuine
Pi-plus-modem cold boot, owned-session recovery, warm reboot, LTE-only cold
boot, sustained soak, lit plate/MMC test and final commissioning remain.
The user confirmed the entire current camera/modem/SIM assembly will ship.

### Subsequent boot: cellular acceptance failed, Wi-Fi rescue works

The initial 05:54–05:56 UTC offline check was not a failed boot: the user
clarified that only `sudo poweroff` had been run. Do not treat that interval
as an outage or a power-cycle failure.

After the user later reported power restored, inspection at 12:55–12:58 UTC
found a new boot ID `651f6812-23d8-4372-bbaf-8e00c5ee6940`, approximately
16 minutes uptime, and working Wi-Fi/Tailscale/AWS. Early wall-clock service
timestamps were stale until time synchronization at 12:40:10 UTC; Docker's
apparent seven-hour age is not evidence of a seven-hour soak. Use monotonic
boot timing for this incident.

- Camera/Stream remained healthy at 1456×1088, 5 FPS, health100. Core services
  had no automatic restarts, temperature54.3°C, throttled0x0, and both queue
  counts were zero. AWS bench heartbeat had resumed at 12:53:56.945 UTC.
- **Cellular failed to acquire an IPv4 address/default.** Only Wi-Fi provided
  an external default. The supervisor started at boot+4.84 seconds and logged
  `WDS allocation uncertain; paused until modem generation changes or operator
  recovery` at boot+15.01 seconds. It remains active but intentionally latched;
  service-active is not cellular-health proof.
- USB modem enumeration occurred at boot+2.82 seconds; QMI/wwan0 appeared at
  about+3.50 seconds. The Telit identified as LE910C4-NF. raw_ip was Y and
  control/network sysfs parents matched. ModemManager was not installed.
- The surviving root qmi-proxy belonged to cellular-connect.service. Later
  NAS telemetry succeeded and showed T-Mobile LTE registered/PS-attached;
  registration does not supply the missing host IP/data route.
- The installed qmicli version was1.36.0. The first allocation's underlying
  stderr was not retained by the supervisor, so its exact failure phase is
  not proven. Starting before modem control readiness is a hypothesis, not a
  confirmed diagnosis. Do not clear uncertain ownership or invent a packet
  handle to force a connection.

Root diagnostics require a fresh user sudo authentication after reboot.
Keep Wi-Fi available; no new software, network reset, release approval or
production activation was performed during these read-only checks. Shipping
remains blocked pending diagnosis, reviewed correction and repeated acceptance.
