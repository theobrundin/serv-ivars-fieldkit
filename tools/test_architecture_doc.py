#!/usr/bin/env python3
"""Fail when ARCHITECTURE.md stops telling the truth.

A stale architecture doc is worse than none: it actively misleads whoever
reads it next, human or model. This repo has already lost real time to three
documents that were confidently wrong (a brief specifying a codec this
hardware lacks, vendor docs describing the wrong wire format, a handoff naming
a UI control that does not exist).

So the load-bearing claims in ARCHITECTURE.md are asserted against the code
here. Change the engine list, the shipping gates, or capture ownership, and
this test breaks until the doc is updated to match.

It deliberately checks FACTS, not prose. Explanations and rationale cannot be
machine-verified and are marked *(unverified)* in the doc itself.

Run: python3 tools/test_architecture_doc.py
"""
import ast
import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DOC = (REPO / "ARCHITECTURE.md").read_text()
EXAMPLE = json.loads((REPO / "config.example.json").read_text())

_passed = _failed = 0


def check(label, got, want):
    global _passed, _failed
    if got == want:
        _passed += 1
        print(f"PASS  {label}")
    else:
        _failed += 1
        print(f"FAIL  {label}\n        got  {got!r}\n        want {want!r}")


def mentions(*needles):
    return all(n in DOC for n in needles)


print("=== the doc exists and is wired in ===")
check("ARCHITECTURE.md is non-trivial", len(DOC) > 2000, True)
check("CLAUDE.md points at it",
      "ARCHITECTURE.md" in (REPO / "CLAUDE.md").read_text(), True)

print("\n=== engines: doc vs code ===")
from plate_reader.engines import _ENGINES            # noqa: E402
from plate_reader.config import VALID_ENGINES        # noqa: E402
import plate_reader.main as main_mod                 # noqa: E402

# Every registered engine must be loadable via config, or selecting it fails
# at boot -- exactly the gap that blocked the first cutover attempt.
check("every registered engine is accepted by config.py",
      sorted(set(_ENGINES) - set(VALID_ENGINES)), [])
check("doc names the shipping engine",
      "pr_stream" in DOC, True)
check("doc's capture-ownership claim matches main.py",
      main_mod.ENGINES_WITH_OWN_CAPTURE, frozenset({"pr_stream"}))
check("NullCamera is what own-capture engines use",
      "NullCamera" in DOC, True)

print("\n=== shipping config: doc vs config.example.json ===")
from plate_reader.config import RouteQualification  # noqa: E402
from plate_reader import ivars_route  # noqa: E402
check("existing sites keep legacy route policy", RouteQualification().policy, "entry_board")
check("Ivar's route candidate stays explicit and unvalidated",
      mentions("ivars_window_v1", "not enabled by default or validated for production",
               "bottom-centres", "Only the resolved trajectory", "not human-labelled accuracy results"), True)
check("Ivar's versioned frame matches doc", ivars_route.FRAME_SIZE, (2304,1296))
check("Ivar's candidate zones stay pinned",
      (ivars_route.ENTRY_ZONE, ivars_route.WINDOW_ZONE),
      ((0.40,0.55,0.95,0.74),(0.05,0.65,0.39,1.0)))
th = EXAMPLE["alpr"]["thresholds"]
check("example still ships the engine the doc describes",
      EXAMPLE["alpr"]["engine"], "pr_stream")
# TRAP 5: the two vote_min_frames copies must agree, and the doc must say so.
check("both vote_min_frames copies agree in the example",
      (EXAMPLE["thresholds"]["vote_min_frames"],
       EXAMPLE["presence"]["vote_min_frames"]), (2, 2))
check("doc documents the presence override trap",
      mentions("presence.vote_min_frames", "override"), True)
check("example ships the documented PR app gates",
      (th["app_detect_min"], th["app_ocr_min"]), (0.70, 0.80))
check("doc states the PR app gates",
      mentions("det ≥ 0.70", "OCR ≥ 0.80"), True)
check("example ships the documented consensus rescue",
      (th["consensus_rescue_min_frames"],
       th["consensus_rescue_detect_min"],
       th["consensus_rescue_ocr_min"]),
      (3, 0.65, 0.80))
