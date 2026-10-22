# serv-pi

Plate-reader software for Serv Technologies camera units.

**Ivar's location 001:** start with [FIELD_SETUP.md](FIELD_SETUP.md).
The `./serv` operator tool connects to the Tapo/Sixfab Pi through Tailscale,
checks health, opens a bounded preview with explicit cellular consent, and
controls reviewed sandbox/production profiles. See the acceptance report in
that guide before calling the unit ready for field deployment.

Target hardware: Raspberry Pi 5 + IMX296 global shutter camera + Sixfab LTE HAT.
Target OS: Raspberry Pi OS 64-bit (Bookworm).
Python: 3.11+ (system Python on Bookworm).

## What this does

- Captures frames from the IMX296 camera
- Runs Plate Recognizer Stream in the shipping configuration; legacy engines remain available
- Applies the engine-specific confidence gates and encounter voting described in ARCHITECTURE.md
- Tracks the vehicle box as the authoritative lifecycle, including vehicles with no readable plate
- Normalizes the plate string per **Plate Normalization Specification v1.0** (see `normalizer.py`)
- SHA-256 hashes the normalized string on-device — raw plate text never leaves the Pi
- POSTs the confirmed identification to `/visit` on the Serv cloud API with auth headers and full latency/confidence telemetry
- POSTs failed detection attempts to `/detection-event` so the cloud can measure identification success rate
- Sends a heartbeat with health, Tailscale, and cellular telemetry every 2 minutes
- Persists sends before delivery in a priority SQLite queue with retry scheduling, idempotency keys, and bounded dead-letter retention
- Reports Pi temperature, throttling, process resources, per-frame latency percentiles, network health, and queue health
- Reads all per-device config from `/home/serv/plate_reader/config.json`

## Repo layout

```
serv-pi/
├── README.md                       # this file
├── DEPLOY.md                       # step-by-step deployment runbook
├── NORMALIZATION_SPEC.md           # locked v1.0 plate normalization rules — DO NOT CHANGE
├── FILE_INFRASTRUCTURE.md          # per-file responsibilities and status
├── requirements.txt                # pinned base Python dependencies
├── requirements-fast-alpr.txt      # optional fast-alpr/ONNX dependencies
├── config.example.json             # template config — copy and fill in
├── config.json                     # device-specific config (git-ignored in prod)
├── preview_stream.py               # standalone camera preview MJPEG server (for aim/focus checks)
├── plate_reader/                   # the runtime package
│   ├── __init__.py
│   ├── main.py                     # entry point: starts threads, runs main loop
│   ├── config.py                   # load + validate config.json
│   ├── camera.py                   # Picamera2 wrapper
│   ├── detector.py                 # fast-alpr + voting logic
│   ├── normalizer.py               # plate string normalization (locked spec)
│   ├── hasher.py                   # SHA-256 hashing
│   ├── sender.py                   # cloud HTTP client + local retry queue
│   ├── heartbeat.py                # background heartbeat thread
│   ├── telemetry.py                # tailscale + sixfab + system stat collectors
│   └── log.py                      # logging setup
├── scripts/
│   ├── install.sh                  # idempotent installer (deps, code copy, systemd units)
│   ├── bringup.sh                  # one-shot post-install verification report
│   ├── field_audit.sh              # read-only, secret-sanitized field snapshot
│   ├── cellular-connect.sh         # Sixfab/Telit LTE watchdog (long-running)
│   └── network-priority.sh         # enforces cellular > ethernet default route
└── systemd/
    ├── plate-reader.service        # main service
    ├── cellular-connect.service    # cellular watchdog
    └── network-priority.service    # oneshot: apply route metrics at boot
```

## Deployment

See **DEPLOY.md** for the full runbook. Short version:

1. Clone this repo onto the Pi
2. Copy `config.example.json` → `/home/serv/plate_reader/config.json`, fill in device credentials
3. Run `sudo ./scripts/install.sh`
4. Run `./scripts/bringup.sh` — must print all-green before the unit ships
5. Reboot, verify the service starts on its own, ship

## Failure surface (what to check first)

When something is broken, in this order:

1. `sudo systemctl status plate-reader` — is the service running?
2. `sudo journalctl -u plate-reader -n 200 --no-pager` — what does the log say?
3. `cat /home/serv/plate_reader/config.json` — is the config valid? (api_key etc.)
4. `tailscale status` — is the remote access path alive?
5. `sudo -n /usr/local/sbin/serv-qmi-telemetry signal` — is cellular signal readable through the fixed-function wrapper?
6. `bash /home/serv/serv-pi/scripts/bringup.sh` — do strict config, camera, engine, auth, and service checks pass?

For remote aim/focus, `preview_stream.py` listens only on loopback. Tunnel it
instead of exposing unauthenticated camera imagery:

```bash
ssh -L 8000:127.0.0.1:8000 serv@<PI>
# On the Pi: python3 /home/serv/serv-pi/preview_stream.py
# On your computer: http://127.0.0.1:8000
```

## Versioning

Current version: **2.1.0** (declared in `plate_reader/__init__.py`).
