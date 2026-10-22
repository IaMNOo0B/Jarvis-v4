"""Fan-out: run every route at once and merge what returns.

The owner's doctrine, mechanized a second time: not only
'if one route fails, try another' — but ALSO 'use them ALL
together; combined routes are more power than any single
one, and the search covers more ground at a larger scale'.

run_parallel executes named zero-arg jobs concurrently and
NEVER raises: a job that throws or times out lands in the
failures dict with its reason, and the others still deliver.
merge_hits dedupes result lists by URL and tracks WHICH
routes found each hit (via) — a page three routes agree on
ranks above one a single route happened to surface.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait


def run_parallel(jobs: dict, timeout: float = 30.0):
    """jobs: {route_name: callable() -> dict}.
    Returns (oks, failures): oks maps name -> its result dict;
    failures maps name -> a short reason string. A job that
    returns a dict carrying 'error' (and no ok=True) counts
    as a failure of that route — the result itself is kept
    out of oks so callers merge only real findings."""
    oks, failures = {}, {}
    if not jobs:
        return oks, failures
    pool = ThreadPoolExecutor(max_workers=min(16, len(jobs)))
    try:
        futures = {pool.submit(fn): name
                   for name, fn in jobs.items()}
        done, pending = wait(futures, timeout=timeout)
        for fut in pending:
            failures[futures[fut]] = "timeout"
            fut.cancel()
        for fut in done:
            name = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:  # a route died; others live
                failures[name] = f"{type(exc).__name__}: {exc}"
                continue
            if isinstance(result, dict) and (
                    result.get("error")
                    and result.get("ok") is not True):
                failures[name] = str(result["error"])[:200]
            else:
                oks[name] = result if isinstance(result, dict) \
                    else {"value": result}
    finally:
        pool.shutdown(wait=False)
    return oks, failures


def merge_hits(route_hits, cap: int = 12):
    """route_hits: [(route, [hit, ...]), ...] where a hit is a
    dict with at least url/title/snippet. Merged list: one
    entry per URL, 'via' naming every route that found it,
    ranked by agreement (most routes first), then by first
    appearance."""
    by_url, order = {}, []
    for route, hits in route_hits:
        for hit in hits or []:
            url = str(hit.get("url") or "").strip()
            if not url:
                continue
            if url not in by_url:
                by_url[url] = {
                    "title": str(hit.get("title") or url)[:160],
                    "url": url,
                    "snippet": str(hit.get("snippet") or "")[:300],
                    "via": [],
                }
                order.append(url)
            entry = by_url[url]
            if route not in entry["via"]:
                entry["via"].append(route)
            if not entry["snippet"] and hit.get("snippet"):
                entry["snippet"] = str(hit["snippet"])[:300]
    merged = [by_url[u] for u in order]
    merged.sort(key=lambda e: -len(e["via"]))
    return merged[:cap]