check("doc states the encounter-bound rescue conditions",
      mentions("three exact reads", "same `encounter_id`",
               "det ≥ 0.65", "OCR ≥ 0.80"), True)
check("example ships the documented high-OCR detection rescue",
      (th["provider_high_ocr_rescue_enabled"],
       th["provider_high_ocr_rescue_detect_min"],
       th["provider_high_ocr_rescue_ocr_min"]),
      (True, 0.35, 0.98))
check("doc states the high-OCR rescue safeguards",
      mentions("det ≥ 0.35", "OCR ≥ 0.98",
               "no competing normalized variant", "rear-plate geometry"),
      True)
dg = EXAMPLE["diagnostics"]
check("example ships the bounded all-outcome audit dataset",
      (dg["save_miss_plates"], dg["save_confirmed_plates"],
       dg["save_unidentified_vehicle_frames"],
       dg["miss_plate_retain_hours"], dg["miss_plate_max_cases"]),
      (True, True, True, 0, 300))
check("doc states confirmed/miss/plate-free evidence and the cap",
      mentions("plate miss", "`no_visible_plate`", "newest-300-case cap",
               "Confirmed outcomes retain"), True)
check("example ships LJS open hours",
      (EXAMPLE["hours"]["timezone"], EXAMPLE["hours"]["open"],
       EXAMPLE["hours"]["close"], EXAMPLE["hours"]["warmup_minutes"]),
      ("America/New_York", "10:00", "22:00", 15))
check("doc states capture stops outside hours",
      mentions("warmup_minutes", "MediaMTX and Stream stop"), True)
check("hours watchdog script is shipped",
      (REPO / "scripts" / "serv-capture-hours").is_file(), True)
check("site-local route rollout utility is shipped",
      (REPO / "plate_reader" / "ljs_route_config.py").is_file(), True)
SECURITY_DEPLOY = (
    REPO / "aws" / "deploy_security_release.sh"
).read_text()
check("legacy AWS helper packages shared local-time dependency",
      (
          "package_python_with_location_time" in SECURITY_DEPLOY,
          "location_time.py" in SECURITY_DEPLOY,
      ), (True, True))
route = EXAMPLE["presence"]["route_qualification"]
check("example ships the validated route with guarded reconciliation",
      (route["enabled"], route["shadow_only"],
       route["profile_reconciliation_enabled"],
       route["min_trajectory_points"],
       route["min_trajectory_duration_seconds"],
       route["zone_confirm_points"],
       route["min_leftward_progress"],
       route["min_upward_progress"],
       route["decision_timeout_seconds"],
       route["terminal_exit_zone"],
       route["entry_zone"], EXAMPLE["presence"]["board_zone"]),
      (True, True, True,
       8, 2.0, 2, 0.08, 0.03, 300.0,
       {"x_min_pct": 0.72, "y_min_pct": 0.0,
        "x_max_pct": 1.0, "y_max_pct": 0.28},
       {"x_min_pct": 0.52, "y_min_pct": 0.26,
        "x_max_pct": 1.0, "y_max_pct": 1.0},
       {"x_min_pct": 0.0, "y_min_pct": 0.0,
        "x_max_pct": 0.72, "y_max_pct": 0.25}))
check("example ships the documented sustained-board fallback",
      (route["board_dwell_fallback_min_points"],
       route["board_dwell_fallback_min_board_points"],
       route["board_dwell_fallback_min_seconds"]),
      (20, 8, 8.0))
check("doc states every sustained-board fallback guard",
      mentions("20 total trajectory points",
               "eight actual board-zone samples",
               "eight continuous seconds",
               "cannot rescue a terminal upper-right exit"), True)
check("example ships the documented progressive-board OR path",
      (route["progressive_board_rescue_min_points"],
       route["progressive_board_rescue_min_board_points"],
       route["progressive_board_rescue_min_seconds"]),
      (4, 2, 0.5))
check("doc states every progressive-board OR guard",
      mentions("four trajectory points", "two consecutive terminal board",
               "0.5 seconds", "normal left/up progress",
               "terminal upper-right"), True)
check("doc records the calibrated entry edge and disjoint board gap",
      mentions("x `0.52–1.00`, y `0.26–1.00`",
               "ends at y `0.25`, so they remain disjoint",
               "48 points over 56.8 seconds",
               "next was at y `0.2698`"), True)
