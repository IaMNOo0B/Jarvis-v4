"""RAG upgrade core — Step 11 (LLM Engineering).

The four fixes basic RAG needs, as dependency-free building blocks
(ai-engineering-from-scratch, Phase 11 · Lesson 07 — Advanced RAG):

1. **Hybrid retrieval + Reciprocal Rank Fusion.** BM25 catches exact
   tokens embeddings miss; RRF merges ranked lists of any origin with
   ``score(d) = sum over lists of 1 / (k + rank)``, ``k = 60``. Rank-based,
   so score-scale differences between retrievers vanish. (Worked lesson
   example: a doc ranked (3, 2) beats a doc ranked (1, 5) — consistent
   placement beats one-list brilliance.)
2. **Parent-child chunking.** Index small children (~128 tokens) for
   match precision; return the whole parent (~512 tokens) for context.
3. **BM25 from scratch** (k1 = 1.5, b = 0.75, Robertson IDF) so the
   lexical half of the hybrid runs with zero embedding dependency.
4. **Query transformation.** ``hyde_prompt`` builds the HyDE prompt —
   generate a hypothetical *answer* passage (in English) for the query,
   then retrieve against that; it bridges Hinglish questions to English
   documents. ``interleave_reorder`` applies the lost-in-the-middle fix:
   the best chunks sit at both ends of the context, the weakest in the
   middle, where models attend least.

Tokenisation here is whitespace words (lowercased, edge punctuation
stripped) — deterministic and good enough for chunk sizing and BM25;
swap in a real tokenizer at the call site if exact counts matter.

Stdlib only.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

_WORD_RE = re.compile(r"[^\s]+")
_EDGE_PUNCT = ".,;:!?\"'()[]{}<>«»“”‘’—–-"


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens with edge punctuation stripped."""
    tokens = []
    for raw in _WORD_RE.findall(text.lower()):
        tok = raw.strip(_EDGE_PUNCT)
        if tok:
            tokens.append(tok)
    return tokens


# --------------------------------------------------------------------------
# Reciprocal Rank Fusion
# --------------------------------------------------------------------------

def rrf_scores(rankings: list[list], k: int = 60) -> dict:
    """Fused RRF score per item: ``sum(1 / (k + rank))``, rank 1-based."""
    scores: dict = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return scores


def rrf_fuse(rankings: list[list], k: int = 60) -> list:
    """Merge ranked lists into one ranking by reciprocal rank fusion.

    Ties break by first appearance (earlier list, then earlier rank), so
    the result is fully deterministic.
    """
    scores = rrf_scores(rankings, k=k)
    first_seen: dict = {}
    for li, ranking in enumerate(rankings):
        for rank, item in enumerate(ranking):
            first_seen.setdefault(item, (li, rank))
    return sorted(scores, key=lambda item: (-scores[item], first_seen[item]))


# --------------------------------------------------------------------------
# Parent-child chunking
# --------------------------------------------------------------------------

@dataclass
class Child:
    id: str
    parent_id: str
    text: str
    start_word: int  # word offsets within the parent
    end_word: int


@dataclass
class Parent:
    id: str
    text: str
    children: list[Child] = field(default_factory=list)


def chunk_parent_child(text: str,
                       child_tokens: int = 128,
                       parent_tokens: int = 512) -> list[Parent]:
    """Split ``text`` into parents of <= ``parent_tokens`` words, each
    tiled exactly by children of <= ``child_tokens`` words.

    Children never cross a parent boundary and together cover the parent
    with no gaps or overlaps, so any child hit maps back to exactly one
    parent.
    """
    words = text.split()
    parents: list[Parent] = []
    for p_start in range(0, len(words), parent_tokens):
        p_words = words[p_start:p_start + parent_tokens]
        parent = Parent(id=f"p{len(parents)}", text=" ".join(p_words))
        for c_start in range(0, len(p_words), child_tokens):
            c_words = p_words[c_start:c_start + child_tokens]
            parent.children.append(Child(
                id=f"{parent.id}c{len(parent.children)}",
                parent_id=parent.id,
                text=" ".join(c_words),
                start_word=c_start,
                end_word=c_start + len(c_words),
            ))
        parents.append(parent)
    return parents


# --------------------------------------------------------------------------
# BM25 (lexical retriever)
# --------------------------------------------------------------------------

