#!/usr/bin/env bash
# Read-only field snapshot for a camera-connected Serv Pi.
#
# Usage on the Pi:
#   bash /home/serv/serv-pi/scripts/field_audit.sh | tee field-audit.txt
#
# The report deliberately excludes api_key, license token contents, raw plate
# text, queue payloads, and full journals. It is safe to paste back into a
# support task after a quick human review.

set -uo pipefail

CFG="${SERV_CONFIG:-/home/serv/plate_reader/config.json}"
QUEUE_DB="${SERV_QUEUE_DB:-/home/serv/plate_reader/queue.db}"
REPO="${SERV_REPO:-/home/serv/serv-pi}"
SERVICE="plate-reader.service"

section() { echo; echo "==== $* ===="; }
run_if_present() {
  local command_name="$1"
  shift
  if command -v "$command_name" >/dev/null 2>&1; then
    "$@" 2>&1 || true
  else
    echo "$command_name: not installed"
  fi
}

echo "Serv Pi field audit (read-only)"
date --iso-8601=seconds 2>/dev/null || date

section "Hardware and OS"
tr -d '\0' </proc/device-tree/model 2>/dev/null || true
echo
uname -a
sed -n '1,12p' /etc/os-release 2>/dev/null || true
python3 --version 2>&1 || true
echo "uptime: $(uptime -p 2>/dev/null || true)"

section "Power, cooling, and load"
run_if_present vcgencmd vcgencmd measure_temp
run_if_present vcgencmd vcgencmd get_throttled
run_if_present vcgencmd vcgencmd measure_clock arm
free -h 2>/dev/null || true
df -h / /home 2>/dev/null || true
ps -C python3 -o pid,etimes,%cpu,%mem,rss,cmd --sort=-%cpu 2>/dev/null || true

section "Camera"
if command -v rpicam-hello >/dev/null 2>&1; then
  rpicam-hello --list-cameras 2>&1 || true
elif command -v libcamera-hello >/dev/null 2>&1; then
  libcamera-hello --list-cameras 2>&1 || true
else
  echo "Neither rpicam-hello nor libcamera-hello is installed"
fi

section "Sanitized effective config"
if [[ -f "$CFG" ]]; then
  python3 - "$CFG" <<'PY' 2>&1 || true
import json
import sys

path = sys.argv[1]
raw = json.load(open(path))
alpr = raw.get("alpr", {}) or {}
safe = {
    "device_id": raw.get("device_id"),
    "location_id": raw.get("location_id"),
    "api_base": raw.get("api_base"),
    "sw_version": raw.get("sw_version"),
    "credentials": {
        "api_key_configured": bool(raw.get("api_key")),
        "license_token_file_configured": bool(
            alpr.get("license_token_file") or raw.get("license_token_file")
        ),
    },
    "alpr": {
        "engine": alpr.get("engine") or raw.get("engine"),
        "roi": alpr.get("roi"),
        "thresholds": alpr.get("thresholds"),
        "classifiers": alpr.get("classifiers"),
        "engine_advanced": alpr.get("engine_advanced"),
    },
    "presence": raw.get("presence"),
    "capture": raw.get("capture"),
    "detection_zone": raw.get("detection_zone"),
    "behavior": raw.get("behavior"),
}
print(json.dumps(safe, indent=2, sort_keys=True))
PY
  echo
  if PYTHONPATH=/home/serv/plate_reader python3 -c \
      "from plate_reader.config import load; load()" >/dev/null 2>&1; then
    echo "application_config_validation=PASS"
  else
    echo "application_config_validation=FAIL"
  fi
else
  echo "config missing: $CFG"
fi

section "Runtime and deployment"
systemctl show "$SERVICE" \
  -p ActiveState -p SubState -p NRestarts -p ExecMainStatus \
  -p MemoryCurrent -p CPUUsageNSec -p FragmentPath 2>&1 || true
systemctl is-enabled "$SERVICE" 2>&1 || true
if [[ -f /home/serv/plate_reader/.deployed_sha ]]; then
  echo "deployed_sha=$(cat /home/serv/plate_reader/.deployed_sha)"
fi
if [[ -d "$REPO/.git" ]]; then
  echo "repo_sha=$(git -C "$REPO" rev-parse HEAD 2>/dev/null || true)"
fi
if [[ -d /home/serv/ultimateALPR-SDK/.git ]]; then
  echo "ualpr_sdk_sha=$(git -C /home/serv/ultimateALPR-SDK rev-parse HEAD 2>/dev/null || true)"
fi
du -sh /home/serv/plate_reader /home/serv/ultimateALPR-SDK 2>/dev/null || true
python3 -m pip show requests psutil fast-alpr onnxruntime 2>/dev/null \
  | awk '/^(Name|Version):/ {print}' || true

section "Queue health (counts only; no payloads)"
if [[ -f "$QUEUE_DB" ]]; then
  python3 - "$QUEUE_DB" <<'PY' 2>&1 || true
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as conn:
    for table in ("queue", "dead_letter"):
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error as exc:
            print(f"{table}=unavailable ({exc})")
        else:
            print(f"{table}={count}")
    try:
        rows = conn.execute(
            "SELECT endpoint, COUNT(*), MAX(attempts) FROM queue "
            "GROUP BY endpoint ORDER BY endpoint"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    for endpoint, count, attempts in rows:
        print(f"queue_endpoint={endpoint} count={count} max_attempts={attempts}")
PY
else
  echo "queue database not created yet"
fi

section "Network summary"
ip -brief address 2>/dev/null || true
ip route show default 2>/dev/null || true
if command -v tailscale >/dev/null 2>&1; then
  tailscale status --json 2>/dev/null | python3 -c '
import json, sys
try:
    d=json.load(sys.stdin); s=d.get("Self") or {}
    print("tailscale_state=", d.get("BackendState"))
    print("tailscale_ips=", ",".join(s.get("TailscaleIPs") or []))
    print("tailscale_health=", d.get("Health") or [])
except Exception as e: print("tailscale_parse_error=", e)
' || true
fi
if sudo -n /usr/local/sbin/serv-qmi-telemetry signal >/dev/null 2>&1; then
  sudo -n /usr/local/sbin/serv-qmi-telemetry signal 2>&1 || true
else
  echo "cellular signal wrapper unavailable"
fi

section "Recent non-PII health signals"
journalctl -u "$SERVICE" --since "-20 min" --no-pager 2>/dev/null \
  | grep -E 'FRAME_PIPELINE_HEALTHY|achieved [0-9.]+ fps|CONFIG ERROR|INIT FAILED|AUTH PREFLIGHT FAILED|fatal inference|consecutive|queue full|dead letter|throttl' \
  | tail -80 || true

echo
echo "END field audit"
