#!/usr/bin/env bash
#
# update.sh — pull-based auto-updater for the serv-pi plate reader.
#
# GIT IS THE SOURCE OF TRUTH. This script runs unattended on a timer and makes
# each unit match origin/<channel>. The scp path (update_from_staging.sh) is
# the emergency override — see the "Manual override" section below.
#
# Each cycle:
#   1. Bail out if the unit is under manual override (an scp hotfix is live).
#   2. git fetch the tracked channel. This is the ONLY network call on the
#      common "nothing changed" path — a few KB, safe on a 100MB/mo SIM.
#   3. Refuse deployment unless the exact full commit SHA was approved locally
#      by root. A GitHub push alone is never deployment authority.
#   4. Export only plate_reader/ from the immutable approved Git object. Never
#      copy the serv-writable checkout. Dependency/system changes require a
#      separate reviewed manual install.
#   5. Restart plate-reader and health-check it.
#   6. If it doesn't come back healthy, restore the previous approved package,
#      restart, and report. A bad release to a unit you can only
#      reach over flaky cellular should self-heal, not need a truck roll.
#   7. On any state change, POST a heartbeat with the deployed git SHA.
#
# ---------------------------------------------------------------------------
# MANUAL OVERRIDE (why this exists — read before changing it)
# ---------------------------------------------------------------------------
# update_from_staging.sh (the scp path) drops a marker at
# /home/serv/plate_reader/.manual_override when someone hand-deploys code.
# While that marker exists, THIS script refuses to touch the unit.
#
# Without it, the failure mode is nasty and silent: Theo scp's a hotfix at 2pm,
# it works, and at 1am this script does `git reset --hard origin/main` and
# quietly reverts it. Nobody notices until the bug comes back.
#
# The marker is cleared deliberately, once the fix is committed to git:
#     sudo /usr/local/sbin/serv-update --resume
#
# ---------------------------------------------------------------------------
# Channel: defaults to "main". Put a single line ("staging") in
#   /home/serv/plate_reader/.update_channel
# to make one unit a canary without editing this script.
#
# Usage:
#   sudo ./scripts/update.sh --install         # install root-owned updater
#   sudo /usr/local/sbin/serv-update --status
#   sudo /usr/local/sbin/serv-update --approve <full-reviewed-sha>
#   sudo /usr/local/sbin/serv-update --resume
#   sudo /usr/local/sbin/serv-update            # run an update cycle now

set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

# ---------------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------------
REPO="${SERV_REPO:-/home/serv/serv-pi}"
TARGET="/home/serv/plate_reader"
CODE_ROOT="/opt/serv"
CFG="$TARGET/config.json"
CHANNEL_FILE="$TARGET/.update_channel"
OVERRIDE_FILE="$TARGET/.manual_override"
SHA_FILE="$TARGET/.deployed_sha"
STATUS_FILE="$TARGET/.update_status.json"
APPROVED_SHA_FILE="/etc/serv/approved-deploy-sha"
LOG_DIR="/var/log/serv"
LOG="$LOG_DIR/update.log"
LOCK="/var/lock/serv-update.lock"
UNIT_DIR="/etc/systemd/system"
SERVICE="plate-reader.service"
HEALTH_TIMEOUT=120                 # seconds to wait for a healthy restart
HEALTH_SENTINEL="FRAME_PIPELINE_HEALTHY"  # requires real capture/inference

CHANNEL="$(tr -d '[:space:]' < "$CHANNEL_FILE" 2>/dev/null || true)"
CHANNEL="${CHANNEL:-main}"

mkdir -p "$LOG_DIR" 2>/dev/null || true

log() {
  local line
  line="[update $(date '+%Y-%m-%dT%H:%M:%S%z')] $*"
  echo "$line"
  echo "$line" >> "$LOG" 2>/dev/null || true
}
die() { log "FATAL: $*"; exit 1; }

