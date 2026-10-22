#!/usr/bin/env bash
#
# install.sh — idempotent installer for the serv-pi plate reader.
# Run as: sudo ./scripts/install.sh
#
# What it does (in order, safe to re-run):
#   1. Verify we're root and on a supported OS
#   2. Install system deps (apt)
#   3. Install Python deps (pip with --break-system-packages, pinned)
#   4. Make the serv user/group exist (it should already from build)
#   5. Create the writable state directory and root-owned code directory
#   6. Copy the plate_reader package into /opt/serv
#   7. Drop systemd unit + tmpfiles config + sudoers entry for qmicli
#   8. Install config.example.json (won't overwrite an existing config.json)
#   9. Enable the service but do NOT start it (start happens after config edit)
#
# It does NOT:
#   - Start the service (Erik does that after editing config)
#   - (cellular is now handled automatically by cellular-connect.service)
#   - Set up Tailscale (separate playbook in the build doc)

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="/home/serv/plate_reader"
CODE_ROOT="/opt/serv"
LOG_DIR="/var/log/serv"
# Replacement provisioning defers starts until config/camera ownership is
# switched and backups are complete. Ordinary explicit installs retain their
# existing behavior. This is not a deployment approval override.
START_NETWORK="${SERV_INSTALL_START_NETWORK:-1}"
[[ "$START_NETWORK" == 0 || "$START_NETWORK" == 1 ]] || {
  echo "SERV_INSTALL_START_NETWORK must be 0 or 1" >&2; exit 2;
}

log() { echo "[install] $*"; }
die() { echo "[install][FATAL] $*" >&2; exit 1; }

# ---------- Preflight ----------

if [[ $EUID -ne 0 ]]; then
  die "must be run as root (sudo ./scripts/install.sh)"
fi

if ! id -u serv >/dev/null 2>&1; then
  die "user 'serv' does not exist. Did you flash the SD card with the standard image?"
fi

if [[ ! -f /etc/os-release ]] || ! grep -qi "raspbian\|debian" /etc/os-release; then
  log "WARNING: not running on Raspbian/Debian; proceeding anyway"
fi

