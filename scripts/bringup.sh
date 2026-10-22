#!/usr/bin/env bash
#
# bringup.sh — one-shot verification for a freshly installed unit.
# Designed to be run by Erik after install.sh + editing config.json.
# Prints a colored PASS/FAIL report. Exits 0 if everything passed.
#
# Run from anywhere: bash scripts/bringup.sh
# (Does not require root.)

set -uo pipefail

CFG=/home/serv/plate_reader/config.json
GREEN="\033[0;32m"
RED="\033[0;31m"
YEL="\033[0;33m"
NC="\033[0m"

PASS=0
FAIL=0
WARN=0
HEARTBEAT_BODY="$(mktemp /tmp/serv-bringup.XXXXXX)"
trap 'rm -f "$HEARTBEAT_BODY"' EXIT

pass() { echo -e "${GREEN}PASS${NC}  $*"; PASS=$((PASS+1)); }
fail() { echo -e "${RED}FAIL${NC}  $*"; FAIL=$((FAIL+1)); }
warn() { echo -e "${YEL}WARN${NC}  $*"; WARN=$((WARN+1)); }
section() { echo; echo "==== $* ===="; }

# ---------- 1. Config ----------

section "Config"

if [[ ! -f "$CFG" ]]; then
  fail "config not found at $CFG"
  echo "Aborting — nothing else will work without config."
  exit 1
fi
pass "config file exists"

if ! jq empty "$CFG" 2>/dev/null; then
  fail "config is not valid JSON"
  exit 1
fi
pass "config is valid JSON"

if PYTHONPATH=/home/serv/plate_reader python3 -c \
    "from plate_reader.config import load; load()" >/dev/null 2>&1; then
  pass "application-level config validation passed"
else
  fail "application-level config validation failed (run the command directly for details)"
fi

DEVICE_ID=$(jq -r .device_id "$CFG")
API_KEY=$(jq -r .api_key "$CFG")
LOCATION_ID=$(jq -r .location_id "$CFG")
API_BASE=$(jq -r .api_base "$CFG")

