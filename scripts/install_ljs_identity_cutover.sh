#!/usr/bin/env bash
# Human-approved identity handover. Never approves itself or calls AWS.
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 077
die() { printf '[identity-cutover] ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[identity-cutover] %s\n' "$*"; }
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  cat <<'EOF'
Usage: sudo bash install_ljs_identity_cutover.sh --reviewed-sha SHA40 \
  --production-config /absolute/private/production-config.json

Requires ALREADY human-approved exact fetched codex/ljs-replacement-recovery
head, the reviewed bench installation, and the verified cellular startup patch.
Archives ALL bench state; uses the canonical production configuration only.
No cloud operation, credential output, network restart, reboot, or self-approval.
Hostname becomes serv-ljs-001; existing Tailscale/SSH keys and IP are retained.
This boot is HELD: camera, app, Stream and capture timer remain stopped.
Next boot is armed for LJS production hours. Finish the cloud credential handover
before shipping; an additional bench reboot would also release the boot hold.
The application .deployed_sha stays unchanged; the host handover is separate.
EOF
  exit 0
fi
[[ $# == 4 && "$1" == --reviewed-sha && "$2" =~ ^[0-9a-f]{40}$ && "$3" == --production-config && "$4" == /* && "$4" != / ]] \
  || die 'supply --reviewed-sha SHA40 --production-config /absolute/private/config.json'
SHA=$2
PRODUCTION_CONFIG=$4
BASE=4ad0ab71e0a145daf3a13d80305217e1d43e59b5
HELPER_SHA=63516989ca47525565dc3aa9f8180746271981126330b9d77adf8417acda4f3c
REPO=/home/serv/serv-pi
CHANNEL=codex/ljs-replacement-recovery
[[ $EUID == 0 ]] || die 'human sudo is required; no changes made'
[[ "$(uname -m)" == aarch64 ]] || die 'ARM64 replacement Pi required'

# All read-only checks precede any application or capture state change.
# The functions are independently exercised under an unprivileged test root.
python3 - "$SHA" "$BASE" "$HELPER_SHA" "$PRODUCTION_CONFIG" <<'PY_PREFLIGHT'
import hashlib, json, os, re, stat, sys
from pathlib import Path

def regular(root, raw, owner=None, mode=None):
    target = root / str(raw).lstrip('/')
    if any(p.is_symlink() for p in (target, *target.parents)):
        raise ValueError('unsafe symlink path')
    info = target.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('expected a regular file')
    if owner is not None and info.st_uid != owner:
        raise ValueError('unexpected file owner')
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise ValueError('unexpected file mode')
    return target

def validate_preflight(root, sha, baseline, helper_sha, candidate, root_uid=0):
    for raw in ('/etc/serv', '/var/lib/serv-replacement', '/etc/systemd/system',
                '/usr/local/sbin', '/etc'):
        target = root / raw.lstrip('/')
        if any(p.is_symlink() for p in (target, *target.parents)):
            raise ValueError('unsafe target ancestry')
        info = target.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != root_uid or info.st_mode & 0o022:
            raise ValueError('privileged target directory is unsafe')
    for raw in ('/home/serv/plate_reader', '/home/serv/serv-pi', '/home/serv/stream'):
        target = root / raw.lstrip('/')
        if not target.is_dir() or any(p.is_symlink() for p in (target, *target.parents)):
            raise ValueError('unexpected application or repository path')
    if (root / 'home/serv/plate_reader').stat().st_dev != (root / 'var/lib/serv-replacement').stat().st_dev:
        raise ValueError('state archive must use one filesystem')
    for raw in ('/etc/serv/identity-cutover.json', '/etc/serv/identity-cutover-ready.json', '/run/serv-ljs-shipping-hold'):
        if os.path.lexists(root / raw.lstrip('/')):
            raise ValueError('handover already begun; inspect rather than blindly rerunning')
    for name in ('.manual_override',):
        if os.path.lexists(root / 'home/serv/plate_reader' / name):
            raise ValueError('manual override requires separate review')
    if regular(root, '/etc/serv/approved-deploy-sha', root_uid, 0o600).read_text().strip() != sha:
        raise ValueError('exact human approval does not match')
    bench = json.loads(regular(root, '/etc/serv/replacement-bench.json', root_uid, 0o600).read_text())
    if bench.get('phase') != 'bench_only' or bench.get('reviewed_sha') != baseline:
        raise ValueError('expected reviewed bench hold missing')
    patch = json.loads(regular(root, '/etc/serv/cellular-patch.json', root_uid, 0o600).read_text())
    if patch.get('application_baseline_sha') != baseline or patch.get('installed_helper_sha256') != helper_sha:
        raise ValueError('reviewed cellular patch record missing')
    helper = regular(root, '/usr/local/sbin/serv-cellular-supervisor', root_uid, 0o755)
    if hashlib.sha256(helper.read_bytes()).hexdigest() != helper_sha:
        raise ValueError('installed cellular helper does not match')
    if regular(root, '/home/serv/plate_reader/.deployed_sha', root_uid, 0o640).read_text().strip() != baseline:
        raise ValueError('unsupported application baseline')
    if regular(root, '/home/serv/plate_reader/.update_channel', mode=0o600).read_text().strip() != 'codex/ljs-replacement-recovery':
        raise ValueError('unexpected tracked update channel')
    active = json.loads(regular(root, '/home/serv/plate_reader/config.json', mode=0o600).read_text())
    if (active.get('device_id'), active.get('location_id'), active.get('alpr', {}).get('engine')) != ('serv-unit-bench', 'bench-servcamog', 'pr_stream'):
        raise ValueError('active identity is not the isolated Stream bench')
    new = json.loads(regular(root, candidate, mode=0o600).read_text())
    if (new.get('device_id'), new.get('location_id'), new.get('alpr', {}).get('engine')) != ('serv-unit-001', 'ljs-louisville-001', 'pr_stream'):
        raise ValueError('candidate must use exact LJS production identity')
    endpoint = 'https://sb13iibr7f.execute-api.us-east-1.amazonaws.com'
    if active.get('api_base') != endpoint or new.get('api_base') != endpoint:
        raise ValueError('unexpected API endpoint')
    key = new.get('api_key')
    if not isinstance(key, str) or not re.fullmatch('[0-9a-f]{64}', key) or key == active.get('api_key'):
        raise ValueError('production must use its own valid private credential')
    regular(root, '/etc/hostname', root_uid, 0o644)
    regular(root, '/etc/hosts', root_uid, 0o644)
    if (root / 'etc/hostname').read_text().strip() not in ('servCamOG', 'servcamog'):
        raise ValueError('unexpected physical source hostname')
    # Rename the state directory itself, never follow any links inside it.
    # Fail closed on mounts/special files rather than losing or copying state.
    state = root / 'home/serv/plate_reader'
    for directory, dirs, files in os.walk(state, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or info.st_dev != state.stat().st_dev:
                raise ValueError('state contains an unexpected link, mount or special file')
    for unit in ('plate-reader.service', 'mediamtx.service', 'serv-capture-hours.service', 'serv-capture-hours.timer', 'serv-update.service', 'serv-update.timer'):
        drop = root / 'etc/systemd/system' / (unit + '.d')
        if os.path.lexists(drop):
            info = drop.stat()
            if drop.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != root_uid or info.st_mode & 0o022:
                raise ValueError('unsafe systemd drop-in directory')
            allowed = {'plate-reader.service': {'20-replacement-hold.conf'},
                       'serv-capture-hours.service': {'20-image-pin.conf'}}.get(unit, set())
            if {p.name for p in drop.iterdir()} != allowed:
                raise ValueError('unexpected systemd drop-in')
            for path in drop.iterdir():
                regular(root, '/etc/systemd/system/' + unit + '.d/' + path.name, root_uid, 0o644)

if __name__ == '__main__':
    try:
        validate_preflight(Path('/'), *sys.argv[1:])
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise SystemExit('identity cutover preflight failed; no credentials printed and no service changed') from None
PY_PREFLIGHT

exec 9>/var/lock/serv-update.lock
flock -n 9 || die 'another deployment is running'
gitc() { runuser -u serv -- git -C "$REPO" "$@"; }
gitc cat-file -e "${SHA}^{commit}" || die 'reviewed commit must already be fetched'
[[ "$(gitc rev-parse "refs/remotes/origin/$CHANNEL")" == "$SHA" ]] || die 'approved SHA is not the fetched channel head'
gitc merge-base --is-ancestor "$BASE" "$SHA" || die 'handover must descend from installed baseline'
gitc diff --quiet "$BASE" "$SHA" -- plate_reader scripts/ljs_replacement_guard.py \
  systemd/plate-reader.service systemd/serv-capture-hours.service systemd/serv-capture-hours.timer \
  stream/mediamtx.service scripts/serv-capture-hours scripts/cellular-connect.sh systemd/cellular-connect.service \
  || die 'handover also changes the installed application or service baseline'

STAGE=$(mktemp -d /var/lib/serv-replacement/identity-cutover.XXXXXX)
gitc show "$BASE:scripts/ljs_replacement_guard.py" > "$STAGE/guard.py"
gitc archive --format=tar "$SHA" > "$STAGE/source.tar"
python3 "$STAGE/guard.py" extract "$STAGE/source.tar" "$STAGE/source"
SOURCE="$STAGE/source"
cmp -s "${BASH_SOURCE[0]}" "$SOURCE/scripts/install_ljs_identity_cutover.sh" || die 'executing installer is not the exact approved Git object'
# Snapshot bytes from the private input before validating and installing them.
PYTHONPATH="$SOURCE/scripts:$SOURCE" python3 - "$PRODUCTION_CONFIG" "$STAGE" "$SOURCE" <<'PY_CONFIG'
import json, sys
from pathlib import Path
from ljs_replacement_guard import read_regular, write_private
from plate_reader.ljs_replacement_config import build_config
from plate_reader.config import load
try:
    candidate = read_regular(sys.argv[1], private=True, max_size=1024 * 1024)
    parsed = json.loads(candidate)
    source = Path(sys.argv[3])
    expected = build_config(json.loads((source / 'config.example.json').read_text()), parsed['api_key'], stage='production')
    if parsed != expected:
        raise ValueError('not the canonical reviewed production configuration')
    target = Path(sys.argv[2]) / 'production-config.json'
    write_private(target, candidate)
    load(target)
except Exception:
    raise SystemExit('private production config validation failed; no secret values printed') from None
PY_CONFIG

for spec in 'plate-reader.service:systemd/plate-reader.service' 'mediamtx.service:stream/mediamtx.service' \
            'serv-capture-hours.service:systemd/serv-capture-hours.service' 'serv-capture-hours.timer:systemd/serv-capture-hours.timer'; do
  unit=${spec%%:*}
  file=${spec#*:}
  [[ "$(systemctl show -p FragmentPath --value "$unit")" == "/etc/systemd/system/$unit" ]] || die 'unexpected capture/application unit path'
  cmp -s "/etc/systemd/system/$unit" "$SOURCE/$file" || die 'installed capture/application unit differs from reviewed baseline'
done
[[ "$(systemctl show -p DropInPaths --value plate-reader.service)" == /etc/systemd/system/plate-reader.service.d/20-replacement-hold.conf ]] || die 'unexpected app drop-ins'
[[ "$(systemctl show -p DropInPaths --value serv-capture-hours.service)" == /etc/systemd/system/serv-capture-hours.service.d/20-image-pin.conf ]] || die 'unexpected capture-hours drop-ins'
[[ -z "$(systemctl show -p DropInPaths --value mediamtx.service)" ]] || die 'unexpected MediaMTX drop-ins'
[[ "$(docker inspect -f '{{.HostConfig.NetworkMode}}' stream)" == host ]] || die 'unexpected Stream network'
[[ "$(docker inspect -f '{{.Config.Image}}' stream)" == platerecognizer/alpr-stream@sha256:b3acfbd966127bfc78869ccd46ed68bc4d93d674909c32f97bdc6f4da1422b24 ]] || die 'unexpected Stream image'
[[ "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' stream)" =~ ^(unless-stopped|no)$ ]] || die 'unexpected Stream restart policy'
for competing in plate_reader.service pr-stream.service stream.service ultimatealpr.service; do
  [[ "$(systemctl show -p ActiveState --value "$competing")" != active ]] || die 'another capture owner is active'
  enabled=$(systemctl is-enabled "$competing" 2>/dev/null || true)
  [[ -z "$enabled" || "$enabled" == disabled || "$enabled" == masked || "$enabled" == not-found ]] \
    || die 'another capture owner is enabled or unreviewed'
done
systemctl is-active --quiet tailscaled.service || die 'Tailscale must remain available'
systemctl is-active --quiet cellular-connect.service || die 'cellular helper must remain active'
ip -4 address show dev wlan0 scope global | grep -q 'inet ' || die 'bench WiFi rescue address required'
timeout --kill-after=2s 20s /usr/bin/tailscale status --json > "$STAGE/tailscale.before.json"
python3 - "$STAGE/tailscale.before.json" <<'PY_TAILNET'
import json, sys
try:
    status = json.load(open(sys.argv[1]))
    if status.get('BackendState') != 'Running' or '100.68.10.100' not in status.get('Self', {}).get('TailscaleIPs', []):
        raise ValueError
except Exception:
    raise SystemExit('expected replacement tailnet identity is not running; nothing changed') from None
PY_TAILNET

for unit in plate-reader.service mediamtx.service serv-capture-hours.service serv-capture-hours.timer serv-update.service serv-update.timer; do
  systemctl cat "$unit" > "$STAGE/$unit.before" 2>/dev/null || true
done
install -o root -g root -m 600 /etc/hostname "$STAGE/hostname.before"
install -o root -g root -m 600 /etc/hosts "$STAGE/hosts.before"
docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' stream > "$STAGE/stream-restart-policy.before"

failed() {
  result=$?
  if [[ $result != 0 ]]; then
    systemctl stop serv-capture-hours.timer serv-capture-hours.service serv-update.timer plate-reader.service mediamtx.service >/dev/null 2>&1 || true
    docker update --restart=no stream >/dev/null 2>&1 || true
    docker stop -t 20 stream >/dev/null 2>&1 || true
    log "handover incomplete; capture held. Private backup: $STAGE"
    log 'no automatic rollback, cloud mutation, reboot or network restart performed'
  fi
  exit "$result"
}
trap failed EXIT
# A failure after this point keeps capture held for this entire boot.
python3 - <<'PY_HOLD'
import os
fd = os.open('/run/serv-ljs-shipping-hold', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, 'w') as out:
    out.write('Current boot held: production identity handover and shipment preparation.\n')
PY_HOLD
for unit in plate-reader.service mediamtx.service serv-capture-hours.service serv-capture-hours.timer serv-update.service serv-update.timer; do
  install -d -o root -g root -m 755 "/etc/systemd/system/$unit.d"
  printf '[Unit]\nConditionPathExists=!/run/serv-ljs-shipping-hold\n' > "/etc/systemd/system/$unit.d/40-ljs-shipping-hold.conf"
  chmod 644 "/etc/systemd/system/$unit.d/40-ljs-shipping-hold.conf"
done
systemctl daemon-reload
systemctl stop serv-capture-hours.timer serv-capture-hours.service serv-update.timer
docker update --restart=no stream >/dev/null
docker stop -t 20 stream >/dev/null
systemctl stop plate-reader.service mediamtx.service
for unit in plate-reader.service mediamtx.service serv-capture-hours.service serv-capture-hours.timer; do
  state=$(systemctl show -p ActiveState --value "$unit")
  [[ "$state" == inactive || "$state" == failed ]] || die 'capture or app did not stop; state not moved'
done
[[ "$(docker inspect -f '{{.State.Running}}' stream)" == false ]] || die 'Stream did not stop; state not moved'

# Move the entire stopped state, including queue.db-{wal,shm} and every audit.
# No database is opened, no row is replayed, and no bench artifact is deleted.
python3 - "$STAGE" <<'PY_STATE'
import os, shutil, stat, sys
from pathlib import Path

def archive_state(root, backup, *, uid, gid, root_uid=0, root_gid=0):
    current = root / 'home/serv/plate_reader'
    archived = backup / 'bench-state'
    if archived.exists() or current.is_symlink() or current.stat().st_dev != backup.stat().st_dev:
        raise ValueError('unsafe or repeated state archive')
    # os.rename on one filesystem preserves the whole SQLite family atomically.
    current.rename(archived)
    current.mkdir(mode=0o750)
    os.chown(current, uid, gid)
    for name, owner, group, mode in (('.deployed_sha', root_uid, gid, 0o640),
                                     ('.update_channel', uid, gid, 0o600)):
        source = archived / name
        if not stat.S_ISREG(source.lstat().st_mode):
            raise ValueError('deployment marker is not regular')
        with source.open('rb') as inp, (current / name).open('xb') as out:
            shutil.copyfileobj(inp, out)
        os.chown(current / name, owner, group)
        (current / name).chmod(mode)

if __name__ == '__main__':
    import pwd
    account = pwd.getpwnam('serv')
    archive_state(Path('/'), Path(sys.argv[1]), uid=account.pw_uid, gid=account.pw_gid)
PY_STATE
install -o serv -g serv -m 600 "$STAGE/production-config.json" /home/serv/plate_reader/config.json
# Preserve the bench guard itself in backup; its installed pre-start helper
# becomes a no-op only after this marker has left the active path.
mv /etc/serv/replacement-bench.json "$STAGE/replacement-bench.json.before"
hostnamectl set-hostname serv-ljs-001
python3 - <<'PY_HOSTS'
import os
from pathlib import Path
path = Path('/etc/hosts')
lines = path.read_text().splitlines(keepends=True)
updated = []
for line in lines:
    fields = line.split('#', 1)[0].split()
    if fields and fields[0] == '127.0.1.1':
        line = line.replace('servCamOG', 'serv-ljs-001').replace('servcamog', 'serv-ljs-001')
    updated.append(line)
temporary = Path('/etc/.hosts.ljs-cutover')
with temporary.open('x') as out:
    out.write(''.join(updated))
temporary.chmod(0o644)
os.replace(temporary, path)
PY_HOSTS
timeout --kill-after=2s 20s /usr/bin/tailscale set --hostname serv-ljs-001
timeout --kill-after=2s 20s /usr/bin/tailscale status --json > "$STAGE/tailscale.after.json"
systemctl enable plate-reader.service mediamtx.service serv-capture-hours.timer serv-update.timer >/dev/null
systemctl daemon-reload
for unit in plate-reader.service mediamtx.service serv-capture-hours.service serv-capture-hours.timer serv-update.timer; do
  state=$(systemctl show -p ActiveState --value "$unit")
  [[ "$state" == inactive || "$state" == failed ]] || die 'current-boot hold did not retain stopped state'
done
[[ "$(docker inspect -f '{{.State.Running}}' stream)" == false ]] || die 'Stream unexpectedly restarted'
[[ "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' stream)" == no ]] || die 'Stream restart policy did not remain held'
systemctl is-active --quiet cellular-connect.service || die 'cellular helper no longer active; inspect without restarting it'
systemctl is-active --quiet tailscaled.service || die 'Tailscale no longer active; inspect without restarting it'
python3 - "$SHA" "$BASE" "$HELPER_SHA" "$STAGE" <<'PY_RECORD'
import hashlib, json, os, socket, sys
from datetime import datetime, timezone
from pathlib import Path
config_bytes = Path('/home/serv/plate_reader/config.json').read_bytes()
config = json.loads(config_bytes)
status = json.loads((Path(sys.argv[4]) / 'tailscale.after.json').read_text())
tailnet = status.get('Self', {})
if socket.gethostname() != 'serv-ljs-001' or status.get('BackendState') != 'Running' or '100.68.10.100' not in tailnet.get('TailscaleIPs', []):
    raise SystemExit('host or tailnet identity verification failed; capture remains held')
record = dict(phase='host_prepared_cloud_pending', device_id='serv-unit-001',
    location_id='ljs-louisville-001', hostname='serv-ljs-001', reviewed_sha=sys.argv[1],
    application_baseline_sha=sys.argv[2], cellular_helper_sha256=sys.argv[3], backup=sys.argv[4],
    config_sha256=hashlib.sha256(config_bytes).hexdigest(),
    api_key_sha256=hashlib.sha256(config['api_key'].encode()).hexdigest(),
    queue_isolated=True, shipping_hold=True,
    tailscale_dns_name=tailnet.get('DNSName'), tailscale_ips=tailnet.get('TailscaleIPs'),
    tailscale_hostname=tailnet.get('HostName'),
    tailscale_private_identity_preserved=True, ssh_private_identity_preserved=True,
    current_boot_held=True, production_next_boot_armed=True,
    installed_at=datetime.now(timezone.utc).isoformat())
fd = os.open('/etc/serv/identity-cutover.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, 'w') as output:
    json.dump(record, output, indent=2)
    output.write('\n')
safe = {key: record[key] for key in ('phase', 'reviewed_sha', 'device_id', 'location_id',
    'hostname', 'config_sha256', 'api_key_sha256', 'queue_isolated', 'shipping_hold',
    'tailscale_dns_name', 'tailscale_ips', 'tailscale_hostname')}
fd = os.open('/etc/serv/identity-cutover-ready.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
os.fchmod(fd, 0o644)
with os.fdopen(fd, 'w') as output:
    json.dump(safe, output, indent=2)
    output.write('\n')
PY_RECORD
log 'Exact LJS host identity installed; ALL bench state preserved privately, fresh production queue on next start.'
log 'This boot remains held. Next boot arms production at 10:00–22:00 Eastern, capture warmup 09:45.'
log 'Existing Tailscale/SSH private identity and IP, cellular/WiFi sessions, and application SHA retained.'
log "Cloud credential handover must be verified separately. Private backup: $STAGE"