# ---------- 0. Line endings + exec bits ----------
# Defensive: if this repo was ever touched by a Windows editor / round-tripped
# through a .zip, the .sh files may have CRLF endings or lost their exec bit.
# Fix both before we touch anything else, so the *other* shipped scripts
# (cellular-connect.sh, network-priority.sh, bringup.sh) don't fail later.
log "normalizing line endings and permissions on shipped scripts..."
if ! command -v dos2unix >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y dos2unix > /dev/null
fi
find "$REPO_DIR/scripts" -name "*.sh" -exec dos2unix {} \; 2>/dev/null || true
chmod +x "$REPO_DIR"/scripts/*.sh 2>/dev/null || true

# ---------- 1. APT deps ----------

log "updating apt cache..."
apt-get update -qq

log "installing system packages..."
apt-get install -y --no-install-recommends \
  python3 python3-pip python3-opencv libopencv-dev \
  python3-picamera2 \
  libqmi-utils \
  curl \
  jq \
  ca-certificates \
  > /dev/null

# ---------- 2. Python deps ----------

# Install only the selected engine's Python runtime. onnxruntime and the
# fast-alpr models are substantial and provide no value on an ultimateALPR
# unit. Existing per-device config wins; a fresh unit follows the template.
ENGINE_CONFIG="$TARGET_DIR/config.json"
if [[ ! -f "$ENGINE_CONFIG" ]]; then
  ENGINE_CONFIG="$REPO_DIR/config.example.json"
fi
ENGINE_SELECTED="$(python3 - "$ENGINE_CONFIG" <<'PY' 2>/dev/null || echo ""
import json, sys
try:
    c = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
print((c.get("alpr", {}) or {}).get("engine") or c.get("engine") or "")
PY
)"
ENGINE_SELECTED="${ENGINE_SELECTED:-fast_alpr}"

log "installing pinned base Python packages..."
pip3 install --break-system-packages -r "$REPO_DIR/requirements.txt"
if [[ "$ENGINE_SELECTED" == "fast_alpr" || "$ENGINE_SELECTED" == "fast-alpr" ]]; then
  log "config selects fast-alpr — installing its ONNX runtime..."
  pip3 install --break-system-packages -r "$REPO_DIR/requirements-fast-alpr.txt"
else
  log "config selects $ENGINE_SELECTED — skipping unused fast-alpr/ONNX packages"
fi

# ---------- 3. Directories ----------

log "creating directories..."
mkdir -p "$TARGET_DIR"
mkdir -p "$LOG_DIR"
install -d -m 755 -o root -g root "$CODE_ROOT"
chown -R serv:serv "$TARGET_DIR" "$LOG_DIR"
chmod 750 "$TARGET_DIR" "$LOG_DIR"

# ---------- 4. Copy code ----------

log "copying plate_reader package to root-owned $CODE_ROOT/plate_reader ..."
LIVE_PACKAGE="$CODE_ROOT/plate_reader"
STAGED_PACKAGE="$(mktemp -d "$CODE_ROOT/.plate_reader.new.XXXXXX")"
PREVIOUS_PACKAGE="$CODE_ROOT/.plate_reader.previous"
cp -a "$REPO_DIR/plate_reader/." "$STAGED_PACKAGE/"
if find "$STAGED_PACKAGE" -type l -print -quit | grep -q .; then
  rm -rf "$STAGED_PACKAGE"
  die "refusing package containing symbolic links"
fi
chown -R root:root "$STAGED_PACKAGE"
find "$STAGED_PACKAGE" -type d -exec chmod 755 {} \;
find "$STAGED_PACKAGE" -type f -exec chmod 644 {} \;
rm -rf "$PREVIOUS_PACKAGE"
if [[ -d "$LIVE_PACKAGE" ]]; then
  mv "$LIVE_PACKAGE" "$PREVIOUS_PACKAGE"
fi
if ! mv "$STAGED_PACKAGE" "$LIVE_PACKAGE"; then
  [[ -d "$PREVIOUS_PACKAGE" ]] && mv "$PREVIOUS_PACKAGE" "$LIVE_PACKAGE"
  die "package swap failed; previous live package was restored"
fi
rm -rf "$PREVIOUS_PACKAGE"

# ---------- 5. Config template (no overwrite) ----------

if [[ ! -f "$TARGET_DIR/config.json" ]]; then
  log "no existing config.json; copying template — YOU MUST EDIT THIS"
  cp "$REPO_DIR/config.example.json" "$TARGET_DIR/config.json"
  chown serv:serv "$TARGET_DIR/config.json"
  chmod 600 "$TARGET_DIR/config.json"
else
  log "existing config.json found at $TARGET_DIR/config.json (left untouched)"
fi

# ---------- 6. isolated QMI telemetry collector ----------

log "installing isolated QMI telemetry collector..."
install -m 755 -o root -g root \
  "$REPO_DIR/scripts/serv-qmi-telemetry" \
  /usr/local/sbin/serv-qmi-telemetry
install -m 755 -o root -g root \
  "$REPO_DIR/scripts/serv-qmi-collect" \
  /usr/local/sbin/serv-qmi-collect
install -m 644 -o root -g root \
  "$REPO_DIR/systemd/serv-qmi-telemetry.service" \
  /etc/systemd/system/serv-qmi-telemetry.service
install -m 644 -o root -g root \
  "$REPO_DIR/systemd/serv-qmi-telemetry.timer" \
  /etc/systemd/system/serv-qmi-telemetry.timer
# Older builds granted the application user a NOPASSWD path. The dedicated
# collector replaces it entirely.
rm -f /etc/sudoers.d/serv-qmicli

# ---------- 6b. ultimateALPR engine (optional) ----------
# Only runs when the unit's config selects the ultimateALPR engine. Handles the
# SDK clone, the exec bits git strips, and building the SWIG binding against
# THIS unit's Python — the compiled .so is ABI-specific to the Python minor
# version, so it can never be shipped pre-built between units.
# Parse the JSON properly rather than grepping: config.example.json contains
# explanatory "//" comment lines that mention ultimatealpr, so a grep would
# false-positive and run the SDK setup on a fast-alpr unit.
if [[ "$ENGINE_SELECTED" == "ultimatealpr" || "$ENGINE_SELECTED" == "ultimate_alpr" ]]; then
  log "config selects ultimateALPR — preparing the engine..."
  if sudo -u serv bash "$REPO_DIR/scripts/setup_ualpr.sh"; then
    log "ultimateALPR engine prepared"
    # Licence gate. A unit whose token fails to load runs in trial mode and
    # silently emits '*'-redacted plates that hash into wrong identities, so
    # this must be checked before the unit is declared ready.
    if sudo -u serv CONFIG="$TARGET_DIR/config.json" bash "$REPO_DIR/scripts/verify_ualpr_license.sh"; then
      log "ultimateALPR licence verified (unmasked output confirmed)"
    else
      log "WARNING: ultimateALPR licence NOT verified — see docs/LICENSING.md."
      log "         The '*' guard will refuse to hash redacted plates, so the"
      log "         unit fails loudly rather than writing wrong identities,"
      log "         but it will not read plates until the token is fixed."
    fi
  else
    log "WARNING: ultimateALPR setup failed; see output above."
  fi
else
  log "config selects engine '${ENGINE_SELECTED:-fast_alpr}' — skipping ultimateALPR setup"
fi

# ---------- 7. systemd unit ----------

log "installing systemd unit..."
install -m 644 -o root -g root \
  "$REPO_DIR/systemd/plate-reader.service" \
  /etc/systemd/system/plate-reader.service

# The updater must be root-owned. Executing it from the serv-writable checkout
# would let a compromised application user replace a script that runs as root.
install -m 755 -o root -g root \
  "$REPO_DIR/scripts/update.sh" \
  /usr/local/sbin/serv-update
install -m 644 -o root -g root \
  "$REPO_DIR/systemd/serv-update.service" \
  /etc/systemd/system/serv-update.service
install -m 644 -o root -g root \
  "$REPO_DIR/systemd/serv-update.timer" \
  /etc/systemd/system/serv-update.timer
install -m 644 -o root -g root \
  "$REPO_DIR/systemd/serv-firewall.service" \
  /etc/systemd/system/serv-firewall.service
install -d -m 755 -o root -g root /etc/serv
install -m 600 -o root -g root \
  "$REPO_DIR/systemd/serv-firewall.nft" \
  /etc/serv/serv-firewall.nft
systemctl daemon-reload

# Disable the old plate_reader.service if it exists (from the v1 build doc)
if systemctl list-unit-files | grep -q '^plate_reader\.service'; then
  log "stopping and disabling legacy plate_reader.service (v1)..."
  systemctl stop plate_reader.service 2>/dev/null || true
  systemctl disable plate_reader.service 2>/dev/null || true
fi

# The service runs from root-owned /opt/serv; writable state is separate.

log "enabling plate-reader.service (will start on next boot)..."
systemctl enable plate-reader.service
systemctl enable serv-qmi-telemetry.timer
if [[ "$START_NETWORK" == 1 ]]; then
  systemctl start serv-qmi-telemetry.timer
fi

# ---------- 7b. Cellular auto-connect watchdog ----------
# Gated on a per-unit sentinel, same pattern as the updater's own
# .update_channel file (see UPDATING_PI.md). A unit that has been
# deliberately set to WiFi-only — e.g. because cellular was racking up data
# charges even with WiFi present and reachable — must stay that way across
# every future install.sh/update.sh run. Without this gate, the very next
# code update silently re-enables and STARTS the modem via
# `systemctl enable --now`, undoing a manual fix with no log line a human
# would ever see in time.
WIFI_ONLY_SENTINEL="$TARGET_DIR/.wifi_only"

if [[ -f "$WIFI_ONLY_SENTINEL" ]]; then
  log "found $WIFI_ONLY_SENTINEL — this unit is WiFi-only by design."
  log "SKIPPING cellular-connect.service and network-priority.service install/enable."
  log "to re-permit cellular on this unit, remove that file and re-run install.sh."
else
  log "installing cellular auto-connect watchdog..."
  install -m 755 -o root -g root \
    "$REPO_DIR/scripts/cellular-connect.sh" \
    /usr/local/bin/cellular-connect.sh
  install -m 755 -o root -g root \
    "$REPO_DIR/scripts/serv-cellular-supervisor" \
    /usr/local/sbin/serv-cellular-supervisor

  install -m 644 -o root -g root \
    "$REPO_DIR/systemd/cellular-connect.service" \
    /etc/systemd/system/cellular-connect.service

  systemctl daemon-reload
  systemctl enable cellular-connect.service
  if [[ "$START_NETWORK" == 1 ]]; then
    # enable --now does not reload an already-running old supervisor.
    systemctl restart cellular-connect.service
    log "cellular-connect.service enabled and restarted"
  else
    log "cellular-connect.service installed/enabled; start deferred"
  fi

  # ---------- 7c. Network route priority (cellular > ethernet) ----------
  # Optional but installed by default. Ensures that if this unit is deployed
  # with an Ethernet cable ALSO plugged in (e.g. via PoE splitter for power
  # where the restaurant also has a data-carrying uplink), cellular remains
  # the preferred default route. Ethernet becomes a fallback only.
  if [[ -f "$REPO_DIR/scripts/network-priority.sh" ]] && \
     [[ -f "$REPO_DIR/systemd/network-priority.service" ]]; then
    log "installing network-priority (cellular > ethernet)..."

    install -m 755 -o root -g root \
      "$REPO_DIR/scripts/network-priority.sh" \
      /usr/local/bin/network-priority.sh

    install -m 644 -o root -g root \
      "$REPO_DIR/systemd/network-priority.service" \
      /etc/systemd/system/network-priority.service

    systemctl daemon-reload
    systemctl enable network-priority.service
    if [[ "$START_NETWORK" == 1 ]]; then
      systemctl restart network-priority.service || \
        log "network-priority.service restart returned non-zero; inspect before shipping"
    fi
    log "network-priority.service enabled"
  else
    log "network-priority files not present in repo; skipping (cellular will still work)"
  fi
fi

# ---------- 8. Summary ----------

cat <<EOF

================================================================
Install complete.

NEXT STEPS:
  1. Edit /home/serv/plate_reader/config.json — fill in:
       device_id, api_key, location_id

  2. Run the bringup test:
       ./scripts/bringup.sh

  3. If bringup is all-green:
       sudo systemctl start plate-reader

  4. Watch the log:
       sudo journalctl -u plate-reader -f

================================================================
EOF
