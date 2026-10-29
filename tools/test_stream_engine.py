#!/usr/bin/env python3
"""Tests for the Plate Recognizer Stream webhook engine.

The point of these is the seam: Stream's payload shape is external and can
drift, and everything downstream (hashing, dedup, /visit) assumes the
engine-neutral RawPlateRead contract. These lock that mapping down, and
prove the one property that must never regress — raw plate text is hashed
before it can reach AWS.

Uses the real payload shape from Plate Recognizer's Stream webhook docs.

Run: python3 tools/test_stream_engine.py
"""
import json
import pathlib
import time
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from plate_reader.hasher import hash_plate  # noqa: E402
from plate_reader.normalizer import normalize  # noqa: E402
from plate_reader.camera import bbox_inside_zone  # noqa: E402
from plate_reader.config import DetectionZone  # noqa: E402
from plate_reader.engines import RawPlateRead  # noqa: E402
from plate_reader.stream_engine import (  # noqa: E402
    StreamWebhookEngine,
    classify_results,
    extract_json_field,
    extract_webhook_image,
    mmc_same_vehicle,
    parse_webhook_payload,
    vehicle_from_read,
)
from plate_reader.miss_diagnostics import prune_miss_plates  # noqa: E402

_passed = _failed = 0


def check(label, got, want):
    global _passed, _failed
    if got == want:
        _passed += 1
        print(f"PASS  {label}  (got={got!r})")
    else:
        _failed += 1
        print(f"FAIL  {label}  (got={got!r} want={want!r})")


# Verbatim from Plate Recognizer's Stream webhook documentation.
SAMPLE = {
    "hook": {
        "event": "recognition",
        "filename": "camera-1_screenshots/21-10-27/06-20-55.161444.jpg",
        "id": "camera-1",
        "target": "http://127.0.0.1:8129/",
    },
    "data": {
        "results": [
            {
                "plate": "slz9043m",
                "score": 0.906,
                "dscore": 0.641,
                "box": {"xmin": 153, "ymin": 91, "xmax": 302, "ymax": 125},
                "candidates": [{"plate": "slz9043m", "score": 0.904}],
                "vehicle": {
                    "type": "SUV",
                    "score": 0.254,
                    "box": {"xmin": 603, "ymin": 0, "xmax": 1099, "ymax": 174},
                },
                "model_make": [
                    {"make": "Mercedes-Benz", "model": "Citan", "score": 0.075}
                ],
                "color": [{"color": "white", "score": 0.889}],
                "camera_id": "camera-1",
            }
        ],
        "timestamp": "2021-10-27T06:20:55.161444Z",
    },
}


print("\n=== payload -> RawPlateRead mapping ===")

reads = parse_webhook_payload(SAMPLE)
check("one result yields one read", len(reads), 1)
r = reads[0]
check("plate text carried through verbatim", r.text, "slz9043m")
# The two scores are NOT interchangeable: dscore gates app_detect_min and
# score gates app_ocr_min. Swapping them silently breaks both thresholds.
check("dscore -> detection_confidence", r.detection_confidence, 0.641)
check("score  -> ocr_confidence", r.ocr_confidence, 0.906)
check("plate box -> (x1,y1,x2,y2)", r.bbox, (153, 91, 302, 125))
check("vehicle box carried", r.vehicle_bbox, (603, 0, 1099, 174))
check("vehicle score carried", r.vehicle_confidence, 0.254)

print("\n=== the property that must never regress ===")

# Stream emits lowercase; every historical hash was built from the
# NORMALIZATION_SPEC v1.0 uppercase form. If these diverge, every returning
# customer silently becomes a new customer.
normalized = normalize(r.text, min_length=4, max_length=12)
check("normalizes to the canonical uppercase form", normalized, "SLZ9043M")
check("lowercase and uppercase hash identically",
      hash_plate(normalize("slz9043m", min_length=4, max_length=12)),
      hash_plate("SLZ9043M"))
check("hash is a 64-char lowercase hex string the Lambda will accept",
      len(hash_plate(normalized)) == 64 and hash_plate(normalized).islower(),
      True)
check("raw plate text is NOT the value that would be sent",
      hash_plate(normalized) == r.text, False)
check("3-character vanity plates normalize and hash",
      normalize("abc", min_length=3, max_length=12), "ABC")
check("2-character strings still rejected",
      normalize("AB", min_length=3, max_length=12), None)

print("\n=== malformed input must not kill the detection loop ===")

check("no data block", parse_webhook_payload({}), [])
check("results not a list", parse_webhook_payload({"data": {"results": 7}}), [])
check("result missing plate",
      parse_webhook_payload({"data": {"results": [{"score": 0.9}]}}), [])
check("non-dict result is skipped",
      parse_webhook_payload({"data": {"results": ["nope"]}}), [])
missing_scores = parse_webhook_payload(
    {"data": {"results": [{"plate": "ABC123"}]}}
)
check("absent scores default to 0.0, not None",
      (missing_scores[0].detection_confidence,
       missing_scores[0].ocr_confidence), (0.0, 0.0))
out_of_range = parse_webhook_payload(
    {"data": {"results": [{"plate": "ABC123", "score": 42, "dscore": -3}]}}
)
check("out-of-range scores clamp into the 0-1 contract",
      (out_of_range[0].detection_confidence,
       out_of_range[0].ocr_confidence), (0.0, 1.0))

print("\n=== body parsing: multipart (what Stream actually sends) ===")

boundary = "----StreamBoundary123"
multipart = (
    f"--{boundary}\r\n"
    'Content-Disposition: form-data; name="json"\r\n'
    "Content-Type: application/json\r\n\r\n"
    f"{json.dumps(SAMPLE)}\r\n"
    f"--{boundary}\r\n"
    'Content-Disposition: form-data; name="upload"; filename="x.jpg"\r\n'
    "Content-Type: image/jpeg\r\n\r\n"
    "\xff\xd8\xff-not-a-real-jpeg\r\n"
    f"--{boundary}--\r\n"
).encode("latin-1")

parsed = extract_json_field(
    multipart, f"multipart/form-data; boundary={boundary}"
)
check("json field extracted from multipart alongside an image part",
      parsed is not None and parsed["data"]["results"][0]["plate"],
      "slz9043m")
jpeg = extract_webhook_image(
    multipart, f"multipart/form-data; boundary={boundary}"
)
check("multipart upload part is the JPEG bytes",
      jpeg, b"\xff\xd8\xff-not-a-real-jpeg")
multipart_with_original = multipart.replace(
    f"--{boundary}--\r\n".encode(),
    (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="original"; filename="o.jpg"\r\n'
        "Content-Type: image/jpeg\r\n\r\n"
        "original-full-frame\r\n"
        f"--{boundary}--\r\n"
    ).encode(),
)
check("explicit original frame wins over an earlier vehicle/upload crop",
      extract_webhook_image(
          multipart_with_original,
          f"multipart/form-data; boundary={boundary}",
      ), b"original-full-frame")
check("urlencoded body has no image part",
      extract_webhook_image(b"json=%7B%7D", "application/x-www-form-urlencoded"),
      None)
check("application/json body also accepted (for curl testing)",
      extract_json_field(json.dumps(SAMPLE).encode(), "application/json")
      is not None, True)
check("garbage body rejected, not crashed",
      extract_json_field(b"not json", "application/json"), None)
check("unsupported content-type rejected",
      extract_json_field(b"x", "text/plain"), None)

print("\n=== body parsing: urlencoded (what Stream 1.64 REALLY sends) ===")

# REGRESSION GUARD. The published docs say multipart/form-data. A live
# capture from Stream v1.64.0 with `image = no` shows
# application/x-www-form-urlencoded instead — no file parts, so no multipart.
# Parsing only what the docs describe silently dropped every result: the
# webhook returned 200, the counter incremented, and zero plates reached the
# pipeline. Captured verbatim off the wire on servCamOG.
REAL_BODY = (
    "json=%7B%22hook%22%3A+%7B%22target%22%3A+%22http%3A%2F%2F127.0.0.1%3A812"
    "9%2F%22%2C+%22id%22%3A+%22camera-1%22%2C+%22event%22%3A+%22recognition%22"
    "%7D%2C+%22data%22%3A+%7B%22camera_id%22%3A+%22camera-1%22%2C+%22timestamp"
    "%22%3A+%222026-08-16T01%3A32%3A29.180958Z%22%2C+%22results%22%3A+%5B%7B%22"
    "box%22%3A+%7B%22xmin%22%3A+1118%2C+%22ymin%22%3A+553%2C+%22xmax%22%3A+1195"
    "%2C+%22ymax%22%3A+577%7D%2C+%22plate%22%3A+%22usot2512813%22%2C+%22score%22"
    "%3A+0.97%2C+%22dscore%22%3A+0.638%2C+%22vehicle%22%3A+%7B%22score%22%3A+0."
    "346%2C+%22type%22%3A+%22Pickup+Truck%22%2C+%22box%22%3A+%7B%22xmin%22%3A+"
    "1033%2C+%22ymin%22%3A+365%2C+%22xmax%22%3A+1279%2C+%22ymax%22%3A+683%7D%7D"
    "%7D%5D%7D%7D"
).encode()

real = extract_json_field(REAL_BODY, "application/x-www-form-urlencoded")
check("urlencoded json= field is decoded", real is not None, True)
real_reads = parse_webhook_payload(real or {})
check("...and yields a usable read", len(real_reads), 1)
check("...with the real plate", real_reads[0].text, "usot2512813")
check("...real dscore -> detection_confidence", real_reads[0].detection_confidence, 0.638)
check("...real score  -> ocr_confidence", real_reads[0].ocr_confidence, 0.97)
check("...real vehicle box carried", real_reads[0].vehicle_bbox, (1033, 365, 1279, 683))
check("urlencoded body with no json= field is rejected",
      extract_json_field(b"other=1", "application/x-www-form-urlencoded"), None)


print("\n=== live listener end-to-end over real HTTP ===")


class _Cfg:
    class stream:
        webhook_host = "127.0.0.1"
        webhook_port = 8231

    class alpr:
        class thresholds:
            # This section tests transport, not identity settlement.
            identity_settle_quiet_seconds = 0.0
            identity_settle_max_seconds = 0.0


