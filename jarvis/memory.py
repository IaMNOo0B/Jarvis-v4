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
import re
import sqlite3
import threading
from typing import List, Optional

from . import rag
from .journal import Journal
from .memory_trust import TRUST_LEVELS, FactStore

FACTS_FILE = "facts.jsonl"
HISTORY_DB = "history.sqlite3"
JOURNAL_FILE = "journal.jsonl"


_URL_IN_TEXT_RE = re.compile(r"(https?://\S+|www\.\S+)", re.I)
# Real number tokens: grouped thousands or plain digits, with
# an optional decimal part. A sentence-final period is NOT
# part of the number (the old \d[\d,.]* swallowed it, so
# "500." never matched the evidence's "500" and honest
# corrections were blocked — audit 2026-10-22).
_NUMBER_IN_TEXT_RE = re.compile(
    r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


def _number_key(token: str) -> str:
    """Canonical numeric value of a token: '1,000', '1000' and
    '1000.00' are the same figure; '50' and '500' are NOT
    (the old raw-substring check passed '50' inside '500' —
    the exact figure-swap the rule exists to stop)."""
    from decimal import Decimal, InvalidOperation
    try:
        return format(
            Decimal(str(token).replace(",", "")).normalize(),
            "f")
    except (InvalidOperation, ValueError):
        return str(token)


def _numbers_in(text: str) -> set:
    return {_number_key(n)
            for n in _NUMBER_IN_TEXT_RE.findall(str(text))}


def evidence_problems(claim: str, evidence: Optional[str]) -> list:
    """Dodaios's evidence rule as a check: the supporting quote
    must be a real quote (>= 12 chars), and every URL or number the
    claim carries must appear in it — a distilled fact may not
    invent a link or a figure the owner never said. Numbers
    compare by VALUE, not by substring. No evidence
    supplied -> no problems (the rule binds only provenanced
    writes)."""
    if evidence is None:
        return []
    problems = []
    if len(str(evidence).strip()) < 12:
        problems.append("evidence quote shorter than 12 characters")
    ev = str(evidence)
    for url in _URL_IN_TEXT_RE.findall(str(claim)):
        if url.rstrip(").,") not in ev:
            problems.append(
                f"claim cites a link the evidence never mentions: "
                f"{url}")
    ev_numbers = _numbers_in(ev)
    for num in _NUMBER_IN_TEXT_RE.findall(str(claim)):
        if _number_key(num) not in ev_numbers:
            problems.append(
                f"claim cites a number the evidence never "
                f"mentions: {num}")
    return problems


_ATTRIBUTION_RE = re.compile(
    r"\b(owner|owais|malik|user)\s+(said|says|told|ne kaha|"
    r"ka kehna)\b|\byou told me\b|\bthe owner said\b", re.I)

_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "on", "in",
    "at", "of", "to", "for", "and", "or", "it", "its", "this",
    "that", "hai", "hain", "ka", "ki", "ke", "ko", "me", "mein",
    "par", "se", "ne", "bhi", "tha", "thi", "ek", "ye", "woh",
})


