"""Run Reporter (Sentient Spec 5): every substantial run
ends with one structured report {summary, done, failed,
files, links, tools_used, open} written from the run's
real step log and the code-side outcome facts — never
from the worker's self-assessment. Failed steps are
reported failed even when a narration claims success;
files/links are copied only from the evidence; with no
usable brain the deterministic report still lands, and a
run that produced tool results but no model answer gets
a plain findings line (section 7.6)."""

import json
from types import SimpleNamespace

import jarvis.chat as chat_mod
from jarvis.chat import ChatApp, _report_for_run, _run_delegated
from jarvis.journal import Journal
from jarvis.reporter import (build_report,
                             deterministic_report,
                             findings_line, render_text,
                             steps_from_records)


class _FakeBrain:
    def __init__(self, reply):
        self._reply = reply

    def respond(self, history, text):
        return SimpleNamespace(reply=self._reply)


def _steps():
    return [
        {"label": "web_search", "tool": "web_search",
         "ok": True, "error": None},
        {"label": "file_write", "tool": "file_write",
         "ok": False, "error": "disk full"},
    ]


def test_deterministic_report_counts_and_failures():
    report = deterministic_report(
        "goal", _steps(),
        {"status": "failed", "detail": "worker ruka"})
    assert report["source"] == "deterministic"
    assert report["done"] == ["web_search"]
    assert len(report["failed"]) == 1
    assert "file_write" in report["failed"][0]
    assert "disk full" in report["failed"][0]
    assert report["tools_used"] == ["web_search",
                                    "file_write"]


def test_brain_report_cannot_hide_a_failure():
    lying = json.dumps({
        "summary": "Sab kuch ho gaya, ekdum safal.",
        "done": ["web_search", "file_write"],
        "failed": [], "files": [], "links": [],
        "open": []})
    report = build_report(lambda: _FakeBrain(lying),
                          goal="goal", steps=_steps(),
                          outcome={"status": "failed",
                                   "detail": ""})
    assert report["source"] == "brain"
    # Code-side charter: the failed step is reported
    # failed even though the narration claimed success.
    assert any("file_write" in f for f in report["failed"])


def test_brain_report_cannot_invent_files_or_links():
    inventing = json.dumps({
        "summary": "Ho gaya.",
        "done": [], "failed": [],
        "files": ["ghost/never-made.md"],
        "links": ["https://invented.example.com/x"],
        "open": []})
    report = build_report(
        lambda: _FakeBrain(inventing), goal="goal",
        steps=_steps(),
        outcome={"status": "done",
                 "detail": "asal file bani: real/out.md "
                           "dekho https://real.example.com/a"})
    assert "ghost/never-made.md" not in report["files"]
    assert "https://invented.example.com/x" \
        not in report["links"]
    assert "real/out.md" in report["files"]
    assert "https://real.example.com/a" in report["links"]


def test_brain_down_falls_back_deterministically():
    def _boom():
        raise RuntimeError("chain down")

    report = build_report(_boom, goal="goal", steps=_steps(),
                          outcome={"status": "done",
                                   "detail": "x"})
    assert report["source"] == "deterministic"
    assert report["done"] == ["web_search"]
    report2 = build_report(lambda: _FakeBrain("json nahi"),
                           goal="goal", steps=_steps(),
                           outcome={"status": "done",
                                    "detail": "x"})
    assert report2["source"] == "deterministic"


def test_findings_line_from_records():
    records = [
        SimpleNamespace(tool="web_search", ok=True,
                        error=None),
        SimpleNamespace(tool="file_write", ok=False,
                        error="permission"),
    ]
    line = findings_line(records)
    assert "web_search" in line and "file_write" in line
    assert findings_line([]) == ""


def test_steps_from_records_shape():
    records = [SimpleNamespace(tool="time_now", ok=True,
                               error=None)]
    steps = steps_from_records(records)
    assert steps == [{"label": "time_now", "tool": "time_now",
                      "ok": True, "error": None}]


# -- wiring: delegated runs carry the report -------------- #

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


def test_run_delegated_stores_report_and_tokens(tmp_path):
    parts = SimpleNamespace(
        chain=_Chain([
            {"content": "", "tool_calls": [
                {"id": "c1", "name": "time_now",
                 "arguments": {}}]},
            {"content": "Report ban gaya, sir."}]),
        registry=_Registry(),
        dispatch=lambda name, args=None: {"ok": True},
        journal=Journal(str(tmp_path / "journal.jsonl")),
        memory=None, allowances=frozenset())
    _run_delegated(parts, tmp_path, "t9", "weekly report banao")
    rows = [json.loads(x) for x in (tmp_path / "tasks.jsonl")
            .read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["status"] == "done"
    assert "report" in rows[-1]
    assert rows[-1]["report"]["summary"]
    assert "time_now" in rows[-1]["report"]["tools_used"]
    notes = (tmp_path / "notifications.jsonl").read_text(
        encoding="utf-8")
    assert "Background task done" in notes


def test_report_for_run_never_raises(tmp_path):
    parts = SimpleNamespace(chain=_Chain(),
                            registry=_Registry(),
                            dispatch=lambda n, a=None:
                            {"ok": True},
                            journal=Journal(
                                str(tmp_path / "j.jsonl")),
                            memory=None,
                            allowances=frozenset())
    report = _report_for_run(parts, "goal", _steps(),
                             {"status": "done", "detail": "x"})
    assert report["summary"]
    assert render_text(report)
