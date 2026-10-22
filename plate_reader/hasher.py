"""SHA-256 hashing of normalized plate strings. No salt, no prefix.

Includes an import-time self-test so the script refuses to start if the
hashing library is broken (very rare, but a silent failure here would
corrupt every visit going forward — well worth the millisecond check).
"""
import hashlib
import re


_NORMALIZED_PLATE = re.compile(r"^[A-Z0-9]{3,12}$")


def hash_plate(normalized: str) -> str:
    """Return lowercase 64-character hex SHA-256 of the normalized plate."""
    if not isinstance(normalized, str) or not _NORMALIZED_PLATE.fullmatch(
        normalized
    ):
        raise ValueError(
            "hash input must already satisfy NORMALIZATION_SPEC v1.0 "
            "([A-Z0-9], length 3-12)"
        )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def self_test() -> None:
    """Verify SHA-256 produces known-good values. Called at import time of main.

    The expected values below were independently computed and verified against
    `echo -n 'ABC123' | sha256sum` and similar. If they ever change, hashlib
    in this Python build is broken and we refuse to start rather than corrupt
    every visit's hash silently.
    """
    expected = {
        "ABC123":  "e0bebd22819993425814866b62701e2919ea26f1370499c1037b53b9d49c2c8a",
        "TEST001": "f4662a95cd58bd092252d0211ba633199102a538037e1f4c4bcabb3a979efd99",
    }
    for plate, want in expected.items():
        got = hash_plate(plate)
        if got != want:
            raise RuntimeError(
                f"SHA-256 self-test failed. hash_plate({plate!r}) = {got!r}, "
                f"expected {want!r}. Hashing module is unsafe; refusing to start."
            )
