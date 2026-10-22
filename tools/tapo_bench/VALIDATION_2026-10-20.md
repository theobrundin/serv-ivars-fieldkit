# Tapo C113 sandbox validation — 20 October 2026 (Pacific)

This report covers the home `servTest` Pi and the Tapo C113 at `10.42.0.36`.
It is a sandbox acceptance test, not approval to deploy at a restaurant.
The application retains its bench identity and disabled backend delivery.
Production credentials, hashing, identity gates, and fleet update approval
were not changed.

## Result and limits

Local RTSP reception, the running Plate Recognizer process, application
hashing, duplicate suppression, MMC, durable queuing, HTTP retry behavior,
and the backend Visit contract have been exercised. A fresh real Tapo plate
identification, dark-condition recognition, and automatic light/dark switching
have **not** yet been established. No authenticated AWS ingestion or dashboard
update was attempted; the bench deliberately has no production backend key.

Keep these inputs distinct:

- One new live event was classified as a white motorcycle with no visible
  plate. Its real-world correctness is unverified because the scene has not
  been human-labeled. It is not proof of plate recognition or counting accuracy.
- One explicitly synthetic loopback webhook produced one correctly hashed
  Visit and preserved MMC. Repeating the same webhook produced no second Visit.
  Geometry was adjusted to fit the Tapo's measured image dimensions.
- The earlier 20 Visits predate Tapo operation and include replay/synthetic
  testing. They are not Tapo optical results. Prior on-Pi recorded-video tests
  exercised real PR OCR, but cannot substitute for this camera's day/night test.

## Measured checks

| Check | Result |
|---|---|
| Camera identity | C113, hardware 1.0, firmware 1.2.4 Build 260605 Rel.15668n, read from authenticated ONVIF. |
| Main RTSP | H.264, 2304×1296, nominal 15 fps. Ten-second decode: 150 frames, no errors. |
| PR runtime | Pinned Stream 1.64.0 image, main camera feed, sample interval 3. The nominal analysis request is 5 fps; source reception and vendor health do not measure actual inference throughput. |
| Code identity | Installed normalizer, hasher, detector, Stream adapter, and sender match the reviewed checkout byte for byte. |
| Privacy boundary | Webhook binds loopback; monitor protected by firewall. TCP 8129 and 8001 were unreachable from the Mac on the home LAN. |
| Live sandbox webhook | Correct expected SHA-256, MMC retained, repeat webhook suppressed. Raw candidate and audit media stayed on Pi. |
| Local HTTP delivery sandbox | A separate temporary queue and loopback receiver exercised the real Sender: HTTP 503 retained the actual synthetic Visit; HTTP 200 drained it after normal backoff. Retry payload and idempotency key were unchanged. The running app's delivery remained disabled. |
| Backend contract | All 22 accumulated Visit records passed the actual local backend identity/appearance/route parsing functions; no AWS requests. |
| Queue privacy | Three accumulated recognized Visits have valid 64-character hashes; anonymous Visits have no identity hash; no raw-plate/image named fields found. Zero backend attempts and zero dead-letter rows. |
| WAN loss | 38.5-second software block of Ethernet WAN and cellular. Public TCP probe failed as intended; PR remained running, health at least 95, reception at least 14.9 fps. Local management and camera connectivity persisted. |
| Camera connection loss | 55.6-second RTSP packet block. PR health reached zero. Healthy updated reports resumed 20.26 seconds after restoration; three distinct healthy reports confirmed recovery by 40.51 seconds. Watchdog restarted PR; app PID remained unchanged. Visit count remained 22. |
| Fault cleanup | Each fault had an independent 120-second systemd rollback. All temporary fault tables were removed; persistent camera-isolation and monitor guards remained. |
| Ten-minute monitored run | Completed: 41 samples over 600.0 seconds, including deliberate faults. Peak 52.35°C, at least 3002.1 MiB available RAM, zero swap, throttle flags always `0x0`. Final health 95, reception 14.9 fps, no reported buffering, zero backend attempts. |
| Pi warm reboot | Passed: verified new boot ID; all seven checked services active and PR receiving 14.2 fps with health 100 by 37.0 seconds uptime (60.4 seconds after scheduling reboot). All 22 saved Visit IDs/hashes survived. Ethernet, camera hotspot, and cellular addresses returned. |
| Post-reboot isolation and cellular | Both persistent firewall guards restored, no failed systemd units, cellular supervisor connected, and explicitly cellular-bound HTTPS returned 200. Camera forwarding remained blocked. |

A stale failure flag on the disabled Reolink watchdog was signal 15, from its
prior stop. It was recorded and cleared; the active Tapo watchdog is separate.

## Image controls and darkness