check("example ships full-left plate eligibility with bottom band excluded",
      (EXAMPLE["detection_zone"]["enabled"],
       EXAMPLE["detection_zone"]["x_min_pct"],
       EXAMPLE["detection_zone"]["x_max_pct"],
       EXAMPLE["detection_zone"]["y_min_pct"],
       EXAMPLE["detection_zone"]["y_max_pct"]),
      (True, 0.0, 1.0, 0.0, 0.75))
check("doc states route shadow cannot mutate traffic",
      mentions("Shadow classification cannot create", "release a Visit"), True)
check("doc states anonymous traffic needs completed trajectory evidence",
      mentions("completed dwell trajectory", "8 usable trajectory points",
               "2.0 seconds"), True)
STREAM_SOURCE = (REPO / "plate_reader" / "stream_engine.py").read_text()
check("doc states board-plate evidence stays provisional until full route",
      mentions("provisional trigger evidence", "remains `pending`",
               "completed trajectory qualifies"), True)
check("live board-plate traffic authority is absent from the engine",
      "plate_at_board_live" in STREAM_SOURCE, False)
check("anonymous live-route fallback is absent from the engine",
      "live_route_dwell_fallback" in STREAM_SOURCE, False)
check("doc forbids speculative directed handoff and marks the key a no-op",
      mentions("no live directed handoff",
               "`presence.encounter_handoff_seconds`",
               "accepted as a no-op"), True)
check("speculative directed handoff is absent from the engine",
      "_directed_plate_handoff" in STREAM_SOURCE, False)

print("\n=== traps the doc must keep warning about ===")
MTX = (REPO / "stream" / "mediamtx.yml").read_text()
INI = (REPO / "stream" / "config.ini.template").read_text()
LJS_STREAM_CONFIG = (
    REPO / "plate_reader" / "ljs_stream_config.py"
).read_text()

# TRAP 1: hardwareH264 is fatal on Pi 5, not merely ignored.
check("mediamtx.yml does NOT use hardwareH264",
      "rpiCameraCodec: hardwareH264" in MTX, False)
check("MediaMTX's new MoQ service cannot expose raw camera video",
      re.search(r"^moq:\s*no\s*$", MTX, re.M) is not None, True)
check("doc records the new MediaMTX default-listener trap",
      mentions("MoQ", "`moq: no`", "loopback-only"), True)
check("doc warns that hardwareH264 exits 1 on Pi 5",
      mentions("hardwareH264", "exits 1"), True)

# TRAP 3: the sensor is 4:3; a 16:9 request silently crops.
w = re.search(r"rpiCameraWidth:\s*(\d+)", MTX)
h = re.search(r"rpiCameraHeight:\s*(\d+)", MTX)
check("mediamtx captures at the sensor's native resolution",
      (w.group(1), h.group(1)), ("1456", "1088"))
check("doc explains the 4:3 crop trap", mentions("4:3", "crop"), True)
check("mediamtx ships the reviewed short-exposure canary",
      re.search(r"^\s*rpiCameraExposure:\s*short\s*$", MTX, re.M)
      is not None, True)
check("mediamtx template matches the live five-fps canary baseline",
      re.search(r"^\s*rpiCameraFPS:\s*5\s*$", MTX, re.M)
      is not None, True)
check("LJS camera canary has an explicit rollback utility",
      (
          (REPO / "plate_reader" / "ljs_camera_config.py").is_file(),
          mentions("ljs_camera_config.py", "3333 microseconds",
                   "16667 microseconds", "automatic gain", "5-FPS"),
      ), (True, True))

# TRAP 4: prediction selection and same-vehicle reacquisition are independent.
check("config.ini uses independent selection and reacquisition windows",
      (
          re.search(r"^\s*sample\s*=\s*1\s*$", INI, re.M) is not None,
          re.search(r"^\s*max_prediction_delay\s*=\s*12\s*$", INI, re.M)
          is not None,
          re.search(r"^\s*memory_decay\s*=\s*90\s*$", INI, re.M) is not None,
          re.search(r"^\s*report_static\s*=\s*no\s*$", INI, re.M)
          is not None,
          re.search(r"^\s*plates_per_vehicle\s*=\s*1\s*$", INI, re.M)
          is not None,
      ), (True, True, True, True, True))
