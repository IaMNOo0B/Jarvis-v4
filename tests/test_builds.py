"""Antigravity's Planning Mode, JARVIS-bound: a plan with
the fixed schema (changes tagged NEW/MODIFY/DELETE, steps,
a Verification Plan) or it is refused; the Proceed gate
holds execution until the owner says go; execution ticks a
live task.md; and the plan is BOUND to the real tree diff —
planned work that never happened is reported missing, and
the confidence is computed, never vibes."""

import json
import time

from jarvis.builds import (BuildStore, binding_report,
                           computed_confidence, diff_trees,
                           render_plan_md, snapshot_tree,
                           validate_plan)
from jarvis.chat import ChatApp, _run_build_app

PLAN = {
    "goal": "ek chhota calculator banao",
    "review_notes": ["koi khatra nahi"],
    "changes": [{"path": "calc.py", "action": "NEW",
                 "what": "calculator module"},
                {"path": "test_calc.py", "action": "NEW",
                 "what": "uske tests"}],
    "steps": ["calc.py likho", "test_calc.py likho"],
    "verification": [{"kind": "command",
                      "detail": "python3 -c \"print(42)\""},
                     {"kind": "check",
                      "detail": "calc.py me add function hai"}],
}


def test_plan_schema_is_enforced():
    assert validate_plan(PLAN) == []
    no_verify = dict(PLAN, verification=[])
    assert any("verification" in p
               for p in validate_plan(no_verify))
    bad_action = dict(PLAN, changes=[{
        "path": "x.py", "action": "EDIT", "what": "w"}])
    assert any("action" in p for p in validate_plan(bad_action))
    escape = dict(PLAN, changes=[{
        "path": "../evil.py", "action": "NEW", "what": "w"}])
    assert any("path" in p for p in validate_plan(escape))
    md = render_plan_md(PLAN)
    assert "[NEW] `calc.py`" in md and "Verification Plan" in md


def test_binding_reports_missing_and_extras(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    before = snapshot_tree(ws)
    (ws / "calc.py").write_text("def add(a, b): return a + b")
    (ws / "surprise.txt").write_text("plan me nahi tha")
    diff = diff_trees(before, snapshot_tree(ws))
    rep = binding_report(PLAN, diff)
    assert rep["done"] == ["[NEW] calc.py"]
    assert rep["missing"] == ["[NEW] test_calc.py"]
    assert rep["extras"] == ["surprise.txt"]
    conf, why = computed_confidence(
        [{"ok": True}, {"ok": False}], rep)
    # Base 0.5 (verify 1/2, binding 1/2), diluted by the one
    # unplanned extra: x planned/(planned+extras) = 2/3
    # (audit 2026-10-22 — extras used to cost nothing).
    assert conf == 0.33 and "verification 1/2" in why
    assert "bina-plan" in why


class _Chain:
    """Planner + worker + critic in one scripted body."""

    def complete(self, messages, tools=None):
        text = json.dumps(messages)
        if "planner ho" in text:
            return {"content": json.dumps(PLAN),
                    "tool_calls": []}
        if "JAANCH" in text:
            return {"content": '{"ok": true, "note": '
                               '"add function maujood hai"}',
                    "tool_calls": []}
        if "BUILD TASK" in text:
            return {"content": "kadam pura",
                    "tool_calls": [{
                        "name": "create_project",
                        "arguments": {
                            "path": "PLACEHOLDER",
                            "files": []}}]}
        return {"content": "ok", "tool_calls": []}


def test_planning_gate_then_proceed_builds(tmp_path,
                                           monkeypatch):
    import jarvis.chat as chat_mod

    def fake_delegated(parts, work):
        # The worker writes the planned files for real. The
        # CURRENT step is named after "Sirf ye kadam" — the
        # whole plan rides in every step's text, so keying on
        # mere mentions would write the wrong file.
        import re
        ws = re.search(r"WORKSPACE: (\S+)", work).group(1)
        step = re.search(r"Sirf ye kadam pura karo: (.+)",
                         work).group(1)
        from pathlib import Path
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
    app = ChatApp(tmp_path, chain=_Chain())
    out = app.execute_tool("build_task",
                           {"goal": PLAN["goal"],
                            "mode": "planning"})
    assert out["status"] == "awaiting_proceed"
    build_id = out["build_id"]
    plan_text = app.build_plan_text(build_id)
    assert "[NEW] `calc.py`" in plan_text
    got = app.proceed_build(build_id)
    assert got["ok"] is True
    deadline = time.time() + 30
    state = None
    while time.time() < deadline:
        state = app.list_builds()[0]
        if state["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert state["status"] == "done", state
    assert state["confidence"] == 1.0
    walk = (tmp_path / "builds" / build_id
            / "walkthrough.md").read_text(encoding="utf-8")
    assert "Confidence: 1.0" in walk
    assert "NAHI hue: —" in walk
    task = (tmp_path / "builds" / build_id
            / "task.md").read_text(encoding="utf-8")
    assert task.count("- [x]") == 2


def test_deny_retires_the_build(tmp_path):
    app = ChatApp(tmp_path, chain=_Chain())
    out = app.execute_tool("build_task",
                           {"goal": PLAN["goal"],
                            "mode": "planning"})
    got = app.proceed_build(out["build_id"], approve=False)
    assert got["status"] == "denied"
    again = app.proceed_build(out["build_id"])
    assert again["ok"] is False
