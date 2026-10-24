"""Self-grounding loop: four factual questions, asked on a loop,
recorded only when an answer changes. Doubt is honest, never
invented; the loop never raises and never spams."""
from types import SimpleNamespace

from jarvis.selfcheck import (QUESTIONS, changed, gather,
                              maybe_self_check)


class _Journal:
    def __init__(self):
        self.kinds = []

    def append(self, kind, payload=None):
        self.kinds.append(kind)
        return {}


def _app(tmp_path, builds=None, broken=False):
    journal = _Journal()
    if broken:
        def _boom():
            raise RuntimeError("no builds store")
        builds_fn = _boom
    else:
        builds_fn = lambda: builds or []
    return SimpleNamespace(data_dir=str(tmp_path), brain=None,
                           journal=journal, list_builds=builds_fn)


def test_four_questions_answered_from_facts(tmp_path):
    record = gather(_app(tmp_path))
    assert set(QUESTIONS) <= set(record)
    assert "JARVIS" in record["who"] and "program" in record["who"]
    assert str(tmp_path) in record["where"]
    assert "Jarvis-v4" in record["origin"]
    assert record["task"] == "koi pending build nahi"
    assert record["unknown"] == []


def test_pending_build_becomes_the_task(tmp_path):
    app = _app(tmp_path,
               builds=[{"id": "b7", "state": "awaiting_proceed"}])
    assert "b7" in gather(app)["task"]


def test_unanswerable_question_is_honest_unknown(tmp_path):
    record = gather(_app(tmp_path, broken=True))
    assert record["task"] == "pata nahi"
    assert "task" in record["unknown"]


def test_change_detection():
    base = {"who": "a", "where": "b", "origin": "c", "task": "d",
            "unknown": []}
    assert changed(None, base) is True
    assert changed(dict(base), dict(base)) is False
    assert changed(dict(base), {**base, "task": "e"}) is True


def test_loop_records_once_then_stays_quiet(tmp_path):
    app = _app(tmp_path)
    first = maybe_self_check(app)
    assert first is not None
    assert app.journal.kinds == ["self.grounding"]
    assert maybe_self_check(app) is None  # asked again, no change
    assert app.journal.kinds == ["self.grounding"]


def test_loop_speaks_again_when_the_task_changes(tmp_path):
    app = _app(tmp_path)
    assert maybe_self_check(app) is not None
    app.list_builds = lambda: [{"id": "b9", "state": "planned"}]
    assert maybe_self_check(app) is not None
    assert app.journal.kinds == ["self.grounding", "self.grounding"]


def test_broken_app_never_breaks_the_loop(tmp_path):
    app = _app(tmp_path, broken=True)
    first = maybe_self_check(app)  # records honest "pata nahi"
    assert first is not None and first["task"] == "pata nahi"
    assert maybe_self_check(app) is None  # stable: quiet after that
