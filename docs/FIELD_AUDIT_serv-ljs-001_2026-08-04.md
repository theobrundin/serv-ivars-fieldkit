# Serv Pi field audit - serv-ljs-001

Audit time: 2026-08-04 17:19-17:23 EDT
Mode: observational SSH audit; no service restart, code deployment,
configuration change, queue mutation, or application data change. Normal SSH
shell history may retain the audit commands.

## Release decision

Do not deploy the 2.1.0 canary or run an additional inference benchmark until
active cooling is installed. The existing 2.0.0 service is meeting its 5 fps
target, but the Pi is operating at the thermal-control boundary and has
already throttled since boot.

## Hardware and operating system

- Raspberry Pi 5 Model B Rev 1.0, 4 GiB RAM.
- Sony IMX296 camera, 1456 x 1088, 10-bit, advertised 30 fps mode.
- No kernel cooling device was exposed; no active cooler/fan was detected.
- Debian 13 (trixie), Linux 6.18.34+rpt-rpi-2712, aarch64.
- Python 3.13.5.
- Root filesystem: 117 GiB, 10% used.

## Thermal and power result

- Initial temperature: 78.5 C.
- Ten samples over 50 seconds: 77.4-79.6 C under the live recognition load.
- Configured maximum ARM clock: 2.4 GHz.
- Observed clock during the sample: approximately 1.5-1.8 GHz.
- `get_throttled=0xe0000`: no throttle condition was active at the sampled
  instants, but ARM frequency capping, throttling, and the soft-temperature
  limit have all occurred since boot.
- The under-voltage history bit was not set.
- Pi 5 external input reading: approximately 5.109 V.
- No under-voltage message appeared in the current kernel journal.

Conclusion: heat is the immediate constraint. The PoE splitter did not show a
power fault during this audit, although it should remain part of the longer
canary telemetry.

## Running service

- `plate-reader.service` active since 09:37 EDT, with zero service restarts.
- Application process: about 88.3% CPU and 295 MiB RSS at the audit snapshot.
- Logs reported a stable 5.00 fps during every sampled minute.
- Installed software reports 2.0.0.
- Repository and deployed SHA:
  `2ebf6bbd96132f4dd958f9be4ef692f5c348aa0e`.
- UltimateALPR SDK SHA:
  `972506c84ad286bb64331943682084a078f43494`.
- Repository worktree was clean.
- The old systemd service runs at `Nice=0`; 2.1.0 has not been installed.

## Queue and network

- Delivery queue: zero rows.
- No dead-letter table exists in the 2.0.0 schema; 2.1.0 adds it.
- Tailscale was running and healthy.
- Default network route was cellular (`wwan0`). Ethernet and Wi-Fi were down.
- The 2.7 GB validation video should not be uploaded over this cellular route.
  Use local USB storage or a temporary non-cellular LAN route, or transfer a
  carefully prepared high-quality 5 fps validation derivative.

## Current configuration observations

- UltimateALPR selected; vehicle and color classifiers enabled.
- VMMR/make-model and other heavy classifiers disabled.
- Full-frame ALPR ROI; dwell zone disabled.
- Two agreeing frames are required by the installed configuration.
- Global near-match suppression is enabled at edit distance 1. Release 2.1.0
  disables this by default because it can merge two real nearby plate strings
  when it is not constrained to one vehicle track.
- Application configuration validation passed under the installed 2.0.0 code.

## Required next actions

1. Install a Raspberry Pi 5 Active Cooler, or an enclosure-specific active or
   thermally coupled solution appropriate for the final environmental seal.
2. Reboot only in a maintenance window so historical throttle flags reset.
3. Run a 30-minute live load and require no current or historical thermal or
   under-voltage flags. Target sustained temperature below 75 C; below 70 C is
   preferable for environmental margin.
4. Obtain the actual plate strings for the 21 conventional plate-visible cars
   in `dtfootagefinal.mp4`. The supplied PDF contains times and missing-plate
   labels, not plate text.
5. Place the validation video on the Pi without using cellular data.
6. Run the same labeled clip against 2.0.0 for a baseline, then deploy 2.1.0
   with backup/rollback and rerun the identical clip.
7. Separately test the live IMX296 capture path; a prerecorded clip does not
   validate camera exposure, mount geometry, lane zones, or real capture cost.
