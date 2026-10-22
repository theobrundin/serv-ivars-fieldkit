# Reolink home bench

This installation is for the verified home host `servTest`, reached as
`serv@servtest.local`. It is separate from the fleet application and updater.
Do not run these commands on a restaurant unit. Production installation still
requires the human approval step described in `AGENTS.md` and `SECURITY.md`.

## What runs

```
Reolink main RTSP (4K H.265) -> Plate Recognizer Stream ARM64
    -> 127.0.0.1:8129 -> existing detector -> normalizer -> hasher
    -> private queue.dry-run.db (no backend requests)
```

PR already owns capture for the `pr_stream` engine. Its native RTSP input is
the source selection; an additional Python decoder would create a redundant
video pipeline. MediaMTX is unnecessary for a camera that already serves RTSP.
`NullCamera` supplies dimensions to the application, not real image frames.
The application's `FRAME_PIPELINE_HEALTHY` log therefore does **not** prove
camera freshness. Check the independent PR status and actual events.

The code leaves normalization, hashing, identity gates, MMC treatment and
deduplication unchanged. The bench uses a separate device/location identity,
placeholder API credentials, an invalid backend hostname and
`backend_writes_enabled=false`. Sender disables preflight and delivery at the
HTTP boundary. Its separate, bounded SQLite queue cannot be replayed into the
normal queue by enabling delivery later. The app service also blocks all
non-loopback IP communication.

The bench enables full-frame plate eligibility. The example's LJS route
geometry remains **uncalibrated shadow evidence**, never an authoritative
drive-through route for this camera. Do not promote these coordinates.

## Confirmed connection

* Pi 5, 4 GB RAM, Debian 13 ARM64; Ethernet 100 Mb/s full duplex.
* Management Wi-Fi: `192.168.39.152/24`, gateway `192.168.39.1`.
* Camera-only Ethernet: Pi `192.168.50.1/24`, Reolink `192.168.50.2`.
* Observed camera MAC: `ec:71:db:a6:d3:f2`.
* Camera verified through port 9000: RLC-811A, hardware `IPC_560B158MP`,
  firmware `v3.1.0.4695_2504301440`.
* Main path `/Preview_01_main`: 3840x2160 H.265, about 25 fps, configured 6144 kb/s.
* Sub path `/Preview_01_sub`: 640x360 H.264, about 10 fps.
* RTSP port 554 works. Camera HTTP/HTTPS/ONVIF ports were closed.

The persistent NetworkManager profile is `serv-reolink-camera`.
`serv-reolink-dhcp.service` reserves the camera address and advertises neither
a gateway nor DNS. The original Ethernet DHCP profile is retained. This
dedicated DHCP service is intentional because the camera still uses DHCP.
The verified data path matches a powered injector: Pi -> DATA IN, PoE OUT ->
camera. The accessory model/label remains unverified.

## Install on this home Pi

The source lives in `/opt/serv-reolink`, owned by root and readable by `serv`.
Deploy only `plate_reader/`, `config.example.json` and `tools/reolink_bench/`;
exclude local credentials, media, caches and unrelated working files. Do not
copy the original working checkout over a live installation.

Dependencies installed on Debian 13:

```sh
sudo apt-get install --no-install-recommends docker.io docker-cli \
  python3-opencv python3-numpy python3-requests python3-psutil ffmpeg busybox nftables
sudo systemctl enable --now docker
sudo bash /opt/serv-reolink/tools/reolink_bench/install_network.sh --check
sudo bash /opt/serv-reolink/tools/reolink_bench/install_network.sh --apply
```

`install_network.sh` is host-specific, checks independent Wi-Fi management
and subnet overlap, records its owned profile, and supports repeat apply and
rollback. It does not bridge/NAT the camera onto the home network.

Credentials stay on the Pi under `/home/serv/reolink-bench` (directory 0700):

* `camera-credentials.json`: owned by `serv`, mode 0600, JSON `username` and
  `password`. The probe's `--credentials` mode prompts without echoing passwords.
