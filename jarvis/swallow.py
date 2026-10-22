"""Swallow — JARVIS v4: the silent-failure ledger.

Some failures must never break the conversation (a persistence
write, a background save) — so the code swallows them, BY
DESIGN. What was missing: a count. A swallowed error that is
never counted is invisible forever; the owner's /diagnose once
showed "all well" while the goodput ledger had never once
been written. Now every deliberate swallow leaves a line in
data/swallowed.jsonl — where, what type, when — and /diagnose
reports the total. Swallowing stays; silence ends.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


def note(data_dir, where: str, exc: BaseException) -> None:
    """Record one swallowed failure. Never raises — this is
    the code path taken when something else already failed."""
    try:
        path = Path(data_dir) / "swallowed.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": time.time(), "where": str(where)[:80],
                "error": f"{type(exc).__name__}: "
                         f"{str(exc)[:160]}"}) + "\n")
    except Exception:
        pass


def count(data_dir) -> int:
    try:
        path = Path(data_dir) / "swallowed.jsonl"
        if not path.exists():
            return 0
        return sum(1 for line in
                   path.read_text(encoding="utf-8").splitlines()
                   if line.strip())
    except Exception:
        return 0


def recent(data_dir, limit: int = 3) -> list:
    try:
        path = Path(data_dir) / "swallowed.jsonl"
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(
                encoding="utf-8").splitlines()[-limit:]:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
        return rows
    except Exception:
        return []
