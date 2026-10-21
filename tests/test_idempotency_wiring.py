"""Wiring tests: the idempotency ledger inside make_dispatch.

The Dodaios audit found jarvis/idempotency.py built and tested but
referenced by NOTHING — destructive effects could repeat freely.
These tests pin the wiring: a destructive tool's identical call
executes ONCE per session; the replay returns the recorded result;
a key left PENDING by a crash is never auto-retried; non-destructive
tools are untouched.
"""

from types import SimpleNamespace

from jarvis.chat import make_dispatch
from jarvis.idempotency import IdempotencyLedger, business_key
from jarvis.journal import Journal


class _Gate:
    def evaluate(self, name, args, origin, allowances):
        return SimpleNamespace(verdict="allow", reason="")


class _Registry:
    def __init__(self):
        self.calls = []

    def advertised_tools(self, allowances):
        return [{"name": "danger", "destructive": True},
                {"name": "peek", "destructive": False}]

    def dispatch(self, name, args, decision=None):
        self.calls.append((name, dict(args)))
        return {"ok": True, "did": name, "n": len(self.calls)}


def _setup(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    ledger = IdempotencyLedger(journal)
    registry = _Registry()
    dispatch = make_dispatch(_Gate(), registry, journal, set(),
                             ledger=ledger, session_id="s-test")
    return journal, ledger, registry, dispatch


def test_destructive_call_executes_once_then_replays(tmp_path):
    _, _, registry, dispatch = _setup(tmp_path)
    first = dispatch("danger", {"x": 1})
    second = dispatch("danger", {"x": 1})
    assert first["ok"] and second["ok"]
    assert len(registry.calls) == 1          # effect ran exactly once
    assert second == first                   # recorded result returned


def test_different_args_are_a_different_action(tmp_path):
    _, _, registry, dispatch = _setup(tmp_path)
    dispatch("danger", {"x": 1})
    dispatch("danger", {"x": 2})
    assert len(registry.calls) == 2


def test_non_destructive_tools_are_not_guarded(tmp_path):
    _, _, registry, dispatch = _setup(tmp_path)
    dispatch("peek", {"x": 1})
    dispatch("peek", {"x": 1})
    assert len(registry.calls) == 2


def test_pending_key_is_never_auto_retried(tmp_path):
    journal, ledger, registry, dispatch = _setup(tmp_path)
    key = business_key("s-test", {"tool": "danger", "args": {"x": 9}})
    ledger.begin(key, {"tool": "danger", "args": {"x": 9}})
    result = dispatch("danger", {"x": 9})
    assert result["ok"] is False
    assert result.get("needs_decision") is True
    assert registry.calls == []              # effect never ran


def test_replay_is_journaled(tmp_path):
    journal, _, _, dispatch = _setup(tmp_path)
    dispatch("danger", {"x": 1})
    dispatch("danger", {"x": 1})
    entries = [e for e in journal.replay()
               if e["kind"] == "tool.result"]
    assert entries[-1]["payload"].get("idempotent_replay") is True


def test_no_ledger_keeps_old_behaviour(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    registry = _Registry()
    dispatch = make_dispatch(_Gate(), registry, journal, set())
    dispatch("danger", {"x": 1})
    dispatch("danger", {"x": 1})
    assert len(registry.calls) == 2
