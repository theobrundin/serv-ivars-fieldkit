# Ivar's 001 route release — manual approval and rollback

This release is a calibration candidate with a known missed arrival (V38).
Active mode excludes unqualified traffic, including that known miss; shadow
mode evaluates routes without enforcing them on counts. The user chooses the
mode. See IVARS_ROUTE_CALIBRATION.md for actual evidence, not a readiness claim.

## Preparation

The old generic updater is absent and its paths do not match this Pi. This
release provides a one-time site-specific helper, `fieldkit/update_routes.py`.
It verifies the exact baseline source, physical device, root-owned code and
production profiles. Only config.py, stream_engine.py, ivars_route.py and
route_audit.py may change in the application. Normalizer, hasher, confidence
gates, credentials, modem, network, camera and PR services are not replaced.

The exact Git commit must be present in the root-owned bare repository at
`/var/lib/serv-ivars-updates/repository.git`, with the fetched
`refs/remotes/origin/codex/ivars-001-fieldkit` pointing to that SHA. The staged
bootstrap helper must match the same commit's Git blob byte for byte.
A source bundle and bootstrap helper are inert staged files; staging does not
install the updater, record approval, change live configs, or restart anything.

## Human steps

Use the full release SHA supplied with the prepared release, not `HEAD` or a
short hash. On the Pi via the existing Tailscale SSH login, the human runs:

```sh
sudo python3 -I /var/lib/serv-ivars-updates/releases/FULL_SHA/update_routes.py --install FULL_SHA
sudo /usr/local/sbin/serv-update --approve FULL_SHA --mode active
```

Use `--mode shadow` instead if shadow evaluation is intended. Bootstrap
installs only the helper and its pinned channel; it creates no approval and
installs no timer. The second command records approval, validates the proposed
profile and captures the current configuration digest. The agent must not run
that approval step. Private configuration and credentials remain on the Pi.

## Apply and verify

Once approval exists and the lane is verified empty, apply:

```sh
sudo /usr/local/sbin/serv-update --lane-empty
```

The updater shares the field-control lock, refuses drift since approval,
backs up code and configuration, and restarts only the application. It patches
the active and saved production profiles so an operator mode change does not
silently discard the zones. The sandbox profile remains on its old site policy.
The root-private last-update.json records mode, SHA, backup and activation time.
Approval is consumed after application or a caught rollback.

Check `./serv status`: new release, healthy camera/PR, services active, queue
and dead letters. Check the loopback-only webhook, fresh native route audit,
representative arrivals/exclusions, and AWS delivery. A successful listener
startup is not proof of correct counting. Do not replay old shadow visits.
The first cars after a restart can lack approach proof; choose an empty lane.

## Rollback

With an empty lane:

```sh
sudo /usr/local/sbin/serv-update --rollback --lane-empty
```

This restores the exact saved application, active and production profiles,
release manifest and prior SHA marker. It restarts only the application.
Queues and already delivered AWS counts are never rewound. Check health again.
A caught startup error triggers this restoration automatically; a power loss
or killed updater during the transaction requires this explicit recovery.
There is no claim of crash-proof or exactly-once delivery.