# Run git as 'serv' so the checkout stays serv-owned (avoids git's "dubious
# ownership" complaints and keeps perms consistent with install.sh).
gitc() { runuser -u serv -- git -C "$REPO" "$@"; }

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Copy the package into the live dir with install.sh's exact perms.
# Mirrors install.sh's "copy code" step — if you change perms there, change
# them here too.
sync_code() {
  local ref="$1" live staging previous
  install -d -m 755 -o root -g root "$CODE_ROOT"
  live="$CODE_ROOT/plate_reader"
  staging="$(mktemp -d "$CODE_ROOT/.plate_reader.new.XXXXXX")"
  previous="$CODE_ROOT/.plate_reader.previous"

  log "staging package from immutable git object ${ref:0:12} -> $live"
  # Archive the approved object itself. Never copy the serv-writable working
  # tree: a compromised application user could modify it or race the updater.
  if ! gitc archive --format=tar "$ref" plate_reader \
      | tar --no-same-owner --no-same-permissions -x -C "$staging" \
          --strip-components=1; then
    rm -rf "$staging"
    return 1
  fi
  if find "$staging" -type l -print -quit | grep -q .; then
    log "refusing package containing symbolic links"
    rm -rf "$staging"
    return 1
  fi
  chown -R root:root "$staging"
  find "$staging" -type d -exec chmod 755 {} \;
  find "$staging" -type f -exec chmod 644 {} \;

  rm -rf "$previous"
  if [[ -d "$live" ]]; then
    mv "$live" "$previous"
  fi
  if ! mv "$staging" "$live"; then
    log "package swap failed; restoring previous live package"
    [[ -d "$previous" ]] && mv "$previous" "$live"
    rm -rf "$staging"
    return 1
  fi
  rm -rf "$previous"
}

# Refresh systemd unit files from the checkout. Returns 0 if anything changed.
refresh_units() {
  local changed=1 unit src dst
  for unit in "$SERVICE" serv-update.service serv-update.timer; do
    src="$REPO/systemd/$unit"
    dst="$UNIT_DIR/$unit"
    [[ -f "$src" ]] || continue
    if ! cmp -s "$src" "$dst"; then
      install -m 644 -o root -g root "$src" "$dst"
      log "installed/updated unit: $unit"
      changed=0
    fi
  done
  return $changed
}

# Best-effort heartbeat so the cloud can see what version each unit is on.
# Only sent on a real state change — never on a no-op cycle (saves data).
post_heartbeat() {
  local status="$1" old="$2" new="$3"
  [[ -f "$CFG" ]] || return 0
  command -v jq >/dev/null 2>&1 || return 0
  local api_base device_id api_key sw payload
  api_base="$(jq -r '.api_base // empty' "$CFG")"
  device_id="$(jq -r '.device_id // empty' "$CFG")"
  api_key="$(jq -r '.api_key // empty' "$CFG")"
  sw="$(jq -r '.sw_version // empty' "$CFG")"
  [[ -n "$api_base" && -n "$device_id" && -n "$api_key" ]] || return 0
  payload="$(jq -nc \
    --arg sw "$sw" --arg st "$status" \
    --arg new "$new" --arg old "$old" --arg ch "$CHANNEL" \
    '{sw_version:$sw, update:{event:"update", status:$st, git_sha:$new, previous_sha:$old, channel:$ch}}')"
  curl -s --max-time 15 -X POST "${api_base%/}/heartbeat" \
    -H "x-device-id: $device_id" \
    -H "x-api-key: $api_key" \
    -H "Content-Type: application/json" \
    -d "$payload" >/dev/null 2>&1 \
    || log "heartbeat POST failed (non-fatal)"
}

# Local status file (free, on-disk) for bringup/debugging.
write_status() {
  local status="$1" old="$2" new="$3" tmp
  command -v jq >/dev/null 2>&1 || return 0
  tmp="$(mktemp)"
  jq -nc \
    --arg ts "$(date '+%Y-%m-%dT%H:%M:%S%z')" \
    --arg st "$status" --arg old "$old" --arg new "$new" --arg ch "$CHANNEL" \
    '{checked_at:$ts, status:$st, previous_sha:$old, deployed_sha:$new, channel:$ch}' \
    > "$tmp" && mv "$tmp" "$STATUS_FILE"
  chown serv:serv "$STATUS_FILE" 2>/dev/null || true
}

# Restart and wait until healthy. Returns 0 healthy, 1 not.
restart_and_verify() {
  systemctl reset-failed "$SERVICE" 2>/dev/null || true
  local since deadline state
  since="$(date +%s)"
  systemctl restart "$SERVICE"
  deadline=$(( since + HEALTH_TIMEOUT ))
  while (( $(date +%s) < deadline )); do
    # Fast-fail on known-fatal log lines rather than waiting out the timeout.
    if journalctl -u "$SERVICE" --since "@$since" --no-pager 2>/dev/null \
        | grep -qiE 'config validation failed|auth preflight failed|detector startup failed|license.*failed|camera.*failed'; then
      log "health: fatal log line detected"
      return 1
    fi
    state="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"
    if [[ "$state" == "failed" ]]; then
      log "health: service entered failed state"
      return 1
    fi
    if [[ "$state" == "active" ]] && journalctl -u "$SERVICE" --since "@$since" \
        --no-pager 2>/dev/null | grep -q "$HEALTH_SENTINEL"; then
      log "health: service active and reached '$HEALTH_SENTINEL'"
      return 0
    fi
    sleep 5
  done
  log "health: timed out after ${HEALTH_TIMEOUT}s waiting for healthy boot"
  return 1
}

# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------
case "${1:-}" in
  --status)
    echo "channel:      $CHANNEL"
    echo "repo:         $REPO"
    if [[ -d "$REPO/.git" ]]; then
      echo "checkout sha: $(runuser -u serv -- git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo '?')"
    fi
    echo "deployed sha: $(cat "$SHA_FILE" 2>/dev/null || echo '(none recorded)')"
    echo "approved sha: $(cat "$APPROVED_SHA_FILE" 2>/dev/null || echo '(none)')"
    if [[ -f "$OVERRIDE_FILE" ]]; then
      echo
      echo "!! MANUAL OVERRIDE ACTIVE — auto-updates are PAUSED on this unit."
      sed 's/^/   /' "$OVERRIDE_FILE"
      echo
      echo "   Commit the hotfix to git, then clear with:"
      echo "     sudo bash $0 --resume"
    else
      echo "override:     none (auto-updates active)"
    fi
    systemctl is-active "$SERVICE" >/dev/null 2>&1 \
      && echo "service:      active" || echo "service:      NOT active"
    systemctl list-timers serv-update.timer --no-pager 2>/dev/null | sed -n '2p' || true
    exit 0
    ;;

  --resume)
    [[ $EUID -eq 0 ]] || die "must run as root (sudo bash $0 --resume)"
    if [[ -f "$OVERRIDE_FILE" ]]; then
      log "clearing manual override:"
      sed 's/^/    /' "$OVERRIDE_FILE" | tee -a "$LOG"
      rm -f "$OVERRIDE_FILE"
      log "manual override cleared — auto-updates resume on the next timer tick"
      echo
      echo "Reminder: make sure the hotfix is committed and pushed to"
      echo "origin/$CHANNEL, or the next update will revert it."
    else
      log "no manual override in place; nothing to clear"
    fi
    exit 0
    ;;

  --approve)
    [[ $EUID -eq 0 ]] || die "must run as root (sudo $0 --approve <sha>)"
    candidate="${2:-}"
    [[ "$candidate" =~ ^[0-9a-f]{40}$ ]] \
      || die "approval requires one full 40-character lowercase git SHA"
    gitc cat-file -e "${candidate}^{commit}" 2>/dev/null \
      || die "commit is not present locally; fetch and review it first"
    install -d -m 755 -o root -g root "$(dirname "$APPROVED_SHA_FILE")"
    tmp_approval="$(mktemp)"
    printf '%s\n' "$candidate" > "$tmp_approval"
    install -m 600 -o root -g root "$tmp_approval" "$APPROVED_SHA_FILE"
    rm -f "$tmp_approval"
    log "approved exact deployment commit ${candidate}"
    exit 0
    ;;

  --install)
    [[ $EUID -eq 0 ]] || die "must run as root (sudo $0 --install)"
    [[ -d "$REPO/.git" ]] || die "repo not found at $REPO (set SERV_REPO if it's elsewhere)"
    install -m 755 -o root -g root "$REPO/scripts/update.sh" \
      /usr/local/sbin/serv-update
    refresh_units || true
    systemctl daemon-reload
    systemctl enable --now serv-update.timer
    log "updater installed; serv-update.timer enabled on channel '$CHANNEL'"
    systemctl list-timers serv-update.timer --no-pager || true
    exit 0
    ;;
esac

# ---------------------------------------------------------------------------
# Normal update cycle
# ---------------------------------------------------------------------------
[[ $EUID -eq 0 ]] || die "must run as root"
[[ -d "$REPO/.git" ]] || die "repo not found at $REPO"

# Single-flight: if another run (or a manual deploy) is in progress, skip.
exec 9>"$LOCK"
if ! flock -n 9; then
  log "another update run is in progress; skipping this cycle"
  exit 0
fi

# --- Manual override gate --------------------------------------------------
# An scp hotfix is live on this unit. Do not touch it. Reverting someone's
# emergency fix at 1am without telling them is the worst thing this script
# could do, so it does nothing at all instead.
if [[ -f "$OVERRIDE_FILE" ]]; then
  log "MANUAL OVERRIDE active — skipping auto-update. Details:"
  sed 's/^/    /' "$OVERRIDE_FILE" | tee -a "$LOG" >/dev/null
  write_status "override_held" "$(cat "$SHA_FILE" 2>/dev/null || echo unknown)" \
                                "$(cat "$SHA_FILE" 2>/dev/null || echo unknown)"
  exit 0
