#!/usr/bin/env bash
# Prepare a unit to run the ultimateALPR engine.
#
# Idempotent. Safe to re-run. Call from scripts/install.sh, or by hand during
# bringup. Every step here exists because it bit us at least once:
#
#   1. git clone does NOT preserve the exec bit on the SDK's shipped binaries.
#      runtimeKey, benchmark and recognizer have each failed with
#      "Permission denied" for this reason.
#   2. setup.py MUST run from binaries/<os>/<arch>/, never python/ — it uses
#      ../../../ relative paths and dies with
#      "cc1plus: fatal error: /python/...SWIG_python.cxx: No such file".
#   3. The compiled _ultimateAlprSdk.so is ABI-SPECIFIC to the Python minor
#      version. A binding built for 3.13 will NOT load on 3.11 (bookworm), so
#      it must be built ON the target unit against ITS python3 — never shipped
#      pre-built between units.
#   4. Debian 13 / Python 3.13 has no Cython and no distutils, so we compile
#      the pre-generated SWIG source directly with g++ when setup.py can't run.
#   5. LD_LIBRARY_PATH is required at runtime: the dynamic linker resolves
#      libultimate_alpr-sdk.so, so PYTHONPATH alone is not enough.
#
# Usage:
#   scripts/setup_ualpr.sh [SDK_DIR] [TOKEN_SRC]
set -euo pipefail

SDK="${1:-/home/serv/ultimateALPR-SDK}"
TOKEN_SRC="${2:-}"
RUNTIME_DIR="${RUNTIME_DIR:-/home/serv/plate_reader}"
SDK_REPO="https://github.com/DoubangoTelecom/ultimateALPR-SDK.git"
# Audited upstream master on 2026-08-04. Override only for an intentional,
# separately benchmarked upgrade; never deploy an unreviewed moving HEAD.
SDK_COMMIT="${UALPR_SDK_COMMIT:-972506c84ad286bb64331943682084a078f43494}"

say() { printf '\n=== %s ===\n' "$*"; }

# ---------------------------------------------------------------- arch -----
say "architecture"
ARCH="$(uname -m)"
echo "uname -m: $ARCH"
if [ "$ARCH" != "aarch64" ]; then
    echo "ERROR: expected aarch64 (64-bit Raspberry Pi OS / Debian)." >&2
    echo "       binaries/raspbian/ is 32-bit armv7l and is NOT usable on a Pi 5." >&2
    exit 2
fi
ARCH_DIR="$SDK/binaries/linux/aarch64"

# ----------------------------------------------------------------- sdk -----
say "SDK checkout"
if [ ! -d "$SDK" ]; then
    echo "cloning $SDK_REPO -> $SDK"
    git clone --no-checkout "$SDK_REPO" "$SDK"
else
    echo "already present: $SDK"
fi
echo "pinning SDK to $SDK_COMMIT"
git -C "$SDK" fetch --depth 1 origin "$SDK_COMMIT"
git -C "$SDK" checkout --detach "$SDK_COMMIT"
[ "$(git -C "$SDK" rev-parse HEAD)" = "$SDK_COMMIT" ] || {
    echo "ERROR: SDK checkout did not resolve to pinned commit" >&2
    exit 2
}
[ -d "$ARCH_DIR" ] || { echo "ERROR: $ARCH_DIR missing" >&2; exit 2; }

