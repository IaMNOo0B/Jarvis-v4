"""Tests for jarvis.builtin_skills — the ops are real and run."""

import sys
import urllib.error

import pytest

from jarvis.builtin_skills import (build_default_registry, op_open_app,
                                   op_time_now, op_web_search)

DDG_PAGE = """
<html><body>
<div class="result">
  <a rel="nofollow" class="result__a"
     href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fmuse&amp;rut=abc">Muse <b>AI</b> — Example</a>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">A short snippet about Muse.</a>
</div>
<div class="result">
  <a rel="nofollow" class="result__a"
     href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Ftwo&amp;rut=def">Second result</a>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=y">Another snippet.</a>
</div>
</body></html>
"""


class _FakeResponse:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_time_now_is_the_real_clock():
    out = op_time_now()
    assert out["iso"] and out["unix"] > 0


def test_web_search_parses_duckduckgo_html(monkeypatch):
    monkeypatch.setattr(
        "jarvis.builtin_skills.urllib.request.urlopen",
        lambda req, timeout=None: _FakeResponse(DDG_PAGE))
    out = op_web_search("muse ai")
    results = out["results"]
    assert len(results) == 2
    assert results[0]["url"] == "https://example.com/muse"
    assert "Muse" in results[0]["title"]
    assert results[0]["snippet"] == "A short snippet about Muse."


def test_web_search_failure_is_an_honest_error(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("no route to host")
    monkeypatch.setattr("jarvis.builtin_skills.urllib.request.urlopen", boom)
    out = op_web_search("anything")
    assert "error" in out and "web_search failed" in out["error"]


def test_web_search_respects_limit(monkeypatch):
    monkeypatch.setattr(
        "jarvis.builtin_skills.urllib.request.urlopen",
        lambda req, timeout=None: _FakeResponse(DDG_PAGE))
    assert len(op_web_search("q", limit=1)["results"]) == 1


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Windows path is exercised on the owner's PC")
def test_open_app_off_windows_is_honest_not_an_exception():
    out = op_open_app("notepad")
    assert "error" in out and "driver not present" in out["error"]


def test_memory_ops_delegate_to_injected_memory():
    class FakeMemory:
        def __init__(self):
            self.store = []

        def remember(self, text):
            self.store.append(text)
            return {"stored": text}

        def recall(self, query):
            return {"facts": [f for f in self.store if query in f]}

    memory = FakeMemory()
    registry = build_default_registry(memory=memory)
    registry.dispatch("remember_fact", {"text": "chai pasand hai"})
    assert memory.store == ["chai pasand hai"]
    out = registry.dispatch("recall_memory", {"query": "chai"})
    assert out["facts"] == ["chai pasand hai"]
