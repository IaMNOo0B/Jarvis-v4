"""Knowledge graph — JARVIS v4, Wave 4 (Thoth/row-bot's pattern).

Facts are sentences; a graph is what lets JARVIS walk from one
sentence to its neighbours: "workshop" -> fee -> 500 rupees ->
Aisha. Extraction is deterministic pattern work over the claim
text (no model call, nothing invented): a small set of copula and
relation patterns, each producing (subject, predicate, object)
edges tied to the fact they came from.

The graph never replaces facts — it INDEXES them. Edges point at
fact ids; a lookup returns the edges plus the current fact claims
behind them, so a superseded fact's edges stop surfacing the
moment the fact itself is superseded.

Store: data/graph.jsonl, one row per edge, idempotent per fact.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional

from . import rag

_MAX_EDGES_PER_FACT = 3

# (pattern, predicate) — conservative on purpose: a missed edge is
# a shrug, a wrong edge is a lie the graph will repeat.
_PATTERNS = [
    (re.compile(r"^(.{2,60}?)\s+is\s+(?:a\s+|an\s+|the\s+)?(.{1,80})$",
                re.I), "is"),
    (re.compile(r"^(.{2,60}?)\s+lives?\s+in\s+(.{1,60})$", re.I),
     "lives in"),
    (re.compile(r"^(.{2,60}?)\s+works?\s+(?:at|for)\s+(.{1,60})$",
                re.I), "works at"),
    (re.compile(r"^(.{2,60}?)\s+(?:likes|loves|prefers)\s+(.{1,60})$",
                re.I), "likes"),
    (re.compile(r"^(.{2,60}?)\s+owns\s+(.{1,60})$", re.I), "owns"),
    (re.compile(r"^(.{2,60}?)\s+uses\s+(.{1,60})$", re.I), "uses"),
    (re.compile(r"^(.{2,60}?)\s+wants\s+(.{1,60})$", re.I), "wants"),
]

_STOP_SUBJECTS = {"it", "this", "that", "there", "he", "she", "they"}

# A subject containing a verb phrase is a CLAUSE, not an
# entity: "Aisha is a teacher who lives in Pune" must not
# mint the subject "Aisha is a teacher who" (audit 2026-10-22).
_SUBJECT_CLAUSE = re.compile(
    r"\s+(?:is|are|was|were|has|have|who|which)\s+", re.I)


def extract_edges(claim: str) -> list:
    """(subject, predicate, object) triples a claim plainly
    states — at most a few, each one literally in the text."""
    text = re.sub(r"\s+", " ", str(claim)).strip().rstrip(".")
    edges = []
    for pattern, predicate in _PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        subject = match.group(1).strip(" ,")
        obj = match.group(2).strip(" ,")
        if subject.lower() in _STOP_SUBJECTS or not obj:
            continue
        if _SUBJECT_CLAUSE.search(subject):
            continue    # a clause wearing a subject's clothes
        if len(subject.split()) > 6 or len(obj.split()) > 10:
            continue
        edges.append((subject, predicate, obj))
        if len(edges) >= _MAX_EDGES_PER_FACT:
            break
    return edges


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).lower()).strip()


class KnowledgeGraph:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "graph.jsonl"
        self._edges: List[dict] = []
        self._seen_facts: set = set()
        self._edge_keys: set = set()
        self.skipped_lines = 0
        if self.path.exists():
            # Tolerant PER LINE (audit 2026-10-22: one torn
            # line zeroed the whole graph, and the rebuild
            # then re-added every fact — duplicating edges).
            try:
                lines = self.path.read_text(
                    encoding="utf-8").splitlines()
            except Exception:
                lines = []
            for line in lines:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("row not a dict")
                except Exception:
                    self.skipped_lines += 1
                    continue
                key = (row.get("fact_id"), row.get("subject"),
                       row.get("predicate"), row.get("object"))
                if key in self._edge_keys:
                    continue
                self._edge_keys.add(key)
                self._edges.append(row)
                self._seen_facts.add(row.get("fact_id"))

    def __len__(self) -> int:
        return len(self._edges)

    def add_fact(self, fact: dict) -> int:
        """Index one fact's edges (idempotent per fact id)."""
        fact_id = fact.get("id")
        if not fact_id or fact_id in self._seen_facts:
            return 0
        self._seen_facts.add(fact_id)
        added = 0
        for subject, predicate, obj in extract_edges(
                fact.get("claim", "")):
            key = (fact_id, subject, predicate, obj)
            if key in self._edge_keys:
                continue
            self._edge_keys.add(key)
            row = {"fact_id": fact_id, "subject": subject,
                   "predicate": predicate, "object": obj,
                   "ts": fact.get("ts")}
            self._edges.append(row)
            try:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False)
                             + "\n")
            except Exception:
                pass
            added += 1
        return added

    def build_from_facts(self, facts) -> int:
        added = 0
        for fact in facts:
            added += self.add_fact(fact)
        return added

    def edges(self) -> List[dict]:
        return list(self._edges)

    def about(self, entity: str, current_fact_ids: Optional[set]
              = None) -> dict:
        """One-hop neighbourhood of an entity: edges where it is
        the subject, the object, or plainly named — optionally
        restricted to edges whose facts are still current."""
        needle = _norm(entity)
        needle_tokens = set(rag.tokenize(needle))
        hits = []
        for edge in self._edges:
            if current_fact_ids is not None and \
                    edge.get("fact_id") not in current_fact_ids:
                continue
            # Whole-TOKEN matching (audit 2026-10-22: the raw
            # substring test matched 'fee' inside 'coffee').
            subj_tokens = set(rag.tokenize(_norm(edge["subject"])))
            obj_tokens = set(rag.tokenize(_norm(edge["object"])))
            if needle_tokens and (needle_tokens <= subj_tokens
                                  or needle_tokens <= obj_tokens):
                hits.append(edge)
        neighbours = set()
        for edge in hits:
            neighbours.add(edge["subject"])
            neighbours.add(edge["object"])
        # The entity itself leaves the neighbour list by
        # NORMALIZED comparison — 'Aisha' the query vs 'aisha'
        # the stored spelling used to survive the discard.
        neighbours = {n for n in neighbours
                      if _norm(n) != needle}
        return {"entity": entity, "edges": hits,
                "neighbours": sorted(neighbours)}
