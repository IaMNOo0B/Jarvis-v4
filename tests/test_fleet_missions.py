"""Fleet + autopilot: hundreds of jobs flow concurrently with
results in order and failures contained; a mission walks
itself to done / stuck / budget / stopped without pestering
anyone in between; the app wiring resolves its calibration
decisions when the mission lands."""

import threading
import time

import jarvis.chat as chat_mod
from jarvis.autopilot import run_mission
from jarvis.calibration import Calibrator
from jarvis.chat import ChatApp
from jarvis.fleet import Fleet


# -- fleet -----------------------------------------------------

def test_fleet_order_peak_and_failure_containment():
    fleet = Fleet(max_workers=16)

    def fn(i):
        time.sleep(0.02)
        if i == 13:
            raise ValueError("takleef wala kaam")
        return i * 2

    results = fleet.run_many(range(40), fn)
    assert len(results) == 40
    assert [r["result"] for r in results if r["ok"]] == \
        [i * 2 for i in range(40) if i != 13]
    assert results[13]["ok"] is False
    assert fleet.peak >= 4                    # overlap is measured
    assert fleet.stats()["completed"] == 40


def test_fleet_ceiling_is_300():
    assert Fleet(10_000).max_workers == 300
    assert Fleet(0).max_workers == 1


# -- autopilot loop -------------------------------------------

def test_mission_walks_to_done():
    checks = iter([{"state": "continue", "confidence": 0.8},
                   {"state": "done", "confidence": 0.95,
                    "note": "ho gaya"}])
    out = run_mission("goal", lambda g, log, n: f"kadam {n}",
                      lambda g, log: next(checks), max_steps=5)
    assert out["state"] == "done" and out["steps_taken"] == 2


def test_mission_budget_and_stop():
    out = run_mission("goal", lambda g, log, n: "kadam",
                      lambda g, log: {"state": "continue"},
                      max_steps=3)
    assert out["state"] == "budget" and out["steps_taken"] == 3
    out = run_mission("goal", lambda g, log, n: "kadam",
                      lambda g, log: {"state": "continue"},
                      should_stop=lambda: True)
    assert out["state"] == "stopped" and out["steps_taken"] == 0


def test_mission_stuck_when_step_crashes():
    def crash(g, log, n):
        raise RuntimeError("band")
    out = run_mission("goal", crash,
                      lambda g, log: {"state": "continue"})
    assert out["state"] == "stuck"


# -- app wiring -------------------------------------------------

def _wait(pred, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _tasks(app):
    import json
    p = app.data_dir / "tasks.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in
            p.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_batch_runs_quietly_with_one_report(tmp_path,
                                            monkeypatch):
    app = ChatApp(tmp_path)
    monkeypatch.setattr(
        chat_mod, "_delegated_summary",
        lambda parts, t: f"sar: {t}")
    out = app.execute_tool("delegate_tasks",
                           {"tasks": ["a", "b", "c"]})
    assert out["ok"] and out["count"] == 3
    assert _wait(lambda: any(
        r.get("status") in ("done", "failed")
        for r in _tasks(app)))
    rows = [r for r in _tasks(app) if "-" in r["id"]]
    assert len(rows) == 3
    notes = chat_mod._read_notifications(app.data_dir)
    assert sum(1 for n in notes if n["kind"] == "batch") == 1


def test_mission_app_end_to_end_with_calibration(
        tmp_path, monkeypatch):
    app = ChatApp(tmp_path)
    monkeypatch.setattr(
        chat_mod, "_delegated_summary",
        lambda parts, t: "kadam pura hua")
    checks = iter([{"state": "continue", "confidence": 0.9},
                   {"state": "done", "confidence": 0.95,
                    "note": "goal pura"}])
    monkeypatch.setattr(chat_mod, "_mission_check",
                        lambda parts, g, log: next(checks))
    out = app.execute_tool("run_mission", {"goal": "chhota goal"})
    assert out["ok"]
    assert _wait(lambda: any(
        r.get("kind") == "mission" and r.get("status") == "done"
        for r in _tasks(app)))
    stats = app.calibrator.stats()
    assert stats["resolved"] >= 2 and stats["accuracy"] == 1.0
    notes = chat_mod._read_notifications(app.data_dir)
    assert sum(1 for n in notes if n["kind"] == "mission") == 1
