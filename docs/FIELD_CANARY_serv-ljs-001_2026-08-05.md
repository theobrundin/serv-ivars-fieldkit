# Serv Pi 2.1.0 field canary — serv-ljs-001

Canary date: 2026-08-05

Final state: 2.1.0 installed and running with the tested recognition gates

Mode: supervised deployment, isolated video replay, thermal guard, private
ground-truth scoring, and production AWS auth/heartbeat validation

## Outcome

Version 2.1.0 is live on `serv-ljs-001`. The service passed configuration,
licensed UltimateALPR, real camera/inference, AWS authentication, and heartbeat
delivery checks. The tuned group-confidence floors improved exact plate recall
on the same bounded 120-frame sprint from 8/21 (38.1%) to 12/21 (57.1%), with
one wrong string in each run and zero false confirmations across nine
no-plate/nonstandard-placard events.

The Pi is not approved for a sustained replay or production soak without
cooling. The longer replay reached 82.9 C and asserted the current soft
temperature-limit bit after about 31 seconds. The harness killed the replay as
designed before it could continue heating the uncooled board.

## Final deployed configuration

- Installed package: `2.1.0`.
- Effective per-frame gates: detection `0.50`, OCR `0.75`.
- Effective group gates: detection `0.50`, OCR `0.75`.
- Vote: two agreeing frames within three seconds.
- Live configuration `sw_version`: `2.1.0`.
- Code rollback directory:
  `/home/serv/plate_reader_backup_20260805_115310`.
- Pre-tuning configuration backup:
  `/home/serv/plate_reader/config.json.before_group_floor_20260805T162335Z`.
- Automatic updater remains paused by the manual-override marker until this
  supervised deployment is formally accepted.

The config edit was atomic, retained mode `0600`, validated before restart,
and changed only the two explicit group floors plus the stale software-version
metadata. The legacy single group value remains in the file for compatibility,
but the explicit tested values are authoritative in 2.1.0.

## Video artifacts and isolation

The raw 2.7 GB recording was converted locally into random-access H.264 clips
and then transferred to the Pi:

| Artifact | Duration | Frames | Size | Purpose |
|---|---:|---:|---:|---|
| 30-event compact replay | 233.4 s | 1,167 | 197,408,749 bytes | Longer thermal/logic replay |
| 30-event sprint | 24.0 s | 120 | 26,822,704 bytes | Four-frame-per-event A/B recognition test |

The compact replay SHA-256 matched on both machines:
`ab2238baa900002abd5ece746b507adad546690ad83c3e012d114688964584f6`.
The sprint SHA-256 also matched:
`8611ab2149d988910ea03ce2217c128dd1bc2ccb6c4c263bc1d1d5c8d9560cfa`.

All replay runs used a private harness identity, a redacted API key, and no
sender thread. No synthetic replay visit, detection event, or plate string was
sent to AWS.

## Recognition result

| Metric | Existing group floors | Tuned group floors |
|---|---:|---:|
| Group detection / OCR floor | 0.80 / 0.80 | 0.50 / 0.75 |
| Conventional plate events | 21 | 21 |
| Exact matches | 8 | 12 |
| Wrong plate strings | 1 | 1 |
| Misses | 12 | 8 |
| Exact-match recall | 38.1% | 57.1% |
| Any-read recall | 42.9% | 61.9% |
| Non-plate / placard false confirmations | 0/9 | 0/9 |
| Raw confirms | 9 | 13 |
| Processing time for 120 frames | 33.8 s | 33.6 s |

The four gained confirmations were exact matches. The remaining wrong read is
a stable one-character substitution, so lowering the group floors did not
create it and did not make it worse.

This sprint is valid for frame-segment OCR scoring only. Hard cuts join
unrelated cars every 0.8 seconds, so its track association, dwell, drive-off,
and vehicle-only counts are not valid production KPIs. Those require the
continuous recording or a live lane.

## Thermal result

| Measurement | Existing sprint | Tuned sprint | Longer replay |
|---|---:|---:|---:|
| Peak temperature | 77.9 C | 79.0 C | 82.9 C |
| Average process CPU | 100.7% | 100.7% | 92.1% before abort |
| Current throttle samples | 0 | 0 | 1 |
| Final/trigger throttle flags | `0xe0000` | `0xe0000` | `0xe0008` |

`0xe0008` includes the current soft-temperature-limit bit. Historical
frequency-cap, throttle, and soft-temperature flags remain latched in
`0xe0000`; no current low-order bit was present after cooling back down. At the
final live check, the service had run for 2 minutes 29 seconds at 67.8% CPU,
about 271 MiB RSS, and 73.6 C.

## AWS validation

The live 2.1.0 service performed its normal startup authentication request;
AWS returned HTTP 200. The immediate startup heartbeat and the next scheduled
heartbeat both cleared the durable queue. At the final check:

- queue: 0 rows;
- dead letter: 13 preserved rows;
- service restarts: 0;
- camera/inference health sentinel: passed;
- cloud errors after the restart: none.

The 13 dead-letter rows are legacy 2.0.0 `/detection-event` records rejected by
the current backend with HTTP 400. Their outcomes are `vehicle_only` and
`group_confidence_floor`. They were preserved, not deleted or replayed. The
backend owner must decide whether those outcome types should be accepted before
any replay is attempted.

## Earlier same-day canary

An earlier code-only canary temporarily installed 2.1.0, passed real
camera/inference and license checks at about 5 fps, delivered 321 valid queued
records, quarantined the 13 permanent HTTP-400 records, and then deliberately
rolled back to 2.0.0. That rollback demonstrated the recovery path before the
final 2.1.0 deployment described above.

## Final Pi state

- `plate-reader.service`: active and healthy.
- Main process restarts since final deployment: 0.
- Installed/reporting software: 2.1.0.
- Effective gates: per-frame and group detection/OCR `0.50/0.75`, vote 2.
- AWS auth: HTTP 200; heartbeat queue drained.
- Final temperature: 73.6 C.
- Final throttle state: `0xe0000`, no current low-order bits.
- Default route during the test: cellular `wwan0`; Wi-Fi was disconnected.
- SSH management path: Tailscale.

## Required next actions

1. Install a Raspberry Pi 5 Active Cooler or a tested enclosure-specific
   active thermal solution before any long replay or production soak.
2. After cooling is installed, run the full continuous labeled recording while
   collecting exact-match accuracy, vehicle outcomes, CPU, RSS, temperature,
   clock, throttling, and per-frame latency.
3. At the real drive-through, calibrate the lane, plate, and menu-board zones
   from a privacy-safe preview before judging vehicle recall or drive-off logic.
4. Resolve the AWS schema contract for `vehicle_only` and
   `group_confidence_floor` before replaying the 13 dead-letter rows.
5. Rotate the disclosed SSH password and replace password access with a key.
6. Move large future validation transfers off cellular where practical.
