#!/usr/bin/env bash
# Reviewed host-helper-only update for the already converted replacement bench.
# Does not approve releases, replace app state, clear QMI uncertainty or reboot.
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 077
die() { printf '[cellular-patch] ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[cellular-patch] %s\n' "$*"; }
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  cat <<'EOF'
Usage: sudo bash install_cellular_patch.sh --reviewed-sha FULL_SHA40

BENCH ONLY: requires the existing replacement hold, serv-unit-bench identity,
4ad0ab71e0a145daf3a13d80305217e1d43e59b5 application baseline and verified WiFi.
The exact fetched codex/ljs-replacement-recovery channel head must ALREADY be
human-approved in /etc/serv/approved-deploy-sha. No approval is created here.
Only /usr/local/sbin/serv-cellular-supervisor is replaced and its service restarted.
App .deployed_sha stays unchanged; /etc/serv/cellular-patch.json records the patch.
No camera, cloud key, hostname, unit, QMI state, hold or queue is changed.
EOF
  exit 0
fi
[[ $# == 2 && "$1" == --reviewed-sha && "$2" =~ ^[0-9a-f]{40}$ ]] || die "supply one exact --reviewed-sha FULL_SHA40"
SHA=$2
BASE=4ad0ab71e0a145daf3a13d80305217e1d43e59b5
REPO=/home/serv/serv-pi
CHANNEL=codex/ljs-replacement-recovery
TARGET=/usr/local/sbin/serv-cellular-supervisor
[[ $EUID == 0 ]] || die "human sudo approval is required; no changes made"
[[ "$(uname -m)" == aarch64 ]] || die "ARM64 replacement Pi required"

# Kept as one independently tested preflight; no state changes or credentials
# printed. Only its fixed-root call runs in the installer.
python3 - "$SHA" "$BASE" <<'PY_PREFLIGHT'
import json, os, stat, sys
from pathlib import Path

def validate_preflight(root, sha, baseline, root_uid=0):
    def path(raw):
        return root / raw.lstrip('/')
    def regular(raw, owner=None, mode=None):
        target = path(raw)
        if any(p.is_symlink() for p in (target, *target.parents)):
            raise ValueError('unsafe symbolic-link path')
        info = target.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('required regular file missing')
        if owner is not None and info.st_uid != owner:
            raise ValueError('unexpected file owner')
        if mode is not None and stat.S_IMODE(info.st_mode) != mode:
            raise ValueError('unexpected file permissions')
        return target
    for raw in ('/etc/serv', '/var/lib/serv-replacement', '/home/serv/serv-pi',
                '/usr/local/sbin', '/run/serv-cellular'):
        target = path(raw)
        if any(p.is_symlink() for p in (target, *target.parents)):
            raise ValueError('unsafe target ancestry')
        if not target.is_dir():
            raise ValueError('replacement target directory missing')
        if raw != '/home/serv/serv-pi':
            info = target.stat()
            if info.st_uid != root_uid or info.st_mode & 0o022:
                raise ValueError('privileged directory has unsafe ownership or permissions')
    approval = regular('/etc/serv/approved-deploy-sha', root_uid, 0o600).read_text().strip()
    if approval != sha:
        raise ValueError('exact human approval does not match')
    marker = json.loads(regular('/etc/serv/replacement-bench.json', root_uid, 0o600).read_text())
    if marker.get('phase') != 'bench_only' or marker.get('reviewed_sha') != baseline:
        raise ValueError('supported replacement bench hold missing')
    if os.path.lexists(path('/etc/serv/cellular-patch.json')):
        raise ValueError('a cellular patch is already recorded; inspect before another patch')
    config = json.loads(regular('/home/serv/plate_reader/config.json', mode=0o600).read_text())
    if (config.get('device_id') != 'serv-unit-bench' or config.get('location_id') != 'bench-servcamog'
        or config.get('api_base') != 'https://sb13iibr7f.execute-api.us-east-1.amazonaws.com'
        or not isinstance(config.get('alpr'), dict) or config['alpr'].get('engine') != 'pr_stream'):
        raise ValueError('configuration is not the isolated replacement bench')
    if regular('/home/serv/plate_reader/.deployed_sha', root_uid, 0o640).read_text().strip() != baseline:
        raise ValueError('unsupported deployed application baseline')
    if regular('/home/serv/plate_reader/.update_channel').read_text().strip() != 'codex/ljs-replacement-recovery':
        raise ValueError('unexpected tracked channel')
    regular('/usr/local/sbin/serv-cellular-supervisor', root_uid, 0o755)
    regular('/usr/local/bin/cellular-connect.sh', root_uid, 0o755)
    regular('/etc/systemd/system/cellular-connect.service', root_uid, 0o644)
    if os.path.lexists(path('/etc/serv/cellular.env')):
        regular('/etc/serv/cellular.env', root_uid, 0o600)
    # Persistent uncertainty/session files must remain intact, never followed
    # as symlinks into arbitrary files during diagnostic snapshots.
    for target in path('/run/serv-cellular').iterdir():
        regular('/run/serv-cellular/' + target.name, root_uid, 0o600)

if __name__ == '__main__':
    try:
        validate_preflight(Path('/'), sys.argv[1], sys.argv[2])
    except (OSError, ValueError, TypeError, AttributeError):
        raise SystemExit('cellular patch preflight failed; no credentials printed and no state changed') from None
PY_PREFLIGHT

exec 9>/var/lock/serv-update.lock
flock -n 9 || die "another deployment is running"
gitc() { runuser -u serv -- git -C "$REPO" "$@"; }
gitc cat-file -e "${SHA}^{commit}" || die "reviewed commit must be fetched locally first"
[[ "$(gitc rev-parse "refs/remotes/origin/$CHANNEL")" == "$SHA" ]] || die "approved SHA is not the fetched tracked channel head"
gitc merge-base --is-ancestor "$BASE" "$SHA" || die "patch must descend from the installed baseline"
# This is deliberately not an application/unit migration. Reject a mixed
# release rather than writing a misleading application deployment marker.
gitc diff --quiet "$BASE" "$SHA" -- plate_reader scripts/cellular-connect.sh systemd/cellular-connect.service scripts/ljs_replacement_guard.py \
  || die "patch also changes application, wrapper, unit or trusted archive helper"
[[ "$(systemctl show -p FragmentPath --value cellular-connect.service)" == /etc/systemd/system/cellular-connect.service ]] \
  || die "unexpected cellular systemd unit path"
[[ -z "$(systemctl show -p DropInPaths --value cellular-connect.service)" ]] || die "cellular unit has unreviewed drop-ins"

# Prove an independent, working host Internet path, not just a WiFi icon.
ip -4 address show dev wlan0 scope global | grep -q 'inet ' || die "bench WiFi address required"
ip -4 route show default dev wlan0 | grep -q '^default ' || die "bench WiFi default route required"
ip -4 route get 1.1.1.1 | grep -q 'dev wlan0' || die "WiFi must currently be the preferred Internet route"
WIFI_HTTP=$(curl -4 --noproxy '*' --proto '=https' --interface wlan0 --silent --show-error --connect-timeout 5 --max-time 15 \
  --output /dev/null --write-out '%{http_code}' https://controlplane.tailscale.com/) \
  || die "WiFi-bound TLS connectivity failed; will not restart cellular"
[[ "$WIFI_HTTP" =~ ^[234][0-9][0-9]$ ]] || die "WiFi-bound HTTPS did not return a usable response"

STAGE=$(mktemp -d /var/lib/serv-replacement/cellular-patch.XXXXXX)
# The base helper is already reviewed and unchanged in the new release.
gitc show "$BASE:scripts/ljs_replacement_guard.py" > "$STAGE/guard.py"
gitc archive --format=tar "$SHA" scripts/serv-cellular-supervisor > "$STAGE/source.tar"
python3 "$STAGE/guard.py" extract "$STAGE/source.tar" "$STAGE/source"
gitc show "$BASE:scripts/serv-cellular-supervisor" > "$STAGE/baseline-supervisor"
gitc show "$BASE:scripts/cellular-connect.sh" > "$STAGE/baseline-wrapper"
gitc show "$BASE:systemd/cellular-connect.service" > "$STAGE/baseline-unit"
cmp -s "$STAGE/baseline-supervisor" "$TARGET" || die "installed helper differs from the supported baseline"
cmp -s "$STAGE/baseline-wrapper" /usr/local/bin/cellular-connect.sh || die "installed wrapper differs from the supported baseline"
cmp -s "$STAGE/baseline-unit" /etc/systemd/system/cellular-connect.service || die "installed unit differs from the supported baseline"
NEW="$STAGE/source/scripts/serv-cellular-supervisor"
[[ -f "$NEW" && ! -L "$NEW" ]] || die "reviewed helper is not a regular file"
cmp -s "$NEW" "$TARGET" && die "reviewed release does not change the cellular helper"
python3 - "$NEW" <<'PY'
import ast, pathlib, sys
ast.parse(pathlib.Path(sys.argv[1]).read_text())
PY
install -o root -g root -m 600 "$TARGET" "$STAGE/previous-supervisor"
for optional in /etc/serv/cellular.env /run/serv-cellular/status.json /run/serv-cellular/session.json /run/serv-cellular/allocation-uncertain.json /run/serv-cellular/last-failure.json; do
  if [[ -f "$optional" && ! -L "$optional" ]]; then
    install -o root -g root -m 600 "$optional" "$STAGE/$(basename "$optional").before"
  fi
done
systemctl show cellular-connect.service > "$STAGE/service.before.txt"
journalctl -u cellular-connect.service -n 200 --no-pager > "$STAGE/journal.before.txt"
ip -4 address show > "$STAGE/address.before.txt"
ip -4 route show table all > "$STAGE/routes.before.txt"
failure_report() {
  result=$?
  if [[ $result != 0 ]]; then
    log "patch incomplete; helper may be partially installed or service stopped"
    log "private backup and diagnostics: $STAGE"
    log "no automatic rollback, camera changes, reboot or QMI-state clearing performed"
  fi
  exit "$result"
}
trap failure_report EXIT
log "bench, approval, baseline helper and WiFi rescue verified; patching cellular helper only"
systemctl stop cellular-connect.service
STATE=$(systemctl show -p ActiveState --value cellular-connect.service)
[[ "$STATE" == inactive || "$STATE" == failed ]] || die "cellular service did not stop; helper not replaced"
NEXT=$(mktemp /usr/local/sbin/.serv-cellular-supervisor.patch.XXXXXX)
install -o root -g root -m 755 "$NEW" "$NEXT"
mv -f "$NEXT" "$TARGET"
python3 - "$SHA" "$BASE" "$STAGE" "$TARGET" <<'PY'
import hashlib, json, os, pathlib, sys
from datetime import datetime, timezone
record = dict(patch_sha=sys.argv[1], application_baseline_sha=sys.argv[2], backup=sys.argv[3],
              installed_helper_sha256=hashlib.sha256(pathlib.Path(sys.argv[4]).read_bytes()).hexdigest(),
              phase='installed_not_yet_verified', installed_at=datetime.now(timezone.utc).isoformat(),
              uncertainty_state_cleared=False)
fd = os.open('/etc/serv/cellular-patch.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, 'w') as output:
    json.dump(record, output, indent=2)
    output.write('\n')
PY
if ! systemctl start cellular-connect.service; then
  log "helper installed but service start failed; WiFi rescue retained. Backup: $STAGE"
  exit 1
fi
log "patched helper started; application baseline and QMI uncertainty state preserved"
log "diagnostic backup: $STAGE; inspect actual cellular status/HTTPS before calling recovery successful"
