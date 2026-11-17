"""Detection of restricted personal/financial data in free text and uploaded cells.

Used to reject uploads and reviewer edits that contain data this system must never hold.
Patterns are deliberately conservative (false positives are rejected, not silently stored).
"""

from __future__ import annotations

import re

RESTRICTED_PATTERNS: dict[str, re.Pattern[str]] = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "phone": re.compile(r"(?:\+?\d{1,2}[\s.-])?\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b"),
    "card_or_account_number": re.compile(r"\b(?:\d[ -]?){12,19}\b"),
    "routing_number_label": re.compile(r"\brouting\b|\baba\b|\biban\b", re.IGNORECASE),
}


def find_restricted(text: str) -> list[str]:
    """Names of restricted data categories found in ``text`` (never the matched values)."""
    return [name for name, pattern in RESTRICTED_PATTERNS.items() if pattern.search(text)]
