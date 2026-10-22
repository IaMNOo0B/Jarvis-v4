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


def _copy_record(rec: dict) -> dict:
    """A detached copy — the store's records are append-only
    and never edited; handing out the live dicts let any
    caller rewrite history in-process (audit 2026-10-22)."""
    out = dict(rec)
    if isinstance(out.get("supersedes"), list):
        out["supersedes"] = list(out["supersedes"])
    return out


class FactStore:
    """Append-only fact store with provenance and supersession."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._records: list[dict] = []
        self.skipped_lines = 0
        self._stamp = None
        if os.path.exists(path):
            self._load()

    def _load(self) -> None:
        """Tolerant reader (audit 2026-10-22): one torn or
        foreign line must not brick the whole store — an
        append-only file's realistic failure is a half-written
        tail, and bricking Memory bricks app startup. Bad
        lines are skipped and counted; records whose claimed
        authority exceeds their channel (a hand-written
        poisoning attempt) are kept for audit but quarantined
        out of current_facts."""
        records = []
        skipped = 0
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    skipped += 1
                    continue
                if not isinstance(rec, dict) or not rec.get("id"):
                    skipped += 1
                    continue
                st = rec.get("source_trust")
                pa = rec.get("presented_as", st)
                if st not in TRUST_LEVELS or pa not in TRUST_LEVELS:
                    skipped += 1
                    continue
                if TRUST_LEVELS[pa] > TRUST_LEVELS[st]:
                    rec["_quarantined"] = True
                records.append(rec)
        self._records = records
        self.skipped_lines = skipped
        self._restamp()

    def _restamp(self) -> None:
        try:
            st = os.stat(self.path)
            self._stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            self._stamp = None

    def _ensure_fresh(self) -> None:
        """Another FactStore on the same file (the app builds
        several Memory objects) must not serve a frozen view:
        if the file changed since our load, reload it."""
        try:
            st = os.stat(self.path)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            return
        if self._stamp is not None and stamp != self._stamp:
            with self._lock:
                self._load()

    # -- writing ----------------------------------------------------------

    def add(self,
            claim: str,
            source: str,
            source_trust: str,
            presented_as: Optional[str] = None,
            supersedes: Optional[str] = None,
            evidence: Optional[str] = None) -> dict:
        """Append one fact. Runs the poisoning guard first.

        `supersedes` names the id of the record this one corrects; the
        old record stays in history untouched. `evidence` is the
        verbatim owner quote the claim rests on (Dodaios's evidence
        rule) — stored with the record so any fact can be traced
        back to the words that support it. Returns the record.
        """
        record = {
            "id": uuid.uuid4().hex,
            "claim": claim,
            "source": source,
            "source_trust": source_trust,
            "presented_as": presented_as or source_trust,
            "ts": datetime.now(timezone.utc).isoformat(),
            "supersedes": supersedes,
            "evidence": evidence,
        }
        assert_can_become_fact(record)
        with self._lock:
            if supersedes is not None:
                targets = supersedes if isinstance(supersedes, list) \
                    else [supersedes]
                resolved = []
                for target in targets:
                    # Superseding an already-dead record forks
                    # the chain (two 'current' truths). Redirect
                    # to the live head that replaced it.
                    head = self._live_head(target)
                    if head is None:
                        raise MemoryTrustError(
                            f"cannot supersede unknown fact id "
                            f"{target!r}")
                    if head not in resolved:
                        resolved.append(head)
                record["supersedes"] = (
                    resolved if isinstance(supersedes, list)
                    or len(resolved) != 1 else resolved[0])
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._records.append(record)
            self._restamp()
            return _copy_record(record)

    def _live_head(self, fact_id: str) -> Optional[str]:
        """The id of the CURRENT record at the end of
        fact_id's supersession chain (fact_id itself when it
        is still current), or None when unknown."""
        known = {r["id"] for r in self._records}
        if fact_id not in known:
            return None
        current = fact_id
        seen = set()
        while current not in seen:
            seen.add(current)
            nxt = None
            for rec in self._records:
                sup = rec.get("supersedes")
                if sup == current or (isinstance(sup, list)
                                      and current in sup):
                    nxt = rec["id"]
                    break
            if nxt is None:
                return current
            current = nxt
        return current

    # -- reading ------------------------------------------------------------

    def get(self, fact_id: str) -> Optional[dict]:
        self._ensure_fresh()
        for rec in self._records:
            if rec["id"] == fact_id:
                return _copy_record(rec)
        return None

    def history(self) -> list[dict]:
        """Every record ever written, in write order — superseded ones
        included. History is never rewritten."""
        self._ensure_fresh()
        return [_copy_record(r) for r in self._records]

    def current_facts(self) -> list[dict]:
        """Only non-superseded records: the facts JARVIS may act on now."""
        self._ensure_fresh()
        superseded = set()
        for rec in self._records:
            sup = rec.get("supersedes")
            if isinstance(sup, list):
                superseded.update(sup)
            elif sup:
                superseded.add(sup)
        return [_copy_record(r) for r in self._records
                if r["id"] not in superseded
                and not r.get("_quarantined")]

    def superseded_by(self, fact_id: str) -> Optional[dict]:
        """The LIVE record that ultimately replaced
        `fact_id` — the chain is followed to its head, so a
        correction flow never quotes a dead replacement as
        the current truth (audit 2026-10-22)."""
        self._ensure_fresh()
        head = self._live_head(fact_id)
        if head is None or head == fact_id:
            return None
        return self.get(head)

    def __len__(self) -> int:
        return len(self._records)