class BM25Index:
    """Okapi BM25 over a fixed document list.

    ``score(q, d) = sum over query terms of
        idf(t) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avgdl))``
    with Robertson IDF ``ln(1 + (N - df + 0.5) / (df + 0.5))`` — always
    non-negative, so rare terms help and never hurt.
    """

    def __init__(self, documents: list[str],
                 k1: float = 1.5, b: float = 0.75):
        self.k1 = float(k1)
        self.b = float(b)
        self.doc_tokens = [tokenize(d) for d in documents]
        self.n_docs = len(documents)
        self.doc_len = [len(t) for t in self.doc_tokens]
        self.avgdl = (sum(self.doc_len) / self.n_docs) if self.n_docs else 0.0
        self.tf: list[dict[str, int]] = []
        df: dict[str, int] = {}
        for toks in self.doc_tokens:
            counts: dict[str, int] = {}
            for tok in toks:
                counts[tok] = counts.get(tok, 0) + 1
            self.tf.append(counts)
            for tok in counts:
                df[tok] = df.get(tok, 0) + 1
        self.idf = {
            term: math.log(1.0 + (self.n_docs - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }

    def search(self, query: str, top_k: int = 10) -> list[tuple[int, float]]:
        """Ranked ``(doc_index, score)`` pairs, best first, score > 0."""
        if self.n_docs == 0 or self.avgdl == 0.0:
            return []
        terms = set(tokenize(query))
        scored = []
        for idx in range(self.n_docs):
            dl = self.doc_len[idx]
            norm = self.k1 * (1.0 - self.b + self.b * dl / self.avgdl)
            score = 0.0
            for term in terms:
                tf = self.tf[idx].get(term)
                if tf:
                    score += self.idf[term] * tf * (self.k1 + 1.0) / (tf + norm)
            if score > 0.0:
                scored.append((idx, score))
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored[:top_k]


# --------------------------------------------------------------------------
# Child index: BM25 over children, retrieval returns parents
# --------------------------------------------------------------------------

class ChildIndex:
    """All children of all parents, BM25-indexed, with parent lookup."""

    def __init__(self, parents: list[Parent]):
        self.parents = list(parents)
        self.parent_by_id = {p.id: p for p in self.parents}
        self.children: list[Child] = [c for p in self.parents
                                      for c in p.children]
        self.child_by_id = {c.id: c for c in self.children}
        self._child_pos = {c.id: i for i, c in enumerate(self.children)}
        self.bm25 = BM25Index([c.text for c in self.children])

    def search_children(self, query: str, top_k: int = 20) -> list[str]:
        """Child ids ranked by BM25 against the query."""
        return [self.children[idx].id
                for idx, _ in self.bm25.search(query, top_k=top_k)]

    def retrieve_parents(self, ranked_child_ids: list[str]) -> list[Parent]:
        """Map ranked child ids to their parents — deduplicated, in the
        order each parent's best child ranked."""
        return retrieve_parents(self, ranked_child_ids)

    def search_parents(self, query: str,
                       top_k_children: int = 20,
                       extra_rankings: list[list[str]] | None = None,
                       top_parents: int | None = None) -> list[Parent]:
        """End-to-end hybrid path: BM25 child ranking (fused via RRF with
        any extra rankings supplied, e.g. a dense retriever's) ->
        deduplicated parents."""
        rankings = [self.search_children(query, top_k=top_k_children)]
        if extra_rankings:
            rankings.extend(extra_rankings)
        fused = rrf_fuse(rankings) if len(rankings) > 1 else rankings[0]
        parents = self.retrieve_parents(fused)
        return parents[:top_parents] if top_parents else parents


def index_children(parents: list[Parent]) -> ChildIndex:
    """Build a :class:`ChildIndex` over chunked parents."""
    return ChildIndex(parents)


def retrieve_parents(index: ChildIndex,
                     ranked_child_ids: list[str]) -> list[Parent]:
    """Deduplicated parents in rank order for ranked child ids."""
    seen: set[str] = set()
    ordered: list[Parent] = []
    for child_id in ranked_child_ids:
        child = index.child_by_id[child_id]
        if child.parent_id not in seen:
            seen.add(child.parent_id)
            ordered.append(index.parent_by_id[child.parent_id])
    return ordered


# --------------------------------------------------------------------------
# Query transformation
# --------------------------------------------------------------------------

def hyde_prompt(query: str) -> str:
    """The HyDE prompt: ask the LLM for a hypothetical answer passage.

    The passage — not the question — is what gets embedded/searched, which
    bridges question-space to document-space. The instruction to answer
    in English is deliberate: JARVIS's knowledge library is English while
    owner queries arrive in Hinglish, so the hypothetical document is
    generated in the library's language.
    """
    return (
        "Write a short, factual passage in English that directly answers "
        "the following question, as if it were an excerpt from a reference "
        "document on the topic. Do not mention the question; write only "
        "the passage. If the question is in Hinglish (Roman Hindi mixed "
        "with English), first understand its intent, then still write the "
        "passage in English.\n\n"
        f"Question: {query}\n\n"
        "Passage:"
    )


def interleave_reorder(ranked: list) -> list:
    """Lost-in-the-middle placement for a best-first ranked list.

    Even-ranked items (1st, 3rd, 5th...) keep their order at the front;
    odd-ranked items (2nd, 4th...) are appended in reverse. The best
    chunk lands first, the second-best last — the two positions models
    attend to most — and the weakest chunk sits in the middle.
    """
    evens = ranked[0::2]
    odds = ranked[1::2]
    return list(evens) + list(reversed(odds))
