"""RLCD — Reinforcement Learning from Calibrated Decisions,
implemented as JARVIS v4's decision core (the owner's order,
2026-10-22: "pura model bithao — AI khud decision le, calibrated
taur par").

The loop, end to end, exactly as the study prescribes:

1. DECIDE WITH A NUMBER. Every judgement call JARVIS records
   carries a stated confidence (record_decision).
2. OUTCOME ARRIVES. The owner, a tool result, or reality itself
   marks the decision right or wrong (resolve_decision).
3. A PROPER SCORING RULE PAYS. The log score — log(p) when
   right, log(1-p) when wrong — is the RLCD reward: it pays most
   for confident-and-right, punishes confident-and-wrong hardest,
   and pays honest doubt more than false certainty. No other
   scoring rule has all three properties; that is the paper's
   point, kept intact.
4. CALIBRATION IS LEARNED PER DOMAIN. Stated confidences are
   binned; each bin's empirical accuracy becomes the map from
   "what JARVIS says" to "what JARVIS should believe".
5. THE MAP GOVERNS AUTONOMY. policy(): calibrated confidence
   >= 0.80 -> act alone; 0.55-0.80 -> verify with a tool first;
   below -> ask the owner. JARVIS takes its own decisions — with
   measured, not imagined, self-knowledge.
6. FEEDBACK CLOSES THE LOOP. The worst miscalibrations are
   written back into the brain's context as plain lines, so the
   next stated confidence starts corrected.

This is the app-level RLCD: the reward shapes behaviour through
the log, the map and the feedback — no weights are trained, and
nothing here pretends otherwise.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

_EPS = 0.01
_BINS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.85), (0.85, 1.01))
ACT_AT = 0.80
VERIFY_AT = 0.55

# One lock per decisions file, shared by EVERY Calibrator
# instance pointing at it (the app builds several). A
# per-instance lock made resolve()'s read-modify-write race
# across instances: records appended mid-cycle were silently
# overwritten out of the log (audit 2026-10-22).
_PATH_LOCKS: dict = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _lock_for(path) -> threading.Lock:
    key = str(Path(path).resolve())
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = _PATH_LOCKS[key] = threading.Lock()
        return lock


def _domain(domain) -> str:
    """The one normalization for domain names, applied on
    write AND on read — the old [:60] truncation on write
    alone meant long domains silently never matched."""
    return str(domain or "general")


def log_score(confidence: float, right: bool) -> float:
    """The RLCD reward for one resolved decision."""
    p = min(1.0 - _EPS, max(_EPS, float(confidence)))
    return math.log(p) if right else math.log(1.0 - p)


class Calibrator:
    """The decision log + calibration map over data/decisions.jsonl.
    Thread-safe: background workers record too."""

    def __init__(self, data_dir):
        self._path = Path(data_dir) / "decisions.jsonl"
        self._lock = _lock_for(self._path)

    # -- the log ------------------------------------------------------
    def _rows(self) -> list:
        """Tolerant per-line reader: a torn or foreign line is
        SKIPPED, never a truncation point — the old reader
        stopped at the first bad line and the next resolve()
        rewrote the file without the hidden rows, erasing
        decisions permanently (audit 2026-10-22)."""
        if not self._path.exists():
            return []
        rows = []
        for line in self._path.read_text(
                encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def _append(self, row: dict) -> None:
        # Write failures RAISE: a decision that never reached
        # disk must not hand its caller a usable-looking id.
        with self._lock:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False)
                         + "\n")

    def _rewrite(self, rows: list) -> None:
        """Atomic whole-file replace (temp + os.replace): a
        crash mid-resolve can no longer tear the log. The
        CALLER holds the shared lock (resolve's RMW cycle);
        this never acquires it itself."""
        tmp = self._path.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "".join(json.dumps(r, ensure_ascii=False)
                    + "\n" for r in rows),
            encoding="utf-8")
        os.replace(tmp, self._path)

    # -- steps 1-3: record, resolve, score -----------------------------
    def record(self, domain: str, claim: str,
               confidence: float) -> dict:
        row = {"id": uuid.uuid4().hex[:16], "ts": time.time(),
               "domain": _domain(domain),
               "claim": str(claim)[:300],
               "confidence": min(1.0, max(0.0,
                                          float(confidence))),
               "outcome": None, "score": None}
        self._append(row)
        return row

    def resolve(self, decision_id: str, right: bool) -> Optional[dict]:
        # The whole read-modify-write runs under the shared
        # per-path lock: no record can be appended (and then
        # clobbered) mid-cycle, on any instance.
        with self._lock:
            rows = self._rows()
            for row in rows:
                if row.get("id") == decision_id \
                        and row.get("outcome") is None:
                    row["outcome"] = "right" if right else "wrong"
                    row["score"] = round(
                        log_score(row["confidence"], right), 4)
                    row["resolved_ts"] = time.time()
                    self._rewrite(rows)
                    return row
        return None

    def pending(self, limit: int = 10) -> list:
        rows = [r for r in self._rows()
                if r.get("outcome") is None]
        if limit <= 0:
            return []
        return rows[-limit:]

    # -- steps 4-5: the map and the policy ------------------------------
    def _resolved(self, domain: Optional[str] = None) -> list:
        rows = [r for r in self._rows()
                if r.get("outcome") in ("right", "wrong")]
        if domain:
            want = _domain(domain)
            rows = [r for r in rows if r.get("domain") == want]
        return rows

    def _bin_accuracy(self, rows: list, stated: float):
        for low, high in _BINS:
            if low <= stated < high:
                in_bin = [r for r in rows
                          if low <= float(r["confidence"]) < high]
                if len(in_bin) >= 3:
                    hits = sum(1 for r in in_bin
                               if r["outcome"] == "right")
                    return hits / len(in_bin), len(in_bin)
                return None, len(in_bin)
        return None, 0

    def calibrated(self, domain: str, stated: float) -> float:
        """What JARVIS should believe, given what it said and how
        its word has held up in this domain (falling back to all
        domains, then to the stated value itself)."""
        stated = min(1.0, max(0.0, float(stated)))
        for rows in (self._resolved(domain), self._resolved()):
            accuracy, n = self._bin_accuracy(rows, stated)
            if accuracy is not None:
                if n >= 10:
                    return round(accuracy, 3)
                return round(0.5 * stated + 0.5 * accuracy, 3)
        return stated

    def policy(self, domain: str, stated: float) -> dict:
        value = self.calibrated(domain, stated)
        action = ("act" if value >= ACT_AT
                  else "verify" if value >= VERIFY_AT else "ask")
        return {"stated": float(stated), "calibrated": value,
                "action": action}

    # -- step 6 + reporting ----------------------------------------------
    def stats(self, domain: Optional[str] = None) -> dict:
        rows = self._resolved(domain)
        if not rows:
            return {"resolved": 0}
        stated_mean = sum(float(r["confidence"])
                          for r in rows) / len(rows)
        accuracy = sum(1 for r in rows
                       if r["outcome"] == "right") / len(rows)
        # Legacy/hand-edited rows may carry a null score —
        # average the scores that exist, never crash on one.
        scores = [float(r["score"]) for r in rows
                  if r.get("score") is not None]
        mean_score = (sum(scores) / len(scores)) if scores else 0.0
        return {"resolved": len(rows),
                "stated_mean": round(stated_mean, 3),
                "accuracy": round(accuracy, 3),
                "mean_log_score": round(mean_score, 3),
                "bias": round(stated_mean - accuracy, 3)}

    def domains(self) -> list:
        seen = []
        for row in self._rows():
            if row.get("domain") not in seen:
                seen.append(row.get("domain"))
        return seen

    def feedback_lines(self, limit: int = 3) -> list:
        """The loop's return path: plain lines for the brain's
        context, naming each domain where JARVIS's word and its
        record disagree by >= 0.15, worst first."""
        scored = []
        for domain in self.domains():
            stats = self.stats(domain)
            if stats.get("resolved", 0) >= 5 \
                    and abs(stats.get("bias", 0)) >= 0.15:
                scored.append((abs(stats["bias"]), domain, stats))
        scored.sort(reverse=True)
        lines = []
        for _, domain, stats in scored[:limit]:
            direction = ("zyada bolta hai"
                         if stats["bias"] > 0 else "kam bolta hai")
            lines.append(
                f"Calibration ({domain}): JARVIS ne is hisse me "
                f"ausatan {round(stats['stated_mean'] * 100)}% "
                f"confidence bola aur asal me "
                f"{round(stats['accuracy'] * 100)}% sahi raha "
                f"(n={stats['resolved']}) — woh apna bharosa "
                f"{direction}; confidence naapa-tula bolo.")
        return lines

    def report(self) -> str:
        lines = ["Calibration report, sir:"]
        total = self.stats()
        if not total.get("resolved"):
            lines.append("Abhi koi resolved decision nahi hai — "
                         "jaise-jaise faisle honge, naqsha banta "
                         "jayega.")
            return "\n".join(lines)
        lines.append(
            f"Kul {total['resolved']} faisle: kaha gaya "
            f"{round(total['stated_mean'] * 100)}%, asal "
            f"{round(total['accuracy'] * 100)}%, mean log-score "
            f"{total['mean_log_score']}.")
        for domain in self.domains():
            stats = self.stats(domain)
            if stats.get("resolved"):
                lines.append(
                    f"• {domain}: n={stats['resolved']}, kaha "
                    f"{round(stats['stated_mean'] * 100)}%, sahi "
                    f"{round(stats['accuracy'] * 100)}%")
        pending = self.pending()
        if pending:
            lines.append(f"{len(pending)} faisle abhi khule hain "
                         "(nateeja baaki):")
            for row in pending:
                lines.append(
                    f"• {row['id']} [{row.get('domain', '')}] "
                    f"{round(float(row['confidence']) * 100)}% — "
                    f"{str(row.get('claim', ''))[:70]}")
        return "\n".join(lines)