# --------------------------------------------------------------- chmod -----
# Gotcha 1.
say "restoring exec bits (git clone drops them)"
chmod +x "$ARCH_DIR"/* 2>/dev/null || true
ls -l "$ARCH_DIR" | awk '/recognizer|benchmark|runtimeKey/ {print "  " $1, $NF}'

# --------------------------------------------------------------- python ----
# Gotcha 3: build against THIS unit's interpreter.
say "python"
PYBIN="${PYBIN:-python3}"
PYVER="$("$PYBIN" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
echo "building binding against $PYBIN ($PYVER)"

SO="$ARCH_DIR/_ultimateAlprSdk.so"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Import exactly the way production does — via plate_reader.engines, which
# installs the `imp` shim the vendor's SWIG 2.0.9 wrapper needs on Python
# >= 3.12. A bare `import ultimateAlprSdk` would fail here for a reason that
# has nothing to do with whether the binding actually works.
IMPORT_CHECK="
import sys
sys.path.insert(0, '$REPO_DIR')
from plate_reader.engines import _install_imp_shim
_install_imp_shim()
import ultimateAlprSdk
print('ultimateAlprSdk import OK (python ' + sys.version.split()[0] + ')')
"

if PYTHONPATH="$ARCH_DIR:$SDK/python" LD_LIBRARY_PATH="$ARCH_DIR" \
        "$PYBIN" -c "$IMPORT_CHECK" >/dev/null 2>&1; then
    echo "binding already importable — skipping build"
else
    say "building SWIG binding"
    rm -f "$SO"
    cd "$ARCH_DIR"          # Gotcha 2: must build from here, not python/
    if "$PYBIN" -c "import Cython, setuptools" 2>/dev/null; then
        echo "using the SDK's setup.py (Cython present)"
        "$PYBIN" "$SDK/python/setup.py" build_ext --inplace
    else
        # Gotcha 4: no Cython / no distutils (Debian 13, Python >= 3.12).
        # setup.py only wraps a single g++ invocation, so do it directly.
        echo "no Cython/distutils — compiling the SWIG source directly with g++"
        INC="$("$PYBIN" -c 'import sysconfig; print(sysconfig.get_paths()["include"])')"
        g++ -std=c++11 -fPIC -shared \
            -I"$SDK/c++" -I"$INC" \
            "$SDK/python/ultimateALPR-SDK-API-PUBLIC-SWIG_python.cxx" \
            -L"$ARCH_DIR" -lultimate_alpr-sdk \
            -o "$SO"
    fi
    [ -f "$SO" ] || { echo "ERROR: binding was not produced" >&2; exit 2; }
    ls -l "$SO"
fi

say "verifying import"
PYTHONPATH="$ARCH_DIR:$SDK/python" LD_LIBRARY_PATH="$ARCH_DIR" \
    "$PYBIN" -c "$IMPORT_CHECK"

# ---------------------------------------------------------------- token ----
say "licence token"
TOKEN_DST="$RUNTIME_DIR/license.token"
if [ -n "$TOKEN_SRC" ]; then
    [ -f "$TOKEN_SRC" ] || { echo "ERROR: no token at $TOKEN_SRC" >&2; exit 2; }
    mkdir -p "$RUNTIME_DIR"
    cp "$TOKEN_SRC" "$TOKEN_DST"
    chmod 600 "$TOKEN_DST"
    chown "$(id -un):$(id -gn)" "$TOKEN_DST" 2>/dev/null || true
    # Deliberately prints metadata only — never the token contents.
    echo "installed: $TOKEN_DST ($(stat -c%s "$TOKEN_DST") bytes, mode $(stat -c%a "$TOKEN_DST"))"
elif [ -f "$TOKEN_DST" ]; then
    echo "already present: $TOKEN_DST (mode $(stat -c%a "$TOKEN_DST"))"
else
    echo "WARNING: no licence token installed."
    echo "  Without one the SDK runs in TRIAL mode and redacts the last"
    echo "  character of every plate. normalize() then strips the '*' and the"
    echo "  remainder hashes into a confident WRONG identity."
    echo "  The '*' guard will refuse to hash, so the unit fails loudly —"
    echo "  but it will not read plates. See docs/LICENSING.md."
fi

say "done"
cat <<EOF
Runtime environment (systemd sets these in plate-reader.service):
  LD_LIBRARY_PATH=$ARCH_DIR
  PYTHONPATH=$SDK/python:$ARCH_DIR

Next: scripts/verify_ualpr_license.sh to confirm unmasked output.
EOF
