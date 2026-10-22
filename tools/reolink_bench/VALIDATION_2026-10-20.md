# Reolink home bench validation — 20 October 2026

This is a home bench installation on `servTest`, not a restaurant deployment.
The original working checkout was preserved. Work is isolated on
`codex/reolink-bench`; the starting working changes are a separate baseline
snapshot commit, `77be46b`.

## Installed and observed

* Raspberry Pi 5, 4 GB, Debian 13 ARM64. Management remains home Wi-Fi.
* Reolink **RLC-811A**, hardware `IPC_560B158MP`, firmware
  `v3.1.0.4695_2504301440`, queried directly over its authenticated port 9000.
* Ethernet is 100 Mb/s full duplex. Persistent dedicated network:
  Pi `192.168.50.1`, camera `192.168.50.2`; DHCP advertises no router/DNS.
* Main stream is **3840x2160 H.265, 25 fps**, configured bitrate 6144 kb/s.
  Substream is 640x360 H.264, approximately 10 fps.
* The initial bounded FFmpeg probe decoded 251 main-stream frames and 101
  substream frames in ten seconds of source video, with no decoder errors.
  Main-stream decode consumed about 7.0 CPU-seconds over 11.3 wall-seconds.
  This measures decoding, not PR inference.
* PR Stream **v1.64.0**, pinned ARM64 image
  `sha256:b3acfbd966127bfc78869ccd46ed68bc4d93d674909c32f97bdc6f4da1422b24`.
  The second supplied trial activated successfully: three cameras supported,
  expiration `2026-12-31T21:46:14Z`. MMC was exercised by actual results.
* The app uses `serv-unit-bench-reolink` / `bench-reolink-house` and
  `backend_writes_enabled=false`. Its dedicated queue is `queue.dry-run.db`.
  Every observed queued request has zero delivery attempts. No AWS write was
  made and no production credential or device identity was installed.
* The webhook binds `127.0.0.1:8129`. PR's monitor is blocked from non-loopback
  interfaces by an owned nftables table. Both ports were inaccessible from
  the Mac over management Wi-Fi while local operation succeeded.

## Pipeline proof: distinguish the inputs

The live Reolink feed reaches PR. A human-labeled live vehicle pass has **not**
yet been supplied, so live-camera plate accuracy and lane behavior remain
unverified.

One explicitly synthetic on-Pi webhook produced exactly one correctly hashed
Visit, preserved MMC, and did not produce another Visit when repeated. This
checks the installed receiver/application/sender boundary, not camera OCR.

Actual recorded drive-through clips were processed by PR **on this Pi**. The
resulting local queue contained 16 replay Visits: one confirmed identification,
four insufficient-confidence vehicles and eleven no-visible-plate vehicles.
All 16 had descriptive MMC fields. File-mode replay ran faster than real time,
so it is not a drive-through dwell/timing calibration. The confirmed identity's hash was checked
against its OCR evidence entirely on the Pi. Raw OCR and audit images were
not exported. These are test event counts across several clips, including
overlapping footage, **not** a labeled unique-vehicle count or accuracy rate.

The initial dim clips produced localization/OCR scores below the existing
gates. Those gates were retained; no threshold was relaxed to manufacture a
passing identification. A daylight segment then exercised real OCR through
confirmation and on-device hashing.

All 17 queued Visits, including the separate synthetic test, passed the actual
backend Visit-schema functions locally. This verifies the outgoing contract;
it does not constitute an authenticated cloud ingestion test.

## Load and recovery

At `sample=5`, the 4K/25-fps source nominally requested five analysis frames
per second. PR health fell to 60–70% after warmup. The installed bench profile
uses **sample=6**, nominally 4.17 analysis frames per second; settled health
was generally 75–80% before the deliberate interruption tests. Reception
stayed around 24–25 fps with zero reported buffering outside outages.

PR's health is a vendor performance indicator, not a direct measured inference
FPS counter. This Pi has not demonstrated sustained five-fps inference at 4K.
Resolution was retained. Camera exposure, lighting, zoom, orientation, source
frame rate and encoding settings were not changed. The camera reports that
10-fps source output is available; that remains a separate tuning experiment.

