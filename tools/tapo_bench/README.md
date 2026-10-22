# Tapo C113 on the isolated home bench

This is the user-authorized `servTest` home bench, not a production rollout.
The camera is reserved at `10.42.0.36` (MAC `58:d8:12:ad:3e:f1`) on the Pi's
`SERV-Camera` Wi-Fi. The Pi's local address is `10.42.0.1`.

See [the sandbox validation report](VALIDATION_2026-10-20.md) for the expanded
pipeline, privacy, retry, outage, and resource checks, plus the outstanding
physical night/vehicle acceptance and deployment concerns.

Authenticated RTSP probes on 20 October 2026 decoded `/stream1` at
2304×1296 H.264, nominal 15 fps, and `/stream2` at 1280×720 H.264, nominal
15 fps. Both ten-second decodes completed without errors after hotspot
internet sharing was removed. The main-feed probe decoded 150 frames.

## Running configuration

* `serv-tapo-stream.service` reads the local main feed with the existing
  pinned PR Stream 1.64.0 image and existing trial environment file. Its
  camera ID is `tapo-bench`, sample interval is 3 (nominal 5 analysis fps),
  and its webhook remains `127.0.0.1:8129`.
* The existing `serv-reolink-app.service` and bench device identity are
  retained. Only capture geometry changed to 2304×1296 and source frame
  rate to 15. Hashing, confidence gates, descriptive-only MMC, and disabled
  backend delivery are unchanged. Existing local queue counts are cumulative
  across both camera experiments; they are not all Tapo observations.
* Private source credentials, config, license state, and probe reports live
  under `/home/serv/tapo-bench`, mode 0700; credential/config files are 0600.
  Never copy those files into Git. The previous app config is retained as
  `config.before-tapo.json` in that private directory.
* The PR service conflicts with the Reolink stream service, preventing
  simultaneous recognition containers. Reolink Stream, its watchdog timer,
  and its Ethernet DHCP service are disabled while Ethernet connects to
  the home router.
* `serv-tapo-watchdog.timer` checks the recognizer every ten seconds. Its
  implementation preserves the Reolink bench's bounded recovery policy;
  only the camera ID, IP, service, and private state directory differ.
  Host-specific helpers are installed root-owned in `/opt/serv-tapo-bench`.
* `status.py` prints allowlisted health and aggregate local queue counts,
  excluding raw OCR, authenticated URLs, and images.

## Network recovery and isolation

The router Ethernet profile is `serv-home-router`, UUID
`78a73061-d9f0-4acc-a1cf-b07b2661266d`, with DHCP and autoconnect enabled.
Its current lease is `192.168.39.111`; this address may change. Cellular is
enabled at boot and is a fallback internet route. Tailscale SSH remains
`serv@100.77.200.119`; use `HostKeyAlias=servtest.local` to check the existing
verified host key. Wired SSH was tested independently of cellular.

At the user's request the hotspot is currently open, on 2.4 GHz channel 1.
The original secured profile is backed up root-only at
`/etc/serv-bench-network/serv-camera-before-open.nmconnection`. The password
in `hotspot.json` belongs to that earlier profile and is not currently active.

The user explicitly authorized a temporary internet exception for Tapo app
onboarding. It used runtime-only DHCP gateway/DNS options and narrow
Wi-Fi-to-uplink NAT/forward rules. A real Mac client received `10.42.0.55`
and HTTPS 200 through the hotspot, then returned to GloverFi. The exception
was explicitly closed before authenticated camera tests. Persistent policy
blocks camera transit and routes camera addresses only through `wlan0`.
Pi management and license access may still use Ethernet or cellular.

`/run` is mounted `noexec`: any transient rollback must invoke
`/bin/bash /run/serv-tapo-onboarding/close.sh`, not execute the script directly.
The corrected timer command was verified, and explicit closure was verified
against live nftables rules. A reboot clears runtime sharing exceptions.

The temporary Mac recovery alias `192.168.50.3` is no longer needed.
Removing it requires Mac administrator access:
`sudo ifconfig en0 inet 192.168.50.3 -alias`.

## Measured validation

On 20 October 2026, a two-minute live-camera soak produced nine samples:
PR health 95–100%, received frame rate 14.7–15.0 fps, zero buffered frames,
zero swap use, and a maximum temperature of 50.7°C during that interval.
The private report is
`/home/serv/tapo-bench/soak-20261021T040743.jsonl`.

During a separate twenty-second streaming interval, Wi-Fi received
3,286,997 bytes while cellular received 60 bytes and transmitted 1,787 bytes.
The camera route was directly through `wlan0`, and live firewall inspection
confirmed camera forwarding and wrong-interface camera routes were blocked.
These measurements establish the local video path; they do not claim the
Pi sends no management or license traffic over cellular.

All five installed Tapo helper/unit files matched the repository hashes.
The stream, watchdog timer, app, isolation guard, and cellular supervisor
were active and enabled. Repository checks passed: architecture 75,
Stream engine 331, and AWS 39. Pipeline features reported 106 passes and
the four documented baseline failures.

At 21:13 Pacific, the queue contained only new heartbeats since Tapo startup;
there was no fresh Tapo vehicle/plate event yet. Backend delivery attempts
remained zero. Earlier queue visits and hashes belong to prior experiments
and cannot establish Tapo optical acceptance.

Real vehicle/plate acceptance, lane geometry, night readability, and camera
power-cycle recovery remain separate hardware acceptance checks. The camera
account/RTSP proof does not establish those results.
