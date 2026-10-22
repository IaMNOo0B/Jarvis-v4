"""Swarm front-end (Sentient Spec 4): the Extractor pulls
the discrete item list out of a free-form request; the
Dispatcher groups items by handling with a self-contained
instruction and the minimal tool set per group; code
validates the plan (indices in range, each item exactly
once, tools real) with one repair round; execution rides
the existing fleet/batch path with per-group tool
restriction."""

import json
import time
from types import SimpleNamespace

from jarvis import swarm as swarm_mod
from jarvis.chat import ChatApp, _run_swarm
from jarvis.journal import Journal


class _FakeBrain:
    def __init__(self, replies):
        self.replies = list(replies)

    def respond(self, history, text):
        reply = self.replies.pop(0) if self.replies \
            else "{}"
        return SimpleNamespace(reply=reply)


def _factory(*replies):
    brain = _FakeBrain(replies)
    return lambda: brain


def test_extractor_pulls_items_in_order():
    items = swarm_mod.extract_items(
        _factory('{"items": ["pehla", "doosra", "teesra"]}'),
        "in teenon par kaam karo: pehla, doosra, teesra")
    assert items == ["pehla", "doosra", "teesra"]


def test_extractor_empty_is_honest_and_failure_is_none():
    assert swarm_mod.extract_items(
        _factory('{"items": []}'), "kuch karo") == []
    assert swarm_mod.extract_items(
        _factory("json nahi hai"), "kuch karo") is None


def test_dispatcher_groups_and_validates():
    items = ["a", "b", "c"]
    plan = json.dumps({"groups": [
        {"item_indices": [0, 1], "instruction": "padho: "
         "{item}", "tools": ["web_search"]},
        {"item_indices": [2], "instruction": "ginno: "
         "{item}", "tools": []}]})
    groups = swarm_mod.dispatch_plan(
        _factory(plan), "goal", items,
        {"web_search", "time_now"})
    assert groups is not None and len(groups) == 2
    jobs = swarm_mod.jobs_for_groups(groups, items)
    assert [t for t, _ in jobs] == ["padho: a", "padho: b",
                                    "ginno: c"]
    assert jobs[0][1] == ["web_search"]
    assert jobs[2][1] == []


def test_dispatcher_rejects_bad_plans_then_repairs():
    items = ["a", "b"]
    bad = json.dumps({"groups": [
        {"item_indices": [0, 0, 9],
         "instruction": "x {item}",
         "tools": ["ghost_tool"]}]})
    good = json.dumps({"groups": [
        {"item_indices": [0, 1], "instruction": "x {item}",
         "tools": []}]})
    groups = swarm_mod.dispatch_plan(
        _factory(bad, good), "goal", items, {"time_now"})
    assert groups is not None
    # Two bad rounds -> honest failure, not a half-plan.
    assert swarm_mod.dispatch_plan(
        _factory(bad, bad), "goal", items,
        {"time_now"}) is None


def test_validate_groups_names_every_problem():
    problems = swarm_mod.validate_groups(
        [{"item_indices": [0, 3], "instruction": "",
          "tools": ["nope"]}],
        ["a", "b"], {"time_now"})
    joined = " ".join(problems)
    assert "hadd se bahar" in joined
    assert "instruction khaali" in joined
    assert "registry me nahi" in joined
    assert "ek baar assign" in joined


# -- the full pipeline over a fake app --------------------- #

class _Chain:
    def __init__(self, script=()):
        self.script = list(script)

    def complete(self, messages, tools=None):
        if self.script:
            return self.script.pop(0)
        return {"content": "Ho gaya, sir."}


class _Registry:
    def advertised_tools(self, allowances):
        return [{"name": "time_now", "skill": "core.time",
                 "permission": "clock.read",
                 "destructive": False,
                 "schema": {"type": "object", "properties": {},
                            "additionalProperties": False}}]


def test_run_swarm_end_to_end(tmp_path):
    # Chain script: extractor JSON, dispatcher JSON, then
    # one reply per fleet worker.
    script = [
        {"content": '{"items": ["ek", "do"]}'},
        {"content": '{"groups": [{"item_indices": [0, 1], '
                    '"instruction": "ginno: {item}", '
                    '"tools": ["time_now"]}]}'},
        {"content": "ho gaya ek"},
        {"content": "ho gaya do"},
    ]
    parts = SimpleNamespace(
        chain=_Chain(script), registry=_Registry(),
        dispatch=lambda name, args=None: {"ok": True},
        worker_dispatch=None,
        journal=Journal(str(tmp_path / "journal.jsonl")),
        memory=None, allowances=frozenset(),
        rules_text="")
    parts.worker_dispatch = parts.dispatch
    _run_swarm(parts, tmp_path, "sw1",
               "in par ginno wala kaam karo: ek, do")
    rows = [json.loads(x) for x in (tmp_path / "tasks.jsonl")
            .read_text(encoding="utf-8").splitlines()]
    item_rows = [r for r in rows if r["id"].startswith("sw1-")]
    assert len(item_rows) == 2
    notes = (tmp_path / "notifications.jsonl").read_text(
        encoding="utf-8")
    assert "Batch report" in notes


def test_run_swarm_stops_honestly_without_items(tmp_path):
    parts = SimpleNamespace(
        chain=_Chain([{"content": '{"items": []}'}]),
        registry=_Registry(),
        dispatch=lambda name, args=None: {"ok": True},
        journal=Journal(str(tmp_path / "journal.jsonl")),
        memory=None, allowances=frozenset(), rules_text="")
    _run_swarm(parts, tmp_path, "sw2", "kuch bhi karo")
    notes = (tmp_path / "notifications.jsonl").read_text(
        encoding="utf-8")
    assert "Swarm ruka" in notes
    assert not (tmp_path / "tasks.jsonl").exists()


def test_swarm_command_starts_pipeline(tmp_path):
    app = ChatApp(tmp_path, chain=_Chain())
    out = app.handle_command("/swarm ye teen kaam karo")
    assert "Swarm shuru" in out
    time.sleep(0.3)   # the pipeline stops honestly (stub
    # chain yields no items JSON) without touching tasks
    notes = app.data_dir / "notifications.jsonl"
    if notes.exists():
        assert "Swarm" in notes.read_text(encoding="utf-8")