[[ "$DEVICE_ID" == *REPLACE* ]] && fail "device_id is still placeholder" || pass "device_id: $DEVICE_ID"
[[ "$API_KEY" == *REPLACE* ]] && fail "api_key is still placeholder" || pass "api_key set (${#API_KEY} chars)"
[[ "$LOCATION_ID" == *REPLACE* ]] && fail "location_id is still placeholder" || pass "location_id: $LOCATION_ID"
[[ "$API_BASE" == https://* ]] && pass "api_base: $API_BASE" || fail "api_base must start with https://"

# Config perms — must not be world-readable (contains api_key)
PERMS=$(stat -c '%a' "$CFG")
if [[ "$PERMS" == "600" ]]; then
  pass "config perms = 600 (private)"
else
  warn "config perms = $PERMS (should be 600 — chmod 600 $CFG)"
fi

# ---------- 2. Python deps ----------

section "Python dependencies"

python3 -c "import requests; print(f'requests {requests.__version__}')" 2>/dev/null \
  && pass "requests importable" || fail "requests not importable"

# Which engine this unit runs. Parsed as JSON, not grepped: config.example.json
# has explanatory "//" lines mentioning the engine name.
ENGINE_SELECTED="$(python3 - "$CFG" <<'PY' 2>/dev/null || echo ""
import json, sys
try:
    c = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
print((c.get("alpr", {}) or {}).get("engine") or c.get("engine") or "")
PY
)"

# fast_alpr is only required when this unit actually uses it. A ualpr unit
# need not have it installed, and failing on that would fail every ualpr
# bringup for no reason.
if [[ "$ENGINE_SELECTED" == "ultimatealpr" || "$ENGINE_SELECTED" == "ultimate_alpr" ]]; then
  python3 -c "import fast_alpr" 2>/dev/null \
    && pass "fast_alpr importable (not used by this unit)" \
    || pass "fast_alpr absent — not required, engine is ultimateALPR"
else
  python3 -c "import fast_alpr" 2>/dev/null \
    && pass "fast_alpr importable" || fail "fast_alpr not importable"
fi

python3 -c "import cv2; print(f'opencv {cv2.__version__}')" 2>/dev/null \
  && pass "cv2 importable" || fail "cv2 not importable"

python3 -c "from picamera2 import Picamera2" 2>/dev/null \
  && pass "picamera2 importable" || fail "picamera2 not importable"

python3 -c "import psutil; print(f'psutil {psutil.__version__}')" 2>/dev/null \
  && pass "psutil importable" || fail "psutil not importable"

# Self-test the package's own hasher (catches a broken hashlib)
python3 -c "
import sys
sys.path.insert(0, '/home/serv/plate_reader')
from plate_reader.hasher import self_test
self_test()
print('hasher OK')
" 2>/dev/null \
  && pass "hasher self-test" || fail "hasher self-test failed"

# Normalizer sanity
python3 -c "
import sys
sys.path.insert(0, '/home/serv/plate_reader')
from plate_reader.normalizer import normalize
assert normalize('ABC 123') == 'ABC123'
assert normalize('abc-123') == 'ABC123'
assert normalize('A') is None
assert normalize('AAAAAAAAAAAAAAAA') is None
print('normalizer OK')
" 2>/dev/null \
  && pass "normalizer behaves per spec" || fail "normalizer self-check failed"

# ---------- 3. Camera ----------

section "Camera"

CAM_OUT=$(rpicam-hello --list-cameras 2>/dev/null)
if grep -q imx296 <<< "$CAM_OUT"; then
  pass "imx296 camera detected"
else
  fail "imx296 camera not detected — check ribbon cable"
fi

# ---------- 4. Cellular / Sixfab ----------

section "Cellular / Sixfab"

if lsusb 2>/dev/null | grep -qi telit; then
  pass "Telit modem detected via lsusb"
else
  warn "Telit modem not in lsusb output (skip if this unit will use WiFi for pilot)"
fi

if [[ -e /dev/cdc-wdm0 ]]; then
  pass "/dev/cdc-wdm0 present"
  # Fixed-function wrapper is the only root QMI surface install.sh permits.
  if sudo -n /usr/local/sbin/serv-qmi-telemetry signal 2>/dev/null | grep -q dBm; then
    pass "qmicli RSSI readable (sudoers fragment OK)"
  else
    warn "qmicli RSSI not readable — telemetry will be empty"
  fi
else
  warn "/dev/cdc-wdm0 not present (Wi-Fi-only deployment? OK to skip)"
fi

# ---------- 5. Tailscale ----------

section "Tailscale"

if command -v tailscale >/dev/null 2>&1; then
  pass "tailscale binary installed"
  TS_STATE=$(tailscale status --json 2>/dev/null | jq -r .BackendState 2>/dev/null || echo "")
  if [[ "$TS_STATE" == "Running" ]]; then
    TS_IP=$(tailscale ip -4 2>/dev/null | head -1)
    pass "tailscale state = Running; IP = ${TS_IP:-unknown}"
  else
    warn "tailscale state = ${TS_STATE:-unknown} (you'll lose remote access if this unit goes offline)"
  fi
else
  warn "tailscale not installed (no remote SSH path after deployment)"
fi

# ---------- 6. Clock ----------

section "Clock"

CLK=$(timedatectl show --property=NTPSynchronized --value 2>/dev/null || echo "")
if [[ "$CLK" == "yes" ]]; then
  pass "system clock NTP-synced"
elif [[ "$CLK" == "no" ]]; then
  warn "system clock NOT NTP-synced — wait a few minutes and re-run"
else
  warn "could not determine NTP sync state"
fi

# ---------- 7. Cloud auth ----------

section "Cloud auth"

if [[ "$DEVICE_ID" == *REPLACE* ]] || [[ "$API_KEY" == *REPLACE* ]]; then
  warn "skipping cloud auth check — placeholder credentials"
else
  HTTP_STATUS=$(curl -s -o "$HEARTBEAT_BODY" -w "%{http_code}" \
    -X POST "$API_BASE/heartbeat" \
    -H "Content-Type: application/json" \
    -H "x-device-id: $DEVICE_ID" \
    -H "x-api-key: $API_KEY" \
    -d '{}' \
    --max-time 10 || echo "000")

  if [[ "$HTTP_STATUS" == "200" ]] || [[ "$HTTP_STATUS" == "201" ]]; then
    pass "cloud accepted authenticated heartbeat (HTTP $HTTP_STATUS)"
  elif [[ "$HTTP_STATUS" == "401" ]] || [[ "$HTTP_STATUS" == "403" ]]; then
    fail "cloud rejected auth (HTTP $HTTP_STATUS) — wrong api_key or device_id revoked"
  elif [[ "$HTTP_STATUS" == "000" ]]; then
    fail "could not reach cloud at all (network down?)"
  else
    warn "cloud returned HTTP $HTTP_STATUS (unexpected; response kept private and removed on exit)"
  fi
fi

# ---------- 8. Systemd ----------

section "Systemd"

UNIT_OUT=$(systemctl list-unit-files 2>/dev/null)
if grep -q '^plate-reader\.service' <<< "$UNIT_OUT"; then
  pass "plate-reader.service installed"
  STATE=$(systemctl is-enabled plate-reader 2>/dev/null || echo "")
  [[ "$STATE" == "enabled" ]] && pass "service enabled (will start on boot)" \
    || warn "service is $STATE — sudo systemctl enable plate-reader"
else
  fail "plate-reader.service not installed — run scripts/install.sh"
fi

# Confirm the legacy v1 service is gone or stopped
if systemctl is-active plate_reader.service 2>/dev/null | grep -q active; then
  warn "legacy plate_reader.service is still active — stop & disable it"
fi

# ---------- 9. Disk ----------

section "Disk"

DISK_PCT=$(df -h / | awk 'NR==2 {gsub(/%/,"",$5); print $5}')
if (( DISK_PCT < 80 )); then
  pass "root disk usage ${DISK_PCT}%"
elif (( DISK_PCT < 90 )); then
  warn "root disk usage ${DISK_PCT}% (clean up before shipping)"
else
  fail "root disk usage ${DISK_PCT}% (too full)"
fi

# ---------- ultimateALPR engine ----------
# Only meaningful when config selects ultimateALPR. Parsed as JSON rather than
# grepped, because config.example.json has explanatory "//" lines mentioning
# the engine name and a grep would false-positive on a fast-alpr unit.
section "ultimateALPR engine"

if [[ "$ENGINE_SELECTED" != "ultimatealpr" && "$ENGINE_SELECTED" != "ultimate_alpr" ]]; then
  pass "engine is '${ENGINE_SELECTED:-fast_alpr}' — ultimateALPR checks not applicable"
else
  SDK_DIR="/home/serv/ultimateALPR-SDK"
  ARCH_DIR="$SDK_DIR/binaries/linux/aarch64"

  if [[ "$(uname -m)" == "aarch64" ]]; then
    pass "architecture is aarch64"
  else
    fail "architecture is $(uname -m), expected aarch64 (binaries/raspbian is 32-bit and wrong for Pi 5)"
  fi

  if [[ -d "$ARCH_DIR" ]]; then
    pass "SDK present at $ARCH_DIR"
  else
    fail "SDK missing at $ARCH_DIR — run scripts/setup_ualpr.sh"
  fi

  if [[ -f "$ARCH_DIR/_ultimateAlprSdk.so" ]]; then
    pass "python binding built"
  else
    fail "python binding missing — run scripts/setup_ualpr.sh (it must be built on THIS unit, against ITS python)"
  fi

  # The binding is ABI-specific to the Python minor version, so a .so copied
  # from another unit will not load. Import the way production does.
  if LD_LIBRARY_PATH="$ARCH_DIR" \
     PYTHONPATH="$ARCH_DIR:$SDK_DIR/python:/home/serv/plate_reader" \
     python3 -c "
from plate_reader.engines import _install_imp_shim
_install_imp_shim()
import ultimateAlprSdk
" >/dev/null 2>&1; then
    pass "ultimateAlprSdk imports (python $(python3 -c 'import sys;print(".".join(map(str,sys.version_info[:3])))'))"
  else
    fail "ultimateAlprSdk import FAILED — rebuild against this unit's python (scripts/setup_ualpr.sh)"
  fi

  # Token presence. Contents are never read or printed.
  TOKEN="$(python3 - "$CFG" <<'PY' 2>/dev/null || echo ""
import json, sys
try:
    c = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
print((c.get("alpr", {}) or {}).get("license_token_file") or c.get("license_token_file") or "")
PY
)"
  if [[ -z "$TOKEN" ]]; then
    fail "no licence token configured — the SDK would run in TRIAL mode and emit '*'-redacted plates that hash into WRONG identities"
  elif [[ -r "$TOKEN" ]]; then
    pass "licence token readable ($(stat -c%s "$TOKEN") bytes, mode $(stat -c%a "$TOKEN"))"
    if [[ "$(stat -c%a "$TOKEN")" != "600" ]]; then
      warn "licence token is not chmod 600"
    fi
  else
    fail "licence token configured but not readable: $TOKEN"
  fi

  # The decisive check: a token that fails to load does NOT error, it silently
  # degrades to trial mode. "init succeeded" proves nothing; measure it.
  if [[ -n "$TOKEN" && -r "$TOKEN" ]]; then
    if CONFIG="$CFG" bash "$(dirname "$0")/verify_ualpr_license.sh" >/dev/null 2>&1; then
      pass "licence VERIFIED — unmasked output confirmed"
    else
      fail "licence NOT verified — unit would emit redacted plates; see docs/LICENSING.md"
    fi
  fi

  # systemd must carry the env; it does not inherit an interactive shell.
  UNIT=/etc/systemd/system/plate-reader.service
  if [[ -f "$UNIT" ]]; then
    if grep -q "LD_LIBRARY_PATH" "$UNIT"; then
      pass "systemd unit sets LD_LIBRARY_PATH"
    else
      fail "systemd unit is MISSING LD_LIBRARY_PATH — the dynamic linker will not find libultimate_alpr-sdk.so"
    fi
  fi
fi

# ---------- Summary ----------

section "Summary"

echo "PASS: $PASS    FAIL: $FAIL    WARN: $WARN"
echo

if (( FAIL > 0 )); then
  echo -e "${RED}NOT READY${NC} — fix the FAIL items above before shipping."
  exit 1
fi

if (( WARN > 0 )); then
  echo -e "${YEL}OK with warnings${NC} — review the WARN items above and decide if you want to proceed."
else
  echo -e "${GREEN}ALL GREEN${NC} — unit looks ready."
fi
exit 0
