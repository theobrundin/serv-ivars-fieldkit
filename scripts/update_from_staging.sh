#!/usr/bin/env bash
#
# update_from_staging.sh — apply a code update that was copied to the Pi via scp.
#
# WHO RUNS THIS: whoever is deploying, AFTER scp'ing the serv-pi folder to the
# Pi's home directory (~/serv-pi-staging). See SCP_DEPLOY.md for the full path.
#
# WHAT IT DOES (all the fiddly privileged steps, so you don't hand-type them):
#   1. Sanity-checks that the staged code is actually there and imports cleanly
#   2. Stages and permission-checks the new package
#   3. Atomically swaps it into root-owned /opt/serv/plate_reader/
#   4. Fixes ownership + permissions
#   5. Keeps the live config by default (explicit opt-in to replace it)
#   6. Restarts, requires real camera+inference health, and auto-rolls back
#
# It is SAFE TO RE-RUN. If anything looks wrong it stops before touching the
# live service.
#
# Run it as:   sudo bash ~/serv-pi-staging/scripts/update_from_staging.sh

set -euo pipefail

STAGING="${HOME}/serv-pi-staging"
# If invoked via sudo, HOME may be /root; fall back to the serv user's home.
if [[ ! -d "$STAGING" && -d "/home/serv/serv-pi-staging" ]]; then
  STAGING="/home/serv/serv-pi-staging"
fi

TARGET="/home/serv/plate_reader"
CODE_ROOT="/opt/serv"
LIVE_PACKAGE="$CODE_ROOT/plate_reader"
BACKUP="$CODE_ROOT/plate_reader_backup_$(date +%Y%m%d_%H%M%S)"
HEALTH_TIMEOUT=120
HEALTH_SENTINEL="FRAME_PIPELINE_HEALTHY"

GREEN="\033[0;32m"; RED="\033[0;31m"; YEL="\033[0;33m"; NC="\033[0m"
ok()   { echo -e "${GREEN}[ok]${NC}   $*"; }
info() { echo -e "        $*"; }
die()  { echo -e "${RED}[stop]${NC} $*" >&2; exit 1; }
warn() { echo -e "${YEL}[warn]${NC} $*"; }

echo "==== Serv code update (scp/staging method) ===="

# ---------- 0. Must be root ----------
if [[ $EUID -ne 0 ]]; then
  die "Please run with sudo:  sudo bash $STAGING/scripts/update_from_staging.sh"
fi

# ---------- 1. Staging present? ----------
if [[ ! -d "$STAGING/plate_reader" ]]; then
  die "No staged code found at $STAGING/plate_reader
       Did the scp step finish? See SCP_DEPLOY.md, Step 2."
fi
ok "found staged code at $STAGING"

# ---------- 2. Staged code imports cleanly (catches a broken/partial copy) ----------
# We check the pure-python modules only (no camera / fast-alpr needed for this).
if runuser -u serv -- python3 - "$STAGING" <<'PYEOF'
import sys, importlib.util, pathlib
staging = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(staging))
# Import the modules that don't need the camera or fast-alpr present.
for m in ["plate_reader.config", "plate_reader.presence",
          "plate_reader.color", "plate_reader.telemetry",
          "plate_reader.normalizer", "plate_reader.hasher"]:
    importlib.import_module(m)
print("import-check-ok")
PYEOF
then
  ok "staged code imports cleanly"
else
  die "staged code failed an import check — copy is incomplete or corrupted.
       NOT touching the live service. Re-do the scp step and try again."
fi

# ---------- 3/4/5. Stage, permission, then atomic swap ----------
install -d -m 755 -o root -g root "$CODE_ROOT"
STAGED_PACKAGE="$(mktemp -d "$CODE_ROOT/.plate_reader.staged.XXXXXX")"
cp -a "$STAGING/plate_reader/." "$STAGED_PACKAGE/"
if find "$STAGED_PACKAGE" -type l -print -quit | grep -q .; then
  rm -rf "$STAGED_PACKAGE"
  die "refusing package containing symbolic links"
fi
chown -R root:root "$STAGED_PACKAGE"
find "$STAGED_PACKAGE" -type d -exec chmod 755 {} \;
find "$STAGED_PACKAGE" -type f -exec chmod 644 {} \;
ok "staged new package as root-owned, read-only application code"

if [[ -d "$LIVE_PACKAGE" ]]; then
  mv "$LIVE_PACKAGE" "$BACKUP"
  ok "moved current package to rollback backup $BACKUP"
else
  warn "no existing live package (first install?)"
