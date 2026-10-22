"""The team mechanism (Dodaios's coordination): ownership is
exclusive or the plan dies; workers write in isolation; the
critic is a separate judge; repair is BOUNDED; the deliverable
is verified by execution."""

from pathlib import Path

from jarvis.chat import ChatApp
from jarvis.team import run_team, validate_plan


def _writer(name_to_content):
    def worker(task, subtask, workspace, issues):
        for name, content in name_to_content.items():
            if name in subtask["files"]:
                path = Path(workspace) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
        return "ban gaya"
    return worker


def test_overlapping_ownership_kills_the_plan():
    plan, error = validate_plan({"subtasks": [
        {"title": "a", "files": ["x.py"], "brief": ""},
        {"title": "b", "files": ["x.py"], "brief": ""}]})
    assert plan is None and "x.py" in error


def test_team_happy_path_merges_and_verifies(tmp_path):
    planner = lambda task: {"subtasks": [
        {"title": "engine", "files": ["engine.py"],
         "brief": "add function"},
        {"title": "cli", "files": ["cli.py"],
         "brief": "print it"}]}
    worker = _writer({
        "engine.py": "def add(a, b):\n    return a + b\n",
        "cli.py": "from engine import add\nprint(add(1, 2))\n"})
    critic = lambda t, s, content, summary: {"ok": True}
    report = run_team(tmp_path, "calculator banao",
                      planner, worker, critic)
    assert report["ok"] is True
    out = Path(report["output_dir"])
    assert (out / "engine.py").exists() and (out / "cli.py").exists()
    assert report["verification"]["ok"] is True


def test_repair_is_bounded_and_failure_is_honest(tmp_path):
    planner = lambda task: {"subtasks": [
        {"title": "solo", "files": ["a.py"], "brief": ""}]}
    calls = {"n": 0}

    def worker(task, subtask, workspace, issues):
        calls["n"] += 1
        (Path(workspace) / "a.py").write_text("x = 1\n")
        return "koshish"

    critic = lambda t, s, c, summary: {
        "ok": False, "issues": "hamesha kharab"}
    report = run_team(tmp_path, "kuch banao",
                      planner, worker, critic)
    assert report["ok"] is False
    sub = report["subtasks"][0]
    assert sub["ok"] is False and sub["rounds"] == 3  # 1 + 2 repairs
    assert calls["n"] == 3
    assert "kharab" in sub["issues"]


def test_repair_recovers_when_the_worker_listens(tmp_path):
    planner = lambda task: {"subtasks": [
        {"title": "solo", "files": ["a.py"], "brief": ""}]}
    state = {"fixed": False}

    def worker(task, subtask, workspace, issues):
        if issues:
            state["fixed"] = True
        (Path(workspace) / "a.py").write_text("x = 2\n")
        return "ho gaya"

    def critic(t, s, c, summary):
        return {"ok": state["fixed"],
                "issues": "" if state["fixed"] else "pehle theek karo"}
    report = run_team(tmp_path, "theek wala banao",
                      planner, worker, critic)
    assert report["ok"] is True
    assert report["subtasks"][0]["rounds"] == 2


def test_app_advertises_delegate_team(tmp_path):
    app = ChatApp(tmp_path)
    names = {t["name"] for t in
             app.registry.advertised_tools(app.allowances)}
    assert "delegate_team" in names
    out = app.execute_tool("delegate_team", {"task": "  "})
    assert out["ok"] is False
