# Serv Pi 2.1 technical change report

Date: 2026-08-10
Live deployment completed: 2026-08-11
Target: `serv-unit-001` / `serv-ljs-001` (Raspberry Pi 5)
Release branch: `codex/pi-security-hardening`

## Scope

This release merges Theo's latest tiered OCR and encounter-coalescing changes
with the vehicle-centric `/visit` backend, then hardens the Pi and AWS
contract. It preserves all existing field data and treats `/visit` as the only
authoritative physical-vehicle ledger. Detection events remain diagnostic and
must never increase vehicle totals.

## Logic and data-contract changes

- AWS now accepts `below_threshold` and `unreadable` detection diagnostics in
  addition to `group_confidence_floor`. The daily aggregator and Analytics
  expose separate counters for all three.
- `vehicle_only` remains rejected. It overlaps other detector paths and lacks
  a stable, authoritative vehicle lifecycle, so replaying it would risk double
  counting. Anonymous physical vehicles belong in `/visit` with a generated
  visit identity and an explicit plate-detection status.
- `group_confidence_floor`, `below_threshold`, and `unreadable` remain
  recognition-attempt diagnostics. They contribute to diagnostic attempt
  rates but not `total_vehicles`.
- Theo's paired best-frame telemetry is merged. Confidence pairs now come from
  one real frame instead of independent maxima from different frames, and
  each miss carries bounded gate counts, vote counts, and a random episode
  identity. AWS explicitly whitelists and type/size-bounds these additive
  fields before storage; arbitrary nested device data is discarded.
- AWS uses only the authenticated authorizer's location. A request-body
  `locationId` can no longer redirect a write into another restaurant.
- The dead-letter recovery tool defaults to dry-run, preserves the forensic
  row, reuses the original idempotency key, and excludes `vehicle_only`. A
  recovered delivery gets a fresh queue timestamp so the sender cannot purge
  an old forensic event under the normal 24-hour queue TTL.

## Pi privacy and integrity changes

- Rejected OCR logs no longer contain raw plate text. They retain only text
  length, scores, and bounding-box diagnostics.
- Queue DB, WAL/SHM files, configuration, and preview images are owner-only.
- Heartbeats report the approved Git SHA and a deterministic SHA-256 hash of
  the actual Python files being executed. This detects mixed or manually
  modified deployments.
- Executable application code moves to root-owned `/opt/serv/plate_reader`.
  Writable state stays in `/home/serv/plate_reader`; the unprivileged `serv`
  process cannot replace its own executable package.
- The long-running reader has `NoNewPrivileges=true`, an empty capability
  bounding set, strict filesystem protection, a private temp directory,
  restricted namespaces/address families, and a `0077` umask.
- Modem telemetry no longer gives the application a `NOPASSWD sudo` path. A
  confined root oneshot collects read-only QMI snapshots into `/run/serv`.

## Deployment and supply-chain changes

- The nightly updater no longer executes a script from the `serv`-writable
  checkout and never runs repository installers automatically as root.
- The installed updater is root-owned at `/usr/local/sbin/serv-update`.
- A GitHub push is not deployment authority. Deployment requires an exact
  40-character Git SHA in a root-owned approval file.
- The updater exports files from that immutable Git object, rejects symbolic
  links, blocks dependency/system changes for manual review, health-checks the
  real camera pipeline, and restores the previous package if boot fails.

## Host hardening

- A default-deny nftables policy permits inbound administration only to SSH on
  `tailscale0`; direct cellular/LAN SSH, RPC, discovery, and GUI services are
  blocked.
- SSH root login, X11 forwarding, agent forwarding, remote forwarding, tunnel
  devices, user environment files, and user RC files are disabled. Local TCP
  forwarding remains available for the loopback-only camera preview.
- Password authentication remains a temporary fallback until an independent
  key-only login is demonstrated. The current password is considered
  compromised because it was shared in chat and must not remain the fleet
  credential.
- Avahi, Bluetooth, LightDM, rpcbind, and Raspberry Pi Connect are disabled on
  this headless field unit.
- Passwordless local-console auto-login was found after the first reboot and
  removed. The original drop-in is retained mode `0600` in the protected field
  backup directory; tty1 now presents a normal password-protected login.

