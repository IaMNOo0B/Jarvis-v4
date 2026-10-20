"""Memory canary entries — JARVIS v4's exfiltration tripwire (Step 15).

Design (from the ai-eng study, P15-L11 browser agents + P15-L14 kill
switches/canary tokens):
- A canary is a fake "fact" planted in memory carrying a unique random
  token. It has no legitimate reason to ever leave the machine.
- If a canary token ever appears in text headed to an untrusted channel,
  memory is being exfiltrated — that is an alarm, full stop.
- The REGISTRY is the ground truth, never the model's output: it stores
  only SHA-256 hashes of the tokens, sealed with a checksum, so the
  detector does not depend on the (possibly poisoned) facts file and a
  tampered registry is itself detectable.

Stdlib only. The registry lives beside the facts it watches.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Optional

REGISTRY_NAME = "canary_registry.json"
FACTS_NAME = "canary_facts.jsonl"

# Plausible-sounding memory facts. The token is the payload; the sentence
# around it is camouflage so the canary sits in memory like a real fact.
_FACT_TEMPLATES = (
    "The owner's library membership reference is {token}.",
    "The home renovation project is filed under codename {token}.",
    "The owner's bicycle frame serial is recorded as {token}.",
    "The guest-room wifi voucher code is {token}.",
    "The owner's old scooter registration note reads {token}.",
)

_TOKEN_ALPHABET = "abcdef0123456789"
_CANDIDATE_RE = re.compile(r"[A-Za-z0-9]+")
_MIN_CANDIDATE_LEN = 8


class CanaryError(Exception):
    """Raised when the canary registry is missing, corrupt, or tampered."""


def _new_token() -> str:
    """A unique single-run alphanumeric token (hashable, greppable)."""
    return "zqx" + secrets.token_hex(8)  # 19 chars, one [A-Za-z0-9] run


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.lower().encode("ascii")).hexdigest()


def _seal(entries: list[dict]) -> str:
    canon = json.dumps(entries, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _registry_path(store_dir: str) -> str:
    return os.path.join(store_dir, REGISTRY_NAME)


def _facts_path(store_dir: str) -> str:
    return os.path.join(store_dir, FACTS_NAME)


def _load_registry(store_dir: str) -> dict:
    path = _registry_path(store_dir)
    if not os.path.exists(path):
        raise CanaryError(f"no canary registry in {store_dir} — plant first")
    try:
        with open(path, "r", encoding="utf-8") as f:
            reg = json.load(f)
    except ValueError as exc:
        raise CanaryError(f"canary registry unreadable: {exc}") from exc
    entries = reg.get("entries")
    if not isinstance(entries, list) or reg.get("seal") != _seal(entries):
        raise CanaryError("canary registry seal mismatch — registry tampered")
    return reg


def plant(store_dir: str, count: int = 3) -> list[dict]:
    """Create `count` new canary facts and record their hashes.

    Returns the planted facts (INCLUDING the raw tokens) — the caller
    inserts these into memory. The registry on disk stores hashes only.
    Safe to call repeatedly; new canaries append to the existing set.
    """
    if count < 1:
        raise ValueError("count must be >= 1")
    os.makedirs(store_dir, exist_ok=True)
    entries: list[dict] = []
    if os.path.exists(_registry_path(store_dir)):
        entries = list(_load_registry(store_dir)["entries"])
    known_hashes = {e["token_hash"] for e in entries}

    planted: list[dict] = []
    with open(_facts_path(store_dir), "a", encoding="utf-8") as facts_f:
        for i in range(count):
            token = _new_token()
            while _token_hash(token) in known_hashes:  # uniqueness, paranoia
                token = _new_token()
            known_hashes.add(_token_hash(token))
            fact_text = _FACT_TEMPLATES[(len(entries) + i) % len(_FACT_TEMPLATES)]
            fact_text = fact_text.format(token=token)
            entry = {
                "canary_id": f"canary-{len(entries) + i + 1:03d}",
                "token_hash": _token_hash(token),
                "fact_hash": hashlib.sha256(
                    fact_text.encode("utf-8")).hexdigest(),
                "planted_at": datetime.now(timezone.utc).isoformat(),
            }
            entries.append(entry)
            record = {"canary_id": entry["canary_id"], "fact": fact_text,
                      "token": token, "planted_at": entry["planted_at"]}
            facts_f.write(json.dumps(record, ensure_ascii=False) + "\n")
            planted.append(record)

    reg = {"version": 1, "entries": entries, "seal": _seal(entries)}
    tmp = _registry_path(store_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _registry_path(store_dir))
    return planted


def scan_text(store_dir: str, text: str) -> list[dict]:
    """Return one alarm dict per canary token found in `text`.

    Matching is hash-based: every alphanumeric run in the text is hashed
    and compared against the registry — the registry never needs (and
    never stores) a raw token.
    """
    if not text:
        return []
    reg = _load_registry(store_dir)
    by_hash = {e["token_hash"]: e for e in reg["entries"]}
    alarms: list[dict] = []
    seen: set[str] = set()
    for m in _CANDIDATE_RE.finditer(text):
        run = m.group(0)
        if len(run) < _MIN_CANDIDATE_LEN:
            continue
        h = _token_hash(run)
        if h in by_hash and h not in seen:
            seen.add(h)
            entry = by_hash[h]
            alarms.append({
                "canary_id": entry["canary_id"],
                "token_hash": h,
                "matched_at_offset": m.start(),
            })
    return alarms


def scan_and_log(store_dir: str, journal, channel: str,
                 text: str) -> list[dict]:
    """Scan `text` destined for `channel`; journal-log every alarm.

    `journal` is a jarvis.journal.Journal (anything with .append(kind,
    payload)). Clean text writes nothing — the journal stays signal-only.
    Returns the alarm list (empty = clean).
    """
    alarms = scan_text(store_dir, text)
    for alarm in alarms:
        journal.append("canary.alarm", {
            "canary_id": alarm["canary_id"],
            "token_hash": alarm["token_hash"],
            "channel": channel,
        })
    return alarms


class CanaryGuard:
    """Convenience wrapper binding a store_dir."""

    def __init__(self, store_dir: str):
        self.store_dir = store_dir

    def plant(self, count: int = 3) -> list[dict]:
        return plant(self.store_dir, count)

    def scan_text(self, text: str) -> list[dict]:
        return scan_text(self.store_dir, text)

    def scan_and_log(self, journal, channel: str, text: str) -> list[dict]:
        return scan_and_log(self.store_dir, journal, channel, text)
