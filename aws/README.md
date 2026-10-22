# AWS backend source mirror

This directory contains reviewed source mirrors for the production Lambda
functions involved in Pi detection telemetry and vehicle visits:

- `InsertDetectionEvent` accepts recognition outcomes from a Pi.
- `AggregateDetectionEvents` rolls raw outcomes into daily location counters.
- `InsertData` accepts both plate-identified and unidentified vehicle visits.
- `AggregateVisits` rolls visits into traffic and plate-recognition counters.
- `Analytics` returns those counters to the dashboard/API consumer.
- `GetCustomers` returns the guest list; appearance fields are passed through.
- `AuthDeviceKey` is the HTTP API request authorizer. Dashboard read
  routes that pass `location_id` are scoped to that key's
  `allowed_location_ids` (default: the device's own `location_id`).
  Ingest routes with no location query param are unaffected. The
  authorizer cache keys only on `x-device-id` + `x-api-key`, so a deny
  for one location would poison every other call from that key for the
  TTL; GET `/analytics` and GET `/customers` therefore use a second
  authorizer whose identity source also includes `location_id`. A new
  HTTP API authorizer must also get `lambda:InvokeFunction` on
  AuthDeviceKey (`SourceArn` `.../authorizers/<id>`); without it API
  Gateway returns 5xx and `serv-api-5xx` fires.

## Outcome semantics

`group_confidence_floor` is a recognition diagnostic: a candidate group did not
meet the minimum confidence required for confirmation. The backend accepts it,
stores it, and includes it in recognition-attempt metrics. It must not create a
visit or increment a physical vehicle count.

An unidentified vehicle is posted once to `/visit` with `plateDetection` set to
`insufficient_confidence` or `no_visible_plate`. It increments traffic without
creating a fake customer identity. The server creates a retry-stable anonymous
key when the client sends an `Idempotency-Key` header.

A plate-identified vehicle uses `plateDetection: recognized` and the existing
64-character `licensePlate` hash. Existing repeat-visit and customer-profile
behavior is preserved. `color`, `make`, `model`, and `vehicle_type` are
descriptive only. Their classifier confidence is stored alongside the label.
Recognized Customer profiles retain the highest-confidence make/model pair and
highest-confidence color independently; make/model remains one atomic provider
result. Visit-level observations, including anonymous cars, feed
`/analytics.vehicle_mix`. None of these values enters the plate hash.

Route status, reason, trajectory size, dwell duration, and a plate-free track
fingerprint are optional Visit audit fields. `triggered_at` records the first
qualified menu-board sample. These values explain why a profile counted and
support future context timing; they never contribute to customer identity.

`vehicle_only` is intentionally unsupported. Accepting it would create a second,
ambiguous way to count an unidentified vehicle and could double-count traffic.

Visit `timestamp` values stay UTC ISO. `DAY#` / `HOUR#` in `LocationStats`
are the location's local calendar day and hour (`America/New_York` for
LJS, `America/Puerto_Rico` for Qdoba) so a dashboard `from`/`to` in store
time matches the day the store ran. `/analytics` returns `timezone` so
consumers do not have to guess. `hour_heatmap` keys 0–23 are those local
hours, not UTC.

## Vehicle ledger and recognition KPI

`Visits` is the only vehicle ledger. `DetectionEvents` is a diagnostic log:
one physical car can produce multiple weak-read episodes and can later
confirm, so an episode ID must never increment traffic.

The Pi assigns one `encounterId` to the existing Stream encounter and carries
it through the plate vote, DetectionEvent, and one terminal Visit. The KPI is:

```
identified / (identified + insufficient_confidence)
```

`no_visible_plate` remains in cars-through but is excluded from the KPI because
the system did not observe enough plate evidence to identify that vehicle.
`/analytics` exposes `cars_through`, `identifiable_vehicles`,
`identified_vehicles`, `missed_identifiable_vehicles`, `identification_rate`,
and `missed_identifiable_rate` directly. Legacy estimate fields remain at zero
only for response compatibility.

## Deployment dependency order

1. Deploy `InsertData` so `/visit` accepts unidentified vehicles and stores the
   expanded visit schema.
2. Deploy `AggregateVisits` so traffic totals include identified and
   unidentified visits exactly once.
3. Deploy `Analytics` so the API exposes traffic totals, recognized and
   unidentified counts, and plate-recognition rate.

That three-step encounter-ledger/local-time baseline is already live. Do not
repeat it for the current route/profile release; the targeted procedure below
updates only `InsertData` and `GetCustomers`.

`InsertDetectionEvent` and `AggregateDetectionEvents` remain responsible for
recognition diagnostics. They do not create visits.

`InsertDetectionEvent` and `AggregateDetectionEvents` already run the
local-time diagnostic-only release and are intentionally not redeployed by the
targeted vehicle-profile helper. The broader legacy security helper packages
their required `location_time.py` dependency if it is ever used again.

Tiered detector misses use `below_threshold` when a localized/readable
candidate did not clear the per-frame gates and `unreadable` when
normalization could not produce an identifier. Both are first-class
diagnostics, not vehicle counts.

Run the regression suite before deployment:

```sh
python3 -m unittest discover -s aws/tests -v
node --test aws/tests/visit_schema.test.mjs
```

For the additive route-audit and highest-confidence MMC fields, deploy only
`InsertData` and `GetCustomers` with the targeted helper. `Analytics` already
runs the correct local-time encounter-ledger release and is deliberately not
repackaged. The script requires a full release SHA equal to checked-out `HEAD`,
exports only immutable Git objects, snapshots and checksums both live packages,
and binds each update to the snapshotted Lambda `RevisionId`.

```sh
aws/deploy_vehicle_profile_release.sh <full-40-character-release-sha> --deploy
```

The helper pins production account `502954288533` and region `us-east-1` before
any snapshot or update. Do not use the broader historical
`deploy_security_release.sh` for this scoped release.

Deployment is intentionally separate from committing this source. Production
Lambda edits should be backed up/versioned and explicitly approved.
