"""Candidates — JARVIS v4: rules JARVIS proposes, never imposes.

JARVIS learns how the owner likes things and may PROPOSE a
lasting rule ("shaam ke baad notifications band rakho"). A
proposal is only a line in data/rule_candidates.jsonl until
the owner accepts it — acceptance is the owner's own act
(/rules accept <id>), which writes the line into the ONE
rules file (JARVIS-RULES.txt, TAREEKA section). Until then it
changes nothing, ever. The Daily Brief surfaces how many
proposals are waiting, so they are seen, not buried.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path


def _path(data_dir) -> Path:
    return Path(data_dir) / "rule_candidates.jsonl"


def _rows(data_dir) -> list:
    path = _path(data_dir)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def propose(data_dir, text: str, why: str = "") -> dict:
    row = {"id": uuid.uuid4().hex[:8], "ts": time.time(),
           "text": str(text).strip()[:300],
           "why": str(why).strip()[:200], "status": "pending"}
    if not row["text"]:
        raise ValueError("rule khaali hai")
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def propose_skill(data_dir, skill: dict, why: str = "",
                  action: str = "create") -> dict:
    """Queue a proposed SKILL PLAYBOOK (Spec 6) in the same
    owner-decision queue: the row carries the full playbook
    and a kind marker, so accepting it writes into the
    SkillBook — never into JARVIS-RULES.txt."""
    skill = dict(skill or {})
    name = str(skill.get("name", ""))[:80]
    row = {"id": uuid.uuid4().hex[:8], "ts": time.time(),
           "kind": "skill", "action": str(action)[:12],
           "skill": skill,
           "checks": [c for c in skill.get("checks") or []
                      if isinstance(c, dict)],
           "text": (f"Skill salah ({action}): {name} — "
                    f"{str(skill.get('description', ''))}"
                    )[:300],
           "why": str(why).strip()[:200],
           "status": "pending"}
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def pending(data_dir) -> list:
    return [r for r in _rows(data_dir)
            if r.get("status") == "pending"]


def decide(data_dir, candidate_id: str, status: str):
    """Mark one candidate accepted/dropped; returns the row or
    None. Acceptance itself (writing the rules file) is the
    caller's act — this only keeps the ledger honest."""
    rows = _rows(data_dir)
    found = None
    for row in rows:
        if row.get("id") == candidate_id \
                and row.get("status") == "pending":
            row["status"] = status
            row["decided_ts"] = time.time()
            found = row
    if found is not None:
        path = _path(data_dir)
        with path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return found
