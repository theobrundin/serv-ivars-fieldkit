#!/usr/bin/env python3
"""Tune app_detect_min / app_ocr_min for Plate Recognizer Stream.

Scores captured reads against the hand-labelled ground truth:

  conventional      a real, readable plate -> a read here is a TRUE POSITIVE
  no_plate          no readable plate      -> a read here is a FALSE POSITIVE
  carvana_placard   dealer placard         -> reported separately, not a plate

A read is attributed to the nearest ground-truth event within WINDOW seconds
of its video position. Reads matching no event are counted as unattributed
(background traffic / spurious), which is the other false-positive channel.

Usage: analyze_thresholds.py reads.jsonl ground_truth.csv
"""
import csv
import json
import sys
from collections import defaultdict

WINDOW = 12.0          # seconds either side of a labelled event


def load_reads(path):
    reads = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("plate") and r.get("score") is not None:
                reads.append(r)
    return reads


def load_truth(path):
    events = []
    with open(path) as fh:
        for row in csv.DictReader(fh):
            events.append({
                "car": int(row["car_number"]),
                "t": float(row["timestamp_seconds"]),
                "category": row["plate_category"].strip(),
            })
    return events


def video_times(reads):
    """Reads carry Stream's internal clock (a large constant offset), so
    normalise to seconds-since-first-read. The remaining unknown is a single
    global shift, fitted below rather than assumed."""
    vals = []
    for r in reads:
        p = r.get("position_sec")
        vals.append(float(p) if p is not None else None)
    base = min(v for v in vals if v is not None)
    for r, v in zip(reads, vals):
        r["_vt"] = (v - base) if v is not None else None
    return reads


def fit_offset(reads, events):
    """One free parameter: how far the first read sits into the video.

    Chosen to maximise the number of distinct labelled cars matched, so the
    alignment is derived from the data instead of from assumptions about how
    long ffmpeg took to start.
    """
    best_off, best_score = 0.0, -1
    for off in [x / 2.0 for x in range(0, 120)]:      # 0 .. 60s, 0.5s steps
        matched = set()
        for r in reads:
            if r["_vt"] is None:
                continue
            t = r["_vt"] + off
            for e in events:
                if abs(t - e["t"]) <= WINDOW:
                    matched.add(e["car"])
                    break
        if len(matched) > best_score:
            best_off, best_score = off, len(matched)
    return best_off, best_score


def attribute(reads, events, offset):
    """Attach each read to the nearest labelled event inside WINDOW."""
    for r in reads:
        if r["_vt"] is None:
            r["_event"] = None
            continue
        t = r["_vt"] + offset
        best, best_d = None, None
        for e in events:
            d = abs(t - e["t"])
            if d <= WINDOW and (best_d is None or d < best_d):
                best, best_d = e, d
        r["_event"] = best
    return reads


def evaluate(reads, events, det_min, ocr_min):
    """Recall over readable plates, and false positives, at one gate pair."""
    passing = [
        r for r in reads
        if r["score"] >= ocr_min and (r["dscore"] or 0.0) >= det_min
    ]
    hit_cars, fp_noplate, fp_placard, fp_unattributed = set(), set(), set(), 0
    for r in passing:
        e = r["_event"]
        if e is None:
            fp_unattributed += 1
        elif e["category"] == "conventional":
            hit_cars.add(e["car"])
        elif e["category"] == "no_plate":
            fp_noplate.add(e["car"])
        else:
            fp_placard.add(e["car"])
    total_conventional = sum(1 for e in events if e["category"] == "conventional")
    return {
        "reads": len(passing),
        "cars_read": len(hit_cars),
        "total_cars": total_conventional,
        "recall": len(hit_cars) / total_conventional if total_conventional else 0.0,
        "fp_noplate_cars": len(fp_noplate),
        "fp_placard_cars": len(fp_placard),
        "fp_unattributed_reads": fp_unattributed,
    }


def main():
    reads_path, truth_path = sys.argv[1], sys.argv[2]
    reads = load_reads(reads_path)
    events = load_truth(truth_path)

    video_times(reads)
    offset, matched = fit_offset(reads, events)
    attribute(reads, events, offset)

    print(f"reads captured        : {len(reads)}")
    print(f"fitted start offset   : {offset:.1f}s (matches {matched} labelled cars)")
    print(f"labelled events       : {len(events)} "
          f"({sum(1 for e in events if e['category']=='conventional')} conventional, "
          f"{sum(1 for e in events if e['category']=='no_plate')} no_plate, "
          f"{sum(1 for e in events if e['category']=='carvana_placard')} placard)")
    unattributed = sum(1 for r in reads if r["_event"] is None)
    print(f"unattributed reads    : {unattributed}")

    print("\n=== score distribution of ALL reads ===")
    for label, field in (("ocr score", "score"), ("det score", "dscore")):
        vals = sorted(r[field] for r in reads if r.get(field) is not None)
        if vals:
            def pct(p):
                return vals[min(len(vals) - 1, int(len(vals) * p))]
            print(f"  {label}: min={vals[0]:.3f} p25={pct(.25):.3f} "
                  f"p50={pct(.5):.3f} p75={pct(.75):.3f} max={vals[-1]:.3f}")

    print("\n=== per-category best read per car ===")
    by_cat = defaultdict(list)
    for e in events:
        cands = [r for r in reads if r["_event"] and r["_event"]["car"] == e["car"]]
        best = max((r["score"] for r in cands), default=None)
        by_cat[e["category"]].append((e["car"], best, len(cands)))
    for cat, rows in sorted(by_cat.items()):
        print(f"  {cat}:")
        for car, best, n in rows:
            shown = f"{best:.3f}" if best is not None else "  --  "
            print(f"    car {car:>2}  best_ocr={shown}  reads={n}")

    print("\n=== gate sweep (current production gates marked) ===")
    print(f"  {'det':>5} {'ocr':>5} | {'cars read':>10} {'recall':>7} "
          f"| {'FP no_plate':>11} {'FP placard':>10} {'FP other':>8}")
    for det_min in (0.30, 0.50, 0.60):
        for ocr_min in (0.70, 0.75, 0.80, 0.85, 0.90, 0.95):
            m = evaluate(reads, events, det_min, ocr_min)
            mark = "  <-- current" if (det_min == 0.50 and ocr_min == 0.70) else ""
            print(f"  {det_min:>5.2f} {ocr_min:>5.2f} | "
                  f"{m['cars_read']:>3}/{m['total_cars']:<6} {m['recall']:>6.1%} "
                  f"| {m['fp_noplate_cars']:>11} {m['fp_placard_cars']:>10} "
                  f"{m['fp_unattributed_reads']:>8}{mark}")


if __name__ == "__main__":
    main()
