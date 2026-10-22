# Isolated camera Wi-Fi and Sixfab recovery: home bench

**Current bench overrides:** the user switched to Tapo C113, requested an open
hotspot, and used router Ethernet to recover management access. The installer
baseline below remains the original secured camera-network setup. See
`../tapo_bench/README.md` for current network roles, explicit temporary
onboarding sharing, verified closure, and the `/run` noexec rollback requirement.

This is the explicitly authorized `servTest` home bench, with
`serv-unit-bench-reolink` and backend delivery disabled. It is not a field
installer or production approval. The normalizer, hasher, confidence gates,
production cellular helper, and production updater are unchanged.

## Network boundaries

| Link | Address / purpose | Internet transit |
|---|---|---|
| `eth0` | Reolink, `192.168.50.1/24` | Blocked |
| `wlan0` in AP mode | `SERV-Camera`, `10.42.0.1/24` | Blocked |
| `wwan0` | Sixfab APN `super`, dynamic address | Pi internet |
| `tailscale0` | SSH administration | No advertised camera routes |

The AP uses WPA2/AES and a private generated password, retained in
`/etc/serv-bench-network/hotspot.json` and the mode-600 NetworkManager keyfile.
DHCP assigns `.20`–`.80`, with reservations in `camera-hosts`. It advertises
neither a default gateway nor DNS. It uses manual IPv4 addressing rather than
NetworkManager shared mode; IPv6 on the AP is disabled.

The independent `inet serv_camera_isolation` nft table drops forwarded traffic
entering or leaving either camera interface, including IPv6. Camera subnet
destinations are also blocked from leaving the Pi on a different interface.
This prevents an absent camera route from falling through to cellular. The
Pi can still initiate local RTSP connections. A NetworkManager dependency
loads the guard before networking starts. Reloads replace the table atomically;
stopping the guard service leaves its rules in place. Bare cellular inbound
connections are blocked except established replies, ICMP and Tailscale UDP.
Use `systemctl reload serv-camera-isolation` for policy updates; restarting a
required guard can stop its NetworkManager dependent.

These rules prevent camera transit to the internet. They are not a general
information-flow sandbox for every root process on the Pi. Keep PR webhooks
loopback-only and do not configure a video relay/upload. The existing PR
monitor remains blocked on non-loopback interfaces. The bench application
continues to queue locally with backend delivery disabled.

## Cellular and DNS

`serv-bench-cellular.service` runs the unchanged repository QMI supervisor via
`cellular.py`, installed root-owned under `/opt/serv-bench-network`. Its private
runtime is `/run/serv-bench-cellular`; it retains exact CID/packet ownership,
bounded recovery, locking and uncertain-allocation protection. The initial
manual test's known session was explicitly adopted, never guessed.

Wi-Fi has metric 600 while available; healthy cellular has metric 1600. In AP
mode, cellular is the internet default. `systemd-resolved` receives cellular
DNS directly from modem settings. NetworkManager provides DNS on the home
Wi-Fi link; Tailscale is configured with `accept-dns=false` and no accepted or
advertised subnet routes.

Stopping/restarting the supervisor preserves a healthy session.
`serv-bench-cellular-shutdown.service` runs after its owner stops during an
orderly shutdown and releases only its recorded session. Unexpected power loss
and physical modem power cycles still need hardware acceptance.

## Install and cutover

Dependencies are Debian `libqmi-utils`, `dnsmasq-base`, `systemd-resolved`,
`nftables`, `iproute2`, and the official Tailscale package. The initial installer
requires the current private manual-test ownership file; it is not a generic
fresh-image installer. Review the files, place them plus the unchanged
`scripts/serv-cellular-supervisor` in a root-owned staging directory, then run
`install_bench.py`. It refuses another modem manager or a previous installation.
It stages the AP with autoconnect disabled. Do not rerun it as an update tool.

After verifying a separate Tailscale SSH connection with home Wi-Fi actually
disconnected, activate the AP from that Tailscale connection:

```sh
sudo python3 /opt/serv-bench-network/hotspot.py start
```

A five-minute independent systemd timer restores the saved home Wi-Fi UUID.
Verify fresh Tailscale SSH, the AP address, DHCP, cellular DNS/HTTPS, and camera
isolation before committing:

```sh
sudo python3 /opt/serv-bench-network/hotspot.py commit
```

Commit enables AP autoconnect and cancels the timer. To return to home Wi-Fi:

```sh
sudo python3 /opt/serv-bench-network/hotspot.py rollback
```

The rollback intentionally leaves camera isolation and cellular management
installed. Original Wi-Fi and Reolink profiles are preserved. The saved home
Wi-Fi UUID and host identity make this tooling specific to this bench.

## Wyze commissioning

Activate/update the camera using home Wi-Fi, then verify RTSP on the actual
camera/app. Move it to `SERV-Camera` and reserve its observed MAC address before
changing the recognition source. There is no temporary camera-to-cellular
internet exception. If pairing or startup requires internet, that is an
acceptance failure to investigate; do not quietly open the firewall.

Verify the exact app-provided RTSP path/credentials privately on the Pi. Probe
the actual resolution, codec and frame rate. Compare one inference source at
a time because the existing 4K Reolink workload has limited compute headroom.
Preserve hashing, confidence gates and backend isolation. Test real vehicle
passes, night readability, camera/Pi restart, Wi-Fi interruption, and startup
with the internet absent before treating Wyze as a working replacement.

## Validation

`test_isolation.py` creates disposable network namespaces on Linux. It proves
the simulated WAN path works before loading the exact production policy,
then verifies local camera TCP remains usable while IPv4/IPv6 forwarding and
wrong-interface camera routing are blocked. Its packets never reach the real
internet. Cleanup removes only its own namespaces.

The core cellular suite is `tools/test_cellular_recovery.py`. Use the normal
architecture, Stream, AWS and pipeline suites for repository regression checks.
Four existing pipeline-feature failures remain the established baseline.
