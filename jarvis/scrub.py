"""Scrub — JARVIS v4: raaz ki shakal, pakdi hui.

Deterministic secret-SHAPE detection for speech transcripts
(Dodaios's audio-hold idea, at the level this build can
honestly enforce). The Live voice path streams audio straight
to the cloud model; what comes BACK as the user's transcript
is what enters history and memory. If that transcript carries
a secret's shape — a card number (Luhn-checked, so order IDs
don't trip it), an API key's unmistakable prefix — the words
are stopped at the door: never written to history, the owner
is told, and playback drains.

Shapes only, matched in code, no model judgement: the same
input always gives the same verdict.
"""

from __future__ import annotations

import re

_PATTERNS = (
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("api-key", re.compile(r"\bAQ\.[A-Za-z0-9._-]{10,}\b")),
    ("api-key", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b")),
    ("api-key", re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b")),
    ("api-key", re.compile(
        r"\bxai-[A-Za-z0-9]{16,}\b")),
)
_CARD_RUN = re.compile(r"\b(?:\d[ -]?){13,19}\b")

REDACTED = "[raaz wali baat — suraksha ke liye hata di gayi]"


def _luhn_ok(digits: str) -> bool:
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def find_secret_shapes(text: str) -> list:
    """The KINDS of secret shapes present ('card', 'api-key')
    — never the values themselves."""
    text = str(text or "")
    found = []
    for kind, pattern in _PATTERNS:
        if pattern.search(text) and kind not in found:
            found.append(kind)
    for match in _CARD_RUN.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            if "card" not in found:
                found.append("card")
            break
    return found