def _content_tokens(text: str) -> set:
    """Tokens that carry meaning — function words removed.
    Supersession decisions ride on this: overlap built from
    'the'/'is' alone once let a lesson about Sydney delete
    an unrelated meeting fact (audit 2026-10-22)."""
    return {t for t in rag.tokenize(str(text))
            if t not in _STOPWORDS and len(t) > 1}


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
        self._graph = None
        self.facts = FactStore(os.path.join(self.data_dir, FACTS_FILE))
        # The journal is the app's audit spine: when embedded in
        # ChatApp, the app's ONE Journal instance is passed in so seq
        # numbers stay single-writer. Standalone use gets its own.
        self.journal = journal or Journal(
            os.path.join(self.data_dir, JOURNAL_FILE))
        self._lock = threading.Lock()
        self._db = sqlite3.connect(
            os.path.join(self.data_dir, HISTORY_DB),
            check_same_thread=False, timeout=15.0)
        self._fts = self._init_history(use_fts)

    # -- history store -------------------------------------------------

    def _init_history(self, use_fts: Optional[bool]) -> bool:
        with self._lock:
            # A second connection holding the DB must not make
            # note_turn raise and lose the owner's turn
            # (audit 2026-10-22): wait for the lock, in WAL.
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.execute("PRAGMA busy_timeout=15000")
            except sqlite3.OperationalError:
                pass
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
                # Turns noted while FTS was off (or by an older
                # build) never entered the index and stayed
                # invisible to search forever: when the counts
                # disagree, rebuild the index from the turns
                # table once.
                turns_n = self._db.execute(
                    "SELECT COUNT(*) FROM turns").fetchone()[0]
                fts_n = self._db.execute(
                    "SELECT COUNT(*) FROM turns_fts").fetchone()[0]
                if turns_n != fts_n:
                    self._db.execute(
                        "INSERT INTO turns_fts(turns_fts) "
                        "VALUES('rebuild')")
                    self._db.commit()
                return True
            except sqlite3.OperationalError:
                return False

    @property
    def fts_active(self) -> bool:
        return self._fts

    # -- facts -----------------------------------------------------------

    def learn(self, text: str, source: str = "owner",
              evidence: Optional[str] = None) -> dict:
        """Store a fact. ``source`` names the channel it arrived on
        (owner | tool | web | inference); the channel sets the trust —
        content can never raise its own. `evidence` is the verbatim
        owner quote the claim rests on (Dodaios's evidence rule),
        stored with the record. Journaled, always."""
        trust = source if source in TRUST_LEVELS else "inference"
        # Dedupe: the identical claim already current is not
        # stored twice (audit 2026-10-22 — a duplicated wrong
        # fact survived its own correction because the lesson
        # superseded only one copy).
        norm = " ".join(str(text).lower().split())
        for rec in self.facts.current_facts():
            if " ".join(str(rec["claim"]).lower().split()) == norm:
                self.journal.append("memory.learn_duplicate", {
                    "fact_id": rec["id"], "claim": str(text)})
                return rec
        # The evidence rule binds here too, not only in
        # learn_lesson: invented numbers/links are journaled
        # where anyone can audit them.
        problems = evidence_problems(str(text), evidence)
        # A weak channel's text claiming the owner's authority
        # ("the owner said: ...") is the poisoning shape: it
        # is stored at its channel trust (never higher), the
        # claim is flagged on the record and in the journal,
        # and recall's trust weighting keeps it below real
        # owner facts.
        attribution = trust != "owner" and bool(
            _ATTRIBUTION_RE.search(str(text)))
        record = self.facts.add(claim=str(text), source=str(source),
                                source_trust=trust, presented_as=trust,
                                evidence=evidence)
        self.journal.append("memory.learn", {
            "fact_id": record["id"],
            "source": record["source"],
            "source_trust": record["source_trust"],
            "claim": str(text),
            "has_evidence": bool(evidence),
            "evidence_problems": problems,
            "attribution_claim": attribution,
        })
        if attribution:
            self.journal.append("memory.attribution_flagged", {
                "fact_id": record["id"], "source": str(source),
                "claim": str(text)[:120]})
        if problems:
            record = dict(record)
            record["evidence_problems"] = problems
        try:
            self.graph.add_fact(record)
        except Exception:
            pass            # the graph indexes facts; never blocks one
        return record

    @property
    def graph(self):
        """The knowledge graph over this memory's facts
        (jarvis/graph.py) — built lazily, indexed on every learn."""
        if self._graph is None:
            from .graph import KnowledgeGraph
            self._graph = KnowledgeGraph(self.data_dir)
        return self._graph

    def remember(self, text: str) -> dict:
        """The generic note-taking surface (builtin memory ops and the
        brain's compaction summaries write through here). These writes
        are MODEL-MEDIATED, so they enter at inference trust — the
        high-trust owner path is learn() called directly by the app
        on an owner command."""
        record = self.learn(str(text), source="inference")
        return {"stored": str(text), "fact_id": record["id"]}

    def learn_lesson(self, wrong: str, correct: str,
                     lesson: str,
                     evidence: Optional[str] = None) -> dict:
        """Store a correction as a lesson (Nova's shipped learning).

        One owner-trust fact in lesson framing — the framing is what
        the brain later reads, so the fact itself carries the
        "you got this wrong before" signal. When a current fact
        matches the wrong claim, the lesson SUPERSEDES it: the old
        fact leaves current_facts() but stays in history, untouched
        (the first production caller of FactStore's supersedes=).
        """
        claim = (f"Lesson: {str(lesson).strip()} "
                 f"(Earlier wrong: {str(wrong).strip()}. "
                 f"Correct: {str(correct).strip()}.)")
        # Supersede EVERY current fact the wrong claim matches
        # — not just the single best (a duplicated wrong fact
        # otherwise survives its own correction). Matching
        # runs on content tokens: stopword overlap ('the',
        # 'is') once let an unrelated fact be deleted.
        wrong_tokens = _content_tokens(str(wrong))
        if not wrong_tokens:
            wrong_tokens = set(rag.tokenize(str(wrong)))
        targets = []
        for rec in self.facts.current_facts():
            score = _overlap_score(
                wrong_tokens, _content_tokens(rec["claim"]))
            if score >= 0.5:
                targets.append(rec["id"])
        # Stored shape stays the historical one: a bare id for
        # the single-target case, a list only for a true
        # multi-supersede.
        supersedes = (targets[0] if len(targets) == 1
                      else targets or None)
        problems = evidence_problems(claim, evidence)
        if problems:
            # The evidence rule's teeth: a lesson whose numbers or
            # links the owner's own words never contained may be
            # stored, but it may NOT supersede an existing fact.
            supersedes = None
        record = self.facts.add(claim=claim, source="owner_correction",
                                source_trust="owner",
                                presented_as="owner",
                                supersedes=supersedes,
                                evidence=evidence)
        self.journal.append("memory.lesson", {
            "fact_id": record["id"], "wrong": str(wrong),
            "correct": str(correct), "lesson": str(lesson),
            "supersedes": supersedes,
            "evidence_problems": problems})
        return record

    def excerpts(self, query: str, k: int = 5) -> List[str]:
        """Fact texts relevant to a query, as plain strings — the
        shape the brain's prompt builder consumes."""
        return [hit["text"] for hit in self.recall(query, k=k)]

    def recall(self, query: str, k: int = 5) -> List[dict]:
        """Current (non-superseded) facts ranked by token overlap with
        the query. Only facts sharing at least one token are returned —
        an empty list honestly means "nothing stored about that"."""
        q_tokens = set(rag.tokenize(str(query)))
        # Trust weights the rank (audit 2026-10-22): a terse
        # web claim once outscored the owner's own full fact
        # and the fast path served it verbatim. Channel trust
        # is a multiplier, never a gate — weak sources still
        # recall, they just cannot outrank the owner on a
        # similar overlap.
        trust_weight = {"owner": 1.0, "tool": 0.85,
                        "inference": 0.8, "web": 0.6}
        scored = []
        for idx, rec in enumerate(self.facts.current_facts()):
            score = _overlap_score(q_tokens,
                                   set(rag.tokenize(rec["claim"])))
            score *= trust_weight.get(
                rec.get("source_trust"), 0.8)
            if score > 0.0:
                scored.append((score, idx, rec))
        scored.sort(key=lambda item: (-item[0], -item[1]))
        hits = [{
            "text": rec["claim"],
            "claim": rec["claim"],
            "source": rec["source"],
            "source_trust": rec["source_trust"],
            "score": round(score, 4),
            "id": rec["id"],
        } for score, _idx, rec in scored[:k]]
        # One-hop graph expansion (Thoth): facts tied to the query
        # by an edge join even when their own wording barely
        # overlaps it — "workshop" finds the contact fact through
        # the fee fact's neighbourhood.
        try:
            current = self.facts.current_facts()
            current_ids = {rec["id"] for rec in current}
            seen = {h["id"] for h in hits}
            extra = 0
            for edge in self.graph.about(
                    str(query), current_ids)["edges"]:
                # The caller's k is a budget, not a floor:
                # expansion fills free slots, never overflows
                # past k (audit 2026-10-22: k=1 returned 3).
                if extra >= 2 or len(hits) >= k:
                    break
                rec = self.facts.get(edge["fact_id"])
                if rec is None or rec["id"] in seen:
                    continue
                seen.add(rec["id"])
                hits.append({
                    "text": rec["claim"], "claim": rec["claim"],
                    "source": rec["source"],
                    "source_trust": rec["source_trust"],
                    "score": 0.0, "id": rec["id"], "via_graph": True})
                extra += 1
        except Exception:
            pass
        return hits

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
