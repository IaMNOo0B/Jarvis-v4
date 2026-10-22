"""User model — JARVIS v4, Wave 3 (Sentient's dialectic insights).

Facts say what IS. Insights say what JARVIS has come to UNDERSTAND
about the owner — each with a dimension, an evidence trail, and a
confidence that moves dialectically: supporting observations push
it up, contradictions push it down, and below the dispute line an
insight stops being asserted and becomes a QUESTION for the owner
instead. Insights the owner wrote or confirmed himself are never
auto-changed — evidence against one can only queue a question.

The store is deliberately small (data/user_model.json): a handful
of living insights, not another database.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import List, Optional

from . import rag

DISPUTE_BELOW = 0.35
_SUPPORT_STEP = 0.10
_CONTRADICT_STEP = 0.15
_MAX_EVIDENCE = 8
_MATCH_OVERLAP = 0.45


class UserModel:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "user_model.json"
        self.questions_path = Path(data_dir) / \
            "user_model_questions.jsonl"
        self._insights: List[dict] = []
        if self.path.exists():
            try:
                rows = json.loads(self.path.read_text(
                    encoding="utf-8"))
                if isinstance(rows, list):
                    self._insights = rows
            except Exception:
                self._insights = []

    # -- storage ----------------------------------------------------

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._insights,
                                            ensure_ascii=False),
                                 encoding="utf-8")
        except Exception:
            pass

    def insights(self, include_disputed: bool = True) -> List[dict]:
        return [i for i in self._insights
                if include_disputed or i["status"] == "active"]

    def queue_question(self, insight: dict, reason: str) -> None:
        row = {"id": uuid.uuid4().hex[:8], "ts": time.time(),
               "insight_id": insight["id"],
               "claim": insight["claim"], "reason": reason,
               "question": f"Sir, ek baat pakki karni thi: "
                           f"{insight['claim']} — ye ab bhi sahi "
                           f"hai?"}
        try:
            with self.questions_path.open("a", encoding="utf-8") \
                    as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def pending_questions(self) -> List[dict]:
        rows = []
        if self.questions_path.exists():
            try:
                for line in self.questions_path.read_text(
                        encoding="utf-8").splitlines():
                    if line.strip():
                        rows.append(json.loads(line))
            except Exception:
                return []
        return rows

    # -- the dialectic ------------------------------------------------

    def _match(self, dimension: str, claim: str) -> Optional[dict]:
        claim_tokens = set(rag.tokenize(claim))
        best, best_score = None, 0.0
        for ins in self._insights:
            if ins["dimension"] != dimension:
                continue
            tokens = set(rag.tokenize(ins["claim"]))
            if not tokens or not claim_tokens:
                continue
            score = len(claim_tokens & tokens) / \
                max(1, min(len(claim_tokens), len(tokens)))
            if score > best_score:
                best, best_score = ins, score
        return best if best_score >= _MATCH_OVERLAP else None

    def observe(self, dimension: str, claim: str, evidence: str,
                relation: str = "new", origin: str = "observed") \
            -> dict:
        """One observation enters the dialectic. relation is the
        caller's classification against the matching insight:
        'support' | 'contradict' | 'new'. Returns the insight."""
        dimension = str(dimension).strip().lower()[:40] or "general"
        claim = str(claim).strip()[:300]
        evidence = str(evidence).strip()[:300]
        match = self._match(dimension, claim)
        if match is None or relation == "new" and origin == "owner":
            insight = {"id": uuid.uuid4().hex[:12],
                       "dimension": dimension, "claim": claim,
                       "confidence": 0.9 if origin == "owner" else 0.5,
                       "evidence": [evidence] if evidence else [],
                       "status": "active", "origin": origin,
                       "ts": time.time()}
            self._insights.append(insight)
            self._save()
            return insight
        if match["origin"] == "owner":
            # The owner's own word is never auto-changed; contrary
            # evidence only earns a question.
            if relation == "contradict":
                self.queue_question(match, "contradicting evidence "
                                           "against an owner insight")
            return match
        if relation == "contradict":
            match["confidence"] = round(
                max(0.0, match["confidence"] - _CONTRADICT_STEP), 3)
            if evidence:
                match["evidence"] = (match["evidence"] +
                                     [evidence])[-_MAX_EVIDENCE:]
            if match["confidence"] < DISPUTE_BELOW and \
                    match["status"] == "active":
                match["status"] = "disputed"
                self.queue_question(match, "confidence fell below "
                                           "the dispute line")
        else:   # support (or an unclassified repeat observation)
            match["confidence"] = round(
                min(0.95, match["confidence"] + _SUPPORT_STEP), 3)
            if evidence:
                match["evidence"] = (match["evidence"] +
                                     [evidence])[-_MAX_EVIDENCE:]
        match["ts"] = time.time()
        self._save()
        return match

    # -- prompt surface ----------------------------------------------

    def context_lines(self, query: str, k: int = 2) -> List[str]:
        """Active insights relevant to a query, phrased as the
        understanding they are — never as facts."""
        q = set(rag.tokenize(str(query)))
        scored = []
        for ins in self._insights:
            if ins["status"] != "active":
                continue
            tokens = set(rag.tokenize(ins["claim"])) | \
                set(rag.tokenize(ins["dimension"]))
            score = len(q & tokens)
            if score:
                scored.append((score, ins))
        scored.sort(key=lambda pair: -pair[0])
        return [f"Samajh (confidence {ins['confidence']:.0%}): "
                f"{ins['claim']}" for _s, ins in scored[:k]]
