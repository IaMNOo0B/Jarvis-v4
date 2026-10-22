"""Scheduler: schedules compute honestly, due items fire ONCE
through the runner (missed runs never burst), once-items retire,
and the app surface (tools + commands) works end to end."""

import time
from datetime import datetime

import jarvis.scheduler as sched
from jarvis.chat import ChatApp


def test_daily_next_run_is_today_or_tomorrow():
    now = datetime(2026, 10, 22, 8, 0).timestamp()
    nxt = sched.compute_next({"type": "daily", "at": "09:30"}, now)
    assert datetime.fromtimestamp(nxt).hour == 9
    assert nxt > now
    later = datetime(2026, 10, 22, 10, 0).timestamp()
    nxt2 = sched.compute_next({"type": "daily", "at": "09:30"},
                              later)
    assert datetime.fromtimestamp(nxt2).day == 23


def test_tick_fires_due_routine_once_and_reschedules(tmp_path):
    out = sched.add_item(tmp_path, "Subah ki report",
                         "routines.json",
                         steps=["news dekho", "summary banao"],
                         schedule={"type": "interval", "hours": 6},
                         enabled=True)
    row = out["item"]
    # Force it due, as if the PC slept through the slot.
    rows = sched.load_routines(tmp_path)
    rows[0]["next_run"] = time.time() - 3600
    sched._save(tmp_path, "routines.json", rows)
    calls, notes = [], []
    report = sched.tick(tmp_path,
                        runner=lambda w: calls.append(w) or "done",
                        notify=lambda t, b: notes.append(t))
    assert report["routines"] == 1 and len(calls) == 1
    assert "1. news dekho" in calls[0]
    after = sched.load_routines(tmp_path)[0]
    assert after["runs"] == 1 and after["next_run"] > time.time()
    assert notes and "Subah ki report" in notes[0]
    # A second immediate tick fires nothing — no burst.
    report2 = sched.tick(tmp_path, runner=lambda w: "x")
    assert report2["routines"] == 0


def test_once_goal_retires_after_its_run(tmp_path):
    sched.add_item(tmp_path, "Ek baar wala", "scheduled_goals.json",
                   prompt="report banao",
                   schedule={"type": "once", "at": time.time() - 5},
                   enabled=True, status="active")
    report = sched.tick(tmp_path, runner=lambda w: "ho gaya")
    assert report["goals"] == 1
    goal = sched.load_goals(tmp_path)[0]
    assert goal["status"] == "done"


def test_app_tools_and_commands(tmp_path):
    app = ChatApp(tmp_path)
    names = {t["name"] for t in
             app.registry.advertised_tools(app.allowances)}
    assert {"set_routine", "set_goal"} <= names
    out = app.execute_tool("set_routine", {
        "name": "Shaam check", "steps": ["inbox dekho"],
        "schedule_type": "daily", "at": "19:00"})
    assert out["ok"] is True
    listing = app.handle_command("/routines")
    assert "Shaam check" in listing
    out2 = app.execute_tool("set_goal", {
        "name": "Seekhna", "prompt": "roz ek topic research karo",
        "schedule_type": "interval", "hours": 12})
    assert out2["ok"] is True
    assert "Seekhna" in app.handle_command("/goals")