check("doc explains the independent Stream settings and runtime FPS drift",
      mentions("max_prediction_delay", "same vehicle again",
               "memory_decay = 90", "5 received FPS"), True)
check("doc explains provider observation and retry semantics",
      mentions("same exact vehicle box", "independent OCR votes",
               "process-local cache", "provider timestamp"), True)
check("LJS Stream correction changes only reviewed temporal/vehicle settings",
      (
          '"max_prediction_delay": "12"' in LJS_STREAM_CONFIG,
          '"memory_decay": "90"' in LJS_STREAM_CONFIG,
          '"report_static": "no"' in LJS_STREAM_CONFIG,
          '"plates_per_vehicle": "1"' in LJS_STREAM_CONFIG,
          '"sample"' not in LJS_STREAM_CONFIG.split("TARGET_VALUES", 1)[1]
          .split("}", 1)[0],
      ), (True, True, True, True, True))
check("doc requires an empty-lane Stream restart",
      mentions("ljs_stream_config", "lane is empty", "docker restart stream"),
      True)
check("original full-frame webhook evidence is explicit",
      re.search(r"^\s*image\s*=\s*original\s*$", INI, re.M) is not None,
      True)
check("Stream native dwell trajectory is enabled with the safe delay",
      (
          re.search(r"^\s*dwell_time\s*=\s*true\s*$", INI, re.M) is not None,
          re.search(r"^\s*trajectory\s*=\s*true\s*$", INI, re.M) is not None,
          re.search(r"^\s*max_dwell_delay\s*=\s*30\s*$", INI, re.M)
          is not None,
      ), (True, True, True))

# TRAP 2: the vendor's documented wire format is not what it sends.
from plate_reader.stream_engine import extract_json_field   # noqa: E402
body = b"json=%7B%22data%22%3A%7B%22results%22%3A%5B%5D%7D%7D"
check("urlencoded webhook bodies are still parsed",
      extract_json_field(body, "application/x-www-form-urlencoded") is not None,
      True)
check("doc warns the published webhook format is wrong",
      mentions("x-www-form-urlencoded", "multipart"), True)

print("\n=== invariants the doc promises ===")
from plate_reader.stream_engine import StreamWebhookEngine, StreamVehicle  # noqa: E402


class _Cfg:
    class stream:
        webhook_host = "0.0.0.0"
        webhook_port = 8407

    class thresholds:
        min_detection_confidence = 0.75
        min_ocr_confidence = 0.85


try:
    StreamWebhookEngine(_Cfg()).open()
    check("listener refuses a non-loopback bind", "allowed", "refused")
except ValueError:
    check("listener refuses a non-loopback bind", "refused", "refused")

# A plateless vehicle must have no field that could be hashed into an identity.
check("StreamVehicle carries no plate field",
      any(f in StreamVehicle.__dataclass_fields__ for f in ("plate", "text")),
      False)
check("doc states raw plate text never leaves the device",
      mentions("never leaves"), True)
MAIN_SOURCE = (REPO / "plate_reader" / "main.py").read_text()
INSERT_SOURCE = (
    REPO / "aws" / "lambdas" / "insert_data" / "index.mjs"
).read_text()
CUSTOMERS_SOURCE = (
    REPO / "aws" / "lambdas" / "get_customers" / "lambda_function.py"
).read_text()
ANALYTICS_SOURCE = (
    REPO / "aws" / "lambdas" / "analytics" / "lambda_function.py"
).read_text()
check("recognized vehicle type flows through Pi, Visits, and Customers",
      ("\"vehicleType\"" in MAIN_SOURCE,
       "vehicle_type: vehicleType" in INSERT_SOURCE,
       '"vehicle_type": raw.get("vehicle_type")' in CUSTOMERS_SOURCE),
      (True, True, True))
check("doc keeps descriptive profile fields outside identity",
      mentions("highest-confidence pair and colour", "never enter normalization"),
      True)
check("analytics computes recognition over identifiable vehicles only",
      ("identifiable_vehicles = confirmed + insufficient_confidence" in
       ANALYTICS_SOURCE and
       "confirmed / identifiable_vehicles" in ANALYTICS_SOURCE), True)