Authenticated ONVIF exposes brightness, contrast, saturation, and sharpness,
each from 0 to 100. Each was independently changed from 50 to 51, read back,
and restored to 50. No lasting image changes were made. ONVIF advertises no
PTZ service, no focus movement, and no manual exposure/shutter, white-balance,
or IR-cut setting. Its encoder options currently advertise only 15 fps,
quality 1–5, and bitrate 256–2048 kb/s; the actual main encoder remains at
quality 5 and 1280 kb/s. None of those encoder settings was changed.

The separate Tapo control API rejected the supplied camera-account login.
There were no automatic authentication retries or temporary suspension.
Spotlight/night-mode state and control through that API remain unverified.

Seven sampled, downscaled frames from the current unlabeled scene were stable:
mean luminance approximately 132/255, mean saturation approximately 43–44/255,
about 1.1% very dark pixels and 3.4% near-white clipping. The image was in color.
Those values are scene-level measurements, not plate readability, ambient lux,
shutter speed, or evidence of a successful darkness transition. Images were
processed on Pi and were not exported.

TP-Link documents an automatically switched IR-cut filter for this model and
ambient-light-driven switching when Auto mode is selected. That is a hardware
capability, not proof the current unit has Auto selected or switches correctly.
The fixed image-adjustment sliders do not report the firmware's exposure time.
A controlled lights-on → lights-off → lights-on observation and real vehicle
passes are still needed. The user was asked to supply those physical conditions.

## Deployment concerns

1. **Nighttime plate acceptance remains open.** Automatic night vision does
   not establish sharp moving-plate images. This interface offers no manual
   shutter control; motion blur, plate glare, distance, and framing need tests
   at the intended installation. Do not lower confidence gates to turn weak
   images into reported successes.
2. **Lane geometry is uncalibrated.** Full-frame bench eligibility and inherited
   shadow-only route geometry are not a drive-through route calibration. The
   unverified motorcycle event may be correct or a false positive.
3. **Sandbox delivery is deliberately local.** AWS authentication, ingestion,
   aggregation, and dashboard visibility need a separate staging identity and
   explicit end-to-end acceptance before rollout. Successful local schemas and
   mocked AWS tests are not a deployed-cloud test.
4. **The hotspot is open at the user's request.** Password-protected pairing
   and the documented production host-hardening steps are still required for
   a field deployment. The current bench also permits wired management. The
   built-in Wi-Fi radio serves the camera hotspot, so management depends on
   Ethernet or cellular; simultaneous GloverFi client access is not configured.
5. **Docker memory limits are not enforced by this kernel.** Its cgroup
   controllers omit `memory`. Measured RAM headroom is useful, but the unit's
   Docker memory flags do not protect against a future runaway allocation.
6. **Recovery has an observation gap.** A quiet-scene software outage with no
   extra Visits does not prove correct handling of a car mid-lane. Physical
   power removal, camera restart without internet, and mid-lane recovery remain
   separate acceptance cases. Camera-to-confirmed-hash latency has not been
   measured with a real pass; the PR prediction window is configured at 12 seconds.
7. **License and scale are bounded.** The supplied PR trial expires at the end
   of 2026. Three licensed camera slots do not establish three-camera throughput
   on this Pi. This test uses one live camera.

## Software checks

The reviewed checkout's architecture (75), Stream engine (331), and Python AWS
(39) checks passed earlier in this same bench session. Pipeline features had
106 passes and the four documented baseline failures. This sandbox audit also
ran bench delivery/configuration (6), release hardening (19), and JavaScript
Visit schema (15), all passing.

The four baseline failures concern re-confirm/misread suppression expectations,
`near_match` suppression reporting, and `group_confidence_floor` reporting. They
were not introduced or repaired here. The installed live duplicate-webhook test
passed independently; neither fact makes the other disappear.

Private evidence remains under `/home/serv/tapo-bench`: `synthetic-smoke.json`,
`sandbox-http-delivery.json`, `queue-privacy-check.json`, `internet-outage.json`,
`camera-outage.json`, `scene-baseline-metrics.json`,
`soak-20261021T042045.jsonl`, `ten-minute-soak-summary.json`,
`warm-reboot-acceptance.json`, and image-control reports.
The actual queue is `/home/serv/plate_reader/queue.dry-run.db`. No raw OCR or
camera imagery belongs in this repository or a shared report.

Primary references: [C113 specifications](https://www.tp-link.com/us/home-networking/cloud-camera/tapo-c113/),
[TP-Link day/night mode guide](https://community.tp-link.com/en/home/forum/topic/846488),
[Plate Recognizer camera setup guidance](https://platerecognizer.com/camera-setup-for-best-anpr/).
