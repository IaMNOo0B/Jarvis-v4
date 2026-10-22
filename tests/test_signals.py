"""Turn signals — Nova's reflexion warnings + curiosity gaps.

An honest stop becomes a retrievable warning fact; a hedged reply
queues a knowledge gap; the watch cycle's gap consumer researches
queued gaps and drops only the ones that actually got learned."""

import json
from types import SimpleNamespace

import jarvis.chat as chat_mod
from jarvis.chat import ChatApp, _process_gaps


class _Brain:
    def __init__(self, reply, stopped="completed"):
        self._reply = reply
        self._stopped = stopped

    def respond(self, history, text, correction=False):
        return SimpleNamespace(reply=self._reply,
                               stopped_reason=self._stopped,
                               tool_calls_made=[])


class _Chain:
    def complete(self, messages, tools=None):
        return {"content": "ok"}


def _app(tmp_path, brain):
    app = ChatApp(tmp_path)
    app.chain = _Chain()
    app.brain = brain
    return app


def test_honest_stop_stores_a_warning_fact(tmp_path):
    app = _app(tmp_path, _Brain("That didn't work, sir.",
                                stopped="honest_stop:loop_guard"))
    app.turn("spin the reports thrice")
    facts = app.memory.facts.current_facts()
    assert any(f["claim"].startswith("Warning:") for f in facts)
    hits = app.memory.recall("spin the reports", k=3)
    assert any("Warning" in h["text"] for h in hits)


def test_hedged_reply_queues_a_gap(tmp_path):
    app = _app(tmp_path, _Brain("I'm not sure about that, sir."))
    app.turn("What is the capital of Atlantis?")
    gaps = (tmp_path / "gaps.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(x) for x in gaps.splitlines() if x.strip()]
    assert rows and "Atlantis" in rows[0]["query"]


def test_confident_reply_queues_nothing(tmp_path):
    app = _app(tmp_path, _Brain("The capital is Canberra, sir."))
    app.turn("What is the capital of Australia?")
    assert not (tmp_path / "gaps.jsonl").exists()
    assert app.memory.facts.current_facts() == []


def test_gap_consumer_drops_only_successful_gaps(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "gaps.jsonl").write_text(
        json.dumps({"query": "atlantis capital", "ts": 1}) + "\n" +
        json.dumps({"query": "unfindable thing", "ts": 2}) + "\n",
        encoding="utf-8")

    def fake_research_op(_learned):
        def research(topic, depth=1):
            if "atlantis" in topic:
                return {"ok": True, "sources_found": 3}
            return {"error": "no sources"}
        return research

    monkeypatch.setattr(chat_mod, "_research_op", fake_research_op)
    done = _process_gaps(object(), data)
    assert done == 1
    rows = [json.loads(x) for x in
            (data / "gaps.jsonl").read_text(encoding="utf-8")
            .splitlines() if x.strip()]
    assert len(rows) == 1 and "unfindable" in rows[0]["query"]
