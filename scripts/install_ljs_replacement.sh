#!/usr/bin/env bash
# Human-reviewed, bench-only conversion of servCamOG to the replacement stack.
# Does not approve a commit, activate LJS credentials, rename Tailscale, erase
# the SD card, or declare the camera commissioned. See the replacement runbook.
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 077

usage() {
  cat <<'EOF'
Usage: sudo bash install_ljs_replacement.sh --reviewed-sha SHA40 \
  --repo /home/serv/serv-pi --bench-config /absolute/bench.json \
  --stream-config /absolute/config.ini --mediamtx-config /absolute/mediamtx.yml \
  --mediamtx-archive /absolute/mediamtx.tar.gz --mediamtx-sha256 SHA256 \
  --stream-image platerecognizer/alpr-stream@sha256:SHA256 \
  --channel codex/REVIEWED_CHANNEL [--start-bench] [--key-only]

The exact commit must ALREADY be approved by the human in
/etc/serv/approved-deploy-sha. Default: install and leave capture stopped.
--start-bench starts the BENCH identity only, never serv-unit-001.
--key-only requires a separately verified public-key SSH session.
No production activation, root approval creation, image pull or reboot occurs.
EOF
}
die() { printf '[replacement] ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[replacement] %s\n' "$*"; }
REPO=/home/serv/serv-pi
SHA= BENCH_CONFIG= STREAM_CONFIG= MTX_CONFIG= MTX_ARCHIVE= MTX_SHA= STREAM_IMAGE= CHANNEL=
START_BENCH=0
KEY_ONLY=0
while (($#)); do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --start-bench) START_BENCH=1; shift ;;
    --key-only) KEY_ONLY=1; shift ;;
    --reviewed-sha|--repo|--bench-config|--stream-config|--mediamtx-config|--mediamtx-archive|--mediamtx-sha256|--stream-image|--channel)
      (($# >= 2)) || die "missing option value"
      case "$1" in
        --reviewed-sha) SHA=$2 ;; --repo) REPO=$2 ;;
        --bench-config) BENCH_CONFIG=$2 ;; --stream-config) STREAM_CONFIG=$2 ;;
        --mediamtx-config) MTX_CONFIG=$2 ;; --mediamtx-archive) MTX_ARCHIVE=$2 ;;
        --mediamtx-sha256) MTX_SHA=$2 ;; --stream-image) STREAM_IMAGE=$2 ;;
        --channel) CHANNEL=$2 ;;
      esac
      shift 2 ;;
    *) die "unknown argument; run --help" ;;
  esac
