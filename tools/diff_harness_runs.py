#!/usr/bin/env python3
"""Compare two `replay_harness.py video` runs, ignoring proven noise.

Why this exists
---------------
Byte-identical harness output is NOT achievable, and that is a measured fact,
not an excuse. Running the UNMODIFIED baseline twice and diffing it against
itself produced differences in exactly these places:

    BOARD_TRIGGER track=1   plate=CFZ1E72   vs   plate=4141
    BOARD_TRIGGER track=106 plate=LSE2E58   vs   plate=L3E245
    f00061 FAIL low_confidence              vs   f00060 FAIL low_confidence

Same code, same video, different output. The cause is floating-point
nondeterminism in multithreaded ONNX inference: strong reads are stable,
borderline sub-threshold reads flip between runs. So a plain `diff` reports
noise as if it were a regression, which is how a clean refactor gets
misdiagnosed as a behaviour change.

What is signal and what is noise
--------------------------------
SIGNAL — any difference here is a hard failure:
  * confirmed hashes (the values that become vehicle identities)
  * counts: confirms, suppressions, failures, drive-offs, board triggers
  * frames processed
  * suppression reasons and repeat indices

NOISE — normalized away, each justified by the control run:
  * `plate=` candidates on TRIGGER / BOARD_TRIGGER lines. These are
    sub-threshold reads that never reach the vote, let alone a hash.
  * FAIL-line frame indices. A borderline read opening its vote bucket one
    frame earlier or later shifts these.
  * logger module name (`plate_reader.detector` vs `plate_reader.engines`),
    because the engine refactor moved log statements between modules.
  * timestamps and elapsed seconds.
  * stdout/stderr interleave position — the harness prints results on stdout
    while logging on stderr, so a log line can splice into the middle of a
    result line at a different point each run.

Usage
-----
    python3 tools/diff_harness_runs.py BASELINE.txt CANDIDATE.txt

Exit 0 = equivalent. Exit 1 = a real difference. Exit 2 = bad input.
"""
import argparse
import re
import sys
from pathlib import Path

# --- noise patterns, each traceable to the control experiment --------------
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T[\d:]+Z")
_ELAPSED = re.compile(r"in \d+\.\d+s")
_LOGGER_MOD = re.compile(r"plate_reader\.(?:detector|engines)")
# Weak plate candidate on trigger lines only. Deliberately NOT applied to
# CONFIRM lines — those carry hashes and must stay byte-exact.
_WEAK_PLATE = re.compile(r"plate=[A-Z0-9?]*")
_FAIL_LINE = re.compile(r"^\s*f\d+\s+FAIL\b")

# Lines whose content is authoritative and must never be normalized.
_SIGNAL_LINE = re.compile(r"(CONFIRM|~SUPPRESS|Confirms:|Frames processed|"
                          r"Board triggers|DRIVEOFF)")


def normalize(text: str) -> list[str]:
    """Reduce a harness run to its comparable content."""
    # Undo stdout/stderr interleaving: a stderr log line can splice into the
    # middle of a stdout line. Re-break on an embedded timestamp so both
    # halves land on their own lines regardless of where the splice happened.
    text = _TIMESTAMP.sub(lambda m: "\n" + m.group(0), text)

    out: list[str] = []
    for line in text.splitlines():
        line = _TIMESTAMP.sub("<TS>", line)
        line = _ELAPSED.sub("in <ELAPSED>", line)
        line = _LOGGER_MOD.sub("plate_reader.<MOD>", line)

        # Drop FAIL lines: the control run showed their frame index moves.
        if _FAIL_LINE.match(line):
            out.append("  <FAIL-LINE>")
            continue

        # Mask weak plate candidates, but never on a signal-bearing line.
        if not _SIGNAL_LINE.search(line):
            line = _WEAK_PLATE.sub("plate=<WEAK>", line)

        line = line.rstrip()
        if line:
            out.append(line)
    return out