engine = StreamWebhookEngine(_Cfg())
engine.open()
try:
    check("nothing queued before any webhook arrives", engine.infer(None), [])

    request = urllib.request.Request(
        "http://127.0.0.1:8231/",
        data=multipart,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        check("webhook accepted", response.status, 200)

    drained = engine.infer(None)
    check("read is available to the detector loop", len(drained), 1)
    check("...with the right plate", drained[0].text, "slz9043m")
    check("queue is drained exactly once (no duplicate vehicles)",
          engine.infer(None), [])
    check("encounter retains the compressed original for terminal audit",
          engine._encounters[0]["vehicle"].diagnostic_image_jpeg, jpeg)
    _transport_observation = engine.drain_vehicle_observations()[0]
    check("live tracking observations never retain original JPEGs",
          _transport_observation.diagnostic_image_jpeg, None)
    _same_frame_payload = {
        "hook": {"event": "recognition", "id": "camera-1"},
        "data": {
            "timestamp": "2026-08-26T19:55:00Z",
            "results": [
                {
                    "vehicle": {
                        "type": "Sedan", "score": 0.91,
                        "box": {
                            "xmin": 1000, "ymin": 700,
                            "xmax": 1200, "ymax": 900,
                        },
                    },
                },
                {
                    "vehicle": {
                        "type": "SUV", "score": 0.89,
                        "box": {
                            "xmin": 1050, "ymin": 720,
                            "xmax": 1250, "ymax": 920,
                        },
                    },
                },
            ],
        },
    }
    _same_frame_request = urllib.request.Request(
        "http://127.0.0.1:8231/",
        data=json.dumps(_same_frame_payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(_same_frame_request, timeout=5) as response:
        check("multi-result webhook accepted", response.status, 200)
    _same_frame_observations = engine.drain_vehicle_observations()
    check(
        "overlapping results from one provider frame remain two encounters",
        (
            len(_same_frame_observations),
            len({
                vehicle.encounter_id
                for vehicle in _same_frame_observations
            }),
        ),
        (2, 2),
    )
    _transport_encounter = engine._encounters[0]["encounter_id"]
    engine.mark_encounter_identified(_transport_encounter)
    check("identification immediately releases retained audit JPEG",
          engine._encounters[0]["vehicle"].diagnostic_image_jpeg, None)
finally:
    engine.close()


print("\n=== vehicle-first identity settlement ===")


class _IdentityCfg:
    class thresholds:
        min_detection_confidence = 0.70
        min_ocr_confidence = 0.85
        min_normalized_length = 3
        max_normalized_length = 12
        vote_min_frames = 2
        high_ocr_single_frame = 0.99

    class alpr:
        class thresholds:
            identity_settle_quiet_seconds = 1.0
            identity_settle_max_seconds = 3.0
            consensus_rescue_min_frames = 3
            consensus_rescue_detect_min = 0.65
            consensus_rescue_ocr_min = 0.80


def _identity_read(text, encounter, det=0.90, ocr=0.995, image=None):
    return RawPlateRead(
        text=text,
        detection_confidence=det,
        ocr_confidence=ocr,
        bbox=(100, 100, 200, 140),
        vehicle_bbox=(50, 50, 300, 250),
        encounter_id=encounter,
        image=image,
    )


_settle = StreamWebhookEngine(_IdentityCfg())
_settle.submit([_identity_read("ABC", "car-1")], now=100.0)
_settle.submit([_identity_read("ABC123", "car-1")], now=100.6)
check("encounter identity remains provisional during the quiet wait",
      _settle.infer(None, now=101.0), [])
_settled = _settle.infer(None, now=101.7)
check("a confirmable fuller substring replaces the first fragment",
      [r.text for r in _settled], ["ABC123"])
check("settled encounter drains once",
      _settle.infer(None, now=102.0), [])

_vanity = StreamWebhookEngine(_IdentityCfg())
_vanity.submit([
    _identity_read("ABC", "car-vanity", ocr=0.996),
    _identity_read("ABC", "car-vanity", ocr=0.997),
    _identity_read("ABC1", "car-vanity", ocr=0.999),
], now=150.0)
check("one appended character cannot beat a supported 3-char vanity plate",
      [r.text for r in _vanity.infer(None, now=151.1)], ["ABC", "ABC"])

_agreement = StreamWebhookEngine(_IdentityCfg())
_agreement.submit([
    _identity_read("STRONG7", "car-2", ocr=0.96),
    _identity_read("STRONG7", "car-2", ocr=0.97),
    _identity_read("OTHER9", "car-2", ocr=0.995),
], now=200.0)
_agreement_reads = _agreement.infer(None, now=201.1)
check("exact agreement beats an unrelated high single read",
      [r.text for r in _agreement_reads], ["STRONG7", "STRONG7"])
check("settlement marks the winning result when another variant competed",
      [r.had_competing_variants for r in _agreement_reads], [True, True])

_uncontested = StreamWebhookEngine(_IdentityCfg())
_uncontested.submit([
    _identity_read("ONLY77", "car-only", det=0.39, ocr=1.0),
], now=250.0)
check("an uncontested settled result is not marked disputed",
      [r.had_competing_variants
       for r in _uncontested.infer(None, now=251.1)], [False])

_bounded = StreamWebhookEngine(_IdentityCfg())
_bounded.submit([_identity_read("WAIT88", "car-3")], now=300.0)
_bounded.submit([_identity_read("WAIT88", "car-3")], now=302.8)
check("continuous reads cannot extend settlement past its hard maximum",
      len(_bounded.infer(None, now=303.1)), 2)

_image_stage = StreamWebhookEngine(_IdentityCfg())
_image_stage.submit([
    _identity_read("IMAGE1", "car-4", ocr=0.95, image=object()),
    _identity_read("IMAGE1", "car-4", ocr=0.99, image=object()),
], now=400.0)
_held = _image_stage._identity_stages["car-4"]["reads"]
check("settlement retains only one decoded frame per exact variant",
      sum(r.image is not None for r in _held), 1)
_image_stage.submit([
    _identity_read(f"VARIANT{i}", "car-4", ocr=0.90 + i / 100,
                   image=object())
    for i in range(6)
], now=400.1)
_held = _image_stage._identity_stages["car-4"]["reads"]
check("settlement caps decoded frames even when OCR flaps across variants",
      sum(r.image is not None for r in _held), 4)

print("\n=== detection_mode=vehicle: cars with no readable plate ===")

from plate_reader.stream_engine import parse_vehicle_only  # noqa: E402

VEHICLE_MODE = {"data": {"results": [
    {"plate": "bxv8862", "score": 0.999, "dscore": 0.905,
     "box": {"xmin": 1, "ymin": 2, "xmax": 3, "ymax": 4},
     "vehicle": {"type": "Sedan", "score": 0.88,
                 "box": {"xmin": 10, "ymin": 20, "xmax": 30, "ymax": 40}},
     "model_make": [{"make": "Toyota", "model": "Camry", "score": 0.71},
                    {"make": "Lexus", "model": "ES", "score": 0.10}],
     "color": [{"color": "silver", "score": 0.83},
               {"color": "grey", "score": 0.11}]},
    {"vehicle": {"type": "Pickup Truck", "score": 0.79,
                 "box": {"xmin": 50, "ymin": 60, "xmax": 70, "ymax": 80}},
     "model_make": [{"make": "Ford", "model": "F-150", "score": 0.66}],
     "color": [{"color": "white", "score": 0.91}]},
]}}

_vm_reads = parse_webhook_payload(VEHICLE_MODE)
check("plated car still yields exactly one read", len(_vm_reads), 1)
check("top-ranked MMC is carried, not the runner-up",
      (_vm_reads[0].make, _vm_reads[0].model, _vm_reads[0].color),
      ("Toyota", "Camry", "silver"))

_vm_vehicles = parse_vehicle_only(VEHICLE_MODE)
check("the plateless car is surfaced as a vehicle", len(_vm_vehicles), 1)
check("...with its own MMC and type",
      (_vm_vehicles[0].vehicle_type, _vm_vehicles[0].make,
       _vm_vehicles[0].color), ("Pickup Truck", "Ford", "white"))
# The plated car must never appear in BOTH lists, or one physical vehicle
# would be counted twice: once as a customer and once as an anonymous car.
check("a plated car is never ALSO counted as plateless",
      all(v.vehicle_type != "Sedan" for v in _vm_vehicles), True)
# StreamVehicle has no plate field at all, by construction — a plateless
# vehicle cannot be hashed into an identity even by mistake.
check("StreamVehicle carries no plate/text field to hash",
      hasattr(_vm_vehicles[0], "text") or hasattr(_vm_vehicles[0], "plate"),
      False)
check("plain parsing carries no image without multipart evidence",
      _vm_vehicles[0].diagnostic_image_jpeg, None)
check("plate-mode payloads still surface zero plateless vehicles",
      parse_vehicle_only(SAMPLE), [])



print("\n=== detection_mode=vehicle uses a DIFFERENT payload schema ===")

# THE OUTAGE THIS PREVENTS. Switching detection_mode to `vehicle` silently
# changes the payload SHAPE, which the vendor docs do not present as a schema
# change:
#
#   plate mode  : result["plate"] is a STRING, OCR in result["score"],
#                 detection in result["dscore"], MMC at the top level.
#   vehicle mode: result["plate"] is a DICT whose "score" is the DETECTION
#                 score; the OCR candidates are at ["props"]["plate"], and
#                 MMC moves under ["vehicle"]["props"].
#
# Parsing only the first shape made isinstance(plate, str) fail, so every
# plate was dropped: no vote ever opened, `open votes=0` forever, and each
# car was logged as a plateless VEHICLE_ONLY. Live, this lost 12 confirmable
# customers in one morning. Row below is verbatim from the unit's CSV.
VEHICLE_SCHEMA = {"data": {"results": [{
    "plate": {
        "type": "Plate", "score": 0.88,
        "box": {"xmin": 1337, "ymin": 121, "xmax": 1444, "ymax": 207},
        "props": {
            "plate": [{"value": "m5n232", "score": 1.0}],
            "region": [{"value": "us-ky", "score": 0.475}],
        },
    },
    "vehicle": {
        "type": "Sedan", "score": 0.908,
        "box": {"xmin": 382, "ymin": 5, "xmax": 1455, "ymax": 425},
        "props": {
            "make_model": [{"make": "Ford", "model": "Fusion Energi",
                            "score": 0.161}],
            "color": [{"value": "silver", "score": 0.715}],
        },
    },
}]}}

_vs = parse_webhook_payload(VEHICLE_SCHEMA)
check("vehicle-mode payload yields a read at all", len(_vs), 1)
check("plate text comes from props.plate[0].value", _vs[0].text, "m5n232")
# plate.score is DETECTION here, not OCR. Swapping them silently breaks both
# gates -- 0.88 would masquerade as the OCR score and 1.0 as detection.
check("plate.score -> detection_confidence", _vs[0].detection_confidence, 0.88)
check("props.plate[0].score -> ocr_confidence", _vs[0].ocr_confidence, 1.0)
check("MMC read from vehicle.props.make_model / .color",
      (_vs[0].make, _vs[0].model, _vs[0].color),
      ("Ford", "Fusion Energi", "silver"))
check("region captured as jurisdiction (diagnostics only, never hashed)",
      _vs[0].jurisdiction, "us-ky")
_r, _v = classify_results(VEHICLE_SCHEMA, 0.75, 0.85)
check("this car CONFIRMS rather than logging as plateless",
      (len(_r), len(_v)), (1, 0))

# The older plate-mode shape must keep working -- config can be switched back.
check("plate-mode payload still parses unchanged",
      (parse_webhook_payload(SAMPLE)[0].text,
       parse_webhook_payload(SAMPLE)[0].detection_confidence,
       parse_webhook_payload(SAMPLE)[0].ocr_confidence),
      ("slz9043m", 0.641, 0.906))

print("\n=== a car is counted even when its plate is unreadable ===")

# The three cases that matter, at the live LJS gates. The third is the one
# that silently lost cars before: a vehicle IS present, Stream saw it, but
# the plate came back too weak to trust. Returning only the diagnostic
# dropped it from traffic counts -- the exact blind spot vehicle mode exists
# to close.
_DET, _OCR = 0.75, 0.85
_VEH = {"type": "Sedan", "score": 0.9,
        "box": {"xmin": 0, "ymin": 0, "xmax": 9, "ymax": 9}}
_PBOX = {"xmin": 1, "ymin": 1, "xmax": 2, "ymax": 2}


def _classify(result):
    reads, vehs = classify_results({"data": {"results": [result]}}, _DET, _OCR)
    identified = any(r.detection_confidence >= _DET and r.ocr_confidence >= _OCR
                     for r in reads)
    return len(reads), len(vehs), identified


check("strong plate -> identity, and NOT also an anonymous vehicle",
      _classify({"plate": "bxv8862", "score": 0.99, "dscore": 0.90,
                 "box": _PBOX, "vehicle": _VEH}), (1, 0, True))
check("no plate at all -> counted as a vehicle",
      _classify({"vehicle": _VEH}), (0, 1, False))
check("WEAK plate -> car still counted, read kept as diagnostic",
      _classify({"plate": "bxv8862", "score": 0.60, "dscore": 0.80,
                 "box": _PBOX, "vehicle": _VEH}), (1, 1, False))
check("weak plate with NO vehicle box -> diagnostic only, nothing counted",
      _classify({"plate": "bxv8862", "score": 0.60, "dscore": 0.80,
                 "box": _PBOX}), (1, 0, False))
# Guard the boundary: exactly at the gates must count as identified, so a
# car sitting on the threshold is never counted twice.
check("a read exactly ON the gates is an identity, not a vehicle",
      _classify({"plate": "bxv8862", "score": _OCR, "dscore": _DET,
                 "box": _PBOX, "vehicle": _VEH}), (1, 0, True))

print("\n=== plateless vehicles must be coalesced, not flooded ===")

# THE FAILURE THIS PREVENTS: Stream can report several sparse predictions for
# one plateless vehicle, and unlike a plate read there is no string to dedup on.
# A dwelling car would otherwise post several anonymous visits. Encounters are
# merged while the car is present and emitted once.
from plate_reader.stream_engine import StreamVehicle  # noqa: E402


class _VCfg:
    class stream:
        webhook_host = "127.0.0.1"
        webhook_port = 8401
        vehicle_quiet_seconds = 3.0
        unidentified_resolution_seconds = 3.0


def _veh(box, conf=0.8, **kw):
    return StreamVehicle(vehicle_bbox=box, vehicle_confidence=conf, **kw)


_T = 1000.0
_e = StreamWebhookEngine(_VCfg())
for _i in range(300):                       # 60s of a parked car at eFPS 5
    _e.submit_vehicles([_veh((100, 100, 200, 200))], now=_T + _i * 0.2)
check("a car still present is NOT emitted early",
      _e.drain_vehicles(now=_T + 60), [])
check("300 detections of one parked car collapse to ONE vehicle",
      len(_e.drain_vehicles(now=_T + 64)), 1)

_e2 = StreamWebhookEngine(_VCfg())
_e2.submit_vehicles([_veh((0, 0, 100, 100)), _veh((500, 500, 600, 600))], now=_T)
check("non-overlapping boxes stay two distinct vehicles",
      len(_e2.drain_vehicles(now=_T + 5)), 2)

_e3 = StreamWebhookEngine(_VCfg())
_e3.submit_vehicles([_veh((10, 10, 110, 110))], now=_T)
_e3.note_plate_read(now=_T + 1)
check("a car whose plate WAS read is not also counted anonymously",
      _e3.drain_vehicles(now=_T + 5), [])

_e4 = StreamWebhookEngine(_VCfg())
_e4.submit_vehicles([_veh((10, 10, 110, 110), 0.5)], now=_T)
_e4.submit_vehicles([
    _veh((12, 12, 112, 112), 0.9, make="Ford", model="Escape",
         color="white",
         diagnostic_image_jpeg=b"stronger-frame")
],
                    now=_T + 0.2)
_merged = _e4.drain_vehicles(now=_T + 5)[0]
check("vehicle encounter retains one strongest compressed original",
      _merged.diagnostic_image_jpeg, b"stronger-frame")
check("merged encounter keeps confidence and fills a complete MMC pair",
      (_merged.vehicle_confidence, _merged.make, _merged.model, _merged.color),
      (0.9, "Ford", "Escape", "white"))

_e_enc = StreamWebhookEngine(_VCfg())
_enc_id = _e_enc.observe_vehicle(
    _veh((10, 10, 110, 110), make="Ford", color="blue"),
    now=_T,
    saw_plate=True,
    plated_attempt=True,
    candidate_key="WEAK123",
    plate_detection_confidence=0.68,
    plate_ocr_confidence=0.92,
)
_enc_id_2 = _e_enc.observe_vehicle(
    _veh((12, 12, 112, 112), make="Ford", color="blue"),
    now=_T + 1,
    saw_plate=True,
    plated_attempt=True,
    candidate_key="WEAK123",
    plate_detection_confidence=0.69,
    plate_ocr_confidence=0.94,
)
check("all observations of one car keep one stable encounter ID",
      _enc_id_2, _enc_id)
_live_obs = _e_enc.drain_vehicle_observations()
check("live vehicle observations bridge Stream into presence tracking",
      (len(_live_obs), {v.encounter_id for v in _live_obs}),
      (2, {_enc_id}))
check("live vehicle observations drain exactly once",
      _e_enc.drain_vehicle_observations(), [])
check("a per-frame plate candidate is NOT a confirmed identity",
      _e_enc._encounters[0]["identified"], False)
_weak_terminal = _e_enc.drain_vehicles(now=_T + 5)[0]
check("a never-confirmed plate becomes one insufficient-confidence encounter",
      (_weak_terminal.encounter_id, _weak_terminal.saw_plate,
       _weak_terminal.plate_detection_confidence,
       _weak_terminal.plate_ocr_confidence),
      (_enc_id, True, 0.69, 0.94))

_e_confirm = StreamWebhookEngine(_VCfg())
_confirmed_id = _e_confirm.observe_vehicle(
    _veh((20, 20, 120, 120)), now=_T, saw_plate=True,
)
_meta = _e_confirm.mark_encounter_identified(_confirmed_id)
check("the detector confirmation marks that exact encounter",
      (_meta["encounter_id"], _e_confirm._encounters[0]["identified"]),
      (_confirmed_id, True))
check("a confirmed encounter never also emits an anonymous terminal visit",
      _e_confirm.drain_vehicles(now=_T + 5), [])

print("\n=== speculative directed handoff stays disabled ===")


class _HandoffCfg:
    class stream:
        webhook_host = "127.0.0.1"
        webhook_port = 8408
        vehicle_quiet_seconds = 12.0
        unidentified_resolution_seconds = 90.0

    class presence:
        camera_position = "behind_cars"
        encounter_handoff_seconds = 5.0


_near_box = (900, 500, 1450, 1080)
_far_box = (1050, 0, 1430, 400)

_handoff = StreamWebhookEngine(_HandoffCfg())
_front_id = _handoff.observe_vehicle(
    _veh(_near_box, make="Dodge", model="Dart", color="silver"),
    now=_T,
)
_rear_id = _handoff.observe_vehicle(
    _veh(_far_box, make="Chevrolet", model="Equinox", color="silver"),
    now=_T + 3.1,
    saw_plate=True,
    plated_attempt=True,
    candidate_key="ABC123",
)
check(
    "legacy encounter_handoff_seconds is accepted but cannot join profiles",
    _rear_id == _front_id,
    False,
)
_handoff.mark_encounter_identified(_rear_id, "ABC123")
check(
    "only native trajectory proof may reconcile split front/rear profiles",
    len(_handoff.drain_vehicles(now=_T + 100)),
    1,
)

print("\n=== site route qualifies vehicle profiles, not first snapshots ===")


class _RouteCfg:
    class stream:
        webhook_host = "127.0.0.1"
        webhook_port = 8411
        vehicle_quiet_seconds = 12.0
        unidentified_resolution_seconds = 90.0

    class capture:
        width = 1456
        height = 1088

    class thresholds:
        min_detection_confidence = 0.75
        min_ocr_confidence = 0.90

    class presence:
        camera_position = "behind_cars"
        encounter_handoff_seconds = 5.0
        board_zone = (0.0, 0.0, 0.72, 0.25)

        class route_qualification:
            enabled = True
            shadow_only = False
            profile_reconciliation_enabled = True
            entry_zone = (0.52, 0.26, 1.0, 1.0)
            decision_timeout_seconds = 120.0

        @staticmethod
        def resolved_board_zone():
            return _RouteCfg.presence.board_zone


class _RouteShadowCfg(_RouteCfg):
    class presence(_RouteCfg.presence):
        class route_qualification(
            _RouteCfg.presence.route_qualification
        ):
            shadow_only = True


_ENTRY = (800, 300, 1450, 1080)
_MID = (500, 180, 1150, 700)
_BOARD = (200, 0, 1000, 400)
_BOARD_LEFT_EDGE = (0, 0, 200, 300)
_UPPER_RIGHT = (1080, 0, 1450, 300)


def _box_dict(box):
    return dict(zip(("xmin", "ymin", "xmax", "ymax"), box))


def _dwell_payload(boxes, *, plate=None, start="2026-08-26T15:14:05Z",
                   end="2026-08-26T15:14:12Z", positions=None):
    if positions is None:
        positions = (
            [0.0]
            if len(boxes) == 1
            else [
                6.0 * index / (len(boxes) - 1)
                for index in range(len(boxes))
            ]
        )
    result = {
        "dwell_time": 7.0,
        "first_seen": start,
        "last_seen": end,
        "trajectory": [
            {
                "position_sec": float(position),
                "vehicle": {
                    "type": "Sedan",
                    "dscore": 0.9,
                    "box": _box_dict(box),
                },
            }
            for position, box in zip(positions, boxes)
        ],
    }
    if plate is not None:
        result["plate"] = plate
    return {
        "hook": {"event": "dwell_time", "id": "camera-1"},
        "data": {"results": [result]},
    }


_early = StreamWebhookEngine(_RouteCfg())
_early_id = _early.observe_vehicle(
    _veh(_ENTRY, color="red"), now=_T,
    observed_at="2026-08-26T15:14:05Z",
)
_early.observe_vehicle(
    _veh(_MID, color="red"), now=_T + 1.5,
    observed_at="2026-08-26T15:14:06.5Z",
)
_early.observe_vehicle(
    _veh(_BOARD, color="red"), now=_T + 3,
    observed_at="2026-08-26T15:14:08Z",
)
check("plate-free boxes remain candidates until native dwell completes",
      _early.route_status(_early_id), "pending")
check("plate-free candidate is not prematurely finalized before dwell",
      _early.drain_vehicles(now=_T + 20), [])
_early.submit_dwell_payload(_dwell_payload(
    [_ENTRY, _MID, _BOARD],
    end="2026-08-26T15:14:08Z",
    positions=[0.0, 1.5, 3.0],
))
_early_terminal = _early.drain_vehicles(now=time.monotonic() + 91)
check("completed valid trajectory emits one anonymous vehicle",
      (len(_early_terminal), _early_terminal[0].encounter_id),
      (1, _early_id))

_board_plate = StreamWebhookEngine(_RouteCfg())
_board_plate_id = _board_plate.observe_vehicle(
    _veh(_BOARD, color="blue"), now=_T,
    observed_at="2026-08-26T15:14:08Z",
    saw_plate=True, plated_attempt=True, candidate_key="BOARD123",
)
_board_plate_meta = _board_plate.route_metadata(_board_plate_id)
check("a plated board profile stays pending for terminal route proof",
      (_board_plate_meta["route_status"],
       _board_plate_meta["route_reason"],
       _board_plate_meta["route_qualified_at"]),
      ("pending", "plate_at_board_pending_trajectory", None))

_upper_plate = StreamWebhookEngine(_RouteCfg())
_upper_plate_id = _upper_plate.observe_vehicle(
    _veh(_UPPER_RIGHT, color="blue"), now=_T,
    observed_at="2026-08-26T15:14:08Z",
    saw_plate=True, plated_attempt=True, candidate_key="UPPER123",
)
check("a plated upper-right profile is not board-qualified",
      _upper_plate.route_status(_upper_plate_id), "pending")

_reordered = StreamWebhookEngine(_RouteCfg())
_reordered_id = _reordered.observe_vehicle(
    _veh(_ENTRY, color="brown"), now=_T,
    observed_at="2026-08-26T15:15:00Z",
)
_reordered_payload = _dwell_payload(
    [_ENTRY, _MID, _BOARD],
    start="2026-08-26T15:15:00Z", end="2026-08-26T15:15:07Z",
)
_reordered_payload["data"]["results"][0]["trajectory"].reverse()
_reordered.submit_dwell_payload(_reordered_payload)
_reordered.drain_vehicles(now=time.monotonic() + 31)
check("trajectory position order, not JSON list order, controls the route",
      _reordered.route_status(_reordered_id), "qualified")

_fallback = StreamWebhookEngine(_RouteCfg())
_fallback_id = _fallback.observe_vehicle(
    _veh(_ENTRY, color="silver"), now=_T,
    observed_at="2026-08-26T15:16:00Z",
)
_fallback.observe_vehicle(
    _veh(_MID, color="silver"), now=_T + 1.5,
    observed_at="2026-08-26T15:16:01.5Z",
)
_fallback.observe_vehicle(
    _veh(_BOARD, color="silver"), now=_T + 3,
    observed_at="2026-08-26T15:16:03Z",
)
check("plate-free sparse boxes never gain route authority without dwell",
      _fallback.route_status(_fallback_id), "pending")
check("missing dwell fails closed instead of fabricating traffic",
      _fallback.drain_vehicles(now=_T + 122), [])

_shadow = StreamWebhookEngine(_RouteShadowCfg())
_shadow.submit_dwell_payload(_dwell_payload([_UPPER_RIGHT, _BOARD]))
check("shadow dwell never creates a production encounter",
      _shadow.drain_vehicles(now=_T + 500), [])

_shadow_existing = StreamWebhookEngine(_RouteShadowCfg())
_shadow_existing_id = _shadow_existing.observe_vehicle(
    _veh(_ENTRY, color="green"), now=_T,
    observed_at="2026-08-26T15:18:00Z",
)
_shadow_existing.submit_dwell_payload(_dwell_payload(
    [_ENTRY, _MID, _BOARD],
    start="2026-08-26T15:18:00Z", end="2026-08-26T15:18:07Z",
))
check("shadow dwell never changes a candidate's route authority",
      _shadow_existing.route_status(_shadow_existing_id), "pending")

_cross = StreamWebhookEngine(_RouteCfg())
_cross_id = _cross.observe_vehicle(
    _veh(_UPPER_RIGHT, color="black"), now=_T,
    observed_at="2026-08-26T15:20:00Z",
)
_cross.submit_dwell_payload(_dwell_payload(
    [_UPPER_RIGHT, (900, 0, 1300, 280)],
    start="2026-08-26T15:20:00Z", end="2026-08-26T15:20:04Z",
))
check("upper-band cross traffic is rejected",
      _cross.route_status(_cross_id), "rejected")
check("upper-band cross traffic never becomes a Visit",
      _cross.drain_vehicles(now=time.monotonic() + 1), [])

_driveoff = StreamWebhookEngine(_RouteCfg())
_driveoff_id = _driveoff.observe_vehicle(
    _veh(_ENTRY, color="white"), now=_T,
    observed_at="2026-08-26T15:22:00Z",
)
_driveoff.submit_dwell_payload(_dwell_payload(
    [_ENTRY, _MID, _UPPER_RIGHT],
    start="2026-08-26T15:22:00Z", end="2026-08-26T15:22:06Z",
))
check("entry vehicle that exits upper-right before board is rejected",
      _driveoff.route_status(_driveoff_id), "rejected")
check("entry-lane drive-off never becomes a Visit",
      _driveoff.drain_vehicles(now=time.monotonic() + 1), [])

_retry = StreamWebhookEngine(_RouteCfg())
_retry_id = _retry.observe_vehicle(
    _veh(_ENTRY, color="white"), now=_T,
    observed_at="2026-08-26T15:24:00Z",
)
_retry_payload = _dwell_payload(
    [_ENTRY, _MID, _BOARD],
    start="2026-08-26T15:24:00Z", end="2026-08-26T15:24:07Z",
)
_retry.submit_dwell_payload(_retry_payload)
_retry.drain_vehicles(now=time.monotonic() + 31)
check("first completed dwell retry emits its candidate once",
      len(_retry.drain_vehicles(now=time.monotonic() + 122)), 1)
_retry.submit_dwell_payload(_retry_payload)
check("replayed dwell cannot manufacture a second anonymous Visit",
      _retry.drain_vehicles(now=time.monotonic() + 1), [])

_missing_time = StreamWebhookEngine(_RouteCfg())
_missing_id = _missing_time.observe_vehicle(
    _veh(_ENTRY, color="blue"), now=_T,
    observed_at="2026-08-26T15:26:00Z",
)
_missing_payload = _dwell_payload([_ENTRY, _MID, _BOARD])
_missing_result = _missing_payload["data"]["results"][0]
_missing_result.pop("first_seen")
_missing_result.pop("last_seen")
_missing_time.submit_dwell_payload(_missing_payload)
check("missing timestamps cannot authorize a geometry-only association",
      _missing_time.route_status(_missing_id), "pending")

_ordered_time = StreamWebhookEngine(_RouteCfg())
_ordered_id = _ordered_time.observe_vehicle(
    _veh(_ENTRY, color="orange"), now=_T,
    observed_at="2026-08-26T15:28:10Z",
)
_ordered_time.observe_vehicle(
    _veh(_ENTRY, color="orange"), now=_T + 1,
    observed_at="2026-08-26T15:28:05Z",
)
_ordered_meta = _ordered_time.route_metadata(_ordered_id)
check("cached older webhook extends firstSeen without reversing lastSeen",
      (_ordered_meta["first_seen_at"], _ordered_meta["last_seen_at"]),
      ("2026-08-26T15:28:05Z", "2026-08-26T15:28:10Z"))

# Image 3's measured failure: front profile, then a plate event three seconds
# later, but Stream/MMC changed colour and the non-overlapping boxes opened two
# encounters. The completed native trajectory owns the physical vehicle and
# rejoins both profiles without using make/model/color as identity.
_late_plate = StreamWebhookEngine(_RouteCfg())
_front_profile = _late_plate.observe_vehicle(
    _veh(_ENTRY, make="Peugeot", model="208", color="blue"),
    now=_T, observed_at="2026-08-26T15:14:05Z",
)
_plate_profile = _late_plate.observe_vehicle(
    _veh(_BOARD, make="Ford", model="Focus", color="black"),
    now=_T + 6.0, observed_at="2026-08-26T15:14:11Z",
    saw_plate=True, plated_attempt=True, candidate_key="ABC123",
)
check("non-overlapping front and rear events stay separate before dwell proof",
      _plate_profile == _front_profile, False)
_late_plate.mark_encounter_identified(_plate_profile, "ABC123")
_late_plate.submit_dwell_payload(_dwell_payload(
    [_ENTRY, _MID, _BOARD], plate="abc123",
))
check("native trajectory aliases the early front profile to the plate profile",
      _late_plate.canonical_encounter_id(_front_profile), _plate_profile)
check("late plate resolves the same qualified vehicle profile",
      (_late_plate.route_status(_front_profile),
       _late_plate.route_status(_plate_profile)),
      ("qualified", "qualified"))
check("resolved late-plate profile never also becomes anonymous",
      _late_plate.drain_vehicles(now=time.monotonic() + 1), [])

# Image 30's measured failure: Stream localized a dealer/model decal before
# the real rear plate was visible. Both scores missed their engine gates. That
# false localization remains audit evidence, but it cannot block the later
# real plate from owning the completed physical trajectory.
_false_then_real = StreamWebhookEngine(_RouteCfg())
_false_profile = _false_then_real.observe_vehicle(
    _veh(_ENTRY, make="Toyota", model="Camry", color="white"),
    now=_T, observed_at="2026-08-26T15:14:05Z",
    saw_plate=True, plated_attempt=True, candidate_key="OXMOOR",
    plate_detection_confidence=0.51,
    plate_ocr_confidence=0.70,
)
check("below-both-gates localization is not a separate route owner",
      _false_then_real._has_viable_plate_evidence(
          _false_then_real._encounter_locked(_false_profile)
      ), False)
check("one-gate evidence stays conservative for configured rescue paths",
      _false_then_real._has_viable_plate_evidence({
          "saw_plate": True,
          "plate_detection_confidence": 0.51,
          "plate_ocr_confidence": 0.95,
      }), True)
_real_profile = _false_then_real.observe_vehicle(
    _veh(_BOARD, make="Toyota", model="Camry", color="white"),
    now=_T + 6.0, observed_at="2026-08-26T15:14:11Z",
    saw_plate=True, plated_attempt=True, candidate_key="REAL123",
    plate_detection_confidence=0.91,
    plate_ocr_confidence=0.99,
)
_false_then_real.mark_encounter_identified(_real_profile, "REAL123")
_false_then_real.submit_dwell_payload(_dwell_payload(
    [_ENTRY, _MID, _BOARD], plate="real123",
))
check("below-both-gates decal does not block later real plate ownership",
      _false_then_real.canonical_encounter_id(_false_profile), _real_profile)
check("reconciled real plate owns one qualified physical profile",
      (_false_then_real.route_status(_false_profile),
       len(_false_then_real._encounters)),
      ("qualified", 1))

_left_board = StreamWebhookEngine(_RouteCfg())
_left_board_id = _left_board.observe_vehicle(
    _veh(_ENTRY, color="gray"), now=_T,
    observed_at="2026-08-26T15:30:00Z",
    saw_plate=True, plated_attempt=True, candidate_key="LEFT123",
)
_left_board.submit_dwell_payload(_dwell_payload(
    [_ENTRY, _MID, _BOARD_LEFT_EDGE], plate="left123",
    start="2026-08-26T15:30:00Z", end="2026-08-26T15:30:07Z",
))
check("full-left board zone accepts a valid entry-to-left-edge route",
      _left_board.route_status(_left_board_id), "qualified")

print("\n=== one physical vehicle is one traffic outcome (ROG260, 18 Aug) ===")

# Live failure: ROG260 confirmed at 10:09:04. It dwelled ~80s. Later frames
# still had OCR 0.91–0.95 but plate-box det 0.59–0.66 (below 0.75), so
# classify_results returned them as anonymous vehicles. The 3s quiet window
# was shorter than the 5–8s gaps between those frames, so one green pickup
# became 1 confirm + 4 VEHICLE_ONLY /visit rows. Dashboard 3/7.
#
# Quiet 12s + identified encounter (box and/or MMC) is the fix. Those later
# frames were det 0.59–0.66, still below the 0.70 det gate.


class _RogCfg:
    class stream:
        webhook_host = "127.0.0.1"
        webhook_port = 8402
        vehicle_quiet_seconds = 12.0
        unidentified_resolution_seconds = 12.0


_BOX = (10, 0, 650, 400)          # the live pickup box, roughly
_JUMP = (20, 0, 620, 380)         # later frame, still the same truck

_read = RawPlateRead(
    text="rog260", detection_confidence=0.88, ocr_confidence=0.94,
    bbox=(1, 1, 2, 2), vehicle_bbox=_BOX, vehicle_confidence=0.85,
    make="Ford", color="green",
)
check("vehicle_from_read carries box+MMC off a plated read (never hash input)",
      (vehicle_from_read(_read).make, vehicle_from_read(_read).color,
       vehicle_from_read(_read).vehicle_bbox),
      ("Ford", "green", _BOX))


def _pickup(make, color="green", box=_BOX, conf=0.4, vtype="Pickup Truck"):
    return _veh(box, conf, make=make, color=color, vehicle_type=vtype)


_e5 = StreamWebhookEngine(_RogCfg())
# Confirm at t=0, then the live dwell: Stream kept sending gate-clearing
# frames every couple of seconds. Drain runs every tick, like production.
_e5.note_plate_read(now=_T, vehicles=[_pickup("Ford", conf=0.85)])
for _t in range(2, 50, 2):
    _e5.note_plate_read(now=_T + _t, vehicles=[_pickup("Ford", conf=0.85)])
    _e5.drain_vehicles(now=_T + _t)
# Below-det / high-OCR frame of the same box, 2s after the last clearing hit.
_e5.submit_vehicles([_pickup("Ford", conf=0.27)], now=_T + 50)
check("after a confirm, a later weak frame of the same box is NOT a new car",
      _e5.drain_vehicles(now=_T + 65), [])

_e6 = StreamWebhookEngine(_RogCfg())
_e6.note_plate_read(now=_T, vehicles=[_pickup("Ford", conf=0.85)])
for _t in range(2, 50, 2):
    _e6.note_plate_read(now=_T + _t, vehicles=[_pickup("Ford", conf=0.85)])
    _e6.drain_vehicles(now=_T + _t)
# The four live gaps: +50, +55, +63, +71 (5s, 8s, 8s).
for _dt, _make, _conf in (
    (50, "Ford", 0.27),
    (55, "Dodge", 0.33),          # classifier flipped; still the green pickup
    (63, "Ford", 0.36),
    (71, "Ford", 0.43),
):
    _e6.submit_vehicles(
        [_pickup(_make, box=_JUMP if _dt > 50 else _BOX, conf=_conf)],
        now=_T + _dt,
    )
check("four below-gate hits 5–8s apart after a confirm still emit nothing",
      _e6.drain_vehicles(now=_T + 85), [])

_e6b = StreamWebhookEngine(_RogCfg())
_e6b.note_plate_read(now=_T, vehicles=[_pickup("Ford", conf=0.85)])
_e6b.drain_vehicles(now=_T + 13)  # identified encounter goes quiet
_e6b.submit_vehicles([_pickup("Ford", conf=0.27)], now=_T + 44)
check("plateless frame after quiet starts a new unresolved physical profile",
      len(_e6b.drain_vehicles(now=_T + 60)), 1)
_e6c = StreamWebhookEngine(_RogCfg())
_e6c.note_plate_read(
    now=_T,
    vehicles=[_veh(_BOX, 0.9, make="Toyota", color="white", vehicle_type="Sedan")],
)
_e6c.drain_vehicles(now=_T + 13)
_e6c.submit_vehicles(
    [_veh((900, 500, 1100, 700), 0.8, make="Lexus", model="ES",
          color="white",
          vehicle_type="Sedan")],
    now=_T + 44,
)
_lexus = _e6c.drain_vehicles(now=_T + 60)
check("a later white sedan with a different make is still counted anonymously",
      (len(_lexus), _lexus[0].make if _lexus else None), (1, "Lexus"))

_e6d = StreamWebhookEngine(_RogCfg())
_e6d.note_plate_read(
    now=_T,
    vehicles=[_veh(
        _BOX, 0.9, make="Toyota", model="Camry", color="white",
        vehicle_type="Sedan",
    )],
)
_e6d.drain_vehicles(now=_T + 13)
_e6d.submit_vehicles(
    [_veh(_JUMP, 0.8, make="Lexus", color="white", vehicle_type="SUV")],
    now=_T + 195,
    plated_attempt=True,
)
check("a below-gate plate 3min after a confirm is a NEW car, not the hold",
      _e6d.has_fresh_vehicle(now=_T + 195), True)
check("...and that new attempt is not marked identified",
      any(not e.get("identified") for e in _e6d._encounters), True)
_e6d.submit_vehicles(
    [_veh(_JUMP, 0.8, make="Lexus", color="white", vehicle_type="SUV")],
    now=_T + 201,
)
check("vehicle-only 6s later still counts as the same fresh attempt",
      _e6d.has_fresh_vehicle(now=_T + 201), True)
check("...and 12s after the last look the miss may flush",
      _e6d.has_fresh_vehicle(now=_T + 214), False)

_e7 = StreamWebhookEngine(_RogCfg())
# Never identified: only below-gate / plateless, including the live gaps.
for _dt, _make, _conf in ((0, "Ford", 0.27), (5, "Dodge", 0.33),
                          (13, "Ford", 0.36), (21, "Ford", 0.43)):
    _e7.submit_vehicles([_pickup(_make, conf=_conf)], now=_T + _dt)
check("never-identified dwelling truck is ONE anonymous visit, not four",
      len(_e7.drain_vehicles(now=_T + 35)), 1)

_e8 = StreamWebhookEngine(_RogCfg())
_e8.note_plate_read(now=_T, vehicles=[_pickup("Ford")])
_e8.submit_vehicles(
    [_veh((900, 500, 1100, 700), 0.8, make="Honda", model="Accord",
          color="white",
          vehicle_type="Sedan")],
    now=_T + 1,
)
_anon = _e8.drain_vehicles(now=_T + 20)
check("a second, non-overlapping car is still counted anonymously",
      (len(_anon), _anon[0].make if _anon else None), (1, "Honda"))

_e9 = StreamWebhookEngine(_RogCfg())
_e9.submit_vehicles([_pickup("Ford")], now=_T)
_e9.note_plate_read(now=_T + 1, vehicles=[_pickup("Ford", conf=0.9)])
check("plateless first, then a gate-clearing plate on the same box: 0 anonymous",
      _e9.drain_vehicles(now=_T + 20), [])

check("MMC: make+colour agree -> same vehicle",
      mmc_same_vehicle(_pickup("Ford"), _pickup("Ford", box=_JUMP)), True)
check("MMC: only colour agrees -> too weak",
      mmc_same_vehicle(_pickup("Ford"), _pickup("Honda")), False)
check("MMC: make conflicts (Ford vs Dodge) -> not an MMC match",
      mmc_same_vehicle(_pickup("Ford"), _pickup("Dodge")), False)

# Same appearance but physically disconnected boxes: MMC cannot join them.
# The second profile remains a traffic outcome unless route evidence later
# proves they are one vehicle.
_e10 = StreamWebhookEngine(_RogCfg())
_e10.note_plate_read(
    now=_T,
    vehicles=[_pickup("Ford", box=(0, 0, 100, 100))],
)
_e10.submit_vehicles(
    [_pickup("Dodge", box=(400, 400, 500, 500), conf=0.3)],
    now=_T + 4,
)
check("identified encounter does not join on MMC when boxes miss",
      len(_e10.drain_vehicles(now=_T + 20)), 1)

print("\n=== unresolved candidate continuity ends at physical quiet ===")


class _LateCfg:
    class stream:
        webhook_host = "127.0.0.1"
        webhook_port = 8403
        vehicle_quiet_seconds = 12.0
        unidentified_resolution_seconds = 90.0

    class thresholds:
        min_normalized_length = 3
        max_normalized_length = 12


_late = StreamWebhookEngine(_LateCfg())
_late_id = _late.observe_vehicle(
    _veh((10, 10, 110, 110), make="Nissan", model="Sentra", color="silver"),
    now=_T,
    saw_plate=True,
    plated_attempt=True,
    candidate_key="ABC123",
)
check("quiet encounter is held locally instead of posted anonymously at 12s",
      _late.drain_vehicles(now=_T + 13), [])
_late_id_2 = _late.observe_vehicle(
    _veh((400, 400, 520, 520), make="Nissan", model="Sentra", color="silver"),
    now=_T + 84,
    saw_plate=True,
    plated_attempt=True,
    candidate_key="ABC123",
)
check("an unconfirmed exact candidate cannot bridge 84s without physical proof",
      _late_id_2 == _late_id, False)
_late.mark_encounter_identified(_late_id_2, "ABC123")
_late_terminal = _late.drain_vehicles(now=_T + 180)
check("the original unresolved profile remains one terminal outcome",
      (len(_late_terminal), _late_terminal[0].encounter_id), (1, _late_id))

_different = StreamWebhookEngine(_LateCfg())
_first_id = _different.observe_vehicle(
    _veh((10, 10, 110, 110), make="Ford", model="Explorer", color="white"),
    now=_T, saw_plate=True, plated_attempt=True, candidate_key="ABC123",
)
_second_id = _different.observe_vehicle(
    _veh((12, 12, 112, 112), make="Ford", model="Explorer", color="white"),
    now=_T + 20, saw_plate=True, plated_attempt=True, candidate_key="XYZ789",
)
check("a different plate candidate opens a new car despite overlapping FOV/MMC",
      _second_id == _first_id, False)

print("\n=== NullCamera: this process must not touch the sensor ===")

# When Stream owns the camera (via MediaMTX), plate_reader must NOT open
# Picamera2 — libcamera hands the sensor to one consumer, so competing for
# it would starve the RTSP feed Stream reads. NullCamera keeps the detection
# loop's shape and pacing without any hardware.
from plate_reader.camera import NullCamera  # noqa: E402
from plate_reader.config import Capture  # noqa: E402

_null = NullCamera(Capture(width=1456, height=1088, target_fps=10))
_null.open()
_f = _null.capture()
check("returns a frame with the configured shape", _f.shape, (1088, 1456, 3))
check("same buffer is reused, not reallocated per tick",
      _null.capture() is _f, True)

_t0 = time.monotonic()
for _ in range(6):
    _null.capture()
_elapsed = time.monotonic() - _t0
# 6 further ticks at 10fps should take ~0.6s. Pacing matters because the
# vote window and cooldowns are wall-clock, so a free-running loop would
# collapse them.
check("paces the loop at target_fps rather than free-running",
      0.4 <= _elapsed <= 0.9, True)
_null.close()

print("\n=== config layer accepts pr_stream end to end ===")

# Both of these were real gaps found during the pre-cutover readiness check:
# engines.py knew about pr_stream but config.py's VALID_ENGINES did not, and
# the ultimateALPR license-token guard fired for an SDK pr_stream never loads.
import json as _json  # noqa: E402
import tempfile as _tempfile  # noqa: E402
from plate_reader.config import load as _load, ConfigError as _ConfigError  # noqa: E402
import plate_reader.main as _main  # noqa: E402
from plate_reader.engines import build_engine as _build_engine  # noqa: E402

_REPO = pathlib.Path(__file__).resolve().parents[1]


def _strip(d):
    return {k: _strip(v) for k, v in d.items()
            if not k.startswith("//")} if isinstance(d, dict) else d


def _cfg_path(engine, alpr_th=None, th=None, presence=None):
    c = _strip(_json.loads((_REPO / "config.example.json").read_text()))
    c.update(device_id="serv-unit-001", api_key="ab" * 32,
             location_id="ljs-louisville-001")
    c["alpr"]["engine"] = engine
    c["alpr"]["thresholds"].update(alpr_th or {})
    c["thresholds"].update(th or {})
    c.setdefault("presence", {}).update(presence or {})
    p = pathlib.Path(_tempfile.mkdtemp()) / "config.json"
    p.write_text(_json.dumps(c))
    return p


_GATES = {"min_group_detection_confidence": 0.70,
          "min_group_ocr_confidence": 0.80,
          "high_ocr_single_frame": 0.99, "vote_min_frames": 2}
_ALPR = {"app_detect_min": 0.70, "app_ocr_min": 0.80}

# THE OVERRIDE TRAP, pinned. presence.vote_min_frames silently wins over the
# thresholds copy (config.py ~1224), so editing only one side quietly runs a
# different vote than intended -- a mistake config.example.json records as
# having cost a 24-minute validation run. Both values are set explicitly here
# and deliberately disagree, so this asserts the precedence rule itself
# rather than whatever the example file happens to ship today.
_trap = _load(_cfg_path(
    "pr_stream", alpr_th=_ALPR,
    th={**_GATES, "vote_min_frames": 2},
    presence={"vote_min_frames": 3},
))
check("presence.vote_min_frames OVERRIDES the thresholds copy",
      _trap.thresholds.vote_min_frames, 3)

# ...and the shipping example must not fall into it: both copies agree.
_shipped = _strip(_json.loads((_REPO / "config.example.json").read_text()))
check("config.example.json keeps both vote_min_frames copies in step",
      (_shipped["thresholds"]["vote_min_frames"],
       _shipped["presence"]["vote_min_frames"]), (2, 2))
check("config.example.json ships the pr_stream cutover gates",
      (_shipped["alpr"]["engine"],
       _shipped["alpr"]["thresholds"]["app_detect_min"],
       _shipped["alpr"]["thresholds"]["app_ocr_min"],
       _shipped["thresholds"]["min_group_detection_confidence"],
       _shipped["thresholds"]["min_group_ocr_confidence"],
       _shipped["thresholds"]["high_ocr_single_frame"]),
      ("pr_stream", 0.70, 0.80, 0.70, 0.80, 0.99))
check("config.example.json ships the encounter-bound rescue gates",
      (_shipped["alpr"]["thresholds"]["consensus_rescue_min_frames"],
       _shipped["alpr"]["thresholds"]["consensus_rescue_detect_min"],
       _shipped["alpr"]["thresholds"]["consensus_rescue_ocr_min"]),
      (3, 0.65, 0.80))
check("config.example.json ships bounded vehicle identity settlement",
      (_shipped["alpr"]["thresholds"]["identity_settle_quiet_seconds"],
       _shipped["alpr"]["thresholds"]["identity_settle_max_seconds"]),
      (3.5, 4.0))
check("config.example.json retains no-op handoff key for rollback compatibility",
      _shipped["presence"]["encounter_handoff_seconds"], 5.0)
check("config.example.json ships guarded provider-selected confirmation",
      (_shipped["alpr"]["thresholds"]
       ["provider_selected_single_enabled"],
       _shipped["alpr"]["thresholds"]
       ["provider_selected_single_min_length"],
       _shipped["alpr"]["thresholds"]
       ["provider_selected_single_min_relative_x"]),
      (True, 4, 0.45))
check("config.example.json ships guarded high-OCR detection rescue",
      (_shipped["alpr"]["thresholds"]
       ["provider_high_ocr_rescue_enabled"],
       _shipped["alpr"]["thresholds"]
       ["provider_high_ocr_rescue_detect_min"],
       _shipped["alpr"]["thresholds"]
       ["provider_high_ocr_rescue_ocr_min"]),
      (True, 0.35, 0.98))
check("config.example.json ships a bounded all-outcome audit dataset",
      (_shipped["diagnostics"]["save_miss_plates"],
       _shipped["diagnostics"]["save_confirmed_plates"],
       _shipped["diagnostics"]["save_unidentified_vehicle_frames"],
       _shipped["diagnostics"]["miss_plate_dir"],
       _shipped["diagnostics"]["miss_plate_retain_hours"],
       _shipped["diagnostics"]["miss_plate_max_cases"]),
      (True, True, True,
       "/home/serv/plate_reader/audit_cases", 0.0, 300))
check("board/drive-off tracking ships shadow-only",
      _shipped["presence"]["shadow_only"], True)
check("route classifier ships shadow-only with profile reconciliation",
      (_shipped["presence"]["route_qualification"]["enabled"],
       _shipped["presence"]["route_qualification"]["shadow_only"],
       _shipped["presence"]["route_qualification"]
       ["profile_reconciliation_enabled"],
       _shipped["presence"]["route_qualification"]
       ["min_trajectory_points"],
       _shipped["presence"]["route_qualification"]
       ["min_trajectory_duration_seconds"],
       _shipped["presence"]["route_qualification"]
       ["zone_confirm_points"],
       _shipped["presence"]["route_qualification"]
       ["decision_timeout_seconds"]),
      (True, True, True, 8, 2.0, 2, 300.0))
check("route classifier ships the audited progressive-board OR path",
      (_shipped["presence"]["route_qualification"]
       ["progressive_board_rescue_min_points"],
       _shipped["presence"]["route_qualification"]
       ["progressive_board_rescue_min_board_points"],
       _shipped["presence"]["route_qualification"]
       ["progressive_board_rescue_min_seconds"]),
      (4, 2, 0.5))
check("image-derived entry and board zones ship as separate regions",
      (_shipped["presence"]["route_qualification"]["entry_zone"],
       _shipped["presence"]["board_zone"]),
      ({"x_min_pct": 0.52, "y_min_pct": 0.26,
        "x_max_pct": 1.0, "y_max_pct": 1.0},
       {"x_min_pct": 0.0, "y_min_pct": 0.0,
        "x_max_pct": 0.72, "y_max_pct": 0.25}))
check("plate identity eligibility ships to the left frame edge",
      (_shipped["detection_zone"]["enabled"],
       _shipped["detection_zone"]["x_min_pct"],
       _shipped["detection_zone"]["x_max_pct"],
       _shipped["detection_zone"]["y_min_pct"],
       _shipped["detection_zone"]["y_max_pct"]),
      (True, 0.0, 1.0, 0.0, 0.75))
_shipped_detection_zone = DetectionZone(
    enabled=_shipped["detection_zone"]["enabled"],
    x_min_pct=_shipped["detection_zone"]["x_min_pct"],
    x_max_pct=_shipped["detection_zone"]["x_max_pct"],
    y_min_pct=_shipped["detection_zone"]["y_min_pct"],
    y_max_pct=_shipped["detection_zone"]["y_max_pct"],
)
check("full-left eligibility admits a plate centroid left of old x=0.15",
      bbox_inside_zone(
          (20, 100, 100, 180), (1088, 1456, 3),
          _shipped_detection_zone,
      ), True)
check("full-left eligibility still rejects the near-field bottom band",
      bbox_inside_zone(
          (600, 850, 700, 950), (1088, 1456, 3),
          _shipped_detection_zone,
      ), False)

_missing_board_path = _cfg_path(
    "pr_stream", alpr_th=_ALPR, th=_GATES,
    presence={"vote_min_frames": 2},
)
_missing_board_raw = _json.loads(_missing_board_path.read_text())
_missing_board_raw["presence"].pop("board_zone")
_missing_board_path.write_text(_json.dumps(_missing_board_raw))
try:
    _load(_missing_board_path)
    _missing_board_result = "accepted"
except _ConfigError:
    _missing_board_result = "refused"
check("route authority refuses an implicit placeholder board zone",
      _missing_board_result, "refused")

_overlap_path = _cfg_path(
    "pr_stream", alpr_th=_ALPR, th=_GATES,
    presence={"vote_min_frames": 2},
)
_overlap_raw = _json.loads(_overlap_path.read_text())
_overlap_raw["presence"]["route_qualification"]["entry_zone"] = dict(
    _overlap_raw["presence"]["board_zone"]
)
_overlap_path.write_text(_json.dumps(_overlap_raw))
try:
    _load(_overlap_path)
    _overlap_result = "accepted"
except _ConfigError:
    _overlap_result = "refused"
check("route entry and board zones must not overlap",
      _overlap_result, "refused")

_touch_path = _cfg_path(
    "pr_stream", alpr_th=_ALPR, th=_GATES,
    presence={"vote_min_frames": 2},
)
_touch_raw = _json.loads(_touch_path.read_text())
_touch_raw["presence"]["route_qualification"]["entry_zone"].update({
    "x_min_pct": 0.52,
    "x_max_pct": 1.0,
    "y_min_pct": 0.25,
    "y_max_pct": 1.0,
})
_touch_path.write_text(_json.dumps(_touch_raw))
try:
    _load(_touch_path)
    _touch_result = "accepted"
except _ConfigError:
    _touch_result = "refused"
check("route zones sharing an inclusive boundary are refused",
      _touch_result, "refused")

_cut = _load(_cfg_path(
    "pr_stream", alpr_th=_ALPR, th=_GATES, presence={"vote_min_frames": 2},
))
check("config.py accepts engine 'pr_stream'", _cut.alpr.engine, "pr_stream")
check("build_engine returns the webhook engine",
      type(_build_engine(_cut.alpr)).__name__, "StreamWebhookEngine")
check("pr_stream selects NullCamera, not the sensor",
      _cut.alpr.engine in _main.ENGINES_WITH_OWN_CAPTURE, True)
check("LJS cutover gates survive the config layer intact",
      (_cut.thresholds.min_detection_confidence,
       _cut.thresholds.min_ocr_confidence,
       _cut.thresholds.group_detection_floor,
       _cut.thresholds.group_ocr_floor,
       _cut.thresholds.high_ocr_single_frame,
       _cut.thresholds.vote_min_frames),
      (0.70, 0.80, 0.70, 0.80, 0.99, 2))
check("LJS rescue gates survive the config layer intact",
      (_cut.alpr.thresholds.consensus_rescue_min_frames,
       _cut.alpr.thresholds.consensus_rescue_detect_min,
       _cut.alpr.thresholds.consensus_rescue_ocr_min),
      (3, 0.65, 0.80))
check("LJS provider-selected gates survive the config layer intact",
      (_cut.alpr.thresholds.provider_selected_single_enabled,
       _cut.alpr.thresholds.provider_selected_single_min_length,
       _cut.alpr.thresholds.provider_selected_single_min_relative_x),
      (True, 4, 0.45))
check("LJS high-OCR rescue gates survive the config layer intact",
      (_cut.alpr.thresholds.provider_high_ocr_rescue_enabled,
       _cut.alpr.thresholds.provider_high_ocr_rescue_detect_min,
       _cut.alpr.thresholds.provider_high_ocr_rescue_ocr_min),
      (True, 0.35, 0.98))
try:
    _load(_cfg_path(
        "pr_stream",
        alpr_th={
            **_ALPR,
            "identity_settle_quiet_seconds": 4.0,
            "identity_settle_max_seconds": 3.0,
        },
        th=_GATES,
        presence={"vote_min_frames": 2},
    ))
    _bad_settle = "allowed"
except _ConfigError:
    _bad_settle = "refused"
check("identity quiet wait cannot exceed the hard maximum",
      _bad_settle, "refused")

try:
    _load(_cfg_path(
        "pr_stream", alpr_th={"consensus_rescue_min_frames": 2}
    ))
    _bad_rescue_frames = "accepted"
except _ConfigError:
    _bad_rescue_frames = "refused"
check("consensus rescue refuses fewer than 3 frames",
      _bad_rescue_frames, "refused")

try:
    _load(_cfg_path("ultimatealpr"))
    _cross_engine_rescue = "accepted"
except _ConfigError:
    _cross_engine_rescue = "refused"
check("PR rescue floors cannot silently cross an engine swap",
      _cross_engine_rescue, "refused")

print("\n=== terminal vehicle payloads use the encounter ledger ===")
from types import SimpleNamespace as _NS  # noqa: E402


class _CaptureSender:
    def __init__(self):
        self.visits = []
        self.detection_events = []

    def send_visit(self, payload):
        self.visits.append(payload)

    def send_detection_event(self, payload):
        self.detection_events.append(payload)


class _CaptureCounters:
    def __init__(self):
        self.failures = []
        self.successes = []

    def record_failure(self, outcome):
        self.failures.append(outcome)

    def record_success(self, inference_duration_ms):
        self.successes.append(inference_duration_ms)


_payload_app = _main.App()
_payload_app.cfg = _NS(hours=_NS(is_open=lambda: True))
_payload_app.sender = _CaptureSender()
_payload_app.counters = _CaptureCounters()
_encounter = "ab" * 16
_payload_app._handle_plateless_vehicle(StreamVehicle(
    vehicle_bbox=(10, 10, 100, 100),
    vehicle_confidence=0.91,
    vehicle_type="Sedan",
    make="Nissan",
    model="Sentra",
    color="silver",
    encounter_id=_encounter,
    first_seen_at="2026-08-20T15:00:00.000Z",
    last_seen_at="2026-08-20T15:01:20.000Z",
    saw_plate=True,
    diagnostic_episode_id="cd" * 16,
    plate_detection_confidence=0.68,
    plate_ocr_confidence=0.94,
))
_miss_payload = _payload_app.sender.visits[-1]
check("plate-seen terminal Visit is one insufficient-confidence encounter",
      (_miss_payload["visitId"], _miss_payload["encounterId"],
       _miss_payload["plateDetection"]),
      (_encounter, _encounter, "insufficient_confidence"))
check("terminal Visit keeps first/last evidence and diagnostic link",
      (_miss_payload["firstSeenAt"], _miss_payload["lastSeenAt"],
       _miss_payload["diagnosticEpisodeId"]),
      ("2026-08-20T15:00:00.000Z", "2026-08-20T15:01:20.000Z", "cd" * 16))
check("raw plate text cannot appear on an anonymous terminal Visit",
      "licensePlate" in _miss_payload, False)

_payload_app._handle_plateless_vehicle(StreamVehicle(
    vehicle_bbox=(10, 10, 100, 100),
    vehicle_confidence=0.88,
    encounter_id="ef" * 16,
    first_seen_at="2026-08-20T15:02:00.000Z",
    last_seen_at="2026-08-20T15:02:30.000Z",
    saw_plate=False,
))
check("car with no plate stays traffic-only",
      _payload_app.sender.visits[-1]["plateDetection"], "no_visible_plate")

from plate_reader.detector import ConfirmedIdentification as _Confirmed  # noqa: E402


class _ProfileEngine:
    def mark_encounter_identified(
        self, encounter_id, confirmed_candidate_key=None
    ):
        return {
            "encounter_id": encounter_id,
            "first_seen_at": "2026-08-20T15:03:00.000Z",
            "last_seen_at": "2026-08-20T15:03:04.000Z",
            "color": "blue",
            "make": "Ford",
            "model": "F-150",
            "vehicle_type": "Pickup Truck",
        }


_profile_app = _main.App()
_profile_app.cfg = _NS(hours=_NS(is_open=lambda: True))
_profile_app.sender = _CaptureSender()
_profile_app.counters = _CaptureCounters()
_profile_app.detector = _NS(engine=_ProfileEngine())
_profile_encounter = "12" * 16
_profile_app._handle_outcome(
    _Confirmed(
        normalized="ABC123",
        detected_at_iso="2026-08-20T15:03:03.000Z",
        frame_count=2,
        detection_confidence=0.91,
        ocr_confidence=0.99,
        inference_duration_ms=15,
        encounter_id=_profile_encounter,
    ),
    {},
    300,
)
_profile_payload = _profile_app.sender.visits[-1]
check("recognized Visit keeps the physical vehicle profile ID and times",
      (_profile_payload["visitId"], _profile_payload["encounterId"],
       _profile_payload["firstSeenAt"], _profile_payload["lastSeenAt"]),
      (_profile_encounter, _profile_encounter,
       "2026-08-20T15:03:00.000Z", "2026-08-20T15:03:04.000Z"))
check("recognized Visit carries complete descriptive vehicle attributes",
      (_profile_payload["color"], _profile_payload["make"],
       _profile_payload["model"], _profile_payload["vehicleType"]),
      ("blue", "Ford", "F-150", "Pickup Truck"))


class _TerminalShadowEngine(_ProfileEngine):
    @staticmethod
    def terminal_visit_emitted(encounter_id):
        return True


_terminal_canonical_id = "16" * 16


class _TerminalAliasEngine(_TerminalShadowEngine):
    @staticmethod
    def terminal_visit_owner(encounter_id):
        return _terminal_canonical_id


class _PresenceIdentitySpy:
    def __init__(self):
        self.calls = []

    def mark_identified(self, normalized, vehicle_bbox):
        self.calls.append((normalized, vehicle_bbox))


_late_identity_app = _main.App()
_late_identity_app.cfg = _NS(hours=_NS(is_open=lambda: True))
_late_identity_app.sender = _CaptureSender()
_late_identity_app.counters = _CaptureCounters()
_late_identity_app.detector = _NS(engine=_TerminalShadowEngine())
_late_identity_app.presence = _PresenceIdentitySpy()
_late_identity_app._handle_outcome(
    _Confirmed(
        normalized="LATE12",
        detected_at_iso="2026-08-20T15:03:13.000Z",
        frame_count=2,
        detection_confidence=0.91,
        ocr_confidence=0.99,
        inference_duration_ms=15,
        encounter_id="13" * 16,
    ),
    {},
    300,
)
check("late shadow identity cannot become a second Visit",
      (len(_late_identity_app.sender.visits),
       len(_late_identity_app.counters.successes),
       _late_identity_app.presence.calls),
      (0, 0, [("LATE12", None)]))


class _DeferredProfileEngine(_ProfileEngine):
    def __init__(self):
        self.status = "pending"
        self.rejection_is_final = False

    @staticmethod
    def route_gate_enabled():
        return True

    @staticmethod
    def route_gate_shadow_only():
        return False

    def route_status(self, encounter_id):
        return self.status

    @staticmethod
    def route_decision_timeout_seconds():
        return 120.0

    def route_rejection_final(self, encounter_id, now=None):
        return self.rejection_is_final


_deferred_app = _main.App()
_deferred_app.cfg = _NS(hours=_NS(is_open=lambda: True))
_deferred_app.sender = _CaptureSender()
_deferred_app.counters = _CaptureCounters()
_deferred_engine = _DeferredProfileEngine()
_deferred_app.detector = _NS(engine=_deferred_engine)
_deferred_outcome = _Confirmed(
    normalized="ABC124",
    detected_at_iso="2026-08-20T15:04:03.000Z",
    frame_count=2,
    detection_confidence=0.92,
    ocr_confidence=0.99,
    inference_duration_ms=16,
    encounter_id="78" * 16,
)
_deferred_app._handle_outcome(_deferred_outcome, {}, 300)
check("recognized identity remains local while its route is pending",
      len(_deferred_app.sender.visits), 0)
check("pending recognized identity owns one deferred vehicle profile",
      len(_deferred_app._deferred_identifications), 1)
_deferred_engine.status = "qualified"
_deferred_app._drain_deferred_identifications({}, 300)
check("qualified deferred identity sends exactly one recognized Visit",
      (len(_deferred_app.sender.visits),
       _deferred_app.sender.visits[0]["plateDetection"]),
      (1, "recognized"))
check("qualified identity is removed from the deferred profile ledger",
      len(_deferred_app._deferred_identifications), 0)

_driveoff_app = _main.App()
_driveoff_app.cfg = _NS(hours=_NS(is_open=lambda: True))
_driveoff_app.sender = _CaptureSender()
_driveoff_app.counters = _CaptureCounters()
_driveoff_engine = _DeferredProfileEngine()
_driveoff_app.detector = _NS(engine=_driveoff_engine)
_driveoff_outcome = _Confirmed(
    normalized="TOP123",
    detected_at_iso="2026-08-20T15:04:13.000Z",
    frame_count=2,
    detection_confidence=0.94,
    ocr_confidence=0.99,
    inference_duration_ms=16,
    encounter_id="7a" * 16,
)
_driveoff_app._handle_outcome(_driveoff_outcome, {}, 300)
_driveoff_engine.status = "rejected"
_driveoff_engine.rejection_is_final = True
_driveoff_app._drain_deferred_identifications({}, 300)
check("plated terminal drive-off never reaches the Visit sender",
      (len(_driveoff_app.sender.visits),
       len(_driveoff_app._deferred_identifications)), (0, 0))

_grace_app = _main.App()
_grace_app.cfg = _NS(hours=_NS(is_open=lambda: True))
_grace_app.sender = _CaptureSender()
_grace_app.counters = _CaptureCounters()
_grace_engine = _DeferredProfileEngine()
_grace_engine.status = "rejected"
_grace_app.detector = _NS(engine=_grace_engine)
_grace_outcome = _Confirmed(
    normalized="ABC125",
    detected_at_iso="2026-08-20T15:05:03.000Z",
    frame_count=2,
    detection_confidence=0.93,
    ocr_confidence=0.99,
    inference_duration_ms=17,
    encounter_id="79" * 16,
)
_grace_app._handle_outcome(_grace_outcome, {}, 300)
check("recognized identity survives a provisional route rejection",
      (len(_grace_app.sender.visits),
       len(_grace_app._deferred_identifications)), (0, 1))
_grace_engine.status = "qualified"
_grace_app._drain_deferred_identifications({}, 300)
check("a split follow-up route can release the held identity",
      (len(_grace_app.sender.visits),
       _grace_app.sender.visits[0]["plateDetection"]),
      (1, "recognized"))

from plate_reader.detector import FailedIdentification as _Failed  # noqa: E402


def _failed_outcome(encounter_id, episode_id):
    return _Failed(
        outcome="below_threshold",
        detected_at_iso="2026-08-20T15:06:03.000Z",
        frame_count=2,
        top_detection_confidence=0.61,
        top_ocr_confidence=0.94,
        inference_duration_ms=18,
        reads_in_episode=2,
        episode_id=episode_id,
        episode_start="2026-08-20T15:06:00.000Z",
        episode_end="2026-08-20T15:06:03.000Z",
        encounter_id=encounter_id,
    )


class _AliasDeferredEngine(_DeferredProfileEngine):
    old_id = "84" * 16
    canonical_id = "85" * 16

    @classmethod
    def canonical_encounter_id(cls, encounter_id):
        if encounter_id in {cls.old_id, cls.canonical_id}:
            return cls.canonical_id
        return encounter_id


_reconciled_app = _main.App()
_reconciled_app.cfg = _NS(hours=_NS(is_open=lambda: True))
_reconciled_app.sender = _CaptureSender()
_reconciled_app.counters = _CaptureCounters()
_reconciled_engine = _AliasDeferredEngine()
_reconciled_app.detector = _NS(engine=_reconciled_engine)
_reconciled_app._handle_outcome(
    _failed_outcome(_AliasDeferredEngine.old_id, "86" * 16), {}, 300
)
check("early weak localization waits for physical route authority",
      len(_reconciled_app._deferred_failures), 1)
_reconciled_app._handle_outcome(
    _Confirmed(
        normalized="REAL123",
        detected_at_iso="2026-08-20T15:06:13.000Z",
        frame_count=2,
        detection_confidence=0.91,
        ocr_confidence=0.99,
        inference_duration_ms=15,
        encounter_id=_AliasDeferredEngine.canonical_id,
    ),
    {},
    300,
)
check("later identity cancels the aliased weak miss before KPI emission",
      (len(_reconciled_app._deferred_failures),
       len(_reconciled_app.sender.detection_events),
       len(_reconciled_app.counters.failures)),
      (0, 0, 0))


_late_failure_app = _main.App()
_late_failure_app.cfg = _NS(hours=_NS(is_open=lambda: True))
_late_failure_app.sender = _CaptureSender()
_late_failure_app.counters = _CaptureCounters()
_late_failure_app.detector = _NS(engine=_TerminalAliasEngine())
_late_failure_app._handle_outcome(
    _failed_outcome("14" * 16, "15" * 16), {}, 300
)
check("late shadow miss retains its diagnostic after the traffic Visit",
      (len(_late_failure_app.sender.detection_events),
       len(_late_failure_app.counters.failures),
       _late_failure_app.sender.detection_events[-1]["encounterId"]),
      (1, 1, _terminal_canonical_id))


_failure_app = _main.App()
_failure_app.cfg = _NS(hours=_NS(is_open=lambda: True))
_failure_app.sender = _CaptureSender()
_failure_app.counters = _CaptureCounters()
_failure_engine = _DeferredProfileEngine()
_failure_app.detector = _NS(engine=_failure_engine)
_failure_app._handle_outcome(
    _failed_outcome("80" * 16, "81" * 16), {}, 300
)
check("recognition miss stays local while physical route is pending",
      (len(_failure_app.sender.detection_events),
       len(_failure_app._deferred_failures),
       len(_failure_app.counters.failures)), (0, 1, 0))
_failure_engine.status = "qualified"
_failure_app._drain_deferred_failures()
check("qualified lane miss enters the KPI exactly once",
      (len(_failure_app.sender.detection_events),
       len(_failure_app._deferred_failures),
       _failure_app.counters.failures),
      (1, 0, ["below_threshold"]))

_irrelevant_app = _main.App()
_irrelevant_app.cfg = _NS(hours=_NS(is_open=lambda: True))
_irrelevant_app.sender = _CaptureSender()
_irrelevant_app.counters = _CaptureCounters()
_irrelevant_engine = _DeferredProfileEngine()
_irrelevant_engine.status = "rejected"
_irrelevant_engine.rejection_is_final = True
_irrelevant_app.detector = _NS(engine=_irrelevant_engine)
_irrelevant_app._handle_outcome(
    _failed_outcome("82" * 16, "83" * 16), {}, 300
)
check("final upper-right rejection never enters miss KPI",
      (len(_irrelevant_app.sender.detection_events),
       len(_irrelevant_app._deferred_failures),
       len(_irrelevant_app.counters.failures)), (0, 0, 0))

print("\n=== terminal no-plate vehicles retain one auditable full frame ===")
import tempfile as _tempfile  # noqa: E402,F811
import cv2 as _cv2  # noqa: E402
import numpy as _np  # noqa: E402
import os as _os  # noqa: E402

_audit_dir = pathlib.Path(_tempfile.mkdtemp())
_audit_frame = _np.full((48, 64, 3), 127, dtype=_np.uint8)
_jpeg_ok, _audit_jpeg_array = _cv2.imencode(".jpg", _audit_frame)
check("test JPEG encoded", _jpeg_ok, True)
_audit_jpeg = _audit_jpeg_array.tobytes()
_audit_app = _main.App()
_audit_app.cfg = _NS(
    hours=_NS(is_open=lambda: True),
    diagnostics=_NS(
        save_miss_plates=True,
        save_unidentified_vehicle_frames=True,
        miss_plate_dir=str(_audit_dir),
        miss_plate_retain_hours=0.0,
        miss_plate_max_cases=300,
    ),
)
_audit_app.sender = _CaptureSender()
_audit_app.counters = _CaptureCounters()
_audit_app._handle_plateless_vehicle(StreamVehicle(
    vehicle_bbox=(10, 10, 100, 100),
    vehicle_confidence=0.88,
    vehicle_type="Sedan",
    color="silver",
    encounter_id="12" * 16,
    first_seen_at="2026-08-24T20:00:00.000Z",
    last_seen_at="2026-08-24T20:01:00.000Z",
    saw_plate=False,
    diagnostic_image_jpeg=_audit_jpeg,
))
_audit_jpgs = list(_audit_dir.glob("*.jpg"))
_audit_jsons = list(_audit_dir.glob("*.json"))
check("one no_visible_plate encounter writes one JPEG plus sidecar",
      (len(_audit_jpgs), len(_audit_jsons)), (1, 1))
check("saved vehicle audit frame remains full resolution",
      _cv2.imread(str(_audit_jpgs[0])).shape[:2], (48, 64))
check("saved no-plate frame preserves Stream's original JPEG bytes",
      _audit_jpgs[0].read_bytes() == _audit_jpeg, True)
_audit_meta = _json.loads(_audit_jsons[0].read_text())
check("vehicle audit sidecar explicitly carries no plate candidate",
      (_audit_meta["outcome"], _audit_meta["plateCandidate"]),
      ("no_visible_plate", False))

_route_before_visits = len(_audit_app.sender.visits)
_route_before_failures = len(_audit_app.counters.failures)
_audit_app._handle_route_diagnostic(StreamVehicle(
    vehicle_bbox=(20, 5, 120, 80),
    vehicle_confidence=0.91,
    vehicle_type="SUV",
    encounter_id="ab" * 16,
    first_seen_at="2026-08-24T20:06:00.000Z",
    last_seen_at="2026-08-24T20:06:10.000Z",
    saw_plate=True,
    route_status="rejected",
    route_reason="terminal_upper_right_exit",
    traffic_classification="drive_off",
    trajectory_points=12,
    dwell_seconds=10.0,
    route_trajectory_bboxes=(
        (900, 300, 1400, 1000),
        (1080, 0, 1450, 300),
    ),
    route_trajectory_timestamps=(
        "2026-08-24T20:06:00.000Z",
        "2026-08-24T20:06:10.000Z",
    ),
    route_entry_zone=(0.52, 0.26, 1.0, 1.0),
    route_board_zone=(0.0, 0.0, 0.72, 0.25),
    route_terminal_exit_zone=(0.72, 0.0, 1.0, 0.28),
    diagnostic_image_observed_at="2026-08-24T20:06:09.000Z",
    diagnostic_image_jpeg=_audit_jpeg,
))
_route_json = next(_audit_dir.glob("*_drive_off_*.json"))
_route_meta = _json.loads(_route_json.read_text())
check("route diagnostic is tagged without outbound identity or KPI mutation",
      (
          _route_meta["outcome"],
          _route_meta["trafficClassification"],
          _route_meta["routeStatus"],
          _route_meta["routeReason"],
          _route_meta["plateCandidate"],
          _route_meta["trajectoryTerminalBbox"],
          _route_meta["trajectoryTerminalObservedAt"],
          _route_meta["routeZones"]["terminalExit"],
          _route_meta["diagnosticImageObservedAt"],
          len(_audit_app.sender.visits),
          len(_audit_app.counters.failures),
      ),
      (
          "drive_off",
          "drive_off",
          "rejected",
          "terminal_upper_right_exit",
          True,
          [1080, 0, 1450, 300],
          "2026-08-24T20:06:10.000Z",
          [0.72, 0.0, 1.0, 0.28],
          "2026-08-24T20:06:09.000Z",
          _route_before_visits,
          _route_before_failures,
      ))

_episode_id = "cd" * 16
_episode_stem = "existing_below_threshold_cdcdcdcd"
_episode_jpg = _audit_dir / f"{_episode_stem}.jpg"
_episode_json = _audit_dir / f"{_episode_stem}.json"
_episode_jpg.write_bytes(_audit_jpeg)
_episode_json.write_text(_json.dumps({
    "episodeId": _episode_id,
    "outcome": "below_threshold",
    "rawText": "LOCALONLY",
    "image": _episode_jpg.name,
}))
_cases_before_retag = len(list(_audit_dir.glob("*.json")))
_audit_app._handle_route_diagnostic(StreamVehicle(
    vehicle_bbox=(20, 5, 120, 80),
    vehicle_confidence=0.80,
    encounter_id="ef" * 16,
    first_seen_at="2026-08-24T20:07:00.000Z",
    last_seen_at="2026-08-24T20:07:10.000Z",
    saw_plate=True,
    diagnostic_episode_id=_episode_id,
    route_status="rejected",
    route_reason="left_before_board_trigger",
    traffic_classification="route_unresolved",
    diagnostic_image_jpeg=_audit_jpeg,
))
_retagged_meta = _json.loads(_episode_json.read_text())
check("rejected plate miss retags its existing case instead of duplicating it",
      (
          len(list(_audit_dir.glob("*.json"))),
          _retagged_meta["outcome"],
          _retagged_meta["trafficClassification"],
          _retagged_meta["routeReason"],
          _retagged_meta["plateCandidate"],
          _retagged_meta["rawText"],
      ),
      (
          _cases_before_retag,
          "below_threshold",
          "route_unresolved",
          "left_before_board_trigger",
          True,
          "LOCALONLY",
      ))

_encounter_only = "91" * 16
_encounter_stem = "existing_no_visible_plate_91919191"
_encounter_jpg = _audit_dir / f"{_encounter_stem}.jpg"
_encounter_json = _audit_dir / f"{_encounter_stem}.json"
_encounter_jpg.write_bytes(_audit_jpeg)
_encounter_json.write_text(_json.dumps({
    "encounterId": _encounter_only,
    "outcome": "no_visible_plate",
    "image": _encounter_jpg.name,
    "plateCandidate": False,
}))
_encounter_cases_before = len(list(_audit_dir.glob("*.json")))
_audit_app._handle_route_diagnostic(StreamVehicle(
    vehicle_bbox=(20, 5, 120, 80),
    vehicle_confidence=0.82,
    encounter_id=_encounter_only,
    first_seen_at="2026-08-24T20:08:00.000Z",
    last_seen_at="2026-08-24T20:08:10.000Z",
    saw_plate=False,
    route_status="rejected",
    route_reason="terminal_upper_right_exit",
    traffic_classification="non_drive_through",
    diagnostic_image_jpeg=_audit_jpeg,
))
_encounter_retagged = _json.loads(_encounter_json.read_text())
check("late shadow route tag matches an existing case by encounter id",
      (
          len(list(_audit_dir.glob("*.json"))),
          _encounter_retagged["trafficClassification"],
          _encounter_retagged["routeReason"],
          _encounter_retagged["plateCandidate"],
      ),
      (
          _encounter_cases_before,
          "non_drive_through",
          "terminal_upper_right_exit",
          False,
      ))

_coalesce_encounter = "92" * 16
_coalesce_episode = "93" * 16
_coalesce_anon_jpg = _audit_dir / "duplicate_anon.jpg"
_coalesce_anon_json = _audit_dir / "duplicate_anon.json"
_coalesce_miss_jpg = _audit_dir / "duplicate_miss.jpg"
_coalesce_miss_json = _audit_dir / "duplicate_miss.json"
_coalesce_anon_jpg.write_bytes(_audit_jpeg)
_coalesce_miss_jpg.write_bytes(_audit_jpeg)
_coalesce_anon_json.write_text(_json.dumps({
    "encounterId": _coalesce_encounter,
    "outcome": "no_visible_plate",
    "image": _coalesce_anon_jpg.name,
}))
_coalesce_miss_json.write_text(_json.dumps({
    "episodeId": _coalesce_episode,
    "outcome": "below_threshold",
    "rawText": "LOCALONLY2",
    "image": _coalesce_miss_jpg.name,
}))
_coalesce_cases_before = len(list(_audit_dir.glob("*.json")))
_audit_app._handle_route_diagnostic(StreamVehicle(
    vehicle_bbox=(20, 5, 120, 80),
    vehicle_confidence=0.81,
    encounter_id=_coalesce_encounter,
    first_seen_at="2026-08-24T20:09:00.000Z",
    last_seen_at="2026-08-24T20:09:10.000Z",
    saw_plate=True,
    diagnostic_episode_id=_coalesce_episode,
    route_status="rejected",
    route_reason="left_before_board_trigger",
    traffic_classification="route_unresolved",
    diagnostic_image_jpeg=_audit_jpeg,
))
_coalesced_meta = _json.loads(_coalesce_miss_json.read_text())
check("route tagging coalesces anonymous and plate-miss duplicates",
      (
          len(list(_audit_dir.glob("*.json"))),
          _coalesce_anon_json.exists(),
          _coalesce_anon_jpg.exists(),
          _coalesce_miss_json.exists(),
          _coalesced_meta["encounterId"],
          _coalesced_meta["trafficClassification"],
          _coalesced_meta["rawText"],
      ),
      (
          _coalesce_cases_before - 1,
          False,
          False,
          True,
          _coalesce_encounter,
          "route_unresolved",
          "LOCALONLY2",
      ))

_late_case_encounter = "94" * 16
_late_case_episode = "95" * 16
_late_case_anon_jpg = _audit_dir / "late_case_anon.jpg"
_late_case_anon_json = _audit_dir / "late_case_anon.json"
_late_case_miss_jpg = _audit_dir / "late_case_miss.jpg"
_late_case_miss_json = _audit_dir / "late_case_miss.json"
_late_case_anon_jpg.write_bytes(_audit_jpeg)
_late_case_miss_jpg.write_bytes(_audit_jpeg)
_late_case_anon_json.write_text(_json.dumps({
    "encounterId": _late_case_encounter,
    "outcome": "no_visible_plate",
    "image": _late_case_anon_jpg.name,
}))
_late_case_miss_json.write_text(_json.dumps({
    "episodeId": _late_case_episode,
    "outcome": "below_threshold",
    "rawText": "LOCALONLY3",
    "image": _late_case_miss_jpg.name,
}))
_late_case_app = _main.App()
_late_case_app.cfg = _audit_app.cfg
_late_case_app.sender = _CaptureSender()
_late_case_app.counters = _CaptureCounters()
_late_case_app.detector = _NS(engine=_TerminalShadowEngine())
_late_cases_before = len(list(_audit_dir.glob("*.json")))
_late_case_app._handle_outcome(
    _failed_outcome(_late_case_encounter, _late_case_episode), {}, 300
)
_late_case_meta = _json.loads(_late_case_miss_json.read_text())
check("late miss keeps one retagged case and its diagnostic event",
      (
          len(list(_audit_dir.glob("*.json"))),
          _late_case_anon_json.exists(),
          _late_case_miss_json.exists(),
          _late_case_meta["outcome"],
          _late_case_meta["encounterId"],
          _late_case_meta["lateEvidence"],
          len(_late_case_app.sender.detection_events),
      ),
      (
          _late_cases_before - 1,
          False,
          True,
          "insufficient_confidence",
          _late_case_encounter,
          True,
          1,
      ))

_late_route_miss_encounter = "98" * 16
_late_route_miss_episode = "99" * 16
_late_route_miss_route_jpg = _audit_dir / "late_route_miss_route.jpg"
_late_route_miss_route_json = _audit_dir / "late_route_miss_route.json"
_late_route_miss_plate_jpg = _audit_dir / "late_route_miss_plate.jpg"
_late_route_miss_plate_json = _audit_dir / "late_route_miss_plate.json"
_late_route_miss_route_jpg.write_bytes(_audit_jpeg)
_late_route_miss_plate_jpg.write_bytes(_audit_jpeg)
_late_route_miss_route_json.write_text(_json.dumps({
    "encounterId": _late_route_miss_encounter,
    "outcome": "drive_off",
    "trafficClassification": "drive_off",
    "routeStatus": "rejected",
    "image": _late_route_miss_route_jpg.name,
}))
_late_route_miss_plate_json.write_text(_json.dumps({
    "episodeId": _late_route_miss_episode,
    "outcome": "below_threshold",
    "rawText": "LOCALONLY4",
    "image": _late_route_miss_plate_jpg.name,
}))
_late_route_miss_app = _main.App()
_late_route_miss_app.cfg = _audit_app.cfg
_late_route_miss_app.sender = _CaptureSender()
_late_route_miss_app.counters = _CaptureCounters()
_late_route_miss_app.detector = _NS(engine=_TerminalShadowEngine())
_late_route_miss_app._handle_outcome(
    _failed_outcome(
        _late_route_miss_encounter,
        _late_route_miss_episode,
    ),
    {},
    300,
)
_late_route_miss_meta = _json.loads(
    _late_route_miss_route_json.read_text()
)
check("late miss preserves route label while coalescing its new case",
      (
          _late_route_miss_route_json.exists(),
          _late_route_miss_plate_json.exists(),
          _late_route_miss_meta["outcome"],
          _late_route_miss_meta["trafficClassification"],
          _late_route_miss_meta["lateRecognitionOutcome"],
          _late_route_miss_meta["plateCandidate"],
      ),
      (True, False, "drive_off", "drive_off",
       "insufficient_confidence", True))

_late_identified_encounter = "96" * 16
_late_identified_jpg = _audit_dir / "late_identified.jpg"
_late_identified_json = _audit_dir / "late_identified.json"
_late_identified_jpg.write_bytes(_audit_jpeg)
_late_identified_json.write_text(_json.dumps({
    "encounterId": _late_identified_encounter,
    "outcome": "no_visible_plate",
    "image": _late_identified_jpg.name,
}))
_late_identified_app = _main.App()
_late_identified_app.cfg = _audit_app.cfg
_late_identified_app.sender = _CaptureSender()
_late_identified_app.counters = _CaptureCounters()
_late_identified_app.detector = _NS(engine=_TerminalShadowEngine())
_late_identified_app._handle_outcome(
    _Confirmed(
        normalized="LATE13",
        detected_at_iso="2026-08-24T20:10:00.000Z",
        frame_count=2,
        detection_confidence=0.91,
        ocr_confidence=0.99,
        inference_duration_ms=15,
        encounter_id=_late_identified_encounter,
    ),
    {},
    300,
)
check("late identification removes its stale anonymous audit case",
      (
          _late_identified_json.exists(),
          _late_identified_jpg.exists(),
          len(_late_identified_app.sender.visits),
      ),
      (False, False, 0))

_late_route_encounter = "97" * 16
_late_route_jpg = _audit_dir / "late_route.jpg"
_late_route_json = _audit_dir / "late_route.json"
_late_route_jpg.write_bytes(_audit_jpeg)
_late_route_json.write_text(_json.dumps({
    "encounterId": _late_route_encounter,
    "outcome": "non_drive_through",
    "trafficClassification": "non_drive_through",
    "routeStatus": "rejected",
    "routeReason": "terminal_upper_right_exit",
    "plateCandidate": False,
    "image": _late_route_jpg.name,
}))
_late_route_app = _main.App()
_late_route_app.cfg = _audit_app.cfg
_late_route_app.sender = _CaptureSender()
_late_route_app.counters = _CaptureCounters()
_late_route_app.detector = _NS(engine=_TerminalShadowEngine())
_late_route_app._handle_outcome(
    _Confirmed(
        normalized="LATE14",
        detected_at_iso="2026-08-24T20:11:00.000Z",
        frame_count=2,
        detection_confidence=0.92,
        ocr_confidence=0.99,
        inference_duration_ms=15,
        encounter_id=_late_route_encounter,
    ),
    {},
    300,
)
_late_route_meta = _json.loads(_late_route_json.read_text())
check("late identification preserves route-classified audit evidence",
      (
          _late_route_json.exists(),
          _late_route_jpg.exists(),
          _late_route_meta["trafficClassification"],
          _late_route_meta["lateIdentified"],
          _late_route_meta["plateCandidate"],
          len(_late_route_app.sender.visits),
      ),
      (True, True, "non_drive_through", True, True, 0))

_before_duplicate_guard = sorted(_audit_dir.iterdir())
_audit_app._handle_plateless_vehicle(StreamVehicle(
    vehicle_bbox=(10, 10, 100, 100),
    vehicle_confidence=0.70,
    encounter_id="34" * 16,
    first_seen_at="2026-08-24T20:02:00.000Z",
    last_seen_at="2026-08-24T20:03:00.000Z",
    saw_plate=True,
    diagnostic_image_jpeg=_audit_jpeg,
))
check("plate-seen terminal does not duplicate Detector's miss frame",
      sorted(_audit_dir.iterdir()), _before_duplicate_guard)

_audit_app.cfg.diagnostics.save_miss_plates = False
_audit_app._handle_plateless_vehicle(StreamVehicle(
    vehicle_bbox=(10, 10, 100, 100),
    vehicle_confidence=0.75,
    encounter_id="56" * 16,
    first_seen_at="2026-08-24T20:04:00.000Z",
    last_seen_at="2026-08-24T20:05:00.000Z",
    saw_plate=False,
    diagnostic_image_jpeg=_audit_jpeg,
))
check("master miss-image switch also disables no-plate frames",
      sorted(_audit_dir.iterdir()), _before_duplicate_guard)
_audit_app.cfg.diagnostics.save_miss_plates = True

for _i in range(3):
    (_audit_dir / f"cap-{_i}.jpg").write_bytes(b"jpg")
    (_audit_dir / f"cap-{_i}.json").write_text("{}")
    # Deterministic oldest-first order for the case-cap assertion.
    _case_time = 1_700_000_000 + _i
    _os.utime(_audit_dir / f"cap-{_i}.jpg", (_case_time, _case_time))
    _os.utime(_audit_dir / f"cap-{_i}.json", (_case_time, _case_time))
prune_miss_plates(str(_audit_dir), retain_hours=0.0, max_cases=3)
_remaining_stems = {p.stem for p in _audit_dir.glob("*.jpg")}
check("case cap prunes complete oldest JPEG/JSON pairs",
      (len(_remaining_stems), "cap-0" in _remaining_stems), (3, False))

print("\n=== open hours: LJS 10:00–22:00 America/New_York ===")
from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

_h = _load(_cfg_path("pr_stream")).hours
check("example hours load as enabled LJS window",
      (_h.enabled, _h.timezone, _h.open, _h.close),
      (True, "America/New_York", "10:00", "22:00"))
_et = ZoneInfo("America/New_York")
check("09:06 Eastern is outside hours (the 9am visit we deleted)",
      _h.is_open(datetime(2026, 8, 18, 9, 6, tzinfo=_et)), False)
check("10:00 Eastern is open",
      _h.is_open(datetime(2026, 8, 18, 10, 0, tzinfo=_et)), True)
check("21:59 Eastern is still open",
      _h.is_open(datetime(2026, 8, 18, 21, 59, tzinfo=_et)), True)
check("22:00 Eastern is closed",
      _h.is_open(datetime(2026, 8, 18, 22, 0, tzinfo=_et)), False)
from plate_reader.config import Hours as _Hours  # noqa: E402
check("hours omitted from config means always open",
      _Hours().is_open(datetime(2026, 8, 18, 3, 0, tzinfo=_et)), True)
check("example warmup is 15 minutes", _h.warmup_minutes, 15)
check("09:45 Eastern starts capture (warmup before 10:00)",
      _h.capture_should_run(datetime(2026, 8, 18, 9, 45, tzinfo=_et)), True)
check("09:44 Eastern capture still off",
      _h.capture_should_run(datetime(2026, 8, 18, 9, 44, tzinfo=_et)), False)
check("21:59 Eastern capture still on",
      _h.capture_should_run(datetime(2026, 8, 18, 21, 59, tzinfo=_et)), True)
check("22:00 Eastern capture off",
      _h.capture_should_run(datetime(2026, 8, 18, 22, 0, tzinfo=_et)), False)
check("visits stay closed during capture warmup",
      _h.is_open(datetime(2026, 8, 18, 9, 45, tzinfo=_et)), False)
check("hours omitted means capture unmanaged (should_run True, enabled False)",
      (_Hours().enabled, _Hours().capture_should_run()), (False, True))
_overnight = _Hours(
    enabled=True, timezone="America/New_York",
    open="22:00", close="02:00",
    open_minutes=22 * 60, close_minutes=2 * 60, warmup_minutes=15,
)
check("overnight warmup starts 21:45",
      _overnight.capture_should_run(datetime(2026, 8, 18, 21, 45, tzinfo=_et)),
      True)
check("overnight warmup still off at 21:44",
      _overnight.capture_should_run(datetime(2026, 8, 18, 21, 44, tzinfo=_et)),
      False)

from plate_reader.config import capture_desired_state as _desired  # noqa: E402
_p = pathlib.Path(_tempfile.mkdtemp()) / "config.json"
_p.write_text("{}")
check("capture_desired_state without hours is unmanaged",
      _desired(_p), "unmanaged")
_p.write_text(_json.dumps({
    "alpr": {"engine": "pr_stream"},
    "hours": {"timezone": "America/New_York", "open": "10:00", "close": "22:00"},
}))
check("pr_stream at 03:00 Eastern is capture off",
      _desired(_p, datetime(2026, 8, 18, 3, 0, tzinfo=_et)), "off")
check("pr_stream at 11:00 Eastern is capture on",
      _desired(_p, datetime(2026, 8, 18, 11, 0, tzinfo=_et)), "on")
_p.write_text(_json.dumps({
    "alpr": {"engine": "fast_alpr"},
    "hours": {"timezone": "America/New_York", "open": "10:00", "close": "22:00"},
}))
check("in-process engine with hours is still unmanaged",
      _desired(_p, datetime(2026, 8, 18, 3, 0, tzinfo=_et)), "unmanaged")

# pr_stream is licensed inside the Stream container, so a stale ultimateALPR
# token path must not strand it...
check("pr_stream boots despite an unreadable ultimateALPR token",
      _load(_cfg_path("pr_stream")).alpr.engine, "pr_stream")

# ...but the guard must still protect the engine it was written for, or the
# SDK silently emits '*'-redacted plates that hash to wrong identities.
try:
    _load(_cfg_path(
        "ultimatealpr",
        alpr_th={"consensus_rescue_min_frames": 0},
    ))
    check("ultimatealpr still refuses an unreadable token", "accepted", "refused")
except _ConfigError:
    check("ultimatealpr still refuses an unreadable token", "refused", "refused")

print("\n=== loopback-only enforcement (raw plate text must not leave) ===")

class _BadCfg:
    class stream:
        webhook_host = "0.0.0.0"
        webhook_port = 8232


bad = StreamWebhookEngine(_BadCfg())
try:
    bad.open()
    check("binding a routable interface is refused", "allowed", "ValueError")
    bad.close()
except ValueError:
    check("binding a routable interface is refused", "ValueError", "ValueError")

# Keep the field-derived profile/route cases in a focused module.  They still
# run as part of this required suite so future vehicle-lifecycle changes cannot
# silently reintroduce traffic inflation.
from test_vehicle_route_lifecycle import run_checks as _run_route_checks  # noqa: E402
_run_route_checks(check)

from test_ivars_route import run_checks as _run_ivars_checks  # noqa: E402
_run_ivars_checks(check)

# Provider-time voting, exact retry suppression, and multi-result ownership
# are part of the same required seam. Keep their focused fixtures separate,
# but always run them here so the documented deployment suite covers them.
print("\n=== provider observation integrity regressions ===")
from test_stream_evidence import run_checks as _run_evidence_checks  # noqa: E402
_run_evidence_checks(check)

print("")
print("-" * 60)
print(f"{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