fi

# Fetch is the only network on the no-change path. A transient cellular blip
# is non-fatal — we retry next cycle.
if ! gitc fetch --quiet origin "$CHANNEL"; then
  log "git fetch failed (network?); will retry next cycle"
  exit 0
fi

OLD="$(cat "$SHA_FILE" 2>/dev/null || gitc rev-parse HEAD)"
REMOTE="$(gitc rev-parse "origin/$CHANNEL")"

if [[ "$OLD" == "$REMOTE" ]]; then
  log "up to date on '$CHANNEL' (${OLD:0:12})"
  write_status "noop" "$OLD" "$OLD"
  exit 0
fi

log "update available on '$CHANNEL': ${OLD:0:12} -> ${REMOTE:0:12}"
APPROVED="$(tr -d '[:space:]' < "$APPROVED_SHA_FILE" 2>/dev/null || true)"
if [[ ! "$APPROVED" =~ ^[0-9a-f]{40}$ || "$APPROVED" != "$REMOTE" ]]; then
  log "BLOCKED: remote commit ${REMOTE:0:12} has not been explicitly approved"
  write_status "awaiting_approval" "$OLD" "$REMOTE"
  exit 0
fi
NEW="$REMOTE"

# Did dependencies change? Only then pay the apt/pip data cost.
DEPS_CHANGED=0
if ! gitc diff --quiet "$OLD" "$NEW" -- \
    requirements.txt requirements-fast-alpr.txt scripts/install.sh \
    scripts/setup_ualpr.sh scripts/verify_ualpr_license.sh \
    scripts/serv-qmi-telemetry scripts/serv-qmi-collect \
    scripts/cellular-connect.sh scripts/serv-cellular-supervisor \
    scripts/network-priority.sh scripts/serv-capture-hours \
    scripts/harden_pi.sh scripts/update.sh stream/run_stream.sh \
    systemd/cellular-connect.service systemd/network-priority.service \
    systemd/serv-capture-hours.service systemd/serv-capture-hours.timer \
    systemd/serv-firewall.service systemd/serv-firewall.nft \
    systemd/plate-reader.service systemd/serv-qmi-telemetry.service \
    systemd/serv-qmi-telemetry.timer; then
  DEPS_CHANGED=1
  log "BLOCKED: dependency/install inputs changed; manual reviewed install required"
fi

if [[ "$DEPS_CHANGED" -eq 1 ]]; then
  write_status "manual_install_required" "$OLD" "$NEW"
  exit 0
fi

sync_code "$NEW" || die "could not archive approved application package"

# Deploy: restart and verify the new version is actually healthy.
if restart_and_verify; then
  printf '%s\n' "$NEW" > "$SHA_FILE"
  chown root:serv "$SHA_FILE" 2>/dev/null || true
  chmod 640 "$SHA_FILE" 2>/dev/null || true
  write_status "applied" "$OLD" "$NEW"
  post_heartbeat "applied" "$OLD" "$NEW"
  log "UPDATE APPLIED: now on ${NEW:0:12} ('$CHANNEL')"
  exit 0
fi

# Unhealthy -> roll back to the previous commit.
log "new version unhealthy — rolling back to ${OLD:0:12}"
sync_code "$OLD" || die "could not archive rollback application package"
# NOTE: rollback restores CODE, not pip deps. If a deps-level change broke the
# unit, old code may run against newer libs. Deliberate: a dependency-level bad
# update should page a human, not silently half-work.
if restart_and_verify; then
  printf '%s\n' "$OLD" > "$SHA_FILE"
  chown root:serv "$SHA_FILE" 2>/dev/null || true
  chmod 640 "$SHA_FILE" 2>/dev/null || true
  write_status "rolled_back" "$NEW" "$OLD"
  post_heartbeat "rolled_back" "$NEW" "$OLD"
  log "ROLLED BACK successfully to ${OLD:0:12}"
  exit 1
fi

# Rollback itself didn't come up healthy — stop touching it, shout loudly.
write_status "rolled_back_unhealthy" "$NEW" "$OLD"
post_heartbeat "failed" "$NEW" "$OLD"
die "service unhealthy after rollback — needs hands-on attention (check journalctl -u $SERVICE)"
