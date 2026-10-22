"""Fan-out doctrine: all routes at once, combined power.
One route dying never sinks the others; merged hits are
deduped by URL and ranked by how many routes agree; the
search_all tool reports used AND failed routes honestly."""

import time

from jarvis import builtin_skills as bs
from jarvis.chat import ChatApp
from jarvis.fanout import merge_hits, run_parallel


def test_routes_run_in_parallel_and_failures_isolate():
    def slow():
        time.sleep(0.4)
        return {"ok": True, "v": 1}

    def dead():
        raise RuntimeError("raasta band")

    def errdict():
        return {"error": "backend chup hai", "results": []}

    start = time.monotonic()
    oks, failures = run_parallel(
        {"a": slow, "b": slow, "dead": dead, "err": errdict},
        timeout=10)
    elapsed = time.monotonic() - start
    assert elapsed < 0.75  # parallel: not 0.4 + 0.4 serial
    assert set(oks) == {"a", "b"}
    assert "RuntimeError" in failures["dead"]
    assert failures["err"] == "backend chup hai"


def test_merge_ranks_agreement_and_labels_routes():
    merged = merge_hits([
        ("web", [{"title": "A", "url": "https://a", "snippet": "x"},
                 {"title": "B", "url": "https://b", "snippet": ""}]),
        ("github", [{"title": "A2", "url": "https://a",
                     "snippet": "y"}]),
    ])
    assert merged[0]["url"] == "https://a"
    assert merged[0]["via"] == ["web", "github"]
    assert merged[1]["via"] == ["web"]


def test_search_all_merges_and_reports(monkeypatch, tmp_path):
    monkeypatch.setattr(bs, "op_web_search", lambda q, limit=5: {
        "results": [{"title": "W", "url": "https://w",
                     "snippet": "web se"}]})
    monkeypatch.setattr(bs, "op_github_search", lambda q, limit=5: {
        "ok": True, "repos": [{"name": "r/x", "stars": 9,
                               "description": "repo",
                               "url": "https://gh"}]})
    monkeypatch.setattr(bs, "op_wikipedia", lambda q: {
        "ok": False, "error": "nahi mila"})
    monkeypatch.setattr(bs, "op_search_browser", lambda q: {
        "error": "browser band hai"})
    app = ChatApp(tmp_path)
    out = app.execute_tool("search_all", {"query": "kuch bhi"})
    assert out["ok"] is True
    assert {r["url"] for r in out["results"]} == {"https://w",
                                                  "https://gh"}
    failed = {f["route"] for f in out["routes_failed"]}
    assert failed == {"wikipedia", "real_browser"}
    assert set(out["routes_used"]) == {"web_search", "github"}