check("doc defines the identifiable-only recognition KPI",
      mentions("vehicle_recognition_rate = confirmed / (confirmed +",
               "insufficient_confidence)",
               "`no_visible_plate` vehicle",
               "not a recognition miss",
               "denominator is zero"), True)
check("doc states identity is provisional until encounter settlement",
      mentions("provisional", "3.5s of OCR quiet", "4.0s hard maximum",
               "fuller", "substring", "four per encounter"),
      True)
check("doc forbids changing normalizer/hasher",
      mentions("normalizer.py", "hasher.py"), True)

print("\n=== deploy procedure ===")
check("doc states approve and apply are two steps",
      mentions("--approve", "two steps"), True)
check("doc keeps the shadow-first route calibration procedure",
      mentions("ljs_route_config --phase shadow",
               "Do not run the authoritative phase",
               "does not overwrite"), True)
check("doc names the rollback tag",
      "v2.1.0-ultimatealpr" in DOC, True)

print("\n=== replacement cellular startup boundary ===")
handover = (REPO / "scripts/install_ljs_identity_cutover.sh").read_text()
cloud_handover = (REPO / "tools/prepare_ljs_identity_cutover.py").read_text()
check("doc distinguishes held identity handover from a shipping acceptance claim",
      mentions("scripts/install_ljs_identity_cutover.sh", "any subsequent boot",
               "identity handover is not a claim of completed optical/LTE acceptance"), True)
check("identity handover preserves the complete bench state before production config",
      handover.index("current.rename(archived)") < handover.index('install -o serv -g serv -m 600 "$STAGE/production-config.json"'), True)
check("identity handover gates current-boot capture and disables Stream restart",
      'ConditionPathExists=!/run/serv-ljs-shipping-hold' in handover and
      'docker update --restart=no stream' in handover and
      mentions("/run/serv-ljs-shipping-hold", "Docker restart is disabled"), True)
check("identity handover checks approval instead of granting itself deployment authority",
      "exact human approval does not match" in handover and
      "--approve" not in handover, True)
check("cloud handover only conditionally changes the existing device key",
      '"UpdateExpression": "SET #api_key = :new"' in cloud_handover and
      'attribute_exists(#device_id)' in cloud_handover and
      '#api_key = :old' in cloud_handover and
      '"put-item"' not in cloud_handover and
      mentions("conditional `UpdateItem`", "only `api_key`"), True)
cell_tree = ast.parse((REPO / "scripts/serv-cellular-supervisor").read_text())
supervisor = next(n for n in cell_tree.body
                  if isinstance(n, ast.ClassDef) and n.name == "Supervisor")
methods = {n.name: n for n in supervisor.body if isinstance(n, ast.FunctionDef)}
connect_calls = [n for n in ast.walk(methods["connect"])
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
readiness_call = next(n for n in connect_calls if n.func.attr == "require_control_ready")
allocation_call = next(n for n in connect_calls if n.func.attr == "qmi" and n.args
                       and isinstance(n.args[0], ast.Constant)
                       and n.args[0].value == "--wds-noop")
check("readiness precedes a fresh retained WDS allocation",
      readiness_call.lineno < allocation_call.lineno, True)
control_calls = [n for n in ast.walk(methods["require_control_ready"])
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "qmi"]
check("readiness uses the generic non-retaining CTL query",
      [(ast.literal_eval(n.args[0]),
        {k.arg: ast.literal_eval(k.value) for k in n.keywords}) for n in control_calls],
      [("--get-service-version-info", {"retain": False})])
check("doc separates safe readiness retries from uncertain allocation",
      mentions("non-allocating CTL", "Readiness failures", "ambiguous allocation failures remain",
               "does\nnot erase an existing uncertain allocation"), True)
check("doc keeps incremental host-helper deployment separate from app SHA",
      mentions("scripts/install_cellular_patch.sh", "bench-only", "separately from the application `.deployed_sha`"),
      True)

print("")
print("-" * 60)
print(f"{_passed} passed, {_failed} failed")
if _failed:
    print("\nARCHITECTURE.md no longer matches the code. Update the doc (or")
    print("revert the change) -- a stale architecture doc misleads whoever")
    print("reads it next.")
sys.exit(1 if _failed else 0)
