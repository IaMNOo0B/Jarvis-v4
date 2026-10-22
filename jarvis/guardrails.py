"""Guardrails — ECC's Hookify, JARVIS-bound.

Hookify's insight: some rules should not be prose the
brain is ASKED to obey (JARVIS-RULES.txt) but machinery
that STOPS the call. And the rules worth having are the
ones real life teaches — so they are MINED from the
owner's own words ("kabhi meri photo delete mat karna",
"never send email without asking") instead of being
invented upfront.

The life of a guardrail here:

1. MINE — a prohibition the owner states in a normal turn
   becomes a CANDIDATE in data/guardrails.json. A
   candidate enforces NOTHING. Ever.
2. ACCEPT — only the owner's /guardrails accept <id>
   promotes a candidate to a standing rule.
3. ENFORCE — make_dispatch checks accepted rules BEFORE
   the gate (before cards, before everything): a match is
   a denial carrying the rule's id, journaled as such. A
   refusal is a decision, not a failure — it never falls
   back and is never learned from.

A rule is {tool (a name or '*'), match, action: "deny"}:
it fires when `match` (lowercased) is a substring of the
tool name + the JSON-serialized args (lowercased). Simple
on purpose — the owner can read exactly what will stop.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

# One lock per guardrails file, shared across instances
# (the marathon rule — callers build fresh books freely).
_LOCKS: dict = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path) -> threading.Lock:
    key = str(Path(path).resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


class GuardrailBook:
    """Candidates + accepted rules over
    data/guardrails.json. Atomic saves, tolerant reads."""

    def __init__(self, data_dir):
        self.path = Path(data_dir) / "guardrails.json"
        self._lock = _lock_for(self.path)

    # -- storage ------------------------------------------------

    def _load(self) -> list:
        try:
            data = json.loads(self.path.read_text(
                encoding="utf-8"))
            rows = data.get("rules") if isinstance(data, dict) \
                else data
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)]
        except Exception:
            pass
        return []

    def _save(self, rows: list) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"rules": rows},
                                  ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    # -- reads --------------------------------------------------

    def list(self, status: str = None) -> list:
        with self._lock:
            rows = self._load()
        if status:
            rows = [r for r in rows
                    if r.get("status") == status]
        return rows

    def get(self, gid: str):
        for row in self.list():
            if row.get("id") == gid:
                return row
        return None

    # -- mutations ------------------------------------------------

    def add_candidate(self, tool: str, match: str,
                      source: str, form: str = "deny"):
        """(row, created) — the same (tool, match) is never
        mined twice, whatever its status."""
        with self._lock:
            rows = self._load()
            for row in rows:
                if row.get("tool") == tool \
                        and row.get("match") == match:
                    return row, False
            row = {"id": uuid.uuid4().hex[:8],
                   "tool": str(tool) or "*",
                   "match": str(match),
                   "action": "deny",
                   "status": "pending",
                   "form": form,
                   "source": str(source)[:200],
                   "created": time.time()}
            rows.append(row)
            self._save(rows)
            return row, True

    def accept(self, gid: str):
        with self._lock:
            rows = self._load()
            for row in rows:
                if row.get("id") == gid:
                    row["status"] = "accepted"
                    self._save(rows)
                    return row
            return None

    def drop(self, gid: str) -> bool:
        with self._lock:
            rows = self._load()
            kept = [r for r in rows if r.get("id") != gid]
            if len(kept) == len(rows):
                return False
            self._save(kept)
            return True

    # -- enforcement ----------------------------------------------

    def match_for(self, tool_name: str, args) -> dict:
        """The accepted rule stopping this call, or None.
        Only ACCEPTED rules enforce — candidates are paper."""
        haystack = (str(tool_name) + " " + json.dumps(
            args or {}, ensure_ascii=False,
            default=str)).lower()
        for row in self.list(status="accepted"):
            if row.get("action") != "deny":
                continue
            if row.get("tool") not in ("*", tool_name):
                continue
            needle = str(row.get("match", "")).lower()
            if needle and needle in haystack:
                return row
        return None


# ------------------------------------------------------------------ #
# Mining — the owner's own words -> candidate rules
# ------------------------------------------------------------------ #

_STOPWORDS = {
    "the", "a", "an", "my", "me", "to", "please", "ever",
    "do", "doing", "it", "this", "that", "without", "asking",
    "first", "bhi", "kabhi", "mat", "meri", "mera", "mere",
    "mujhe", "tum", "aap", "ke", "ki", "ka", "se", "par",
    "bina", "liye", "kuch", "koi", "ye", "yeh", "wo", "woh",
    "hai", "ho", "karna", "karo", "kar", "bhejna",
}

# Distinctive anchors, in the shapes the owner actually
# says them; the first one found in the content becomes
# the match string (canonical form on the right).
_ANCHORS = [
    ("whatsapp", "whatsapp"), ("instagram", "instagram"),
    ("facebook", "facebook"), ("telegram", "telegram"),
    ("email", "email"), ("mail", "email"),
    ("message", "message"), ("sms", "sms"),
    ("photo", "photo"), ("tasveer", "photo"),
    ("video", "video"), ("file", "file"),
    ("folder", "folder"), ("call", "call"),
    ("post", "post"), ("tweet", "tweet"),
    ("payment", "payment"), ("paisa", "paisa"),
    ("paise", "paisa"), ("money", "money"),
    ("password", "password"), ("delete", "delete"),
    ("download", "download"), ("upload", "upload"),
]

_PATTERNS = [
    # kabhi (bhi) X mat <verb> — the Hinglish prohibition.
    (re.compile(r"kabhi\s+(?:bhi\s+)?(.+?)\s+mat\s+(\w+)?",
                re.I), "deny"),
    # X kabhi mat <verb>
    (re.compile(r"(.+?)\s+kabhi\s+mat\s+(\w+)?", re.I), "deny"),
    # never (ever) X
    (re.compile(r"never\s+(?:ever\s+)?(.+)", re.I), "deny"),
    # don't / do not ever X
    (re.compile(r"(?:don'?t|do\s+not)\s+ever\s+(.+)", re.I),
     "deny"),
    # hamesha pehle X — always-first; mined as a candidate
    # too (the owner judges the shape at accept time).
    (re.compile(r"hamesha\s+pehle\s+(.+)", re.I),
     "hamesha-pehle"),
    (re.compile(r"always\s+(?:first|pehle)\s+(.+)", re.I),
     "hamesha-pehle"),
]


def _derive_match(content: str):
    """Content words -> one enforcement substring. An
    anchor word wins (it is what will actually appear in a
    tool name or args); otherwise the longest surviving
    content word of 4+ letters."""
    words = re.findall(r"[a-z]+", content.lower())
    for word in words:
        for anchor, canonical in _ANCHORS:
            if word == anchor or word == anchor + "s":
                return canonical
    rest = [w for w in words
            if w not in _STOPWORDS and len(w) >= 4]
    if rest:
        return max(rest, key=len)
    return None


def mine_guardrail(text: str):
    """(tool, match, form) for an owner-stated rule in this
    text, or None. Deterministic regex mining — no model
    call, no guessing beyond the stated patterns."""
    text = str(text or "")
    for pattern, form in _PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        content = m.group(1)
        if m.lastindex and m.lastindex >= 2 \
                and m.group(2):
            content = content + " " + m.group(2)
        content = content.split(".")[0].split("!")[0]
        match = _derive_match(content)
        if match:
            return "*", match, form
    return None