| Test | Observed result |
|---|---|
| 55.7-second software RTSP packet stall, native recovery | Sustained recovery began 76.0 seconds after restoration; beyond the initial 60-second target. |
| Same fault after bounded watchdog installation | Sustained recovery began at 15.4 seconds; three updated healthy reports confirmed it by 35.9 seconds. Visit count stayed 17. |
| 39.0-second WAN block | Public TCP probe was blocked; PR stayed running, minimum health 75, Visit count stayed 17. IPv4 and link-local IPv6 management remained available. |
| Thirty-minute monitored bench run | Completed: 120 samples over 30.0 minutes, including the deliberate faults. Peak 62.25°C; throttling `0x0`; zero swap; at least 2639.9 MiB available RAM. |
| Pi reboot and automatic service/network recovery | Passed: new boot ID; camera processing already running at about 39 seconds uptime. App, PR, DHCP and watchdog timer returned; Ethernet address and Wi-Fi default route persisted; all 17 Visits remained. |

The first recovery checker accepted a transient `running` status too early.
That measurement was explicitly invalidated. The corrected checker requires
positive health and three distinct updated healthy reports. The first WAN
fault script also blocked link-local IPv6 SSH; independent IPv4 management
remained available and the pre-scheduled rollback removed the temporary rule.
The corrected test preserves connected IPv6 LAN/link-local prefixes and was
repeated successfully.

The watchdog restarts only the PR service, with a three-minute rate limit.
The application PID and in-memory deduplication state remained intact during
the recovery test. All Visit counts stayed unchanged. These tests used the
quiet home camera; they do not prove behavior for a car held mid-lane during
an outage. Physical Ethernet removal and camera power cycling remain untested.

Docker memory accounting on this kernel reports `0B / 0B`; that is not real
memory use or proof of an enforced container memory limit. The sampler reads
process RSS, available memory, swap and thermal status independently.
The maximum summed container-process RSS was 709.7 MiB; the app's heartbeat
reported 157.5–193.3 MiB RSS. In the final ten minutes, PR health stayed 75–80,
source reception was 25 fps and reported buffering was zero. The queue ended
with 17 Visits, four diagnostics and 24 heartbeats, all with zero delivery
attempts. This was a bench stress run with interruptions, not thirty minutes
of uninterrupted real-vehicle traffic.

## Cellular finding

The Telit **LE910C4-NF** is present, its operating mode is `online`, its SIM is
present and PIN locking is disabled. Actual QMI queries report
**not registered**, network `none`, signal `-128 dBm`, and `wwan0` has no
address. Automatic network selection is already configured. Thus SIM/account
activation has not yet produced a working cellular connection on this Pi.
LTE antenna connections and SIM-provider information were requested from the
user. No APN was invented and no modem session or default route was changed.

## Code and verification

The sender's disabled mode suppresses preflight and all HTTP delivery paths,
uses a bounded private queue, and cannot replay that bench queue into the
normal queue when delivery is enabled later. Existing configurations default
to delivery enabled. The bench app also has a systemd network policy allowing
only loopback traffic. Normalizer, hasher, identity gates and MMC's descriptive
role were preserved. All 22 installed application source files were verified
byte-for-byte against the isolated worktree.

| Check | Result |
|---|---|
| Architecture documentation | 75 passed, zero failed |
| Stream engine | 331 passed, zero failed |
| Python AWS suites | 39 passed |
| JavaScript Visit schema | 15 passed |
| Bench delivery/configuration | 6 passed |
| Watchdog state/freshness/restart limits | 7 passed |
| Pipeline features | 106 passed, **4 known baseline failures** |
| Python compile, shell syntax, diff whitespace | Passed |

See [README.md](README.md) for exact service, diagnostic and rollback commands.
The sanitized measurements are retained locally on the Mac under
`~/.codex/reolink-bench-20261020`; credentials, raw OCR and camera captures stay
under private directories on the Pi. Production rollout remains subject to
the existing human-approved updater gate.
