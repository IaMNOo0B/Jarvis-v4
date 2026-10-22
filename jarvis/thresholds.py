"""Learned acceptance thresholds — JARVIS v4, Wave 3.

Sentient's suggestion-threshold mechanism, exact formula and all:

    threshold(key) = clamp(base - 0.05 * score, 0.40, 0.95)

Every approval nudges a key's score +1 (its bar drops — the owner
likes this kind of thing, show it more readily); every dismissal
nudges -1 (the bar rises). The clamps are the humility: no key can
ever become a certainty or a never, however long the streak.

v4's consumer is the script-watcher noteworthy judge: the judge
returns a confidence, and whether a change is announced is gated
by the WATCHER's learned threshold. Feedback comes from the
notification itself — read = approval; still unread after 48
hours = dismissal — each notification counted exactly once.
"""

from __future__ import annotations

import json
from pathlib import Path

BASE = 0.60
STEP = 0.05
LOW, HIGH = 0.40, 0.95


class LearnedThresholds:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "thresholds.json"
        self._rows: dict = {}
        if self.path.exists():
            try:
                rows = json.loads(self.path.read_text(
                    encoding="utf-8"))
                if isinstance(rows, dict):
                    self._rows = rows
            except Exception:
                self._rows = {}

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._rows,
                                            ensure_ascii=False),
                                 encoding="utf-8")
        except Exception:
            pass

    def _row(self, key: str) -> dict:
        return self._rows.setdefault(
            str(key), {"score": 0, "counted": []})

    def score(self, key: str) -> int:
        return int(self._row(key).get("score", 0))

    def threshold(self, key: str) -> float:
        raw = BASE - STEP * self.score(key)
        return max(LOW, min(HIGH, raw))

    def allows(self, key: str, confidence: float) -> bool:
        return float(confidence) >= self.threshold(key)

    def feedback(self, key: str, approved: bool) -> None:
        row = self._row(key)
        row["score"] = int(row.get("score", 0)) + (1 if approved
                                                   else -1)
        self._save()

    def is_counted(self, key: str, item_id: str) -> bool:
        return str(item_id) in self._row(key).get("counted", [])

    def mark_counted(self, key: str, item_id: str) -> None:
        row = self._row(key)
        counted = row.setdefault("counted", [])
        if str(item_id) not in counted:
            counted.append(str(item_id))
            row["counted"] = counted[-200:]   # bounded bookkeeping
            self._save()
