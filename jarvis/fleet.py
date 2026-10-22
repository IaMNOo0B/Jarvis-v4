"""The fleet — JARVIS's many-hands engine.

The owner's order: hundreds of things at once, not four. The
honest shape of that on a real PC: a bounded worker pool with a
high ceiling (up to CEILING concurrent operations) through
which work FLOWS. Most background work waits — on networks,
on disks, on other brains — so a deep queue of concurrent
operations is real; what the pool never does is pretend the
CPU is bigger than it is. Results always come home in job
order, one job's failure never sinks its neighbours, and the
peak concurrency is MEASURED, not asserted.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Sequence

CEILING = 300          # the owner's "kam se kam do-tin sau"
DEFAULT_WORKERS = 32


class Fleet:
    def __init__(self, max_workers: int = DEFAULT_WORKERS):
        self.max_workers = max(1, min(CEILING, int(max_workers)))
        self._lock = threading.Lock()
        self._live = 0
        self.peak = 0
        self.submitted = 0
        self.completed = 0

    def _wrap(self, fn: Callable[[Any], Any], job: Any) -> Dict:
        with self._lock:
            self._live += 1
            self.peak = max(self.peak, self._live)
        try:
            return {"ok": True, "result": fn(job)}
        except Exception as exc:  # one bad job never sinks the fleet
            return {"ok": False,
                    "error": f"{type(exc).__name__}: {exc}"}
        finally:
            with self._lock:
                self._live -= 1
                self.completed += 1

    def run_many(self, jobs: Sequence[Any],
                 fn: Callable[[Any], Any]) -> List[Dict]:
        """Run fn over every job concurrently (bounded by the
        pool); results in JOB ORDER, failures captured per job."""
        jobs = list(jobs)
        if not jobs:
            return []
        with self._lock:
            self.submitted += len(jobs)
        workers = min(self.max_workers, len(jobs))
        if workers <= 1:
            return [self._wrap(fn, job) for job in jobs]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(lambda job: self._wrap(fn, job),
                                 jobs))

    def stats(self) -> Dict[str, Any]:
        return {"max_workers": self.max_workers,
                "submitted": self.submitted,
                "completed": self.completed,
                "peak_concurrency": self.peak}
