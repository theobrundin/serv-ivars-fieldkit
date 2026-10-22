#!/usr/bin/env bash
# Fixed root-owned entrypoint; install the reviewed helper alongside this file.
# Application-only OTA intentionally does not install network software.
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
exec /usr/bin/python3 /usr/local/sbin/serv-cellular-supervisor "$@"