fi
if ! mv "$STAGED_PACKAGE" "$LIVE_PACKAGE"; then
  [[ -d "$BACKUP" ]] && mv "$BACKUP" "$LIVE_PACKAGE"
  die "atomic package swap failed; previous package restored"
fi
ok "atomically swapped new package into $TARGET"

restore_package_before_restart() {
  rm -rf "$LIVE_PACKAGE"
  if [[ -d "$BACKUP" ]]; then
    mv "$BACKUP" "$LIVE_PACKAGE"
  fi
}

# ---------- 5b. Install config.json from staging ----------
# Secrets should not travel inside a general code bundle. The live per-unit
# config is retained unless the operator explicitly sets
# SERV_INSTALL_STAGED_CONFIG=1 for a controlled, device-specific bundle.
STAGED_CFG="$STAGING/config.json"
if [[ "${SERV_INSTALL_STAGED_CONFIG:-0}" == "1" && -f "$STAGED_CFG" ]]; then
  if grep -q "REPLACE" "$STAGED_CFG"; then
    restore_package_before_restart
    die "staged config.json still contains REPLACE placeholders.
       This bundle was not prepared for a specific unit. NOT installing it.
       (The live service keeps its current config; nothing was changed.)"
  fi
  if ! PYTHONPATH="$STAGING" python3 - "$STAGED_CFG" <<'PYEOF'
import pathlib, sys
from plate_reader.config import load
load(pathlib.Path(sys.argv[1]))
PYEOF
  then
    restore_package_before_restart
    die "staged config.json failed application-level validation. NOT installing."
  fi
  if [[ -f "$TARGET/config.json" ]]; then
    cp "$TARGET/config.json" "${BACKUP}_config.json"
    chmod 600 "${BACKUP}_config.json"
  fi
  cp "$STAGED_CFG" "$TARGET/config.json"
  chown serv:serv "$TARGET/config.json"
  chmod 600 "$TARGET/config.json"
  CFG_DEVICE=$(python3 -c "import json;print(json.load(open('$TARGET/config.json'))['device_id'])" 2>/dev/null || echo "?")
  CFG_LOC=$(python3 -c "import json;print(json.load(open('$TARGET/config.json'))['location_id'])" 2>/dev/null || echo "?")
  ok "installed config.json for unit: $CFG_DEVICE  /  $CFG_LOC"
else
  if [[ -f "$TARGET/config.json" ]]; then
    ok "kept existing live config (staged config replacement not requested)"
  else
    warn "no live config — service will not start"
  fi
fi

# ---------- 6. Restart service ----------
echo
echo "Restarting the plate reader service..."
systemctl restart plate-reader
SINCE="$(date +%s)"
DEADLINE=$((SINCE + HEALTH_TIMEOUT))
HEALTHY=0
while (( $(date +%s) < DEADLINE )); do
  if systemctl is-active --quiet plate-reader && \
      journalctl -u plate-reader --since "@$SINCE" --no-pager 2>/dev/null \
      | grep -q "$HEALTH_SENTINEL"; then
    HEALTHY=1
    break
  fi
  if systemctl is-failed --quiet plate-reader; then
    break
  fi
  sleep 5
done

if [[ "$HEALTHY" -ne 1 ]]; then
  warn "new package did not reach camera+inference health; rolling back"
  systemctl stop plate-reader 2>/dev/null || true
  rm -rf "$LIVE_PACKAGE"
  if [[ -d "$BACKUP" ]]; then
    mv "$BACKUP" "$LIVE_PACKAGE"
    if [[ -f "${BACKUP}_config.json" ]]; then
      mv "${BACKUP}_config.json" "$TARGET/config.json"
      chown serv:serv "$TARGET/config.json"
      chmod 600 "$TARGET/config.json"
    fi
    systemctl restart plate-reader || true
  fi
  journalctl -u plate-reader -n 30 --no-pager || true
  die "staged deployment rolled back because it never became healthy"
fi
ok "plate-reader reached real camera+inference health"

# Pause the pull updater until this emergency/manual change is committed to
# the tracked channel; otherwise the next timer run would silently erase it.
{
  echo "manual staging deploy: $(date --iso-8601=seconds 2>/dev/null || date)"
  echo "source: $STAGING"
} > "$TARGET/.manual_override"
chown serv:serv "$TARGET/.manual_override"
chmod 640 "$TARGET/.manual_override"
ok "auto-updater paused with .manual_override until --resume"

echo
echo "==== Update complete ===="
echo "Watch it run live with:"
echo "    sudo journalctl -u plate-reader -f"
echo
echo "You should see detection attempts, and (with the new build) occasional"
echo "DRIVE_OFF lines when a car passes through without being identified."
