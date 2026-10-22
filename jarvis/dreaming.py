"""Dreaming — nightly memory consolidation, JARVIS v4, Wave 3.

Sentient's dreaming discipline, adapted to v4's substrate (token
overlap instead of embeddings — stdlib only). The standing warning
from that study governs everything here: SIMILARITY ALONE NEVER
DECIDES. "Lives in Pune" and "lives in Mumbai" are near-identical
strings about different worlds.

One dream pass over the current facts:

1. Pure duplicates (same normalized claim) merge without any
   model call — the oldest survives in a fresh record that
   supersedes the copies.
2. Near-duplicates merge ONLY when token overlap is high AND
   the entity sets are identical — same capitalized names/places
   and same numbers. Any entity difference keeps the pair apart
   forever (recorded, never re-litigated).
3. Same-subject value changes (entity names equal, numbers
   differ) are contradictions: the NEWER fact wins, the older is
   superseded by a restatement, and the settled pair is recorded.
4. Everything else is SKIPPED — dreaming's default verb.

Merges and settlements are ordinary fact records (source
"dreaming"), so history stays append-only and every consolidation
is itself journaled and reversible by inspection. CUD framing:
merge/settle = UPDATE, everything unexamined = SKIP; dreaming
never DELETEs — supersession is the only retirement that exists.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Callable, Optional

from . import rag
from .memory import _overlap_score

_NUMBERS_RE = re.compile(r"\d[\d,.]*")
_CAPITALS_RE = re.compile(r"\b[A-Z][a-z]+\b")
_NEAR_OVERLAP = 0.6
_MAX_FACTS = 60
_MAX_MERGES_PER_RUN = 5


def _normalized(claim: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ",
                                      str(claim).lower())).strip()


def _entities(claim: str):
    numbers = {n.rstrip(",.") for n in _NUMBERS_RE.findall(claim)}
    capitals = set(_CAPITALS_RE.findall(claim))
    return numbers, capitals


def _pair_key(a: dict, b: dict) -> str:
    return "|".join(sorted([a["id"], b["id"]]))


def _load_log(data_dir) -> list:
    path = Path(data_dir) / "dreaming.jsonl"
    rows = []
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        except Exception:
            return []
    return rows


def _append_log(data_dir, row: dict) -> None:
    try:
        with (Path(data_dir) / "dreaming.jsonl").open(
                "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def dream(data_dir, facts_store, merge_text: Optional[Callable] = None,
          journal=None) -> dict:
    """Run one consolidation pass. `facts_store` is the FactStore;
    `merge_text(a, b) -> str` optionally writes the merged claim
    (a model call in the app) — without it, the longer claim wins
    verbatim. Returns counts; never raises."""
    report = {"merged": 0, "settled": 0, "kept_apart": 0,
              "examined": 0}
    try:
        current = list(facts_store.current_facts())[:_MAX_FACTS]
        known = {row.get("pair") for row in _load_log(data_dir)}
        consumed = set()

        def _log(kind, a, b, note=""):
            _append_log(data_dir, {"type": kind,
                                   "pair": _pair_key(a, b),
                                   "ts": time.time(), "note": note})
            if journal is not None:
                journal.append(f"dreaming.{kind}",
                               {"a": a["id"], "b": b["id"],
                                "note": note})

        # 1. pure duplicates — no model, no judgement
        by_norm: dict = {}
        for fact in current:
            by_norm.setdefault(_normalized(fact["claim"]),
                               []).append(fact)
        for norm, group in by_norm.items():
            if len(group) < 2:
                continue
            group.sort(key=lambda f: str(f.get("ts", "")))
            survivor, copies = group[0], group[1:]
            # The fresh record supersedes the WHOLE group (survivor
            # included) and restates the claim once — otherwise the
            # original and the copy would both stay current and the
            # duplicate would survive its own merge.
            facts_store.add(
                claim=survivor["claim"], source="dreaming",
                source_trust=survivor.get("source_trust", "inference"),
                presented_as=survivor.get("source_trust",
                                           "inference"),
                supersedes=[g["id"] for g in group],
                evidence=survivor.get("evidence"))
            consumed.update(c["id"] for c in copies)
            report["merged"] += len(copies)

        # 2 + 3. near pairs — merge on agreement, settle on a
        # numbers-only disagreement, keep apart on anything else
        live = [f for f in facts_store.current_facts()
                if f["id"] not in consumed][:_MAX_FACTS]
        tokens = {f["id"]: set(rag.tokenize(f["claim"])) for f in live}
        merges = 0
        for i, a in enumerate(live):
            if a["id"] in consumed or merges >= _MAX_MERGES_PER_RUN:
                continue
            for b in live[i + 1:]:
                if b["id"] in consumed \
                        or merges >= _MAX_MERGES_PER_RUN:
                    continue
                if _pair_key(a, b) in known:
                    continue
                score = min(
                    _overlap_score(tokens[a["id"]], tokens[b["id"]]),
                    _overlap_score(tokens[b["id"]], tokens[a["id"]]))
                if score < _NEAR_OVERLAP:
                    continue
                report["examined"] += 1
                a_nums, a_caps = _entities(a["claim"])
                b_nums, b_caps = _entities(b["claim"])
                if a_caps != b_caps:
                    _log("kept_apart", a, b,
                         "conflicting names/places")
                    report["kept_apart"] += 1
                    continue
                if a_nums == b_nums:
                    merged_claim = None
                    if merge_text is not None:
                        try:
                            merged_claim = merge_text(a["claim"],
                                                      b["claim"])
                        except Exception:
                            merged_claim = None
                    if not merged_claim:
                        merged_claim = max([a["claim"], b["claim"]],
                                           key=len)
                    facts_store.add(
                        claim=str(merged_claim), source="dreaming",
                        source_trust="inference",
                        presented_as="inference",
                        supersedes=[a["id"], b["id"]])
                    consumed.update({a["id"], b["id"]})
                    merges += 1
                    report["merged"] += 1
                    _log("merged", a, b)
                else:
                    newer, older = (a, b) if str(a.get("ts", "")) \
                        >= str(b.get("ts", "")) else (b, a)
                    # The settled record carries the newer claim and
                    # supersedes BOTH — restating the winner while
                    # leaving it current would duplicate it.
                    facts_store.add(
                        claim=newer["claim"], source="dreaming",
                        source_trust="inference",
                        presented_as="inference",
                        supersedes=[older["id"], newer["id"]])
                    consumed.add(older["id"])
                    report["settled"] += 1
                    _log("settled", older, newer,
                         "newer value wins")
    except Exception:
        pass
    if journal is not None:
        try:
            journal.append("dreaming.run", report)
        except Exception:
            pass
    return report
