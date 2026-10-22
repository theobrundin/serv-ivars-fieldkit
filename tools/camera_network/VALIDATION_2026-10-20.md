# Home camera network validation — 20 October 2026 (Pacific)

The user authorized cellular startup/recovery, an isolated Wyze camera hotspot,
and explicit prevention of camera streaming over cellular. All work was on
`servTest`, the home Reolink bench, with backend delivery disabled. Production
identity, application gates, hashing and fleet deployment authority were not
changed. The Wyze is physically available, but its firmware and RTSP details
have not yet been supplied; no Wyze streaming or plate-read result is claimed.

## Verified

- Sixfab APN `super`; actual modem registered on T-Mobile LTE. Session ownership
  was transferred from the exact private manual-test CID/handle into the bounded
  supervisor. No invented identifiers or uncertain-allocation latch clearing.
- Core cellular supervisor is unchanged; all 47 recovery tests passed.
- Cellular DNS is provided through `systemd-resolved`; Tailscale connected to
  the user's existing account, with camera subnet routing disabled.
- Home Wi-Fi was actually disconnected. Fresh SSH through Tailscale succeeded,
  `wwan0` was the only internet default route, DNS explicitly queried through
  `wwan0` worked, and cellular-bound HTTPS returned 200. The independent
  two-minute timer restored home Wi-Fi.
- Service restart preserved the session. Explicit stop/release/start created
  a new packet handle and restored healthy cellular. Orderly shutdown releases
  the exact owned handle after the supervisor stops.
- First reboot restored cellular DNS, Tailscale, camera isolation/DHCP, and
  both Reolink application/recognition services. At the first check around
  37 seconds uptime, cellular already had an address and its probation route;
  it subsequently returned to connected status.
- `SERV-Camera` activated at `10.42.0.1/24`, 2.4 GHz WPA2/AES. Fresh Tailscale
  SSH and cellular HTTPS succeeded while the hotspot occupied `wlan0`.
  Autoconnect was then enabled and the five-minute rollback timer cancelled.
- A second reboot with AP autoconnect enabled restored `SERV-Camera` at
  `10.42.0.1`, cellular as the sole internet default, all network/camera
  services, and fresh Tailscale SSH. Cellular-bound HTTPS returned 200 at the
  first successful check around 35 seconds uptime. An earlier SSH attempt
  during boot timed out; it was not treated as a completed recovery.
- Disposable network-namespace tests proved the simulated WAN was reachable
  before filtering. With the exact firewall loaded twice, local camera TCP
  still worked while camera IPv4/IPv6 forwarding and a deliberately incorrect
  camera route were blocked. Counters recorded six forwarded packet drops and
  one incorrect-route drop. No test packets used the real internet.
- A DHCP client in a disposable namespace acquired `10.42.0.24` from the actual
  DHCP configuration. Neither router nor DNS options were advertised.
- Live 20-second traffic observation: Ethernet received 15,280,163 bytes and
  sent 29,766 bytes; cellular received 9,131 and sent 6,507 bytes. The camera
  route resolved directly to `eth0`, and actual camera forwarding/misroute
  counters remained zero. This is evidence for that measured period, not a
  claim that every possible host process is sandboxed against video upload.
- Existing Reolink recognition remained live: PR 1.64.0, sample 6, health 80
  in the observed snapshot, no buffering, no throttling, no swap. Backend
  attempts remained zero. No raw OCR or camera images were exported.
- Repository checks: architecture 75 passed; Stream 331 passed; AWS 39 passed;
  pipeline features 106 passed with the four established baseline failures.

## Corrections found during validation

- The first synthetic IPv6 baseline probe needed more than a one-second
  neighbor-discovery window. The test was corrected before treating a firewall
  failure as meaningful; the full subsequent baseline and blocking tests passed.
- Installing the resolver required reloading D-Bus policy and restarting
  `systemd-resolved`; NetworkManager now supplies per-link DNS explicitly.
- Confined dnsmasq initially lacked a writable PID directory and identity-change
  capabilities. Its dedicated runtime directory and explicit capability set
  corrected startup; the real DHCP exchange then passed.
- Guard policy updates use atomic `ExecReload`. Restarting a required unit can
  stop NetworkManager, so policy reload is the supported update operation.

## Still requires the actual Wyze

Confirm firmware, enable its native RTSP endpoint, pair it with `SERV-Camera`,
reserve its observed MAC/IP, and verify local decode. Then test one PR source
at a time, real daylight/night vehicle passes, camera/Pi power removal, Wi-Fi
loss, and offline startup. Do not open camera-to-cellular internet forwarding
if Wyze pairing or startup fails without internet. The existing Reolink stays
the active inference source until a verified replacement is ready.
