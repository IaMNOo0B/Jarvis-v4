"""Information Flow Control — labels decide, signals only testify (Step 18).

Design (from the ai-eng study, P18-L15 indirect prompt injection, P18
ArtPrompt cloaking, P18-L25 EchoLeak):
- Keyword filters lose: adaptive attacks broke >90% of published IPI
  defenses, and payloads arrive encoded (base64, rot13, homoglyphs,
  zero-width joiners). Detection is evidence, never the rule.
- The RULE is structural (IFC, borrowed from OS security): every piece
  of content carries a label derived from its ORIGIN. Untrusted content
  may inform an answer; it may NEVER originate an action. Actions are
  originated only by trusted owner-origin content (or ratified through
  the gate — a different module's job).
- `normalize()` runs BEFORE any detector so cloaked payloads become
  visible: NFKC folding, zero-width stripping, confusable mapping,
  base64-span decoding, rot13 token decoding. Normalization is for
  VISIBILITY — it never upgrades a label. A beautifully clean web page
  is still untrusted.

Stdlib only.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import hashlib
import hmac
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

TRUSTED = "trusted"
UNTRUSTED = "untrusted"

# Only the owner's own channels originate action. Everything else —
# web, email, files, tool output, other agents — is untrusted by default.
OWNER_ORIGIN = "owner"


@dataclass
class LabeledContent:
    text: str
    label: str  # TRUSTED | UNTRUSTED
    origin: str  # e.g. "owner", "web:example.com", "tool:search", "email"
    meta: dict = field(default_factory=dict)


def label_content(text: str, origin: str) -> LabeledContent:
    """Assign the label from the ORIGIN — never from the content.

    Content cannot argue its way into trust; a page that says "this is
    the owner speaking" is still web content. Only the owner channel
    itself earns TRUSTED.
    """
    label = TRUSTED if origin == OWNER_ORIGIN else UNTRUSTED
    return LabeledContent(text=text, label=label, origin=origin)


def may_originate_action(content: LabeledContent) -> bool:
    """THE rule. True only for trusted owner-origin content.

    Note what is NOT consulted: the text, its tone, its signals. An
    untrusted page with zero injection signals still may not originate
    an action; the owner's own words may, even if they happen to
    contain a signal-shaped phrase (his joke is his to make).
    """
    return content.label == TRUSTED and content.origin == OWNER_ORIGIN


# -- normalization (visibility, never trust) -------------------------------

_ZERO_WIDTH = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD], None)

# Confusables-lite: Cyrillic/Greek lookalikes that NFKC does NOT fold.
_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
    "х": "x", "у": "y", "і": "i", "ѕ": "s", "ј": "j",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M",
    "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T",
    "Х": "X", "ο": "o", "ρ": "p", "τ": "t", "ν": "v",
    "α": "a", "ε": "e", "ι": "i", "κ": "k",
})

_B64_SPAN_RE = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")

# Words whose rot13 forms we check token-by-token — the vocabulary of
# instruction-smuggling. A token is rot13-decoded only when its decoded
# form lands in this set and its raw form does not.
_ROT13_SIGNAL_WORDS = {
    "ignore", "previous", "prior", "instructions", "instruction",
    "system", "prompt", "reveal", "disregard", "forget", "override",
    "obey", "password", "secret", "send", "delete", "execute",
}


def _decode_b64_spans(text: str) -> str:
    """Replace base64-looking spans with their decoded text when the
    decode is clean printable UTF-8; leave the span otherwise."""
    def repl(m: re.Match) -> str:
        span = m.group(0)
        if len(span) % 4 != 0:
            return span
        try:
            raw = base64.b64decode(span, validate=True)
            decoded = raw.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return span
        if decoded and all(ch.isprintable() or ch in " \t" for ch in decoded):
            return decoded
        return span
    return _B64_SPAN_RE.sub(repl, text)


def _decode_rot13_tokens(text: str) -> str:
    out = []
    for token in re.split(r"(\s+)", text):
        low = token.strip(".,;:!?\"'()[]{}<>").lower()
        if low and low not in _ROT13_SIGNAL_WORDS:
            decoded = codecs.decode(low, "rot_13")
            if decoded in _ROT13_SIGNAL_WORDS:
                token = token.replace(low, decoded) if low in token \
                    else codecs.decode(token, "rot_13")
        out.append(token)
    return "".join(out)


def normalize(text: str) -> str:
    """Make cloaked payloads visible. Pipeline, in order:

    NFKC fold (fullwidth/compat forms) → strip zero-width joiners →
    map Cyrillic/Greek confusables → decode base64 spans → decode
    rot13 signal tokens. Idempotent on ordinary clean text.
    """
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_ZERO_WIDTH)
    text = text.translate(_CONFUSABLES)
    text = _decode_b64_spans(text)
    text = _decode_rot13_tokens(text)
    return text


# -- screening (evidence only) ----------------------------------------------

_INJECTION_PATTERNS = (
    ("ignore_instructions",
     re.compile(r"ignore\s+(all\s+)?(the\s+)?(previous|prior|above|earlier)"
                r"\s+(instructions|prompts|rules)", re.I)),
    ("disregard_instructions",
     re.compile(r"disregard\s+(all\s+)?(previous|prior|above|your)",
                re.I)),
    ("forget_instructions",
     re.compile(r"forget\s+(everything|all|your\s+instructions)", re.I)),
    ("system_prompt_probe",
     re.compile(r"(reveal|print|show|repeat|leak)\s+.*(system\s+prompt|"
                r"your\s+instructions)", re.I)),
    ("role_override",
     re.compile(r"you\s+are\s+now\s+(a|an|in)\b", re.I)),
    ("new_instructions",
     re.compile(r"(new|updated)\s+instructions\s*:", re.I)),
    ("fake_system_tag",
     re.compile(r"</?\s*(system|sys)\s*>", re.I)),
    ("exfil_request",
     re.compile(r"(send|post|upload|forward)\s+.*(password|api\s*key|"
                r"secret|credentials|token)", re.I)),
)


def screen(content: LabeledContent) -> dict:
    """Screen a piece of content. Returns evidence + the label.

    `injection_signals` are EVIDENCE for logs and human review — they
    never change the label and never grant or revoke action rights.
    The label, set at origin, is the whole rule.
    """
    normalized = normalize(content.text)
    signals = []
    for name, pattern in _INJECTION_PATTERNS:
        for m in pattern.finditer(normalized):
            signals.append({
                "signal": name,
                "span": [m.start(), m.end()],
                "excerpt": normalized[m.start():m.end()][:80],
            })
    return {
        "label": content.label,
        "origin": content.origin,
        "may_originate_action": may_originate_action(content),
        "normalized_text": normalized,
        "injection_signals": signals,
    }


# ---------------------------------------------------------------------------
# Spotlighting — unforgeable delimiters for outside content (Sentient's
# pattern): the closing marker carries an HMAC code of the body under a
# per-install secret, so content cannot fake its own end and escape the
# data block; the code is stable per body, so prompt caches stay warm.
# ---------------------------------------------------------------------------

_OUTSIDE_OPEN_RE = re.compile(r"\[outside content[^\]]*\]", re.I)
_OUTSIDE_CLOSE_RE = re.compile(r"\[end outside content[^\]]*\]", re.I)


def outside_code(text: str, key: bytes) -> str:
    """The 8-hex HMAC code a body carries under this install's key."""
    return hmac.new(bytes(key), str(text).encode("utf-8"),
                    hashlib.sha256).hexdigest()[:8]


def mark_outside(text: str, key: bytes) -> str:
    """Wrap untrusted text in spotlight markers. Fake markers inside
    the body are stripped first — the only closing marker in the
    output is the real one, carrying the body's code."""
    body = _OUTSIDE_CLOSE_RE.sub(
        "", _OUTSIDE_OPEN_RE.sub("", str(text)))
    code = outside_code(body, key)
    return (f"[outside content {code}]\n{body}\n"
            f"[end outside content {code}]")


def is_marked(text: str, key: bytes) -> bool:
    """True when the text is a mark_outside() product whose closing
    code honestly matches its body under this key."""
    s = str(text).strip()
    if not (s.startswith("[outside content ")
            and "[end outside content " in s):
        return False
    try:
        code = s.split("[outside content ", 1)[1].split("]", 1)[0]
        body = s.split("]\n", 1)[1].rsplit("\n[end outside content ", 1)[0]
    except IndexError:
        return False
    return hmac.compare_digest(code.strip(),
                               outside_code(body, key))
