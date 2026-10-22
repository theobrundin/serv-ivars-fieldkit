"""Plate Normalization Specification v1.0 implementation.

DO NOT change the behavior of this module. Every vehicle hash in the system
depends on this exact rule. See NORMALIZATION_SPEC.md.

If you need to change the rule, follow the change procedure in that doc.
"""
import re

from . import NORMALIZATION_SPEC_VERSION  # noqa: F401 — explicit dependency

_ALNUM_RE = re.compile(r"[^A-Z0-9]")


def normalize(raw: str, *, min_length: int = 3, max_length: int = 12) -> str | None:
    """Normalize a raw OCR plate string per spec v1.0.

    Returns the normalized string, or None if the input is invalid (too short
    or too long after stripping). Never raises.

    Steps:
      1. Uppercase.
      2. Strip every character that is not A-Z or 0-9.
      3. Return None if shorter than min_length or longer than max_length.
      4. Leading zeros preserved. No collapsing. No substitutions.
    """
    if not isinstance(raw, str):
        return None
    upper = raw.upper()
    stripped = _ALNUM_RE.sub("", upper)
    if len(stripped) < min_length:
        return None
    if len(stripped) > max_length:
        return None
    return stripped