def extract_signal(text: str) -> dict:
    """Pull the invariants out of a run, for a readable summary."""
    hashes = sorted(set(re.findall(r"CONFIRM\s+hash=([0-9a-f]+)", text)))
    def _int(pattern, default=0):
        m = re.search(pattern, text)
        return int(m.group(1)) if m else default
    return {
        "frames": _int(r"Frames processed:\s+(\d+)"),
        "confirms": _int(r"Confirms:\s+(\d+)"),
        "failures": _int(r"Failures:\s+(\d+)"),
        "driveoffs": _int(r"Drive-offs:\s+(\d+)"),
        "triggers": _int(r"Board triggers:\s+(\d+)"),
        "suppressed": len(re.findall(r"~SUPPRESS", text)),
        "hashes": hashes,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Diff two replay_harness video runs, ignoring proven noise."
    )
    ap.add_argument("baseline")
    ap.add_argument("candidate")
    ap.add_argument("--show", type=int, default=40,
                    help="max differing lines to print (default 40)")
    args = ap.parse_args()

    for p in (args.baseline, args.candidate):
        if not Path(p).is_file():
            print(f"ERROR: no such file: {p}", file=sys.stderr)
            return 2

    base_raw = Path(args.baseline).read_text(errors="replace")
    cand_raw = Path(args.candidate).read_text(errors="replace")

    b_sig, c_sig = extract_signal(base_raw), extract_signal(cand_raw)

    print("=" * 70)
    print(f"BASELINE : {args.baseline}")
    print(f"CANDIDATE: {args.candidate}")
    print("=" * 70)

    # --- invariants -------------------------------------------------------
    hard_fail = False
    print(f"{'metric':<14}{'baseline':>12}{'candidate':>12}   verdict")
    print("-" * 70)
    for k in ("frames", "confirms", "suppressed", "failures",
              "driveoffs", "triggers"):
        ok = b_sig[k] == c_sig[k]
        hard_fail |= not ok
        print(f"{k:<14}{b_sig[k]:>12}{c_sig[k]:>12}   "
              f"{'ok' if ok else '*** DIFFERS ***'}")

    # Confirmed hashes are the identity signal — never normalized, never
    # tolerated to move.
    same_hashes = b_sig["hashes"] == c_sig["hashes"]
    hard_fail |= not same_hashes
    print("-" * 70)
    print(f"confirmed hashes ({len(b_sig['hashes'])} vs {len(c_sig['hashes'])}): "
          f"{'IDENTICAL' if same_hashes else '*** DIFFER — HARD FAILURE ***'}")
    if not same_hashes:
        only_b = set(b_sig["hashes"]) - set(c_sig["hashes"])
        only_c = set(c_sig["hashes"]) - set(b_sig["hashes"])
        if only_b:
            print(f"  only in baseline : {sorted(only_b)}")
        if only_c:
            print(f"  only in candidate: {sorted(only_c)}")

    # --- normalized line comparison ---------------------------------------
    # Compared as a SORTED multiset, not as an ordered sequence. The harness
    # writes results to stdout and logs to stderr, so the relative order of
    # the two streams is nondeterministic — the control run showed the same
    # ~SUPPRESS line landing before vs after a BOARD_TRIGGER log line, with
    # byte-identical content. Ordered comparison reports that as two changed
    # lines, which is noise.
    #
    # This does not weaken the check: every line carries its own frame index
    # (f04078) and payload, so a genuine change in what happened still shows
    # up as different content. Ordering WITHIN a stream is preserved by those
    # embedded frame numbers, and the invariants above are order-independent
    # by construction.
    b_lines, c_lines = normalize(base_raw), normalize(cand_raw)
    import difflib
    delta = [d for d in difflib.unified_diff(
        sorted(b_lines), sorted(c_lines), lineterm="", n=0,
        fromfile="baseline", tofile="candidate")]
    body = [d for d in delta if d.startswith(("+", "-"))
            and not d.startswith(("+++", "---"))]

    print("-" * 70)
    if not body:
        print("normalized line diff: EMPTY")
    else:
        print(f"normalized line diff: {len(body)} differing line(s)")
        for d in body[:args.show]:
            print("  " + d)
        if len(body) > args.show:
            print(f"  ... {len(body) - args.show} more (raise --show)")

    print("=" * 70)
    if hard_fail or body:
        print("RESULT: DIFFERENT — investigate before proceeding.")
        return 1
    print("RESULT: EQUIVALENT (identical after removing proven nondeterminism).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
