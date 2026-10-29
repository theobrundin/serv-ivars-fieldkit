#!/usr/bin/env python3
"""Offline replay of plate-free Ivar's audit geometry. Never deploys config."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plate_reader import ivars_route
from plate_reader.stream_engine import StreamDwellTrack, StreamVehicle, StreamWebhookEngine


def engine(shadow=True):
    return StreamWebhookEngine(NS(
        device_id="serv-ivars-001", location_id="ivars-001",
        capture=NS(width=2304, height=1296),
        presence=NS(board_zone=ivars_route.WINDOW_ZONE,
                    route_qualification=NS(enabled=True, shadow_only=shadow,
                        profile_reconciliation_enabled=True, policy=ivars_route.POLICY,
                        entry_zone=ivars_route.ENTRY_ZONE)),
    ))


def track(case):
    # Audit firstSeenAt/lastSeenAt belong to the recognition profile, which
    # may be a short fragment inside the provider's much longer trajectory.
    # Reconstruct native bounds from its sample timestamps for replay only.
    times = tuple(case.get("routeTrajectoryTimestamps", []))
    known = [t for t in times if t]
    return StreamDwellTrack(
        first_seen_at=min(known) if known else None,
        last_seen_at=max(known) if known else None,
        dwell_seconds=case.get("dwellSeconds") or 0, plate_key=None,
        samples=tuple(StreamVehicle(vehicle_bbox=tuple(b), vehicle_confidence=1)
                      for b in case.get("routeTrajectoryBboxes", [])),
        sample_observed_at=times,
    )


def replay(cases, prefix="T"):
    classifier = engine()
    result = []
    for number, case in enumerate(sorted(cases, key=lambda c: c["file"]), 1):
        native = track(case)
        status, reason, start = classifier._trajectory_route_evidence(native)
        # Deliberate allowlist: do not serialize raw audit dictionaries.
        rows, invalid = classifier._ivars_route_rows(native)
        points = [((b[0]+b[2])/2,b[3]) for _, _, b in rows]
        dx = points[-1][0]-points[0][0] if len(points)>1 else None
        direction = ("left to right" if dx > .1 else "right to left" if dx < -.1
                     else "little net horizontal travel") if dx is not None else "unknown"
        result.append(dict(id=f"{prefix}{number:02}", file=case["file"],
            image=Path(case.get("image", "")).name,
            photo_box=case.get("vehicleBbox") or case.get("bbox"),
            photo_box_kind="vehicle" if case.get("vehicleBbox") else "plate evidence",
            photo_time=case.get("diagnosticImageObservedAt"),
            first_seen=native.first_seen_at, last_seen=native.last_seen_at,
            boxes=case.get("routeTrajectoryBboxes", []),
            times=case.get("routeTrajectoryTimestamps", []),
            proposed_status=status, proposed_reason=reason, window_start=start,
            direction=direction, clean_rows=rows if not invalid else [],
            track_quality=invalid or "usable"))
    return result


def link_fragments(cases):
    """Review-only links: mutual unique temporal/geometry continuity.

    Never supplies production identity or joins paths to manufacture a visit.
    Require short nonoverlapping time bounds, high union IoU, comparable size
    and nearly unchanged anchor. Appearance and plate text are not inputs.
    """
    from collections import defaultdict
    outgoing,incoming=defaultdict(list),defaultdict(list)
    for a in cases:
        if not a["clean_rows"]:continue
        _,end,box=a["clean_rows"][-1]
        for b in cases:
            if a is b or not b["clean_rows"]:continue
            _,start,other=b["clean_rows"][0]
            if not 0 <= start-end <= 2:continue
            area=(box[2]-box[0])*(box[3]-box[1]);other_area=(other[2]-other[0])*(other[3]-other[1])
            inter=max(0,min(box[2],other[2])-max(box[0],other[0]))*max(0,min(box[3],other[3])-max(box[1],other[1]))
            overlap=inter/(area+other_area-inter)
            movement=max(abs((box[0]+box[2]-other[0]-other[2])/2),abs(box[3]-other[3]))
            if overlap>=.7 and .7<=other_area/area<=1.4 and movement<=.04:
                outgoing[a["id"]].append((b["id"],round(start-end,3),round(overlap,3)))
                incoming[b["id"]].append(a["id"])
    links=[]
    for a,targets in outgoing.items():
        if len(targets)==1 and len(incoming[targets[0][0]])==1:
            b,gap,overlap=targets[0];links.append(dict(previous=a,next=b,gap_seconds=gap,iou=overlap))
    for c in cases:
        c["links"]=[link for link in links if c["id"] in (link["previous"],link["next"])]
        p=c.get("photo_box")
        if c["proposed_status"]=="qualified":c["assessment"]="Approach followed by near-window occupancy"
        elif c["links"]:c["assessment"]="Linked physical fragment — not a separate proven arrival"
        elif not c["boxes"] and p and p[3]>800 and p[2]-p[0]>650:c["assessment"]="Near-lane detection; missing trajectory prevents route proof"
        elif p and c["photo_box_kind"]=="vehicle" and p[3]<.65*1296:c["assessment"]="Background target in this photo; foreground car is a different object"
        elif c["clean_rows"] and max(b[3] for _,_,b in c["clean_rows"])<.65:c["assessment"]="Distant path; never reaches the window"
        else:c["assessment"]="Route not proven; retain as unresolved evidence"
    return links


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--html", type=Path, help="Standalone offline photo/trajectory reviewer")
    parser.add_argument("--additional-index", type=Path, help="Second independent batch, H-prefixed cases")
    args = parser.parse_args()
    source = json.loads(args.index.read_text())
    if source.get("capture") != {"width": 2304, "height": 1296}:
        raise SystemExit("Review requires the verified 2304x1296 capture geometry")
    results = replay(source["cases"])
    image_roots={c["id"]:args.index.parent/"photos" for c in results}
    if args.additional_index:
        extra=json.loads(args.additional_index.read_text())
        if extra.get("capture")!={"width":2304,"height":1296}:raise SystemExit("Second batch has different capture geometry")
        more=replay(extra["cases"],prefix="H")
        image_roots.update({c["id"]:args.additional_index.parent/"photos" for c in more})
        results+=more
    links=link_fragments(results)
    args.output.write_text(json.dumps(dict(policy=ivars_route.POLICY,
        human_validated=False, deployed=False, links=links, cases=results), indent=2))
    args.output.chmod(0o600)
    if args.html:
        import base64
        for case in results:
            image = image_roots[case["id"]] / case["image"]
            case["photo"] = "data:image/jpeg;base64," + base64.b64encode(image.read_bytes()).decode()
        html = Path(__file__).with_suffix(".html").read_text()
        findings = [f'{link["previous"]} → {link["next"]}: unique short-gap geometric continuation.'
                    for link in links]
        def case_id(suffix):
            return next((c['id'] for c in results if c['file'].endswith(suffix)),None)
        left,right=case_id('f567ed9b.json'),case_id('9888390d.json')
        if left and right:
            findings.append(f'{left}/{right} target background cars in opposite directions while the same blue SUV remains in the foreground.')
        truck,trailer=case_id('c8456b5e.json'),case_id('1f2c2510.json')
        if truck and trailer:
            findings.append(f'{truck} is the tall towing pickup; {trailer} is the trailer, not a second proven arrival.')
        missing=[c['id'] for c in results if c['assessment'].startswith('Near-lane detection;')]
        if missing:
            findings.append(', '.join(missing)+': near-lane detections with missing trajectories; route proof is incomplete.')
        html=html.replace('__FINDINGS__',' '.join(findings) or 'Inspect the separately marked photo target and native trajectory; no unique fragment links were found.')
        priority = "".join(f'<button data-case="{i}">{i}</button>'
                           for i in ("T13", "T15", "T28", "T29", "T54", "T55", "T56", "T57", "H07", "H49", "H53", "H56")
                           if i in image_roots)
        html=html.replace("__COUNT__",str(len(results))).replace("__QUALIFIED__",str(sum(c['proposed_status']=='qualified' for c in results)))
        html=html.replace("__WINDOW_ZONE__",json.dumps(ivars_route.WINDOW_ZONE))
        args.html.write_text(html.replace("__PRIORITY__", priority).replace(
            "__DATA__", json.dumps(results, separators=(",", ":")).replace("<", "\\u003c")))
        args.html.chmod(0o600)
    from collections import Counter
    print(json.dumps(dict(cases=len(results), outcomes=dict(Counter(
        c["proposed_reason"] for c in results)), links=links, output=str(args.output)), indent=2))


if __name__ == "__main__":
    main()
