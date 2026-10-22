#!/usr/bin/env bash
# Assert this unit's ultimateALPR licence is actually active BEFORE it is
# allowed to write hashes.
#
# Why this is a separate, mandatory bringup gate:
#
# A token that fails to load does NOT error. The SDK silently runs in trial
# mode and redacts the last character of every plate ("3PEDLM*"). That
# redaction survives NORMALIZATION_SPEC v1.0 — rule 2 strips '*' as
# non-alphanumeric, and the 5-6 character remainder clears the rule-3 minimum
# of 4 — so it hashes cleanly into a confident WRONG vehicle identity with no
# error anywhere in the stack.
#
# "init succeeded" therefore proves nothing. The licence has to be MEASURED.
set -euo pipefail

SDK="${SDK:-/home/serv/ultimateALPR-SDK}"
CONFIG="${CONFIG:-/home/serv/plate_reader/config.json}"
ARCH_DIR="$SDK/binaries/linux/aarch64"

export LD_LIBRARY_PATH="$ARCH_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PYTHONPATH="$ARCH_DIR:$SDK/python${PYTHONPATH:+:$PYTHONPATH}"

cd "$(dirname "$0")/.."

python3 - "$CONFIG" <<'PY'
import pathlib
import sys

cfg_path = pathlib.Path(sys.argv[1])
if not cfg_path.is_file():
    print(f"FAIL: no config at {cfg_path}")
    raise SystemExit(2)

from plate_reader.config import load
from plate_reader.engines import build_engine

cfg = load(cfg_path)
print(f"engine            : {cfg.alpr.engine}")
print(f"license_token_file: {cfg.alpr.license_token_file or '(none - TRIAL MODE)'}")
print(f"python            : {sys.version.split()[0]}")

if cfg.alpr.engine != "ultimate_alpr":
    print("SKIP: engine is not ultimateALPR; nothing to verify.")
    raise SystemExit(0)

if not cfg.alpr.license_token_file:
    print("")
    print("FAIL: no licence token configured. The unit would run in TRIAL")
    print("      mode and redact every plate. See docs/LICENSING.md.")
    raise SystemExit(1)

eng = build_engine(cfg)
try:
    # open() runs verify_license_unmasked() internally and raises if the
    # returned text is '*'-redacted.
    eng.open()
    if eng._license_verified:
        print("")
        print("PASS: licence verified — unmasked output confirmed.")
        rc = 0
    else:
        # A token WAS configured, so "couldn't tell" is a failure, not a
        # shrug. Either the probe image is missing or the engine did not read
        # it — both mean this unit has not demonstrated an active licence, and
        # it must not be declared ready to write hashes.
        print("")
        print("FAIL: a licence token is configured but this check could NOT")
        print("  confirm unmasked output. Do not declare the unit ready.")
        print("  Check: assets_folder points at a real SDK assets dir, and")
        print("  <assets>/images/lic_us_1280x720.jpg exists.")
        print("  The '*' guard in infer() remains active regardless, so a")
        print("  trial-mode fallback would still be caught before hashing.")
        rc = 1
finally:
    try:
        eng.close()
    except Exception:
        pass
raise SystemExit(rc)
PY