done
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || die "one exact reviewed 40-character SHA is required"
[[ "$MTX_SHA" =~ ^[0-9a-f]{64}$ ]] || die "reviewed MediaMTX SHA256 is required"
[[ "$STREAM_IMAGE" =~ ^platerecognizer/alpr-stream@sha256:[0-9a-f]{64}$ ]] || die "Stream image must be pinned by repository digest"
[[ "$CHANNEL" =~ ^codex/[A-Za-z0-9._/-]+$ && "$CHANNEL" != *..* ]] || die "a reviewed codex/ update channel is required"
for input in "$REPO" "$BENCH_CONFIG" "$STREAM_CONFIG" "$MTX_CONFIG" "$MTX_ARCHIVE"; do
  [[ "$input" == /* && "$input" != / ]] || die "all input paths must be explicit absolute paths"
done
[[ $EUID -eq 0 ]] || die "human sudo approval is required; no changes made"
[[ "$(uname -m)" == aarch64 ]] || die "this installer is for the ARM64 Pi only"
id serv >/dev/null 2>&1 || die "serv account is required"

APPROVAL=/etc/serv/approved-deploy-sha
[[ -f "$APPROVAL" && ! -L "$APPROVAL" ]] || die "human approval file is missing"
[[ "$(stat -c '%u:%a' "$APPROVAL")" == 0:600 ]] || die "approval file must be root-owned mode 0600"
[[ "$(tr -d '\n' < "$APPROVAL")" == "$SHA" ]] || die "commit differs from the exact human-approved SHA"
[[ ! -e /etc/serv/replacement-bench.json ]] || die "replacement conversion already recorded; use a reviewed incremental update"

# Validate fixed target ancestry before root writes. In particular do not
# follow a serv-owned state-directory symlink into another privileged path.
python3 - <<'PY'
from pathlib import Path
paths = ["/etc/serv", "/var/lib/serv-replacement", "/opt/serv", "/etc/mediamtx",
         "/home/serv/plate_reader", "/home/serv/stream", "/usr/local/bin/mediamtx",
         "/home/serv/pr-stream.env", "/home/serv/.cache"]
for raw in paths:
    path = Path(raw)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise SystemExit("replacement target contains a symbolic link; no changes made")
PY
exec 9>/var/lock/serv-update.lock
flock -n 9 || die "another deployment is running"
gitc() { runuser -u serv -- git -C "$REPO" "$@"; }
gitc cat-file -e "${SHA}^{commit}" || die "reviewed commit must be fetched locally first"
[[ "$(gitc rev-parse "refs/remotes/origin/$CHANNEL")" == "$SHA" ]] || die "reviewed commit is not the locally fetched channel head"
docker image inspect "$STREAM_IMAGE" >/dev/null 2>&1 || die "reviewed Stream image is not cached; no automatic pull allowed"
[[ "$(docker image inspect -f '{{.Architecture}}' "$STREAM_IMAGE")" == arm64 ]] || die "cached Stream image is not ARM64"
if docker inspect stream >/dev/null 2>&1; then
  die "existing Stream container requires explicit review; this installer never removes it"
fi

# Export the helper itself from the immutable object, not a serv-writable
# working tree. It validates every archive member before extracting any file.
install -d -o root -g root -m 700 /var/lib/serv-replacement
STAGE=$(mktemp -d /var/lib/serv-replacement/stage.XXXXXX)
gitc show "$SHA:scripts/ljs_replacement_guard.py" > "$STAGE/guard.py"
gitc archive --format=tar "$SHA" > "$STAGE/source.tar"
python3 "$STAGE/guard.py" extract "$STAGE/source.tar" "$STAGE/source"
SOURCE="$STAGE/source"
python3 "$STAGE/guard.py" inputs \
  --bench-config "$BENCH_CONFIG" --stream-config "$STREAM_CONFIG" \
  --mediamtx-config "$MTX_CONFIG" --mediamtx-archive "$MTX_ARCHIVE" \
  --mediamtx-sha256 "$MTX_SHA" --destination "$STAGE/inputs"
# Read the existing license only to validate that it is a private regular
# file. Its bytes remain on this Pi and are never printed or put into Git.
PYTHONPATH="$SOURCE/scripts" python3 - <<'PY'
import pwd
from ljs_replacement_guard import InvalidInput, validate_pr_env
try:
    validate_pr_env('/home/serv/pr-stream.env', pwd.getpwnam('serv').pw_uid)
except (OSError, InvalidInput):
    raise SystemExit('Stream credential file failed private preflight; no values printed') from None
PY
# Generated configs must match the current reviewed builder/templates. This
# also rejects arbitrary shell-capable MediaMTX runOn* hooks. Do not log JSON.
PYTHONPATH="$SOURCE" python3 - "$SOURCE" "$STAGE/inputs" <<'PY'
import json, sys
from pathlib import Path
from plate_reader.ljs_replacement_config import build_config, build_stream_config, build_camera_config
from plate_reader.config import load
source, inputs = map(Path, sys.argv[1:])
bench = json.loads((inputs / 'bench-config.json').read_text())
expected = build_config(json.loads((source / 'config.example.json').read_text()), bench['api_key'], stage='bench')
if bench != expected:
    raise SystemExit('bench config differs from the reviewed builder')
if (inputs / 'config.ini').read_text() != build_stream_config((source / 'stream/config.ini.template').read_text()):
    raise SystemExit('Stream config differs from the reviewed builder')
if (inputs / 'mediamtx.yml').read_text() != build_camera_config((source / 'stream/mediamtx.yml').read_text()):
    raise SystemExit('MediaMTX config differs from the reviewed builder')
try:
    load(inputs / 'bench-config.json')
except Exception:
    raise SystemExit('bench configuration failed runtime validation; no values printed') from None
PY

BACKUP=$(mktemp -d /var/lib/serv-replacement/backup.XXXXXX)
log "reviewed SHA and artifact checks passed; preserving the old bench installation"
# Unit snapshots can contain historical Environment values: store privately
# and do not send their content to stdout. No journal plate text is collected.
for unit in plate-reader.service plate_reader.service mediamtx.service cellular-connect.service network-priority.service serv-capture-hours.service serv-capture-hours.timer; do
  systemctl cat "$unit" > "$BACKUP/$unit.txt" 2>/dev/null || true
  systemctl is-enabled "$unit" > "$BACKUP/$unit.enabled" 2>/dev/null || true
  systemctl is-active "$unit" > "$BACKUP/$unit.active" 2>/dev/null || true
done
install -m 600 /home/serv/pr-stream.env "$BACKUP/pr-stream.env"
for previous in /usr/local/bin/cellular-connect.sh /usr/local/bin/mediamtx /etc/mediamtx/mediamtx.yml; do
  if [[ -f "$previous" && ! -L "$previous" ]]; then
    install -m 600 "$previous" "$BACKUP/$(basename "$previous").previous"
  fi
done

# From this point a failure leaves the new stack stopped for inspection;
# never automatically resurrect an old capture owner beside Stream.
MUTATING=1
failed() {
  result=$?
  if [[ $result -ne 0 && "${MUTATING:-0}" == 1 ]]; then
    systemctl stop serv-capture-hours.timer plate-reader.service mediamtx.service >/dev/null 2>&1 || true
    docker stop -t 15 stream >/dev/null 2>&1 || true
    log "install incomplete; capture stopped. Preserved backup: $BACKUP"
    log "cellular/WiFi/Tailscale were not rolled back automatically; inspect network services separately"
  fi
  exit "$result"
}
trap failed EXIT
stop_existing() {
  local unit="$1" state
  if [[ "$(systemctl show -p LoadState --value "$unit")" == not-found ]]; then
    return 0
  fi
  systemctl stop "$unit"
  state=$(systemctl show -p ActiveState --value "$unit")
  [[ "$state" == inactive || "$state" == failed ]] || die "could not stop $unit; state was not moved"
}
stop_existing serv-capture-hours.timer
stop_existing plate-reader.service
stop_existing plate_reader.service
stop_existing mediamtx.service
# Require the bench's independent WiFi rescue route before disturbing the old
# QMI owner. This installer is not an in-field LTE-only remote upgrade tool.
ip -4 address show dev wlan0 scope global | grep -q 'inet ' || die "bench WiFi rescue address required"
ip -4 route show default dev wlan0 | grep -q '^default ' || die "bench WiFi rescue default route required"
stop_existing serv-qmi-telemetry.timer
stop_existing serv-qmi-telemetry.service
stop_existing cellular-connect.service
systemctl disable plate_reader.service >/dev/null 2>&1 || true
# Entire state moves, including a cleanly stopped SQLite DB plus any WAL/SHM,
# old package and audit images. No bench queue is reused under a new identity.
[[ ! -d /home/serv/plate_reader ]] || mv /home/serv/plate_reader "$BACKUP/plate_reader"
[[ ! -d /home/serv/stream ]] || mv /home/serv/stream "$BACKUP/stream"
install -d -o serv -g serv -m 750 /home/serv/plate_reader /home/serv/stream /home/serv/.cache
install -o serv -g serv -m 600 "$STAGE/inputs/bench-config.json" /home/serv/plate_reader/config.json
SERV_INSTALL_START_NETWORK=0 bash "$SOURCE/scripts/install.sh"
install -d -o root -g root -m 755 /opt/serv/stream /etc/mediamtx
install -o root -g root -m 755 "$STAGE/inputs/mediamtx/mediamtx" /usr/local/bin/mediamtx
install -o root -g root -m 644 "$SOURCE/stream/mediamtx.service" /etc/systemd/system/mediamtx.service
install -o root -g root -m 644 "$STAGE/inputs/mediamtx.yml" /etc/mediamtx/mediamtx.yml
install -o serv -g serv -m 600 "$STAGE/inputs/config.ini" /home/serv/stream/config.ini
install -o root -g root -m 755 "$SOURCE/stream/run_stream.sh" /opt/serv/stream/run_stream.sh
install -o root -g root -m 755 "$SOURCE/scripts/serv-capture-hours" /usr/local/sbin/serv-capture-hours
install -d -o root -g root -m 755 /usr/local/libexec /etc/systemd/system/plate-reader.service.d
install -o root -g root -m 755 "$SOURCE/scripts/ljs_replacement_guard.py" /usr/local/libexec/serv-replacement-guard.py
printf '[Service]\nExecStartPre=/usr/bin/python3 /usr/local/libexec/serv-replacement-guard.py service-check\n' > /etc/systemd/system/plate-reader.service.d/20-replacement-hold.conf
chmod 644 /etc/systemd/system/plate-reader.service.d/20-replacement-hold.conf
install -o root -g root -m 644 "$SOURCE/systemd/serv-capture-hours.service" /etc/systemd/system/serv-capture-hours.service
install -o root -g root -m 644 "$SOURCE/systemd/serv-capture-hours.timer" /etc/systemd/system/serv-capture-hours.timer
install -d -o root -g root -m 755 /etc/systemd/system/serv-capture-hours.service.d
printf '[Service]\nEnvironment="STREAM_IMAGE=%s"\n' "$STREAM_IMAGE" > /etc/systemd/system/serv-capture-hours.service.d/20-image-pin.conf
chmod 644 /etc/systemd/system/serv-capture-hours.service.d/20-image-pin.conf
printf '%s\n' "$SHA" > /home/serv/plate_reader/.deployed_sha
printf '%s\n' "$CHANNEL" > /home/serv/plate_reader/.update_channel
chown root:serv /home/serv/plate_reader/.deployed_sha
chmod 640 /home/serv/plate_reader/.deployed_sha
chown serv:serv /home/serv/plate_reader/.update_channel
chmod 600 /home/serv/plate_reader/.update_channel
if [[ $KEY_ONLY == 1 ]]; then
  bash "$SOURCE/scripts/harden_pi.sh" --key-only
else
  bash "$SOURCE/scripts/harden_pi.sh"
fi
systemctl daemon-reload
# Keep WiFi as the rescue path. The fixed cellular owner is restarted only
# after all its files are installed; do not disconnect WiFi here.
systemctl restart cellular-connect.service
systemctl enable --now serv-qmi-telemetry.timer
systemctl enable serv-update.timer serv-capture-hours.timer
# Install the runtime hold BEFORE any new application start. Its root-owned
# pre-start check rejects an accidental LJS config while the marker exists.
python3 - "$SHA" "$MTX_SHA" "$STREAM_IMAGE" "$BACKUP" <<'PY'
import json, os, sys
from datetime import datetime, timezone
record = dict(phase="bench_only", production_activation_allowed=False,
              reviewed_sha=sys.argv[1], mediamtx_sha256=sys.argv[2], stream_image=sys.argv[3],
              backup=sys.argv[4], installed_at=datetime.now(timezone.utc).isoformat())
fd = os.open('/etc/serv/replacement-bench.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, 'w') as output:
    json.dump(record, output, indent=2)
    output.write('\n')
PY
if [[ $START_BENCH == 1 ]]; then
  systemctl enable mediamtx.service plate-reader.service
  systemctl start mediamtx.service
  runuser -u serv -- env STREAM_IMAGE="$STREAM_IMAGE" SKIP_PULL=1 /opt/serv/stream/run_stream.sh
  systemctl start plate-reader.service
  systemctl start serv-capture-hours.timer
else
  systemctl disable plate-reader.service serv-capture-hours.timer mediamtx.service
fi
log "bench stack installed. Old state retained privately at $BACKUP"
log "production is NOT activated; hostname, Tailscale identity and LJS cloud key are unchanged"
log "requires license/frame/HTTPS/thermal/LTE-only cold-boot acceptance, then onsite aim and explicit cutover"
