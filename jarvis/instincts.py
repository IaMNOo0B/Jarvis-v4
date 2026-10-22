"""Instincts — ECC's Continuous Learning v2, JARVIS-bound.

An instinct is one small learned behaviour: a trigger, an
action, a domain, and a CONFIDENCE that rises and falls with
evidence — ECC's atomic unit of learning, rebuilt here on
v4's own rails (append-only JSONL in the data dir, the same
per-path lock discipline as calibration/confirmations).

The differences from memory facts are the point: a fact says
WHAT is true; an instinct says WHAT TO DO when a pattern
shows up, and how much that pattern has EARNED trust:

- born tentative (confidence 0.3, scope "session");
- every INDEPENDENT piece of evidence adds +0.1 (cap 0.9);
  the same note repeated adds nothing — repetition is not
  evidence;
- negative evidence (the pattern led somewhere bad) costs
  -0.15, floored at 0.0;
- PROMOTION: confidence >= 0.8 with >= 3 evidence notes
  graduates scope session -> standing (ECC promotes
  project -> global on repeated proof; here session ->
  standing on repeated proof);
- DECAY: an instinct unused for 90+ days loses 0.1 per
  maintain() pass — never more than one step per 90 days
  of disuse, so the arithmetic stays deterministic;
- PRUNE: confidence below 0.2 in a maintain() pass retires
  the instinct (a tombstone row; the history stays in the
  file, the fold forgets it).

The store is append-only: every change appends a fresh
full snapshot row for the instinct's id, and reads FOLD
the log (last row per id wins, tombstones remove). A torn
line is skipped, never fatal — the audit-hardened reader
pattern from the 2026-10-22 marathon.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path

# One lock per instincts file, shared across every
# InstinctBook instance over that path (the marathon rule:
# a per-instance lock is no lock at all when callers build
# a fresh book per use).
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


START_CONFIDENCE = 0.3
MAX_CONFIDENCE = 0.9
REINFORCE_STEP = 0.1
NEGATIVE_STEP = 0.15
DECAY_STEP = 0.1
DECAY_AFTER_S = 90 * 24 * 3600
PROMOTE_AT = 0.8
PROMOTE_EVIDENCE = 3
PRUNE_BELOW = 0.2

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set:
    return set(_TOKEN_RE.findall(str(text).lower()))


def instinct_id(trigger: str, action: str, domain: str) -> str:
    """One instinct per (trigger, action, domain) — the
    same pattern observed again REINFORCES; it never
    duplicates."""
    key = "\n".join([str(trigger).strip().lower(),
                     str(action).strip().lower(),
                     str(domain).strip().lower()])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


class InstinctBook:
    """The append-only instinct store over
    data/instincts.jsonl."""

    def __init__(self, data_dir):
        self.path = Path(data_dir) / "instincts.jsonl"
        self._lock = _lock_for(self.path)

    # -- storage ------------------------------------------------

    def _rows(self) -> list:
        if not self.path.exists():
            return []
        out = []
        try:
            lines = self.path.read_text(
                encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue          # a torn line hides nothing
            if isinstance(row, dict) and row.get("id"):
                out.append(row)
        return out

    def _append(self, row: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _fold(self) -> dict:
        """id -> current row. Last snapshot wins; a
        tombstone removes the instinct from the fold."""
        folded = {}
        for row in self._rows():
            if row.get("pruned"):
                folded.pop(row["id"], None)
            else:
                folded[row["id"]] = row
        return folded

    # -- reads --------------------------------------------------

    def all(self) -> list:
        """Every live instinct, most trusted first."""
        with self._lock:
            rows = list(self._fold().values())
        rows.sort(key=lambda r: (-float(r.get("confidence", 0)),
                                 -float(r.get("last_used", 0))))
        return rows

    def get(self, iid: str):
        with self._lock:
            return self._fold().get(iid)

    # -- the learning verb --------------------------------------

    def observe(self, trigger: str, action: str,
                domain: str, evidence_note: str, *,
                negative: bool = False, now=None) -> dict:
        """Create the instinct or fold new evidence into it.

        Positive: a NEW note (one not already in the
        evidence trail) adds +0.1 up to the cap; a repeat
        note only refreshes last_used — the same observation
        counted twice would be a lie about independence.
        Negative: the note joins the trail and confidence
        drops by 0.15 (floor 0.0). Returns the folded row.
        """
        now = time.time() if now is None else float(now)
        note = str(evidence_note).strip()[:300]
        iid = instinct_id(trigger, action, domain)
        with self._lock:
            current = self._fold().get(iid)
            if current is None:
                row = {
                    "id": iid,
                    "trigger": str(trigger).strip()[:200],
                    "action": str(action).strip()[:200],
                    "domain": str(domain).strip()[:60]
                    or "general",
                    "confidence": START_CONFIDENCE,
                    "evidence": [note] if note else [],
                    "scope": "session",
                    "created": now,
                    "last_used": now,
                    "last_decay": now,
                }
                if negative:
                    row["confidence"] = round(max(
                        0.0, START_CONFIDENCE - NEGATIVE_STEP), 3)
                self._append(row)
                return dict(row)
            row = dict(current)
            row["evidence"] = list(current.get("evidence")
                                   or [])
            conf = float(current.get("confidence",
                                     START_CONFIDENCE))
            if note and note not in row["evidence"]:
                row["evidence"].append(note)
                if negative:
                    conf = max(0.0, conf - NEGATIVE_STEP)
                else:
                    conf = min(MAX_CONFIDENCE,
                               conf + REINFORCE_STEP)
            elif negative and note:
                # The same bad note again still counts
                # against — the pattern failed AGAIN.
                conf = max(0.0, conf - NEGATIVE_STEP)
            row["confidence"] = round(conf, 3)
            row["last_used"] = now
            self._append(row)
            return dict(row)

    # -- upkeep ---------------------------------------------------

    def maintain(self, now=None) -> dict:
        """One deterministic upkeep pass over every live
        instinct: decay the long-unused, promote the proven,
        prune the faded. Each step appends its snapshot row,
        so the log tells the whole story."""
        now = time.time() if now is None else float(now)
        counts = {"decayed": 0, "promoted": 0, "pruned": 0}
        with self._lock:
            for row in self._fold().values():
                row = dict(row)
                changed = False
                last_used = float(row.get("last_used", 0))
                last_decay = float(row.get(
                    "last_decay", row.get("created", 0)))
                if now - max(last_used, last_decay) \
                        > DECAY_AFTER_S:
                    row["confidence"] = round(max(
                        0.0, float(row.get("confidence", 0))
                        - DECAY_STEP), 3)
                    row["last_decay"] = now
                    counts["decayed"] += 1
                    changed = True
                if row["confidence"] < PRUNE_BELOW:
                    self._append({"id": row["id"],
                                  "pruned": True, "ts": now})
                    counts["pruned"] += 1
                    continue
                if row.get("scope") == "session" \
                        and row["confidence"] >= PROMOTE_AT \
                        and len(row.get("evidence") or []) \
                        >= PROMOTE_EVIDENCE:
                    row["scope"] = "standing"
                    counts["promoted"] += 1
                    changed = True
                if changed:
                    self._append(row)
        return counts

    # -- surfacing ------------------------------------------------

    def matching(self, query: str, limit: int = 2) -> list:
        """Live instincts whose trigger/domain/action share
        words with the query — best overlap first, trust as
        the tiebreak. Standing instincts always outrank
        session ones at equal overlap: they earned it."""
        qtok = _tokens(query)
        if not qtok:
            return []
        scored = []
        for row in self.all():
            hay = _tokens(f"{row.get('domain', '')} "
                          f"{row.get('trigger', '')} "
                          f"{row.get('action', '')}")
            overlap = len(qtok & hay)
            if overlap:
                scored.append((
                    overlap,
                    1 if row.get("scope") == "standing" else 0,
                    float(row.get("confidence", 0)), row))
        scored.sort(key=lambda t: (-t[0], -t[1], -t[2]))
        return [row for *_s, row in scored[:limit]]

    def context_lines(self, query: str, limit: int = 2) -> list:
        """Short lines for the brain's context — the
        instinct system's return path, riding beside the
        calibration feedback lines. Capped hard: instincts
        advise, they never crowd out the conversation."""
        lines = []
        for row in self.matching(query, limit=limit):
            lines.append(
                f"Seekha pattern [{row.get('domain', '')}] "
                f"(bharosa {float(row.get('confidence', 0)):.2f}): "
                f"jab {str(row.get('trigger', ''))[:80]} — "
                f"{str(row.get('action', ''))[:80]}")
        return lines
