"""Provider goodput accounting — Step 17 (Infrastructure & Production).

The study's rule (ai-eng-study part-I, lesson 17-08): **goodput = the
fraction of requests meeting ALL SLO constraints simultaneously**. Mean
latency is a lie for right-skewed distributions — a provider whose average
is 900 ms but which blows the SLO on a third of turns is a BAD provider,
and an average would call it fine. JARVIS's fallback chain (keyed cloud →
Ollama → OmniRoute → Pollinations → stub) is judged here by goodput, and
by latency percentiles (P50/P90/P99) — never by means.

A request only counts toward goodput when ALL of these hold:

- the provider call itself succeeded (``ok``), AND
- the reply passed the quality bar (``quality_ok``) — a fast bland
  deflection is not good output, AND
- its latency is within the SLO (``latency_ms <= slo_ms``).

Records carry a timestamp so reports can be windowed (last N requests, or
the last hour). The ledger is in-memory with a JSONL save/load so the
acceptance runner (jarvis.acceptance) can tabulate real traffic captured
on the owner's PC.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional


@dataclass(frozen=True)
class RequestRecord:
    provider: str
    latency_ms: float
    ok: bool          # the provider call itself succeeded (a reply was produced)
    quality_ok: bool  # the reply passed the quality bar (not a deflection/error)
    ts: float = 0.0   # epoch seconds

    def meets_slo(self, slo_ms: float) -> bool:
        return bool(self.ok and self.quality_ok and self.latency_ms <= slo_ms)


def _percentile(sorted_vals: List[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile. ``sorted_vals`` must be sorted ascending."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    rank = int(-(-pct * len(sorted_vals) // 100))  # ceil(pct/100 * n)
    rank = max(1, min(rank, len(sorted_vals)))
    return float(sorted_vals[rank - 1])


class GoodputLedger:
    """Append-only record of provider requests + goodput reporting."""

    def __init__(self, records: Optional[Iterable[RequestRecord]] = None):
        self._records: List[RequestRecord] = list(records or [])

    # -- recording ------------------------------------------------------

    def record(self, provider: str, latency_ms: float, ok: bool,
               quality_ok: bool, ts: Optional[float] = None) -> RequestRecord:
        rec = RequestRecord(provider=str(provider),
                            latency_ms=float(latency_ms),
                            ok=bool(ok), quality_ok=bool(quality_ok),
                            ts=float(ts) if ts is not None else time.time())
        self._records.append(rec)
        return rec

    def __len__(self) -> int:
        return len(self._records)

    # -- filtering --------------------------------------------------------

    def _select(self, provider: Optional[str] = None, *,
                last_n: Optional[int] = None,
                window_s: Optional[float] = None,
                now: Optional[float] = None) -> List[RequestRecord]:
        rows = self._records
        if provider is not None:
            rows = [r for r in rows if r.provider == provider]
        if window_s is not None:
            cutoff = (float(now) if now is not None else time.time()) - window_s
            rows = [r for r in rows if r.ts >= cutoff]
        if last_n is not None:
            rows = rows[-int(last_n):] if last_n > 0 else []
        return rows

    def providers(self) -> List[str]:
        seen: List[str] = []
        for r in self._records:
            if r.provider not in seen:
                seen.append(r.provider)
        return seen

    # -- the metric -------------------------------------------------------

    def goodput(self, provider: str, slo_ms: float, *,
                last_n: Optional[int] = None,
                window_s: Optional[float] = None,
                now: Optional[float] = None) -> Optional[float]:
        """Fraction of ``provider``'s requests meeting ALL SLO bars.

        None when the provider has no requests in the selected window —
        "no data" is reported as no data, never as 0.0 or 1.0.
        """
        rows = self._select(provider, last_n=last_n,
                            window_s=window_s, now=now)
        if not rows:
            return None
        good = sum(1 for r in rows if r.meets_slo(slo_ms))
        return good / len(rows)

    def report(self, slo_ms: float, *,
               last_n: Optional[int] = None,
               window_s: Optional[float] = None,
               now: Optional[float] = None) -> Dict[str, Dict[str, object]]:
        """Per-provider table: n, goodput, and latency P50/P90/P99.

        Percentiles are over ALL requests in the window (the distribution
        you actually lived with), goodput is the SLO-meeting fraction.
        """
        out: Dict[str, Dict[str, object]] = {}
        for provider in self.providers():
            rows = self._select(provider, last_n=last_n,
                                window_s=window_s, now=now)
            if not rows:
                continue
            lats = sorted(r.latency_ms for r in rows)
            good = sum(1 for r in rows if r.meets_slo(slo_ms))
            out[provider] = {
                "n": len(rows),
                "good": good,
                "goodput": good / len(rows),
                "ok_rate": sum(1 for r in rows if r.ok) / len(rows),
                "quality_rate": sum(1 for r in rows if r.quality_ok) / len(rows),
                "p50_ms": _percentile(lats, 50),
                "p90_ms": _percentile(lats, 90),
                "p99_ms": _percentile(lats, 99),
            }
        return out

    def weakest_provider(self, slo_ms: float, *, min_requests: int = 1,
                         last_n: Optional[int] = None,
                         window_s: Optional[float] = None,
                         now: Optional[float] = None) -> Optional[str]:
        """The provider with the lowest goodput — the chain's weak link.

        Ties break toward MORE requests (more evidence of weakness), then
        by name, so the answer is deterministic. None when nobody has at
        least ``min_requests`` requests in the window.
        """
        table = self.report(slo_ms, last_n=last_n,
                            window_s=window_s, now=now)
        candidates = [(name, stats) for name, stats in table.items()
                      if int(stats["n"]) >= min_requests]
        if not candidates:
            return None
        candidates.sort(key=lambda kv: (float(kv[1]["goodput"]),
                                        -int(kv[1]["n"]), kv[0]))
        return candidates[0][0]

    # -- persistence --------------------------------------------------------

    def to_rows(self) -> List[Dict[str, object]]:
        return [asdict(r) for r in self._records]

    @classmethod
    def from_rows(cls, rows: Iterable[Dict[str, object]]) -> "GoodputLedger":
        return cls(RequestRecord(provider=str(r["provider"]),
                                 latency_ms=float(r["latency_ms"]),
                                 ok=bool(r["ok"]),
                                 quality_ok=bool(r["quality_ok"]),
                                 ts=float(r.get("ts", 0.0)))
                   for r in rows)

    def save_jsonl(self, path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for row in self.to_rows():
                f.write(json.dumps(row) + "\n")
        return path

    @classmethod
    def load_jsonl(cls, path) -> "GoodputLedger":
        rows = []
        with Path(path).open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return cls.from_rows(rows)


__all__ = ["RequestRecord", "GoodputLedger"]
