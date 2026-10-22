# FILE_INFRASTRUCTURE.md

> Historical v2.0.1 inventory. It is retained for provenance, but its
> `[UNCHANGED]` status labels do not describe the August 2026 hardening work.
> Use the current README and the August 2026 technical change report for the
> operational architecture.

Codebase reference for the Serv Technologies plate reader v2.0.1.
A more polished version of this document is also available in the repo root as **FILE_INFRASTRUCTURE.docx**.

## Status legend

Each file is tagged with one of:

- **[NEW]** — introduced in this release
- **[UPDATED]** — existed before but modified in this release
- **[UNCHANGED]** — carried forward without modification

---

## 1. Two locations, one codebase

- **Source repo**: `/home/serv/serv-pi/` — where the git clone lives. Full tree below. Where you run `install.sh` from.
- **Runtime location**: `/home/serv/plate_reader/` — what `plate-reader.service` actually executes from. Populated by `install.sh` from the source repo's `plate_reader/` subfolder.

Editing the source repo does not immediately change what is running; `install.sh` is the promotion step.

---

## 2. Root files

| File | Status | Purpose |
|---|---|---|
| `README.md` | [UPDATED] | Top-level intro; layout diagram updated for new scripts/units. |
| `DEPLOY.md` | [UPDATED] | Deployment runbook. Fixes 3 known installer failure modes; adds camera pre-check. |
| `NORMALIZATION_SPEC.md` | [UNCHANGED] | Locked v1.0 rules for plate string normalization. Deliberately isolated from code changes. |
| `FILE_INFRASTRUCTURE.md` | [NEW] | This file. |
| `FILE_INFRASTRUCTURE.docx` | [NEW] | Polished Word version of this document. |
| `requirements.txt` | [UNCHANGED] | Pinned Python deps. |
| `config.example.json` | [UNCHANGED] | Config template with placeholders. |
| `config.json` | [UPDATED] | Live credentials stripped — now matches config.example.json format. Fill in per unit before deploy. |
| `preview_stream.py` | [UNCHANGED] | Standalone MJPEG camera server on :8000 for aim/focus checks. |

---

## 3. `plate_reader/` — runtime package

All [UNCHANGED] in this release.

| File | Purpose |
|---|---|
| `__init__.py` | Package marker. `__version__ = "2.1.0"`. |
| `main.py` | Entry point. Boots app, runs detection loop, handles SIGTERM. |
| `config.py` | Loads and validates `/home/serv/plate_reader/config.json`. |
| `camera.py` | Picamera2 wrapper. Saves `zone_preview.jpg` on boot. |
| `detector.py` | fast-alpr wrapper + confidence thresholds + two-frame voting. |
| `normalizer.py` | Implements NORMALIZATION_SPEC.md rules. |
| `hasher.py` | SHA-256 with boot-time self-test against locked vectors. |
| `sender.py` | Cloud HTTP client + local SQLite retry queue. |
| `heartbeat.py` | 120-second background heartbeat thread. |
| `telemetry.py` | Tailscale + Sixfab + system stat collectors. |
| `log.py` | Central logging setup. |

---

## 4. `scripts/` — shell utilities

### `scripts/install.sh` — [UPDATED]
Idempotent installer. Copies code to runtime location, installs deps, drops placeholder config, installs sudoers fragment for qmicli, installs and enables all three systemd units.

**Changes in this release:**
- **New step 0**: runs `dos2unix` and `chmod +x` on every shipped `.sh` before doing anything else. Prevents the "command not found" failure family when the repo was downloaded/unzipped instead of git-cloned.
- **New step 7c**: installs and enables `network-priority.service`.

### `scripts/bringup.sh` — [UNCHANGED]
Post-install PASS/FAIL/WARN check across config, Python deps, camera, cellular, Tailscale, NTP, cloud auth, systemd, disk. Exit non-zero on any FAIL. Output goes to Theo for handoff.

### `scripts/cellular-connect.sh` — [UPDATED]
Long-running Sixfab LTE watchdog. Restarts the QMI session if connectivity drops.

