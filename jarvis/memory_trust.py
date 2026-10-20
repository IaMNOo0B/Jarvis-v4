"""Provenance + append-only supersession — memory JARVIS can trust (Step 16).

Design (from the ai-eng study, P16-L13 shared memory & blackboard):
Memory poisoning needs no crash and no failed test: one hallucinated
"fact" gets written, then cited, then acted on — gradual decay. The three
mitigations that work, implemented here:

1. PROVENANCE ON EVERY WRITE — every record carries who/what produced it
   and the trust of the CHANNEL it arrived on (owner | tool | web |
   inference). The channel sets the trust; content cannot raise its own.
2. APPEND-ONLY + SUPERSESSION — a correction is a NEW record pointing at
   the old one's id. Old records are never edited or deleted, so history
   (and the poisoning attempt itself) stays auditable forever.
3. A POISONING GUARD at the door — a record whose content PRESENTS itself
   as a higher authority than its channel (web page claiming "the owner
   said…") is refused entry as a fact.

Stdlib only. Storage is one append-only JSONL file, same discipline as
jarvis.journal.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

# Authority ladder: a record may present as its own level or lower,
# never higher. The channel (source_trust) is assigned by the system
# that received the content — it is not the content's to choose.
TRUST_LEVELS = {"web": 0, "inference": 1, "tool": 2, "owner": 3}
SOURCE_TRUSTS = tuple(TRUST_LEVELS)


class MemoryTrustError(Exception):
    """Raised when a record is refused by the poisoning guard or the
    store is structurally unreadable."""


def _check_trust(value: str, field: str) -> None:
    if value not in TRUST_LEVELS:
        raise MemoryTrustError(
            f"{field} must be one of {SOURCE_TRUSTS}, got {value!r}")


def assert_can_become_fact(record: dict) -> None:
    """The poisoning guard. Raises MemoryTrustError if `record` may not
    be stored as a fact.

    A record is refused when its `presented_as` (the authority the
    content claims — e.g. text asserting it speaks for the owner) is
    HIGHER than its `source_trust` (the authority of the channel it
    actually arrived on). Web/tool/inference content claiming to be an
    owner fact is the canonical poisoning move, and it stops here.
    """
    source_trust = record.get("source_trust")
    presented_as = record.get("presented_as", source_trust)
    _check_trust(source_trust, "source_trust")
    _check_trust(presented_as, "presented_as")
    if TRUST_LEVELS[presented_as] > TRUST_LEVELS[source_trust]:
        raise MemoryTrustError(
            f"poisoning guard: content from a {source_trust!r} channel "
            f"may not present as {presented_as!r} fact "
            f"(claim: {record.get('claim', '')[:80]!r})")
    if not str(record.get("claim", "")).strip():
        raise MemoryTrustError("a fact needs a non-empty claim")
    if not str(record.get("source", "")).strip():
        raise MemoryTrustError("a fact needs a named source (provenance)")


class FactStore:
    """Append-only fact store with provenance and supersession."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._records: list[dict] = []
        if os.path.exists(path):
            self._load()

    def _load(self) -> None:
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._records.append(json.loads(line))
                except ValueError as exc:
                    raise MemoryTrustError(
                        f"fact store unreadable in {self.path}: {exc}"
                    ) from exc

    # -- writing ----------------------------------------------------------

    def add(self,
            claim: str,
            source: str,
            source_trust: str,
            presented_as: Optional[str] = None,
            supersedes: Optional[str] = None) -> dict:
        """Append one fact. Runs the poisoning guard first.

        `supersedes` names the id of the record this one corrects; the
        old record stays in history untouched. Returns the stored record.
        """
        record = {
            "id": uuid.uuid4().hex,
            "claim": claim,
            "source": source,
            "source_trust": source_trust,
            "presented_as": presented_as or source_trust,
            "ts": datetime.now(timezone.utc).isoformat(),
            "supersedes": supersedes,
        }
        assert_can_become_fact(record)
        with self._lock:
            if supersedes is not None and self.get(supersedes) is None:
                raise MemoryTrustError(
                    f"cannot supersede unknown fact id {supersedes!r}")
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._records.append(record)
            return record

    # -- reading ------------------------------------------------------------

    def get(self, fact_id: str) -> Optional[dict]:
        for rec in self._records:
            if rec["id"] == fact_id:
                return rec
        return None

    def history(self) -> list[dict]:
        """Every record ever written, in write order — superseded ones
        included. History is never rewritten."""
        return list(self._records)

    def current_facts(self) -> list[dict]:
        """Only non-superseded records: the facts JARVIS may act on now."""
        superseded = {r["supersedes"] for r in self._records
                      if r.get("supersedes")}
        return [r for r in self._records if r["id"] not in superseded]

    def superseded_by(self, fact_id: str) -> Optional[dict]:
        """The record that replaced `fact_id`, if any."""
        for rec in self._records:
            if rec.get("supersedes") == fact_id:
                return rec
        return None

    def __len__(self) -> int:
        return len(self._records)