## Field audit evidence

- Hardware: Raspberry Pi 5 Model B Rev 1.0, 4 GB RAM, aarch64.
- Baseline load: approximately 5 FPS, 59.8 C, no throttling bits set, and more
  than 100 GB free disk. A 598-second live-camera soak collected 297 samples:
  56.0 C minimum, 57.7 C mean, 60.4 C maximum, throttle flags `0x0`, and no
  undervoltage. The ARM clock reached its nominal 2.4 GHz and scaled down when
  idle.
- Exposure found: SSH and rpcbind listened on all interfaces; host INPUT and
  FORWARD policy accepted traffic; SSH allowed password/root-key/X11/forwarding
  defaults; multiple unnecessary services ran.
- Supply-chain issue found: a root timer executed a repository script writable
  by the application user. The timer was disabled before further work.
- Deployment drift found: the live package was a mixture of tracked and manual
  files while `.deployed_sha` claimed an older build.
- Delivery state found: queue empty; 29 forensic dead letters: 16
  `below_threshold`, 4 `group_confidence_floor`, and 9 deliberately excluded
  `vehicle_only` records.
- Fifty-three user-space/security updates and the six held Raspberry Pi kernel
  packages were installed. A controlled reboot completed successfully into
  `6.18.39+rpt-rpi-2712`; no package updates remain. The previously missing
  `unattended-upgrades` package is now installed, enabled, and active with
  daily package-list and unattended-upgrade intervals. Automatic reboot is
  not enabled.

## Verification

- Pi regression suite: 19/19 passed on the target Pi.
- AWS Python contract tests: 14/14 passed locally (the deployed pre-telemetry
  release passed 10/10 again inside AWS CloudShell immediately before its
  deployment).
- Paired episode telemetry suite: 45/45 passed, including the cross-frame
  confidence-maxima regression, gate counters, vote timing, episode identity,
  legacy defaults, and unchanged confirmation behavior.
- Dead-letter recovery test: dry-run, apply, duplicate protection, missing-key
  recovery, forensic preservation, and replay exclusion passed.
- Node visit-schema test passed.
- Python compilation, shell syntax, Node syntax, and `git diff --check` passed.
- systemd unit parsing passed. The live service reached
  `FRAME_PIPELINE_HEALTHY` before and after the OS/kernel reboot, and its
  systemd exposure score is `3.4 OK`.
- AWS deployed `InsertDetectionEvent`, `AggregateDetectionEvents`, `Analytics`,
  `InsertData`, and the fail-closed patched `Heartbeat`; all five reported
  `Successful` and `Active`. The final Theo-integrated deployment is release
  `0f8b41a7ba1a84ae42d801c7f38a9902b56c3d5b`; its immediate pre-deployment
  packages are retained under `~/serv-rollbacks/20260811T042801Z` in
  CloudShell (the earlier rollback set is also retained).
- Recovery stored exactly 16 `below_threshold` and 4
  `group_confidence_floor` diagnostics in `DetectionEvents`. Queue depth is
  zero. The nine `vehicle_only` forensic rows remain preserved and unreplayed.
- The final AWS heartbeat reports build
  `0f8b41a7ba1a84ae42d801c7f38a9902b56c3d5b`, runtime hash
  `b52262473e253b38a3ff7926ae4cf24de66d75e064b65884f9cfa3f62dfd20ef`,
  temperature 57.6 C, throttle flags `0x0`, and delivery queue depth zero.
- A synthetic record under the isolated `__serv_deploy_test__` location proved
  the live Lambda stored the bounded paired confidence values, boolean, read
  count, episode ID, and whitelisted reject counter. The exact synthetic item
  was deleted immediately after verification.

## Remaining operator and field gates

1. Unlock the Mac Ed25519 key in `ssh-agent`, prove a second key-only session,
   then disable Pi password authentication.
2. Rotate the exposed Pi device API key and revoke any exposed Kaggle
   credentials through a secret-safe channel.
3. Install passive cooling and the tamper-resistant field enclosure/cabling,
   then repeat the thermal soak inside the final enclosure.
4. Define the real lane, menu-board, and exit zones at the restaurant before
   enabling presence/dwell counting, then run the labeled 30-car acceptance
   recording end to end.
