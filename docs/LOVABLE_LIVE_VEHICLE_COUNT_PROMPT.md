# Prompt for Lovable: wire up the live vehicle-count estimate

Paste this into the Lovable chat for the dashboard project.

---

The backend now emits a vehicle-count **estimate** for locations whose
camera can't run full vehicle tracking (starting with `ljs-louisville-001`).
It's deliberately separate from the existing "Cars Through" / "Vehicles
Identified" numbers — those come from confirmed plate reads and are exact;
this new number is a heuristic estimate of *additional* vehicles that showed
up but were never identified, so it should read like an estimate, not get
blended into the exact count.

**Two new sources, both additive — nothing existing changes shape:**

**1. Live push, over the WebSocket connection you already have open**
(`wss://74hg1j5ig3.execute-api.us-east-1.amazonaws.com/production`).
A new message type appears alongside the existing visit push:

```json
{
  "type": "detection_miss",
  "location_id": "ljs-louisville-001",
  "episode_id": "a1b2c3...",
  "outcome": "below_threshold",
  "detected_at": "2026-08-11T20:05:12.000Z",
  "estimated_additional_vehicles_today": 12
}
```

Existing visit-confirmation messages have no `type` field today, so branch
on `data.type === "detection_miss"` and handle the rest of your message
types exactly as before — this is additive, not a replacement.

`estimated_additional_vehicles_today` is the **authoritative running total
for today**, not a delta — use it to *set* your local value, never add 1 to
whatever you're already showing. (This is deliberate: incrementing a local
counter on every push is exactly what caused the known "visit counter shows
double" bug on the existing confirmed-visit path. Don't repeat that pattern
here.)

**2. Polled `/analytics`, for the periodic-refresh path** — three new fields
inside the existing `identification` block, alongside `total_vehicles` and
`vehicle_recognition_rate`:

```json
{
  "identification": {
    "total_vehicles": 27,
    "vehicle_recognition_rate": 1.0,
    "estimated_additional_vehicles": 12,
    "total_vehicles_including_estimate": 39,
    "recognition_rate_including_estimate": 0.6923
  }
}
```

`vehicle_recognition_rate` is now the identifiable-plate KPI: recognized
vehicles divided by recognized plus `insufficient_confidence` vehicles. Cars
with `no_visible_plate` remain in total traffic but do not depress that rate.
It is `null` when the selected period contains no identifiable vehicles.

`recognition_rate_including_estimate` retains its older total-traffic estimate
for compatibility. Do not use it as the identifiable-plate KPI or relabel it
as recognition accuracy.

**What to build:**

- Add a small, clearly-labeled secondary figure — e.g. "+12 estimated" or
  "~12 more (unread plates)" — next to "Cars Through" and next to
  "Vehicles Identified" / whatever "Live Analytics" currently shows for
  recognition rate. Style it as visibly an estimate (lighter weight, a
  tooltip explaining it's inferred from partial reads, etc.) — never merge
  it into the main number.
- Both the "Live Guests" page and the "Live Analytics" page should read
  `estimated_additional_vehicles_today` off the **same WebSocket
  connection/message** — don't open a second connection or derive it a
  different way per page. Since the backend broadcasts once per genuine new
  event to every open connection for a location, both pages update from the
  exact same instant automatically as long as they're both listening.
- On page load / periodic refresh, seed from `/analytics`'s
  `estimated_additional_vehicles` (or `total_vehicles_including_estimate` /
  `recognition_rate_including_estimate`, whichever your components already
  consume the plain version of); the WebSocket push then keeps it live
  between refreshes.
- If a location has no presence-tracking gap (i.e. it never sends this
  outcome), `estimated_additional_vehicles` is simply `0` and the new UI
  element should either hide itself or show "0" — don't show a raw
  `undefined`/blank for locations where this doesn't apply yet.
