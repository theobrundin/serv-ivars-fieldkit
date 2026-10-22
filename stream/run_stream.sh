#!/usr/bin/env bash
# Start the Plate Recognizer Stream container on a Pi (aarch64).
#
# Credentials are read from an env file that stays mode 0600 on the device and
# is never committed, echoed, or logged. Create it with:
#
#   umask 077 && nano ~/pr-stream.env
#     TOKEN=<plate recognizer api token>
#     LICENSE_KEY=<stream license key>
#
# Reminder from the Plate Recognizer docs: a Stream license is bound to a
# hardware fingerprint and only one machine at a time. Before moving this to
# another unit, release it first (Account Page -> Manage -> Reset for a
# Subscription license; Perpetual licenses cannot be reset).
set -euo pipefail

ENV_FILE="${ENV_FILE:-$HOME/pr-stream.env}"
WORKDIR="${WORKDIR:-$HOME/stream}"
IMAGE="${STREAM_IMAGE:-platerecognizer/alpr-stream:arm}"
NAME="${STREAM_CONTAINER:-stream}"
# Provisioning supplies a tested immutable ARM64 digest. Never download an
# unreviewed engine just because a missing container must be recreated at dawn.
SKIP_PULL="${SKIP_PULL:-1}"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "missing $ENV_FILE — see the header of this script" >&2
    exit 1
fi

# Read this as data, not shell code. Docker consumes the secret values itself;
# neither this check nor the command line prints them. Docker administrators
# can still inspect container environment, so docker access remains privileged.
python3 - "$ENV_FILE" <<'PY'
import os, pathlib, stat, sys
p = pathlib.Path(sys.argv[1])
s = p.lstat()
if not stat.S_ISREG(s.st_mode) or s.st_mode & 0o077 or s.st_uid != os.getuid():
    sys.exit("Stream credentials must be an owned regular mode-600 file")
values = {}
for line in p.read_text().splitlines():
    if not line.strip() or line.lstrip().startswith("#"):
        continue
    key, sep, value = line.partition("=")
    if not sep or key not in {"TOKEN", "LICENSE_KEY"} or key in values:
        sys.exit("Invalid or duplicate Stream credential field")
    if not value.strip() or value != value.strip() or "\x00" in value:
        sys.exit("Invalid Stream credential value")
    values[key] = value
if set(values) != {"TOKEN", "LICENSE_KEY"}:
    sys.exit("Stream TOKEN and LICENSE_KEY are required")
PY

mkdir -p "$WORKDIR"
if [[ ! -f "$WORKDIR/config.ini" ]]; then
    echo "note: $WORKDIR/config.ini absent — Stream will write a default."
    echo "      replace it with stream/config.ini.template and restart."
fi

if docker container inspect "$NAME" >/dev/null 2>&1; then
    echo "container $NAME already exists; inspect/restart it explicitly (nothing removed)" >&2
    exit 1
fi
if [[ "$SKIP_PULL" == 0 ]]; then
    docker pull "$IMAGE"
elif [[ "$SKIP_PULL" != 1 ]]; then
    echo "SKIP_PULL must be 0 or 1" >&2; exit 2
fi
docker image inspect "$IMAGE" >/dev/null

# --user keeps files on the bind mount owned by us rather than root.
# Credentials go in via --env-file so they never appear in process arguments.
# They ARE visible to a privileged Docker administrator via container inspect.
docker run --detach \
    --name "$NAME" \
    --restart unless-stopped \
    --env-file "$ENV_FILE" \
    --user "$(id -u):$(id -g)" \
    --group-add video \
    --volume "$WORKDIR:/user-data" \
    --network host \
    "$IMAGE"

echo "started. logs:   docker logs -f $NAME"
echo "         stop:   docker rm -f $NAME"
