"""GAN rubric loop (ECC Wave B): the plan must declare
what DONE means (a rubric) BEFORE building; after
execution a separate evaluator scores every criterion
against the evidence; unmet criteria buy focused repair
rounds (max 2); 'done' requires binding + verification +
the full rubric. Confidence folds in the met-ratio."""

import json
import re
import time
from pathlib import Path

from jarvis.builds import (computed_confidence, render_plan_md,
                           validate_plan)
from jarvis.chat import ChatApp

PLAN = {
    "goal": "ek chhota calculator banao",
    "review_notes": [],
    "changes": [{"path": "calc.py", "action": "NEW",
                 "what": "calculator module"},
                {"path": "test_calc.py", "action": "NEW",
                 "what": "uske tests"}],
    "steps": ["calc.py likho", "test_calc.py likho"],
    "verification": [{"kind": "check",
                      "detail": "calc.py me add function hai"}],
    "rubric": [{"name": "add function kaam karta hai",
                "pass_when": "calc.py me add(a, b) hai aur "
                             "test pass hota hai"},
               {"name": "koi extra file nahi",
                "pass_when": "workspace me sirf calc.py aur "
                             "test_calc.py hain"}],
}


def test_plan_without_rubric_is_rejected():
    bare = {k: v for k, v in PLAN.items() if k != "rubric"}
    problems = validate_plan(bare)
    assert any("rubric" in p for p in problems)
    hollow = dict(PLAN, rubric=[{"name": "x"}])
    problems = validate_plan(hollow)
    assert any("pass_when" in p for p in problems)
    assert validate_plan(PLAN) == []


def test_plan_md_shows_the_rubric():
    md = render_plan_md(PLAN)
    assert "## Rubric" in md
    assert "add function kaam karta hai" in md


def test_confidence_folds_in_rubric_ratio():
    binding = {"done": ["[NEW] calc.py"], "missing": [],
               "extras": []}
    score, why = computed_confidence(
        [{"ok": True}], binding,
        [{"name": "a", "met": True},
         {"name": "b", "met": False}])
    # 0.4 verify + 0.4 binding + 0.2 * (1/2 rubric)
    assert score == 0.9
    assert "rubric 1/2" in why
    # Two-arg behaviour is untouched (old builds, old tests).
    old, _ = computed_confidence([{"ok": True}], binding)
    assert old == 1.0


class _Chain:
    """Planner + checks + a rubric evaluator whose verdict
    sequence the test controls."""

    def __init__(self, rubric_verdicts):
        self.rubric_verdicts = list(rubric_verdicts)
        self.repairs_seen = 0

    def complete(self, messages, tools=None):
        text = json.dumps(messages, ensure_ascii=False)
        if "planner ho" in text:
            return {"content": json.dumps(PLAN),
                    "tool_calls": []}
        if "RUBRIC (kasauti)" in text:
            verdict = self.rubric_verdicts.pop(0) \
                if self.rubric_verdicts else True
            criteria = [
                {"name": c["name"], "met": verdict,
                 "note": "saboot dekha" if verdict
                         else "saboot adhoora hai"}
                for c in PLAN["rubric"]]
            return {"content": json.dumps(
                        {"criteria": criteria}),
                    "tool_calls": []}
        if "JAANCH" in text:
            return {"content": '{"ok": true, "note": '
                               '"add maujood hai"}',
                    "tool_calls": []}
        return {"content": "ok", "tool_calls": []}


def _run_build(tmp_path, monkeypatch, chain):
    import jarvis.chat as chat_mod

    def fake_delegated(parts, work):
        if "BUILD REPAIR" in work:
            chain.repairs_seen += 1
            return "adhoora hissa theek kar diya"
        ws = re.search(r"WORKSPACE: (\S+)", work).group(1)
        step = re.search(r"Sirf ye kadam pura karo: (.+)",
                         work).group(1)
        if step.startswith("calc.py"):
            (Path(ws) / "calc.py").write_text(
                "def add(a, b):\n    return a + b\n")
        if step.startswith("test_calc.py"):
            (Path(ws) / "test_calc.py").write_text(
                "from calc import add\n"
                "assert add(2, 3) == 5\n")
        return "kadam ho gaya"

    monkeypatch.setattr(chat_mod, "_delegated_summary",
                        fake_delegated)
    app = ChatApp(tmp_path, chain=chain)
    out = app.execute_tool(
        "build_task", {"goal": PLAN["goal"],
                       "mode": "planning"})
    assert out["status"] == "awaiting_proceed", out
    build_id = out["build_id"]
    assert app.proceed_build(build_id)["ok"] is True
    state_path = tmp_path / "builds" / build_id / "state.json"
    deadline = time.time() + 30
    while time.time() < deadline:
        state = json.loads(
            state_path.read_text(encoding="utf-8"))
        if state["status"] in ("done", "failed"):
            return app, state
        time.sleep(0.1)
    raise AssertionError(f"build khatam nahi hua: {state}")


def test_all_criteria_met_means_done(tmp_path, monkeypatch):
    chain = _Chain([True])
    app, state = _run_build(tmp_path, monkeypatch, chain)
    assert state["status"] == "done", state
    assert state["confidence"] == 1.0
    assert chain.repairs_seen == 0
    walk = (tmp_path / "builds" / state["id"]
            / "walkthrough.md").read_text(encoding="utf-8")
    assert "## Rubric" in walk
    assert "[x] add function kaam karta hai" in walk


def test_unmet_criterion_buys_a_repair_round(
        tmp_path, monkeypatch):
    chain = _Chain([False, True])
    app, state = _run_build(tmp_path, monkeypatch, chain)
    assert state["status"] == "done", state
    assert chain.repairs_seen == 1
    assert state["repairs"] == 1
    walk = (tmp_path / "builds" / state["id"]
            / "walkthrough.md").read_text(encoding="utf-8")
    assert "Repair rounds: 1" in walk


def test_still_unmet_after_repairs_fails_with_table(
        tmp_path, monkeypatch):
    chain = _Chain([False, False, False])
    app, state = _run_build(tmp_path, monkeypatch, chain)
    assert state["status"] == "failed", state
    assert chain.repairs_seen == 2
    assert "rubric adhoora" in state["note"]
    assert state["confidence"] < 1.0
    walk = (tmp_path / "builds" / state["id"]
            / "walkthrough.md").read_text(encoding="utf-8")
    assert "## Rubric" in walk
    assert "[!] add function kaam karta hai" in walk
    assert "saboot adhoora hai" in walk
