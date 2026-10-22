"""delegate_task — the Spark pattern: hand a job to a second brain
on a background thread; the conversation never blocks; the result
arrives as a notification and is listed by /tasks."""

import json
import time
from types import SimpleNamespace

from jarvis.chat import (ChatApp, _delegate_op, _run_delegated,
                         _set_stopped)
from jarvis.journal import Journal


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
                 "permission": "clock.read", "destructive": False,
                 "schema": {"type": "object", "properties": {},
                            "additionalProperties": False}}]


def _parts(tmp_path, script=()):
    return SimpleNamespace(
        chain=_Chain(script), registry=_Registry(),
        dispatch=lambda name, args=None: {"ok": True},
        journal=Journal(str(tmp_path / "journal.jsonl")),
        memory=None, allowances=frozenset())


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_run_delegated_completes_and_notifies(tmp_path):
    # The script first CALLS a tool, then reports — v4's completion
    # audit rewrites any "done" claim that no tool call backs up.
    parts = _parts(tmp_path, [
        {"content": "", "tool_calls": [
            {"id": "c1", "name": "time_now", "arguments": {}}]},
        {"content": "Report ban gaya, sir."}])
    _run_delegated(parts, tmp_path, "t1", "make the weekly report")
    rows = [json.loads(x) for x in (tmp_path / "tasks.jsonl")
            .read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["status"] == "done"
    assert "Report ban gaya" in rows[-1]["result"]
    notes = (tmp_path / "notifications.jsonl").read_text(
        encoding="utf-8")
    assert "Background task done" in notes


def test_delegate_op_spawns_and_reports(tmp_path):
    # A real script: one tool call, then the report. (The old
    # default — a bare "Ho gaya" with no tool call — is an
    # honest_stop under the completion audit, and the task
    # log now says so instead of "done": batch-7 honesty.)
    holder = {"parts": _parts(tmp_path, [
        {"content": "", "tool_calls": [
            {"id": "c1", "name": "time_now", "arguments": {}}]},
        {"content": "Report ban gaya, sir."}])}
    op = _delegate_op(holder, tmp_path)
    out = op(task="find the cheapest flight options")
    assert out["ok"] is True and out["task_id"]
    assert _wait_for(lambda: any(
        json.loads(x).get("status") == "done"
        for x in (tmp_path / "tasks.jsonl")
        .read_text(encoding="utf-8").splitlines()))


def test_delegate_op_refuses_when_unwired_or_stopped(tmp_path):
    op = _delegate_op({}, tmp_path)
    assert op(task="x")["ok"] is False
    holder = {"parts": _parts(tmp_path)}
    op = _delegate_op(holder, tmp_path)
    _set_stopped(tmp_path, True)
    assert op(task="x")["ok"] is False


def test_app_advertises_delegate_and_lists_tasks(tmp_path):
    app = ChatApp(tmp_path)
    names = {t["name"] for t in
             app.registry.advertised_tools(app.allowances)}
    assert "delegate_task" in names
    (tmp_path / "tasks.jsonl").write_text(
        json.dumps({"id": "ab12", "task": "research X",
                    "status": "done", "ts": 1}) + "\n",
        encoding="utf-8")
    out = app.handle_command("/tasks")
    assert "ab12" in out and "done" in out
