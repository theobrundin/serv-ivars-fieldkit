#!/usr/bin/env python3
"""Ivar's route geometry, ownership and fail-closed regressions."""
import json
import pathlib
import sys
import time
from dataclasses import replace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from ivars_route_review import engine, track
from plate_reader import ivars_route
from plate_reader.stream_engine import StreamVehicle


def run_checks(check):
    print("\n=== Ivar's window route candidate (not field ground truth) ===")
    fixtures = json.loads((pathlib.Path(__file__).parent / "fixtures" /
                           "ivars_20261028_geometry.json").read_text())["cases"]
    e = engine(False)
    # Exercise the real config loader: stale identity, dimensions or geometry
    # must fail before the capture/application services are started.
    from plate_reader.config import load, ConfigError
    import tempfile
    def strip(d):
        return {k:strip(v) for k,v in d.items() if not k.startswith("//")} if isinstance(d,dict) else d
    raw = strip(json.loads((pathlib.Path(__file__).resolve().parents[1] / "config.example.json").read_text()))
    raw.update(device_id="serv-ivars-001", location_id="ivars-001", api_key="ab"*32)
    raw["capture"] = {"width":2304,"height":1296}
    def zone(z):
        return dict(zip(("x_min_pct","y_min_pct","x_max_pct","y_max_pct"),z))
    raw["presence"]["board_zone"] = zone(ivars_route.WINDOW_ZONE)
    raw["presence"]["route_qualification"].update(policy=ivars_route.POLICY,
        entry_zone=zone(ivars_route.ENTRY_ZONE), shadow_only=True)
    with tempfile.TemporaryDirectory() as tmp:
        p=pathlib.Path(tmp)/"config.json"
        p.write_text(json.dumps(raw))
        check("explicit Ivar's config loads in shadow", load(p).presence.route_qualification.policy, ivars_route.POLICY)
        for name, mutate in [
            ("wrong site", lambda d:d.update(location_id="ljs-louisville-001")),
            ("wrong device", lambda d:d.update(device_id="serv-unit-001")),
            ("wrong dimensions", lambda d:d["capture"].update(width=1456)),
            ("stale zones", lambda d:d["presence"].update(board_zone=zone((0,0,.72,.25)))),
            ("unknown policy", lambda d:d["presence"]["route_qualification"].update(policy="typo")),
        ]:
            bad=json.loads(json.dumps(raw));mutate(bad);p.write_text(json.dumps(bad))
            try:load(p);refused=False
            except ConfigError:refused=True
            check("config refuses "+name,refused,True)
    for case in fixtures:
        check(case["name"], e._trajectory_route_evidence(track(case))[0],
              case["expected_policy_status"])
    positive = track(fixtures[6])
    check("bottom anchor keeps clipped close vehicle in window zone",
          e._box_center_normalized((0, 0, 1100, 1200)), (550/2304, 1200/1296))
    for name, bad in [
        ("missing timestamps", replace(positive, sample_observed_at=())),
        ("reversed timestamps", replace(positive, sample_observed_at=tuple(reversed(positive.sample_observed_at)))),
        ("out-of-frame geometry", replace(positive, samples=(StreamVehicle((-1, 0, 800, 900), 1),) + positive.samples[1:])),
        ("repeated instant cannot create dwell", replace(positive, sample_observed_at=(positive.sample_observed_at[0],)*len(positive.samples))),
        ("restart at window has no approach proof", replace(positive, samples=positive.samples[40:], sample_observed_at=positive.sample_observed_at[40:])),
    ]:
        check(name, e._trajectory_route_evidence(bad)[0], "rejected")
    rows, invalid = e._ivars_route_rows(positive)
    check("valid native track resolves", invalid, None)
    broken = [(i, t + (10 if n >= 10 else 0), b) for n, (i,t,b) in enumerate(rows)]
    check("long gap cannot join two vehicles", ivars_route.classify(broken)[1], "trajectory_discontinuous")
    reverse = [(i,t,b[2]) for (i,t,_),b in zip(rows,reversed(rows))]
    check("opposite-direction path through the same zones is not an arrival",
          ivars_route.classify(reverse)[0], "rejected")
    shallow = [(i,t,(b[0],b[3]-.25,b[2],b[3])) for i,t,b in rows]
    check("broad distant box cannot pass foreground-height guard",
          ivars_route.classify(shallow)[0], "rejected")
    check("equal plausible duplicate boxes are ambiguous",
          ivars_route.unique_continuation((0, 0, 100, 100),
              [(0, (1, 0, 101, 100)), (1, (0, 1, 100, 101))]), None)
    check("small nested detection cannot steal the vehicle's track",
          ivars_route.unique_continuation((0, 0, 100, 100),
              [(0, (1, 0, 101, 100)), (1, (80, 80, 90, 90))]), 0)

    # Check the ownership seam, not just the route classifier. The original
    # pickup track contains unrelated simultaneous boxes. None may be used
    # downstream for physical-profile reconciliation.
    contaminated = track(fixtures[1])
    resolved, invalid = e._ivars_route_rows(contaminated)
    check("pickup duplicate boxes have a unique geometric continuation", invalid, None)
    seen = []
    e._match_dwell_locked = lambda t, *a, **kw: (seen.append(t) or [])
    e._process_dwell_track_locked(contaminated, time.monotonic(), cache_unmatched=False, log_unmatched=False)
    check("ownership receives only resolved unique samples",
          seen[0].samples, tuple(contaminated.samples[i] for i, _, _ in resolved))
    seen.clear()
    ambiguous = replace(positive,
        samples=(positive.samples[0], replace(positive.samples[0], vehicle_bbox=(1, 1, 200, 200))) + positive.samples[1:],
        sample_observed_at=(positive.sample_observed_at[0],) + positive.sample_observed_at)
    e._process_dwell_track_locked(ambiguous, time.monotonic(), cache_unmatched=False, log_unmatched=False)
    check("ambiguous track never participates in identity reconciliation", seen, [])

    # One known physical arrival qualifies once; its trailing fragment cannot
    # downgrade it or create an additional anonymous visit.
    live = engine(False)
    base = time.monotonic()
    owner = live.observe_vehicle(positive.samples[0], now=base,
        observed_at=positive.sample_observed_at[0], candidate_key="SYNTH123",
        plated_attempt=True, saw_plate=True)
    keyed = replace(positive, plate_key="SYNTH123")
    live._process_dwell_track_locked(keyed, base+1, cache_unmatched=False)
    check("completed owned approach qualifies in authoritative mode", live.route_status(owner), "qualified")
    live._process_dwell_track_locked(replace(track(fixtures[7]), plate_key="SYNTH123"), base+2, cache_unmatched=False)
    check("departure cannot undo completed route", live.route_status(owner), "qualified")
    visits = live.drain_vehicles(now=base+400)
    check("one physical route produces one anonymous terminal vehicle", len(visits), 1)
    check("terminal route cannot drain a second time", live.drain_vehicles(now=base+401), [])
    background = engine(False)
    bg = track(fixtures[0])
    bg_owner = background.observe_vehicle(bg.samples[0], now=base,
        observed_at=bg.sample_observed_at[0], candidate_key="BGTEST123",
        plated_attempt=True, saw_plate=True)
    background.mark_encounter_identified(bg_owner)
    background._process_dwell_track_locked(replace(bg, plate_key="BGTEST123"), base+1, cache_unmatched=False)
    check("confirmed OCR cannot qualify a background route", background.route_status(bg_owner) == "qualified", False)
    check("background yields no traffic", background.drain_vehicles(now=base+400), [])

    from plate_reader.route_audit import retain_track
    import os
    with tempfile.TemporaryDirectory() as tmp:
        folder=pathlib.Path(tmp)/"route-tracks"
        secret=replace(positive,plate_key="SYNTHETIC_SECRET_DO_NOT_EXPORT")
        retain_track(folder,secret,status="qualified",reason="approach_then_window_dwell",association="no_unique_candidate")
        files=list(folder.glob("route-*.json"));blob=files[0].read_text()
        check("unmatched native evidence retained without candidate text",
              (len(files),"SYNTHETIC_SECRET" in blob,"plate_key" in blob),(1,False,False))
        check("trace permissions are private",files[0].stat().st_mode&0o777,0o600)
        retain_track(folder,replace(secret,plate_key="ANOTHER_SYNTHETIC_CANDIDATE"),status="qualified",reason="approach_then_window_dwell",association="matched")
        check("audit geometry digest is independent of plate identity",len(list(folder.glob('route-*.json'))),1)
        for fixture in fixtures[:4]:
            retain_track(folder,track(fixture),status="rejected",reason="test",association="test",max_cases=2)
        check("native audit cap prunes whole records",len(list(folder.glob('route-*.json'))),2)
        for file in folder.glob('route-*.json'):os.utime(file,(0,0))
        retain_track(folder,positive,status="qualified",reason="test",association="test")
        check("native audit expires old records",len(list(folder.glob('route-*.json'))),1)
        obstruction=pathlib.Path(tmp)/'not-a-directory';obstruction.write_text('x')
        live._route_audit_dir=str(obstruction)
        prior_status=live.route_status(owner)
        live._retain_route_audit(positive,'qualified','test','test')
        check("unavailable evidence storage does not change route state",live.route_status(owner),prior_status)

    from ivars_route_review import replay,link_fragments
    review=replay([{**f,"file":f["source"]} for f in fixtures[:8]])
    links=link_fragments(review)
    pairs={(link['previous'],link['next']) for link in links}
    check("review links pickup and SUV departures by unique geometry",pairs,{('T02','T03'),('T07','T08')})
    duplicate=dict(review[2],id='DUPLICATE')
    ambiguous_links=link_fragments(review+[duplicate])
    check("two equally plausible successors are not linked",
          any(link['previous']=='T02' for link in ambiguous_links),False)


if __name__ == "__main__":
    failures = []
    def check(name, actual, expected):
        ok = actual == expected
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            failures.append(name)
            print(" actual:", actual, "expected:", expected)
    run_checks(check)
    raise SystemExit(bool(failures))