**Changes in this release:**
- Route metric on `wwan0` default route: **200 → 50**. Pairs with `network-priority.sh` (eth0 = 700). Ensures cellular always wins default route when both interfaces are up.

### `scripts/network-priority.sh` — [NEW]
Enforces cellular-first, ethernet-fallback default routing. Sets `eth0`'s NetworkManager metric to 700, installs a dispatcher hook so the metric re-applies on every eth0 up event, and reconfirms `wwan0` at metric 50 as safety net. Idempotent.

Introduced to support the deployment configuration where the PoE splitter feeds both power (USB-C) and data (Ethernet) into the Pi at the same site.

---

## 5. `systemd/` — service units

| File | Status | Purpose |
|---|---|---|
| `plate-reader.service` | [UNCHANGED] | Main service. Runs as `serv` user from `/home/serv/plate_reader/`. Restart with burst limit. |
| `cellular-connect.service` | [UNCHANGED] | Runs `/usr/local/bin/cellular-connect.sh` as root. Restart=always. |
| `network-priority.service` | [NEW] | Oneshot at boot. `After=NetworkManager.service network-online.target`, `Before=plate-reader.service`. |

---

## 6. Runtime flow on boot

1. **systemd** reaches `multi-user.target`; starts NetworkManager (eth0 if plugged) and `cellular-connect.service`.
2. **cellular-connect.sh** waits for `/dev/cdc-wdm0`, brings up `wwan0`, starts QMI session on APN `super`, installs default route via `wwan0` at metric **50**.
3. **network-online.target** reached once any interface has a default route.
4. **network-priority.service** runs once. Sets eth0 metric to **700**, reconfirms wwan0 at **50**, installs dispatcher hook.
5. **tailscaled** starts. Uses whichever interface currently wins as default (normally cellular).
6. **plate-reader.service** starts. Loads config, self-tests hasher, opens camera, loads fast-alpr, auth preflight, starts sender + heartbeat threads, enters detection loop.
7. **Steady state**: ~5 fps capture, confirmed plates hashed and sent to `/visit`, failed detections to `/detection-event`, heartbeats every 120s.

---

## 7. Known issues fixed in this release

| # | Issue | Fix |
|---|---|---|
| 1 | `sudo ./scripts/install.sh: command not found` — CRLF or missing exec bit | install.sh step 0 normalizes both; DEPLOY.md now uses `sudo bash scripts/install.sh` |
| 2 | Cascading errors from partial install ("No such file or directory", "Unit not found") | DEPLOY.md adds a `systemctl list-unit-files` verification step before proceeding |
| 3 | Camera ribbon issues buried in bringup output | DEPLOY.md adds standalone `rpicam-hello --list-cameras` check with reseat/dtoverlay guidance |
| 4 | Route priority unpredictable with dual network paths | cellular metric 200 → 50; new network-priority.sh sets eth0 metric to 700 |
| 5 | Live API key was in shipped `config.json` | config.json now ships with `REPLACE_WITH_` placeholders like config.example.json |

---

## 8. Path reference

| Path | Purpose |
|---|---|
| `/home/serv/serv-pi/` | Source repo (git clone) |
| `/home/serv/plate_reader/` | Runtime location (populated by install.sh) |
| `/home/serv/plate_reader/config.json` | Per-device config. chmod 600, owned by serv:serv |
| `/home/serv/plate_reader/zone_preview.jpg` | Camera aim verification image saved on each boot |
| `/usr/local/bin/cellular-connect.sh` | Deployed cellular watchdog |
| `/usr/local/bin/network-priority.sh` | Deployed route priority script |
| `/etc/systemd/system/plate-reader.service` | Main service unit |
| `/etc/systemd/system/cellular-connect.service` | Cellular watchdog unit |
| `/etc/systemd/system/network-priority.service` | Route priority oneshot |
| `/etc/NetworkManager/dispatcher.d/90-eth0-metric` | Hook that reapplies eth0 metric on each connect |
| `/etc/sudoers.d/serv-qmicli` | Passwordless sudo for `serv` to run qmicli (telemetry) |
| `/var/log/serv/` | Runtime logs + bootstrap.log |
| `/boot/firmware/config.txt` | Contains `dtoverlay=imx296` |
