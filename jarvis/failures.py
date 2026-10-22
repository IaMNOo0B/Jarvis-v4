"""Failure clustering — the Kodee eval discipline, small and
honest: don't read errors one by one, GROUP them. Kodee
clusters support failures by topic and fixes the biggest
cluster first; JARVIS clusters its own journal errors by
(kind, error class) so /diagnose can say "ye wajah baar-baar
aa rahi hai" instead of reciting a diary.
"""

from __future__ import annotations

from typing import Dict, List


def cluster_errors(records, top: int = 5) -> List[Dict]:
    """records: [{kind, error}] → [{kind, error, count,
    sample}] sorted by count, biggest cluster first."""
    groups: Dict[tuple, Dict] = {}
    for rec in records or []:
        err = str((rec or {}).get("error") or "").strip()
        if not err:
            continue
        head = err.split(":")[0].strip()[:48] or err[:48]
        key = (str(rec.get("kind", "?")), head)
        slot = groups.setdefault(
            key, {"kind": key[0], "error": head, "count": 0,
                  "sample": err[:160]})
        slot["count"] += 1
    return sorted(groups.values(),
                  key=lambda g: (-g["count"], g["kind"],
                                 g["error"]))[:top]
