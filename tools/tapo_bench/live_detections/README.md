# Temporary home-test detections viewer

This is the 21 October 2026 user-authorized exception for displaying raw
candidate plates on the operator's phone during the Tapo house experiment.
It is not included in the fleet install or updater. Do not deploy it to the
restaurant. Never change the production webhook's loopback binding or the
normalizer/hasher to support this viewer.

## Data and transport

The Pi process listens only on `127.0.0.1:8130`. PR retains `serv-local` and
adds `serv-live-view` as a second webhook target with `image=no`, `video=no`,
`caching=no`, and `request_timeout=2`. The hook URL has a random secret. The
snapshot endpoint requires a separate bearer secret. Both secrets stay in
private configuration outside Git. HTTP request logging is disabled.

Only this second target receives viewer traffic. The viewer reads the
existing dry-run SQLite queue in read-only mode and never injects observations
into the app. Source images/video, provider payloads, internal hashes and
bounding boxes are not exposed by the snapshot endpoint.

The Mac uses SSH to `serv@192.168.39.111` with `HostKeyAlias=servtest.local`,
strict host checking and a forward from `127.0.0.1:18130` to Pi loopback 8130.
This direct LAN address has no cellular/Tailscale fallback. The Mac relay
binds its specific GloverFi address `192.168.39.145:8767`. If Ethernet or the
Mac's LAN address changes, the page shows connection loss; repair that local
path instead of silently moving raw detections onto cellular.

The phone uses a private link with a random fragment token exchanged for an
HttpOnly SameSite cookie. The fragment is removed immediately. This local
HTTP page is for trusted GloverFi only; it is not internet publication or a
TLS website. Host allowlisting, no CORS, no-store, a restrictive CSP, and DOM
text rendering prevent casual cross-origin access and OCR-as-HTML execution.
Do not forward the port or share the private link outside the test group.

## Meaning of a reading

Cards are recognizer observations, not a count of unique cars. Exact webhook
retries are deduplicated. Alternative plate entries with the same exact
vehicle box in one payload are grouped, while separate simultaneous vehicle
boxes remain separate. Different observations are not merged by make/model,
colour or a guessed plate. A plate-free vehicle still gets a card.

An unconfirmed plate is a provider guess. A confirmed badge requires a new
`confirmed` queue visit with the candidate's unchanged normalized hash and
the same capture timestamp (within 10 ms), matching exactly one row. Ambiguous
matches remain unconfirmed. This deliberately conservative display may miss
a later confirmation if the app's first-read timestamp differs; it must not
invent confirmation. Previous session queue entries cannot confirm a new row.

The page normally updates every 1.5 seconds after Stream's recognition window
(configured maximum prediction delay 12 seconds). Late/unread outcomes may
take longer; app cooldown can suppress repeat confirmed visits for 10 minutes.
Pausing the display does not pause recognition. Entries expire after 30
minutes, are capped at 200, and are lost on viewer restart. No raw history is
written to disk. MMC is displayed as an estimate, never used for identity.

## Running and teardown

`server.py --config /private/path/config.json` supports `pi` and `relay`
modes. Configuration must be mode 0600. Pi config specifies `app_root`,
`queue`, loopback `bind`, `port`, allowed HTTP `hosts`, and random
`hook_token`/`transport_token`. Relay config specifies a private LAN `bind`,
`port`, allowed `hosts`, loopback tunnel `upstream`, `transport_token`,
`viewer_token`, and a truthful `connection_label`.

The session's private launch state is under
`~/.codex/reolink-bench-20261020/live-detections/` on the Mac, with Pi state
under `/home/serv/tapo-bench/live-detections/`. Only explicitly owned process
IDs may be stopped. The viewer is a transient `serv-live-detections.service`
on the Pi, not enabled at boot. If the Pi reboots, restart the sidecar before
resuming the test or remove the target as below.

For teardown, first remove `serv-live-view` from `webhook_targets` and delete
only its `[webhooks]` subsection in the private PR config; preserve
`serv-local` and every other current setting. Stop the watchdog timer, restart
`serv-tapo-stream.service`, and restart the watchdog timer even on failure.
Verify PR health and the app, then stop `serv-live-detections.service`, the
owned Mac relay/tunnel processes and its wake assertion. Delete session tokens.
Do not restore an old whole config over unrelated subsequent changes.

## Validation

Use Python 3.10+ (the Mac's system Python 3.9 is too old for the existing app):

```sh
python3.11 -m unittest discover -s tools/tapo_bench/live_detections -p 'test_*.py'
python3.11 tools/test_architecture_doc.py
python3.11 tools/test_stream_engine.py
python3.11 -m unittest discover -s aws/tests
python3.11 tools/test_pipeline_features.py
```

The viewer tests use synthetic fixtures only, never the real production
webhook. Validate an empty feed, confirmed/candidate/unread cards, malicious
OCR rendered as text, a lost upstream, recovery, and the mobile layout.
Clear all browser fixture state before opening the live session. A healthy
stream plus a working viewer is not proof that a distant/moving/night plate
will be readable; an actual vehicle pass must establish optical acceptance.
