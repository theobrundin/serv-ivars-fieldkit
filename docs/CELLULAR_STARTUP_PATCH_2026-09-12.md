# Replacement cellular startup correction — 12 September 2026

## Evidence and scope

The replacement passed a supervised LTE-only test using its inherited modem
session. It did not pass its subsequent cold-boot test. At boot+4.84 seconds
the new supervisor started; at+15.01 seconds its first retained WDS allocation
returned no usable client ID. The safety latch then prevented further
allocations. LTE had no IPv4 address/default even though later NAS telemetry
showed the modem registered to T-Mobile.

The user's locked, bounded CTL query subsequently succeeded, reporting CTL1.5
and WDS1.67. This proves present control readiness and the required service;
it does not establish which phase of the original allocation failed. That
release did not retain the original stderr. A startup-readiness race is
plausible, not a proven explanation for all historical Louisville outages.

The installed qmicli1.36.0 format is compatible with our CID parser. Its
generic service-version action does not allocate a WDS client. Its explicit
device-create/open error paths exit before client allocation. A client-creation
error is different and is not automatically safe to retry. See the
[version-matched libqmi source](https://raw.githubusercontent.com/linux-mobile-broadband/libqmi/1.36.0/src/qmicli/qmicli.c).

## Correction and safeguards

- Before any new retained WDS allocation, request CTL service versions using
  the shared QMI lock and a bounded command. Require advertised WDS support.
- Retry unsuccessful readiness checks with backoff. These checks allocate no
  retained WDS clients, so they must not set the unknown-allocation latch.
- Preserve real returned client/packet handles before classifying an error.
  Only narrowly recognized pre-allocation failures may retry without a latch.
- Keep ambiguous allocations fail-closed across service restart. Do not
  remove the current latch simply because the modem responds later.
- Retain one bounded, root-private detailed failure record plus safe
  phase/exit-code/classification fields in operational status. Do not publish
  raw modem output, keys or plate data.

No carrier steering, SIM reset, guessed packet handle, blanket USB reset,
firewall change, Wi-Fi change or production activation is part of this patch.
Vehicle recognition, hashing, camera settings and queue contents are unchanged.

## Deployment boundary

This is a host-helper patch, not a Python application update. Do not rerun the
full replacement installer: that would repeat a one-time state conversion.
The incremental installer requires the exact human-approved fetched channel
head, the known baseline installation and the bench identity/runtime hold. It
keeps Wi-Fi rescue available, backs up the old helper and state evidence, and
only replaces/restarts the cellular supervisor. It does not clear uncertainty.

The application remains recorded at
`4ad0ab71e0a145daf3a13d80305217e1d43e59b5`; the separately installed helper's
release/hash is recorded in `/etc/serv/cellular-patch.json`. This distinction
must remain visible: an application SHA alone is not proof of host networking
code. The ordinary updater should still refuse a release containing host
changes. Agents must not approve their own release.

## Acceptance after the human installs the reviewed patch

1. Verify root-owned helper hash and patch record. The camera, bench identity,
   Wi-Fi, Tailscale and AWS heartbeat must remain healthy.
2. Expect the old uncertain-allocation state to remain paused. This is not a
   reason to delete that file or repeatedly restart the service.
3. Shut down cleanly, wait for shutdown, then physically remove power from
   both Pi and modem for at least60 seconds before reconnecting. A software
   shutdown/reboot alone does not demonstrate modem power removal.
4. Confirm a newly owned session, valid WWAN IPv4/default, two working HTTPS
   probes, fresh authenticated bench heartbeat and independent SSH access.
   If readiness fails, inspect safe phase/error status and the root-private
   diagnostic; do not claim a successful recovery from `active` alone.
5. Repeat short LTE-only testing with automatic Wi-Fi restoration armed,
   then owned-session host-state recovery, warm reboot, and LTE-only cold boot
   with boot-persistent rescue. A transient rollback timer does not survive
   a reboot and must not be treated as protection for that test.
6. Complete sustained soak, a lit plate/MMC test, and final Louisville
   commissioning before shipping/activation. The old site geometry remains a
   starting calibration, not a guarantee for a new mount.

Readiness probing reduces one startup hazard. It cannot guarantee every later
allocation will succeed, and the conservative unknown-CID state still requires
operator recovery if the modem's response leaves ownership unknowable.
The existing 25-second command budget is unchanged. A process crash between
modem allocation and durable ownership recording is not made transactionally
safe by this patch; do not describe the helper as crash-proof. Warm-reboot
ownership and slower carrier session establishment remain acceptance risks.

## Local regression results

The correction passes47 mocked cellular tests,19 incremental-installer
preflight tests,70 architecture checks,331 Stream checks and39 AWS tests.
The broader pipeline remains100pass/4documented pre-existing failures. These
tests do not substitute for installing the approved patch and observing a
fresh hardware boot. No production identity, raw plate data or camera behavior
was changed by these tests.

## First boot after the human installed the patch

The fetched release used for installation was
`ae1be317f00bf84f1977a8895cd251ac9907bf76` (the follow-up commit only makes
test-fixture directory modes independent of the shell umask). Both47 cellular
tests and19 installer preflight tests passed unprivileged on the actual Pi.

The subsequent boot ID was `27505f73-110e-4d5c-81dd-7db8f414ae38`.
The live root-owned helper SHA256 matched the reviewed source:
`63516989ca47525565dc3aa9f8180746271981126330b9d77adf8417acda4f3c`.
The camera, Wi-Fi/Tailscale and authenticated bench heartbeat resumed.

At boot+15.02 seconds the helper logged readiness pending and retried without
retaining a WDS client, rather than entering the old uncertainty latch. At
+45.10 seconds it progressed to a data-session attempt, but logged
`QMI start failed (exit 1); exact CID retained for cleanup`. LTE still had no
IPv4/default during the first90 seconds. This is progress through the startup
path, not a successful cellular acceptance test. The root-private
`last-failure.json` must be inspected for the actual start error before
selecting another correction. Do not assume a SIM/carrier failure from that
generic log line or clear ownership state. Root diagnostics require the
user's new sudo authentication after boot.

The next automatic retry recovered without another reset or manual modem
action: by13:38:45 UTC the helper had installed `100.67.0.3/29`, gateway
`100.67.0.4`, MTU1360 and its probation default metric42760. By13:39:37 UTC
the LTE default was promoted to metric50 after the health checks. A new curl
request explicitly bound to `if!wwan0`, with proxies disabled and the isolated
bench credential supplied via stdin, returned HTTP200 at13:39:37.742 UTC.
An independent AWS read confirmed `last_heartbeat=13:39:37.630Z`.

At13:40:04 UTC, camera/app/cellular had zero automatic service restarts;
Stream reported1456×1088, health100 and5.4 received FPS (settling after the
startup clock correction), temperature53.8°C and throttled0x0. Thus this boot
demonstrated readiness retry, a later successful data-session start, route
promotion and authenticated cellular transport. It was **not an LTE-only
cold boot**: Wi-Fi remained available, including during DNS initialization.
Direct inspection of root-private ownership/patch records, controlled owned
session recovery, warm reboot, LTE-only cold boot and sustained/lit-camera
acceptance still remain. Preserve the saved first start error for later
inspection; the initial failure was transient, not a terminal failure.
