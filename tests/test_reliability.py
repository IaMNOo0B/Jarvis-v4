"""Wave 4.6 — NOVA's reliability pack, proven: schema validation
refuses bad calls BEFORE the op; the rate limiter walls a
runaway tool; memory holds under parallel hands."""

import threading

import jarvis.skills as skills_mod
from jarvis.chat import ChatApp
from jarvis.memory import Memory
from jarvis.validate import validate_args

SCHEMA = {"type": "object",
          "properties": {
              "text": {"type": "string", "minLength": 2},
              "count": {"type": "integer", "minimum": 1,
                        "maximum": 9}},
          "required": ["text"],
          "additionalProperties": False}


def test_validator_names_the_guilty_field():
    assert validate_args(SCHEMA, {"text": "ok"}) == ""
    assert "text" in validate_args(SCHEMA, {})
    assert "text" in validate_args(SCHEMA, {"text": "x"})
    assert "count" in validate_args(
        SCHEMA, {"text": "ok", "count": "three"})
    assert "count" in validate_args(
        SCHEMA, {"text": "ok", "count": 42})
    assert "extra" in validate_args(
        SCHEMA, {"text": "ok", "extra": 1})
    assert validate_args(None, {"anything": 1}) == ""


def test_dispatch_refuses_before_the_op_runs(tmp_path):
    import pytest
    from jarvis.skills import DispatchRefused
    app = ChatApp(tmp_path)
    # The app path refuses bad args before the op (gate's own
    # argument check is the first wall)...
    bad = app.execute_tool("track_add", {"table": "leads"})
    assert bad["ok"] is False
    assert "invalid arguments" in str(bad["error"]) \
        or "schema" in str(bad["error"])
    assert "record" in str(bad["error"])
    # ...and the registry's schema check is the second wall,
    # for anything that reaches dispatch directly.
    with pytest.raises(DispatchRefused) as caught:
        app.registry.dispatch("track_add", {"table": "leads"})
    assert "schema" in str(caught.value)
    with pytest.raises(DispatchRefused):
        app.registry.dispatch("track_add", {
            "table": "leads", "record": "not-a-dict"})


def test_rate_limiter_walls_a_runaway(tmp_path, monkeypatch):
    import pytest
    from jarvis.skills import DispatchRefused
    monkeypatch.setattr(skills_mod, "RATE_LIMIT", 5)
    app = ChatApp(tmp_path)
    done = 0
    for _ in range(7):
        try:
            app.registry.dispatch("time_now", {})
            done += 1
        except DispatchRefused as exc:
            assert "rate limit" in str(exc)
    assert done == 5
    assert app.registry.rate_stats().get("time_now") == 5


def test_memory_is_thread_safe_under_parallel_hands(tmp_path):
    memory = Memory(tmp_path)
    errors = []

    def hand(n):
        try:
            for i in range(30):
                memory.learn(f"haath {n} ki baat {i} pune")
                memory.recall(f"haath {n}", k=2)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=hand, args=(n,))
               for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    hits = memory.recall("haath 3 ki baat", k=3)
    assert hits
