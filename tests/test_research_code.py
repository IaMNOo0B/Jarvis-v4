"""Phase 1.5: the code skill (real execution, scrubbed environment)
and the deep-research skill (multi-angle, reads pages, saves the
study into the auto-learn inbox). Hermetic: fake web ops, tmp dirs."""
import os

import pytest

from jarvis import builtin_skills as bs
from jarvis.chat import ChatApp, _research_op
from jarvis.providers import FallbackChain, StubProvider


class CountingChain:
    name = "counting"

    def __init__(self):
        self.inner = FallbackChain([StubProvider()])
        self.calls = 0

    def complete(self, messages, tools=None):
        self.calls += 1
        return self.inner.complete(messages, tools)


# -- code skill ------------------------------------------------------

def test_run_code_executes_and_reports():
    out = bs.op_run_code("print(40 + 2)")
    assert out["ok"] is True and "42" in out["stdout"]


def test_run_code_cannot_see_environment_secrets(monkeypatch):
    monkeypatch.setenv("JARVIS_TEST_SECRET", "hunter2")
    out = bs.op_run_code(
        "import os; print(os.environ.get('JARVIS_TEST_SECRET'))")
    assert out["ok"] is True
    assert "hunter2" not in out["stdout"]
    assert "None" in out["stdout"]


def test_run_code_timeout_is_honest():
    out = bs.op_run_code("import time; time.sleep(10)", timeout=1)
    assert out["ok"] is False and "timed out" in out["error"]


def test_run_code_failure_carries_stderr():
    out = bs.op_run_code("raise ValueError('boom')")
    assert out["ok"] is False and "boom" in out["stderr"]


def test_create_project_writes_files_and_blocks_escape(tmp_path):
    base = tmp_path / "proj"
    out = bs.op_create_project(str(base), [
        {"path": "main.py", "content": "print('hi')"},
        {"path": "pkg/util.py", "content": "X = 1"},
    ])
    assert len(out["written"]) == 2
    assert (base / "pkg" / "util.py").read_text() == "X = 1"
    bad = bs.op_create_project(str(base),
                               [{"path": "../evil.py", "content": "x"}])
    assert "error" in bad


# -- research skill ----------------------------------------------------

@pytest.fixture()
def fake_web(monkeypatch):
    def fake_search(query, limit=5):
        return {"results": [
            {"title": "Falcon Guide", "url": "https://a.example/1",
             "snippet": "s1"},
            {"title": "Falcon Deep Dive", "url": "https://b.example/2",
             "snippet": "s2"},
        ]}

    def fake_fetch(url, max_chars=12000):
        return {"url": url,
                "text": f"Full text from {url}: the peregrine falcon "
                        "dives at 390 km/h, fastest animal alive.",
                "truncated": False}

    monkeypatch.setattr(bs, "op_web_search", fake_search)
    monkeypatch.setattr(bs, "op_web_fetch", fake_fetch)


def test_deep_research_reads_pages_and_saves_study(tmp_path, fake_web):
    app = ChatApp(tmp_path / "data", chain=CountingChain())
    op = _research_op(app.parts.memory._learned)
    out = op("peregrine falcon", depth=1)
    assert out["pages_read"] == 2
    assert out["saved_to"].startswith("research-peregrine-falcon-")
    hits = app.memory.recall("peregrine falcon fastest animal")
    assert any("390" in hit["text"] for hit in hits)


def test_deep_research_all_fetches_fail_is_honest(tmp_path,
                                                   monkeypatch):
    monkeypatch.setattr(bs, "op_web_search",
                        lambda q, limit=5: {"results": [
                            {"title": "T", "url": "https://x.example",
                             "snippet": ""}]})
    monkeypatch.setattr(bs, "op_web_fetch",
                        lambda url, max_chars=12000:
                        {"error": "web_fetch failed: timeout"})
    app = ChatApp(tmp_path / "data", chain=CountingChain())
    op = _research_op(app.parts.memory._learned)
    out = op("anything")
    assert "error" in out and out["fetch_failed"] == 1


def test_new_skills_are_advertised(tmp_path):
    app = ChatApp(tmp_path / "data", chain=CountingChain())
    names = {t["name"]
             for t in app.registry.advertised_tools(app.allowances)}
    assert {"deep_research", "run_code", "create_project"} <= names