* `pr-stream.env`: owned by `serv`, mode 0600; `TOKEN=...` and
  `LICENSE_KEY=...`. Use the license value without the label `STREAM -`.

Never paste either file into logs, a commit, a chat or a diagnostic bundle.
The generated Stream INI contains an encoded camera URL and is also private.

```sh
mkdir -m 700 -p /home/serv/reolink-bench /home/serv/plate_reader
cp /opt/serv-reolink/tools/reolink_bench/{probe_stream,status,capture,smoke_webhook,check_outage}.py /home/serv/reolink-bench/
PYTHONPATH=/opt/serv-reolink python3 /opt/serv-reolink/tools/reolink_bench/configure.py
sudo install -d -o serv -g serv -m 0700 /var/log/serv
sudo install -m 0644 /opt/serv-reolink/tools/reolink_bench/*.service /etc/systemd/system/
sudo install -m 0644 /opt/serv-reolink/tools/reolink_bench/*.timer /etc/systemd/system/
sudo docker pull platerecognizer/alpr-stream@sha256:b3acfbd966127bfc78869ccd46ed68bc4d93d674909c32f97bdc6f4da1422b24
sudo systemctl daemon-reload
sudo systemctl enable --now serv-reolink-app.service serv-reolink-stream.service
sudo systemctl enable --now serv-reolink-watchdog.timer
```

Configuration generation refuses to overwrite existing files. Startup uses a
pinned Stream v1.64.0 ARM64 image, local webhook, MMC, vehicle mode, trajectory,
12-second prediction window, 90-second memory and 30-second dwell/reconnect
caps. No continuous video, CSV or JSON-lines recognition export is enabled.
The home profile uses `sample=6`: nominally 25/6 = 4.17 analysis frames per
second, before any processing drops. The initial `sample=5` trial fell to
60–70% PR health after warmup. This change retains 4K; it is an explicit
sampling tradeoff, not a claim of measured 4.17-fps inference throughput.
PR's downloaded per-camera mask must be checked independently; a startup
message about loading zones does not prove one exists or is appropriate.

The Stream container uses host networking to reach the loopback webhook.
`serv-reolink-monitor-guard.service` first installs an owned nftables table
blocking non-loopback access to monitor port 8001. Do not remove that table
while PR runs. Do not forward the monitor to the LAN: it can show credentials
and raw recognition results.

`serv-reolink-watchdog.timer` checks independent PR health every ten seconds.
The first measured packet-stall recovery took 76 seconds and included a
misleading transient `running` state. The watchdog requires a fresh status,
positive health and reception, tolerates startup, and restarts only PR when a
30-second unhealthy episode persists while the camera's RTSP port is reachable.
It respects intentional stops and systemd failures, limits restart attempts to
one per three minutes, and leaves the app's voting/deduplication state running.
This does not prove continuity for a real car held mid-lane through an outage;
that physical test remains required before field use.

## Check and diagnose

The helper copies in `/home/serv/reolink-bench` find protected credentials next
to themselves. The code copies under `/opt/serv-reolink` are for review.

```sh
python3 /home/serv/reolink-bench/probe_stream.py
python3 /home/serv/reolink-bench/status.py
python3 /home/serv/reolink-bench/status.py --soak-minutes 30
python3 /home/serv/reolink-bench/capture.py             # one private JPEG
python3 /home/serv/reolink-bench/capture.py --seconds 10 # private bounded clip
python3 /home/serv/reolink-bench/smoke_webhook.py        # explicitly SYNTHETIC
sudo python3 /home/serv/reolink-bench/check_outage.py camera
sudo python3 /home/serv/reolink-bench/check_outage.py internet
```

The decode probe opens each stream with TCP and kills a hung process group;
it prints sanitized metadata, frame counts, timing and thermal status. Capture
is capped at 30 seconds/100 MB, retains five diagnostic files and suppresses
authenticated decoder stderr. These files remain private on the Pi.

