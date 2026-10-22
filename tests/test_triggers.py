"""Triggered tasks: the event fires the work EXACTLY once per
key (idempotent), a changed event fires again, off means off —
and the daily nudge speaks once, only when something waits."""

from pathlib import Path

from jarvis import triggers as trig
from jarvis.chat import ChatApp, _maybe_nudge
from jarvis.chat import _read_notifications as list_notifications
from jarvis.tracker import add as track_add


def test_file_trigger_fires_once_per_version(tmp_path):
    folder = tmp_path / "inbox2"
    folder.mkdir()
    book = trig.TriggerBook(tmp_path)
    book.add("nayi file", "file_appears",
             {"folder": str(folder), "contains": "lead"},
             "is file ko padh kar tracker me dalo")
    runs = []
    runner = lambda work: runs.append(work) or "ho gaya"
    assert trig.tick(tmp_path, runner) == 0     # nothing there
    (folder / "lead-1.txt").write_text("abc")
    assert trig.tick(tmp_path, runner) == 1
    assert trig.tick(tmp_path, runner) == 0     # same key: silent
    (folder / "lead-1.txt").write_text("abcdef")  # changed file
    assert trig.tick(tmp_path, runner) == 1
    assert len(runs) == 2


def test_tracker_trigger_and_off_state(tmp_path):
    book = trig.TriggerBook(tmp_path)
    row = book.add("reply aaya", "tracker_row",
                   {"table": "leads", "status": "replied"},
                   "malik ko khabar do")
    track_add(tmp_path, "leads", {"name": "A"})
    runs = []
    assert trig.tick(tmp_path, lambda w: runs.append(w)) == 0
    from jarvis.tracker import list_rows, set_status
    lead = list_rows(tmp_path, "leads")[0]
    set_status(tmp_path, "leads", lead["id"], "replied")
    assert trig.tick(tmp_path, lambda w: runs.append(w)) == 1
    book.set_state(row["id"], "off")
    track_add(tmp_path, "leads", {"name": "B"})
    lead_b = [r for r in list_rows(tmp_path, "leads")
              if r["name"] == "B"][0]
    set_status(tmp_path, "leads", lead_b["id"], "replied")
    assert trig.tick(tmp_path, lambda w: runs.append(w)) == 0


def test_app_tools_and_daily_nudge(tmp_path):
    app = ChatApp(tmp_path)
    names = {t["name"] for t in
             app.registry.advertised_tools(app.allowances)}
    assert {"set_trigger", "list_triggers"} <= names
    out = app.execute_tool("set_trigger", {
        "name": "x", "event": "file_appears",
        "match": {"folder": str(tmp_path), "contains": "z"},
        "action": "dekho"})
    assert out["ok"] is True
    bad = app.execute_tool("set_trigger", {
        "name": "y", "event": "chaand_nikla", "action": "dekho"})
    assert bad["ok"] is False and "event" in bad["error"]
    _maybe_nudge(app)
    notes = list_notifications(tmp_path)
    assert notes == []                        # nothing waiting
    track_add(tmp_path, "leads", {
        "name": "Due Lead", "follow_up_due": 1})
    _maybe_nudge(app)
    notes = list_notifications(tmp_path)
    assert len(notes) == 1 and "follow-up" in notes[0]["body"]
    _maybe_nudge(app)                         # same day: quiet
    assert len(list_notifications(tmp_path)) == 1
