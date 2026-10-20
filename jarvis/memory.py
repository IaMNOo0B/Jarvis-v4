"""Memory — JARVIS v4's long-term store (spine S5).

Two kinds of remembering, two honest stores:

1. FACTS — things JARVIS may act on ("mera naam Owais hai"). They live
   in :class:`jarvis.memory_trust.FactStore`: every fact carries
   provenance and the trust of the CHANNEL it arrived on, corrections
   supersede instead of editing, and the poisoning guard refuses
   content that presents itself as a higher authority than its channel.
   Facts are recalled ranked by token overlap with the query.

2. HISTORY — the conversation itself, kept in SQLite. Search uses an
   FTS5 index when the SQLite build has one; otherwise it falls back
   to BM25 (jarvis.rag) over the same rows — same results contract,
   ``{role, text, score}`` best-first, duplicates removed
   (consolidation-lite: the same turn stored twice is one memory).

Every learn is journaled (``memory.learn``) — a fact JARVIS acts on
must be traceable in the audit record, never only in the model's
self-report.

Stdlib only.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import List, Optional

from . import rag
from .journal import Journal
from .memory_trust import TRUST_LEVELS, FactStore

FACTS_FILE = "facts.jsonl"
HISTORY_DB = "history.sqlite3"
JOURNAL_FILE = "journal.jsonl"


def _overlap_score(query_tokens: set, claim_tokens: set) -> float:
    """Token-overlap rank: fraction of the claim's tokens the query
    touches, with a bonus for covering the query itself. Deterministic;
    ties fall back to recency at the call site."""
    if not query_tokens or not claim_tokens:
        return 0.0
    hit = len(query_tokens & claim_tokens)
    if hit == 0:
        return 0.0
    return hit / len(claim_tokens) + 0.5 * (hit / len(query_tokens))


class Memory:
    """Facts + conversation history rooted at one data dir."""

    def __init__(self, data_dir, journal: Optional[Journal] = None,
                 use_fts: Optional[bool] = None):
        self.data_dir = str(data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.facts = FactStore(os.path.join(self.data_dir, FACTS_FILE))
        # The journal is the app's audit spine: when embedded in
        # ChatApp, the app's ONE Journal instance is passed in so seq
        # numbers stay single-writer. Standalone use gets its own.
        self.journal = journal or Journal(
            os.path.join(self.data_dir, JOURNAL_FILE))
        self._lock = threading.Lock()
        self._db = sqlite3.connect(
            os.path.join(self.data_dir, HISTORY_DB),
            check_same_thread=False)
        self._fts = self._init_history(use_fts)

    # -- history store -------------------------------------------------

    def _init_history(self, use_fts: Optional[bool]) -> bool:
        with self._lock:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS turns "
                "(id INTEGER PRIMARY KEY, role TEXT, text TEXT)")
            self._db.commit()
            if use_fts is False:
                return False
            try:
                self._db.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts "
                    "USING fts5(role, text, content='turns', "
                    "content_rowid='id')")
                self._db.commit()
                return True
            except sqlite3.OperationalError:
                return False

    @property
    def fts_active(self) -> bool:
        return self._fts

    # -- facts -----------------------------------------------------------

    def learn(self, text: str, source: str = "owner") -> dict:
        """Store a fact. ``source`` names the channel it arrived on
        (owner | tool | web | inference); the channel sets the trust —
        content can never raise its own. Journaled, always."""
        trust = source if source in TRUST_LEVELS else "inference"
        record = self.facts.add(claim=str(text), source=str(source),
                                source_trust=trust, presented_as=trust)
        self.journal.append("memory.learn", {
            "fact_id": record["id"],
            "source": record["source"],
            "source_trust": record["source_trust"],
            "claim": str(text),
        })
        return record

    def remember(self, text: str) -> dict:
        """The generic note-taking surface (builtin memory ops and the
        brain's compaction summaries write through here). These writes
        are MODEL-MEDIATED, so they enter at inference trust — the
        high-trust owner path is learn() called directly by the app
        on an owner command."""
        record = self.learn(str(text), source="inference")
        return {"stored": str(text), "fact_id": record["id"]}

    def excerpts(self, query: str, k: int = 5) -> List[str]:
        """Fact texts relevant to a query, as plain strings — the
        shape the brain's prompt builder consumes."""
        return [hit["text"] for hit in self.recall(query, k=k)]

    def recall(self, query: str, k: int = 5) -> List[dict]:
        """Current (non-superseded) facts ranked by token overlap with
        the query. Only facts sharing at least one token are returned —
        an empty list honestly means "nothing stored about that"."""
        q_tokens = set(rag.tokenize(str(query)))
        scored = []
        for idx, rec in enumerate(self.facts.current_facts()):
            score = _overlap_score(q_tokens,
                                   set(rag.tokenize(rec["claim"])))
            if score > 0.0:
                scored.append((score, idx, rec))
        scored.sort(key=lambda item: (-item[0], -item[1]))
        return [{
            "text": rec["claim"],
            "claim": rec["claim"],
            "source": rec["source"],
            "source_trust": rec["source_trust"],
            "score": round(score, 4),
            "id": rec["id"],
        } for score, _idx, rec in scored[:k]]

    # -- conversation history ---------------------------------------------

    def note_turn(self, role: str, text: str) -> None:
        """Append one conversation turn (user or assistant) to history."""
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO turns (role, text) VALUES (?, ?)",
                (str(role), str(text)))
            if self._fts:
                self._db.execute(
                    "INSERT INTO turns_fts (rowid, role, text) "
                    "VALUES (?, ?, ?)",
                    (cur.lastrowid, str(role), str(text)))
            self._db.commit()

    def search_history(self, query: str, k: int = 5) -> List[dict]:
        """Search past turns. FTS5 when available, BM25 otherwise.
        Results are deduplicated (same role+text kept once, best
        score) — consolidation-lite."""
        rows = self._all_turns()
        if not rows:
            return []
        if self._fts:
            hits = self._search_fts(query, rows)
        else:
            hits = self._search_bm25(query, rows)
        seen = set()
        out = []
        for hit in hits:
            key = (hit["role"], hit["text"])
            if key in seen:
                continue
            seen.add(key)
            out.append(hit)
            if len(out) >= k:
                break
        return out

    def _all_turns(self) -> list:
        with self._lock:
            cur = self._db.execute(
                "SELECT id, role, text FROM turns ORDER BY id")
            return [{"id": r[0], "role": r[1], "text": r[2]}
                    for r in cur.fetchall()]

    def _search_fts(self, query: str, rows: list) -> List[dict]:
        tokens = rag.tokenize(str(query))
        if not tokens:
            return []
        # OR of quoted tokens: any shared word matches; bm25() ranks.
        match = " OR ".join(f'"{t}"' for t in tokens)
        try:
            with self._lock:
                cur = self._db.execute(
                    "SELECT rowid, bm25(turns_fts) FROM turns_fts "
                    "WHERE turns_fts MATCH ? ORDER BY bm25(turns_fts) "
                    "LIMIT ?", (match, max(len(rows), 1)))
                ranked = cur.fetchall()
        except sqlite3.OperationalError:
            return self._search_bm25(query, rows)
        by_id = {r["id"]: r for r in rows}
        out = []
        for rowid, score in ranked:
            row = by_id.get(rowid)
            if row is not None:
                out.append({"role": row["role"], "text": row["text"],
                            "score": round(-float(score), 4)})
        return out

    def _search_bm25(self, query: str, rows: list) -> List[dict]:
        index = rag.BM25Index([r["text"] for r in rows])
        return [{"role": rows[i]["role"], "text": rows[i]["text"],
                 "score": round(score, 4)}
                for i, score in index.search(str(query),
                                             top_k=len(rows))]

    def close(self) -> None:
        with self._lock:
            self._db.close()
