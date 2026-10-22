# Plate Normalization Specification v1.0

**Status: LOCKED. Do not change without bumping the spec version and writing a migration plan.**

Every plate read on the Pi is normalized through these rules before being hashed. The hash is what the cloud stores as the vehicle identifier. If this rule changes, every existing vehicle hash in the system becomes orphaned — the same physical plate produces a new hash and the customer looks new on every future visit.

This is the single most important contract in the system. Treat it accordingly.

## The rules

Given the raw OCR text output by fast-alpr:

1. **Uppercase**: convert every character to upper case.
2. **Strip non-alphanumeric**: remove every character that is not `A-Z` or `0-9`. This drops spaces, hyphens, dots, slashes, and any OCR artifacts.
3. **Minimum length check**: if the result is shorter than 4 characters, treat it as invalid. Do not hash it. Do not send it.
4. **Maximum length check**: if the result is longer than 12 characters, treat it as invalid. Real US plates max out at 8 characters; anything longer is OCR garbage.
5. **Do NOT strip leading zeros** — some real plates legitimately start with `0`.
6. **Do NOT collapse repeated characters** — `BBB123` and `B123` are different plates.
7. **Do NOT do character substitutions** — no `O→0`, no `I→1`, no `S→5`. fast-alpr's OCR is reproducible: the same image produces the same read, so as long as we are consistent, we still hash the same vehicle to the same value across visits even if our read of the plate is slightly off.
8. **No salt, no prefix, no suffix** in the hash input. The SHA-256 input is exactly the normalized string and nothing else. This must be true so that any independent system (a backfill script, a test harness, an integration partner) can produce the same hash from the same plate without knowing a secret.

## Hash format

- Algorithm: SHA-256
- Input: the normalized string, UTF-8 encoded
- Output: lowercase hex (64 characters, `[0-9a-f]`)

The cloud's API validates that incoming `licensePlate` values are exactly 64 lowercase hex characters. Anything else is rejected as a bad request.

## Examples

| Raw OCR text | Normalized | Hash |
|---|---|---|
| `ABC 123` | `ABC123` | `9d3a...` |
| `abc-123` | `ABC123` | `9d3a...` |
| `ABC.123` | `ABC123` | `9d3a...` |
| `0AB12` | `0AB12` | (leading 0 preserved) |
| `BBB123` | `BBB123` | (not collapsed) |
| `O0O0` | `O0O0` | (no substitution) |
| `A` | (rejected — too short) | — |
| `` (empty) | (rejected — too short) | — |
| `RR9900TXXXXXX` | (rejected — too long) | — |

## Change procedure (if ever needed)

1. Bump the spec version to v2.0 in this file and in `normalizer.py`.
2. Update the cloud schema to track which spec version produced each hash.
3. Write a migration plan for existing data — typically: dual-hash for an overlap period, then deprecate v1.0.
4. Coordinate the rollout so no Pi runs a different spec than its cloud counterpart for more than the overlap period.

**Do not skip these steps. Changing the rule silently is how identity systems get destroyed.**