The status tool exports only selected health fields and aggregate queue
counts. It never prints raw OCR, images, credential URLs or arbitrary provider
errors. `received_fps` is **source reception**, not inference throughput.
`buffered_frames` is PR's percentage indicator, not a queue-length measurement.
Health is a vendor metric; do not present a guessed inference rate as measured.
The Pi kernel may disable memory cgroups: Docker's `0B / 0B` is not real memory
usage or evidence that its configured memory limit is enforced. Use measured
process RSS, available RAM, swap and temperature from the status tool.

The synthetic smoke test posts only on Pi loopback, checks the expected
on-device hash, MMC and duplicate suppression, and leaves one tagged local
test record plus `synthetic-smoke.json`. It does not validate camera OCR.
Actual recorded-footage recognition must be labeled separately from a live
vehicle pass. Raw audit JSON/JPEG files stay under
`/home/serv/plate_reader/audit_cases` (newest 60, 24-hour retention).

For a read-only model/encoding query over Reolink's port 9000, the optional
`camera_info.py` helper uses `reolink-aio==0.21.17` from the private
`/home/serv/reolink-bench/camera-lib` directory. It directly calls the Baichuan
read methods, does not enable camera HTTP, and exports no serials/credentials.
Install the optional helper dependency as `serv` with
`python3 -m pip install --target /home/serv/reolink-bench/camera-lib reolink-aio==0.21.17`,
then run `python3 /opt/serv-reolink/tools/reolink_bench/camera_info.py`.
The current camera offers 10 fps at 4K as a possible later tuning experiment;
the installed camera remains at 25 fps. No exposure, lighting, zoom,
orientation or encoding setting was changed in this bench installation.

The two outage tests install only their own temporary nftables output table,
schedule an independent two-minute rollback first, and block RTSP for about
55 seconds or WAN for about 35 seconds.
Camera mode stalls RTSP; internet mode preserves IPv4 management and connected
IPv6 LAN/link-local prefixes and blocks WAN egress. Each reports recovery from vendor status and before/
after Visit counts. This is reported stream recovery, not a measured
camera-to-Pi image latency. Never run fault injection during a labeled live
vehicle pass. Logs/results are private JSON files on the Pi.

PR Docker logs are root-only and rotate at 2 MB x 2; they can contain sensitive
vendor output. Do not copy unfiltered Docker or audit logs off this Pi.
Application logs rotate at 5 MB plus five backups. PR webhook cache is native
vendor behavior; inspect its size during listener outages. No claim is made
that the opaque vendor decoder implements the handoff's custom one-frame
buffer, PTS/session tracking or precise stale-frame rejection contract.

## Stop and rollback

```sh
sudo systemctl disable --now serv-reolink-watchdog.timer
sudo systemctl disable --now serv-reolink-stream.service serv-reolink-app.service
sudo systemctl stop serv-reolink-monitor-guard.service
sudo nft delete table inet serv_reolink_monitor
sudo bash /opt/serv-reolink/tools/reolink_bench/install_network.sh --rollback
```

The guard deliberately keeps its table when merely stopped. Remove it only
after PR has stopped. Rollback restores the recorded original Ethernet DHCP
profile; activation can time out when the camera is its only peer. Wi-Fi is
unchanged. Preserve private test results until reviewed; do not enable backend
delivery to empty the bench queue. Docker packages and downloaded image are
retained and can be reused.

## Before any field deployment

Verify camera and injector models/firmware, adjust framing manually, label
day/night vehicle passes at the actual distance, and calibrate recognition and
lane geometry. Physically unplug/replug Ethernet and power-cycle the camera;
software interruptions do not substitute for those checks. Confirm recovery
after Pi reboot and internet loss, no false/duplicate visits across an outage,
and sustained resource/performance headroom with real moving vehicles. A
license supporting three cameras does not establish Pi throughput for three.

Primary vendor references: [configuration](https://guides.platerecognizer.com/docs/stream/configuration/),
[monitoring](https://guides.platerecognizer.com/docs/stream/monitoring/),
[performance guidance](https://guides.platerecognizer.com/docs/stream/help/troubleshooting/).
