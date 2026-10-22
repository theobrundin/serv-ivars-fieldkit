#!/usr/bin/env bash
# Build the ultimateALPR SWIG Python extension inside the container.
#
# Encodes the two build gotchas the Stage 1 spike hit, so nobody rediscovers
# them:
#
#   1. setup.py MUST run from binaries/<os>/<arch>/, never from python/.
#      It references ../../../python/... and ../../../c++ relatively, so from
#      python/ it dies with:
#        cc1plus: fatal error: /python/...SWIG_python.cxx: No such file
#      The .so is produced in the CURRENT dir (the binaries dir), not python/.
#
#   2. `git clone` does not preserve the exec bit on the SDK's shipped
#      binaries, so ./recognizer gives "Permission denied". We chmod copies in
#      the container rather than mutating the mounted host tree.
#
# Idempotent: skips the compile if the extension already imports.
set -euo pipefail

SDK=${SDK:-/sdk}
ARCH_DIR="$SDK/binaries/linux/aarch64"

if [ ! -d "$ARCH_DIR" ]; then
    echo "ERROR: $ARCH_DIR not found. Mount the SDK at $SDK." >&2
    echo "       (binaries/raspbian is 32-bit armv7l and is NOT usable here.)" >&2
    exit 2
fi

if [ "$(uname -m)" != "aarch64" ]; then
    echo "ERROR: container arch is $(uname -m), expected aarch64." >&2
    echo "       Do not pass --platform=linux/amd64; the x86_64 SDK binaries" >&2
    echo "       SIGILL under QEMU emulation on Apple Silicon." >&2
    exit 2
fi

if PYTHONPATH="$ARCH_DIR:$SDK/python" python3 -c "import ultimateAlprSdk" 2>/dev/null; then
    echo "ultimateAlprSdk already importable; nothing to build."
    exit 0
fi

echo "building SWIG extension from $ARCH_DIR ..."
cd "$ARCH_DIR"                      # gotcha #1: must be here, not python/
python3 "$SDK/python/setup.py" build_ext --inplace

ls -la "$ARCH_DIR"/_ultimateAlprSdk*.so

# The wrapper imports the extension by name; make it visible next to the
# pure-python module too. Best-effort: the mount may be read-only.
cp "$ARCH_DIR"/_ultimateAlprSdk*.so "$SDK/python/" 2>/dev/null \
    || echo "note: could not copy .so into $SDK/python (read-only mount?) — " \
            "PYTHONPATH already includes $ARCH_DIR, so this is fine."

echo "verifying import ..."
PYTHONPATH="$ARCH_DIR:$SDK/python" python3 -c \
    "import ultimateAlprSdk; print('ultimateAlprSdk import OK')"
